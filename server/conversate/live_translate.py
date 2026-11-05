"""Live translation with OpenAI gpt-realtime-translate ($0.034/min while the session is open).

Audio is streamed as it arrives; the model returns the source transcript and the Korean
translation as deltas while the person is still talking, which the per-sentence path
(gpt-transcribe, then a text translation) could not do.

Session API (developers.openai.com/api/docs/guides/realtime-translation):
  wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate
  -> session.update {session: {audio: {output: {language: "ko"}}}}
  -> session.input_audio_buffer.append {audio: base64 PCM16 24 kHz mono}
  <- session.input_transcript.delta {delta}   source language text
  <- session.output_transcript.delta {delta}  translated text
  -> session.close, then read until session.closed
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
from typing import Awaitable, Callable

import numpy as np

log = logging.getLogger(__name__)

URL = "wss://api.openai.com/v1/realtime/translations?model={model}"
_SENTENCE_END = re.compile(r"[.?!。？！]\s*$")
INPUT_TRANSCRIPTION_MODEL = os.environ.get("LIVE_INPUT_TRANSCRIPTION_MODEL", "gpt-transcribe")


def resample_16k_to_24k(pcm16k: bytes) -> bytes:
    """Linear interpolation; plenty for speech going to a recogniser."""
    x = np.frombuffer(pcm16k[: len(pcm16k) - len(pcm16k) % 2], dtype="<i2").astype(np.float32)
    if x.size == 0:
        return b""
    n_out = int(round(x.size * 1.5))
    t = np.linspace(0, x.size - 1, n_out)
    return np.interp(t, np.arange(x.size), x).astype("<i2").tobytes()


class LiveLine:
    """Builds caption lines from the two delta streams. A line is closed when the translation
    has ended a sentence and the source has moved on, or when it gets long."""

    MAX_CHARS = 120

    def __init__(self) -> None:
        self.source = ""
        self.korean = ""

    def add_source(self, delta: str) -> None:
        self.source += delta

    def add_korean(self, delta: str) -> None:
        self.korean += delta

    def should_close(self) -> bool:
        if len(self.korean) > self.MAX_CHARS:
            return True
        if not _SENTENCE_END.search(self.korean):
            return False
        # Source transcript may not be sent at all (it was not, 2026-11-05); then the
        # translation's sentence end is enough.
        return not self.source.strip() or bool(_SENTENCE_END.search(self.source))

    def take(self) -> tuple[str, str]:
        out = (self.source.strip(), self.korean.strip())
        self.source = ""
        self.korean = ""
        return out


class LiveTranslator:
    """One OpenAI translation session. [on_line] gets (source, korean, final) as text grows."""

    def __init__(
        self,
        on_line: Callable[[str, str, bool], Awaitable[None]],
        model: str | None = None,
        language: str = "ko",
        connect: Callable | None = None,
    ) -> None:
        self.on_line = on_line
        self.model = model or os.environ.get("LIVE_TRANSLATE_MODEL", "gpt-realtime-translate")
        self.language = language
        self._connect = connect
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.line = LiveLine()
        self.closed = asyncio.Event()

    async def start(self) -> None:
        if self._connect is not None:
            self.ws = await self._connect()
        else:
            import websockets

            self.ws = await websockets.connect(
                URL.format(model=self.model),
                additional_headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
                max_size=None,
            )
        await self.ws.send(json.dumps(
            {"type": "session.update", "session": {"audio": {"output": {"language": self.language}}}}
        ))
        # Source-language transcript: not documented, and not sent with the update above. Ask
        # for it separately, so a rejection (logged as an error event) leaves translation working.
        await self.ws.send(json.dumps({
            "type": "session.update",
            "session": {"audio": {"input": {"transcription": {"model": INPUT_TRANSCRIPTION_MODEL}}}},
        }))
        self.reader = asyncio.create_task(self._read())
        log.info("live translation: session open (%s -> %s)", self.model, self.language)

    async def feed(self, pcm16k: bytes) -> None:
        if self.ws is None or self.closed.is_set():
            return
        audio = resample_16k_to_24k(pcm16k)
        if audio:
            await self.ws.send(json.dumps(
                {"type": "session.input_audio_buffer.append", "audio": base64.b64encode(audio).decode()}
            ))

    async def _read(self) -> None:
        try:
            seen: set[str] = set()
            async for raw in self.ws:
                event = json.loads(raw)
                kind = event.get("type")
                if kind not in seen:
                    seen.add(kind)
                    log.info("live translation: first %s event: %s", kind, str(raw)[:300])
                if kind == "session.input_transcript.delta":
                    self.line.add_source(event.get("delta", ""))
                    await self._emit()
                elif kind == "session.output_transcript.delta":
                    self.line.add_korean(event.get("delta", ""))
                    await self._emit()
                elif kind == "error":
                    log.warning("live translation error: %s", event.get("error"))
                elif kind == "session.closed":
                    break
        except Exception:
            log.exception("live translation reader failed")
        finally:
            if self.line.source or self.line.korean:
                source, korean = self.line.take()
                await self.on_line(source, korean, True)
            self.closed.set()

    async def _emit(self) -> None:
        if self.line.should_close():
            source, korean = self.line.take()
            await self.on_line(source, korean, True)
        elif self.line.source.strip() or self.line.korean.strip():
            await self.on_line(self.line.source.strip(), self.line.korean.strip(), False)

    async def stop(self) -> None:
        if self.ws is None:
            return
        try:
            await self.ws.send(json.dumps({"type": "session.close"}))
            await asyncio.wait_for(self.closed.wait(), timeout=5)
        except Exception:
            pass
        finally:
            try:
                await self.ws.close()
            except Exception:
                pass
            if self.reader is not None:
                self.reader.cancel()
            log.info("live translation: session closed")
