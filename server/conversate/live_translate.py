"""Live translation with OpenAI gpt-realtime-translate ($0.034/min while the session is open).

Audio is streamed as it arrives; the model returns the source transcript and the Korean
translation as deltas while the person is still talking, which the per-sentence path
(gpt-transcribe, then a text translation) could not do.

Session API (developers.openai.com/api/docs/guides/realtime-translation):
  wss://api.openai.com/v1/realtime/translations?model=gpt-realtime-translate
  -> session.update {session: {audio: {output: {language: "ko"}}}}
  -> session.input_audio_buffer.append {audio: base64 PCM16 24 kHz mono}
  <- session.output_transcript.delta {delta}  translated text
  -> session.close, then read until session.closed
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from typing import Awaitable, Callable

import numpy as np

log = logging.getLogger(__name__)

URL = "wss://api.openai.com/v1/realtime/translations?model={model}"


def resample_16k_to_24k(pcm16k: bytes) -> bytes:
    """Linear interpolation; plenty for speech going to a recogniser."""
    x = np.frombuffer(pcm16k[: len(pcm16k) - len(pcm16k) % 2], dtype="<i2").astype(np.float32)
    if x.size == 0:
        return b""
    n_out = int(round(x.size * 1.5))
    t = np.linspace(0, x.size - 1, n_out)
    return np.interp(t, np.arange(x.size), x).astype("<i2").tobytes()


class KoreanJoiner:
    """Puts back the spaces the translation stream leaves out.

    Output deltas come a few characters at a time ("안", "그") and the model's phrase chunks are
    joined with no space between them: "써봤는데요제가 여러분에게사게 하든말리든" (production,
    2026-11-05). Inside a chunk the deltas arrive back to back; a new chunk comes after a pause,
    so a pause between deltas is taken as a word boundary."""

    GAP_S = 0.35

    def __init__(self) -> None:
        self.last_at: float | None = None
        self.last_char = ""

    def add(self, delta: str, now: float) -> str:
        if not delta:
            return ""
        gap = self.last_at is not None and now - self.last_at >= self.GAP_S
        self.last_at = now
        piece = delta
        if gap and self.last_char and not self.last_char.isspace() and not delta[0].isspace() \
                and delta[0] not in ".,?!)…" and self.last_char not in "(":
            piece = " " + delta
        self.last_char = delta[-1]
        return piece


class LiveTranslator:
    """One OpenAI translation session. [on_korean] gets each piece of the Korean translation as
    it arrives, with spaces restored ([KoreanJoiner]); the caller decides which caption it
    belongs to. The source-language transcript of this API is not used: it arrived 30 s late and
    in one burst (2026-11-05), so the original comes from our own speech recognition."""

    def __init__(
        self,
        on_korean: Callable[[str], Awaitable[None]],
        model: str | None = None,
        language: str = "ko",
        connect: Callable | None = None,
    ) -> None:
        self.on_korean = on_korean
        self.model = model or os.environ.get("LIVE_TRANSLATE_MODEL", "gpt-realtime-translate")
        self.language = language
        self._connect = connect
        self.ws = None
        self.reader: asyncio.Task | None = None
        self.joiner = KoreanJoiner()
        self.logged_deltas = 0
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
                if kind == "session.output_transcript.delta":
                    delta = event.get("delta", "")
                    if self.logged_deltas < 60:  # to check the spacing heuristic in the logs
                        self.logged_deltas += 1
                        log.info("live delta %r elapsed_ms=%s", delta, event.get("elapsed_ms"))
                    piece = self.joiner.add(delta, time.monotonic())
                    if piece:
                        await self.on_korean(piece)
                elif kind == "error":
                    log.warning("live translation error: %s", event.get("error"))
                elif kind == "session.closed":
                    break
        except Exception:
            log.exception("live translation reader failed")
        finally:
            self.closed.set()

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
