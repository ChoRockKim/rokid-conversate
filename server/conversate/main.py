"""WebSocket server the glasses talk to directly (the phone only provides a hotspot).

Protocol (see docs/protocol.md):
  glasses -> server  text   {"type":"hello","sample_rate":16000,"format":"pcm_s16le","channels":1}
                     binary raw PCM, little-endian int16, mono, 16 kHz (any chunk size)
                     text   {"type":"stop"}     finish the current utterance
  server -> glasses  {"type":"ready"}
                     {"type":"level","rms":123.4,"speech":false,"received_ms":5000}
                     {"type":"caption","id":3,"text":"...","final":false}
                     {"type":"caption","id":3,"text":"Hello","final":true,"translation":"안녕하세요"}  (foreign speech)
                     {"type":"assist","title":"...","text":"..."}   AI panel (needs OPENAI_API_KEY)
                     {"type":"assist_status","busy":true}            AI request running / done
                     {"type":"error","message":"..."}
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import threading
import time
from collections import deque
from typing import Callable

import numpy as np
from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect

from .assist import Assist, AssistEngine, Assistant, assistant_from_env
from .ota import ota_router_from_env
from .segmenter import SAMPLE_RATE, Event, Segmenter, SegmenterConfig
from .transcriber import Transcriber, transcriber_from_env
from .translate import Translator, needs_translation, translator_from_env
from .live_translate import LiveTranslator

log = logging.getLogger("conversate")

LEVEL_INTERVAL_S = 0.25
# Live translation closes after this long without foreign speech (it is billed per minute open).
LIVE_IDLE_S = 30.0
# Caption ids for live translation lines, far from the segmenter's ids.
LIVE_ID_BASE = 1_000_000
AUDIO_LOG_INTERVAL_S = 5.0


_FROM_ENV = object()


def create_app(
    transcriber: Transcriber | None = None,
    token: str | None = None,
    assistant: Assistant | None | object = _FROM_ENV,
    ota: APIRouter | None = None,
    segmenter_config: SegmenterConfig | None = None,
    translator: Translator | None | object = _FROM_ENV,
    live_factory: Callable[..., LiveTranslator] | None | object = _FROM_ENV,
) -> FastAPI:
    app = FastAPI(title="rokid-conversate")
    stt = transcriber or transcriber_from_env()
    ai = assistant_from_env() if assistant is _FROM_ENV else assistant
    tr = translator_from_env() if translator is _FROM_ENV else translator
    if live_factory is _FROM_ENV:
        live_on = os.environ.get("OPENAI_API_KEY") and os.environ.get("LIVE_TRANSLATE", "1") != "0"
        live_factory = LiveTranslator if live_on else None
    local = getattr(stt, "local", stt)
    if os.environ.get("WHISPER_PRELOAD") == "1" and hasattr(local, "_get_model"):
        # Load the model in the background at startup, not on the first caption.
        threading.Thread(target=local._get_model, daemon=True, name="whisper-preload").start()
    expected = token if token is not None else os.environ.get("CONVERSATE_TOKEN", "")
    app.include_router(ota or ota_router_from_env(expected))

    @app.get("/health")
    def health() -> dict:
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        if expected and not _authorized(websocket, expected):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        log.info("glasses connected from %s", websocket.client)
        await Session(websocket, stt, ai, segmenter_config, tr, live_factory).run()
        log.info("glasses disconnected")

    return app


def _authorized(websocket: WebSocket, expected: str) -> bool:
    header = websocket.headers.get("authorization", "")
    given = header[7:] if header.lower().startswith("bearer ") else ""
    return hmac.compare_digest(given.encode(), expected.encode())


class Session:
    """One glasses connection: audio in, captions out."""

    def __init__(
        self,
        websocket: WebSocket,
        stt: Transcriber,
        ai: Assistant | None = None,
        segmenter_config: SegmenterConfig | None = None,
        translator: Translator | None = None,
        live_factory: Callable[..., LiveTranslator] | None = None,
    ) -> None:
        self.ws = websocket
        self.stt = stt
        self.translator = translator
        # Live translation: opened when foreign speech is heard, closed after LIVE_IDLE_S without.
        self.live_factory = live_factory
        self.live: LiveTranslator | None = None
        self.live_until = 0.0
        self.live_id = LIVE_ID_BASE
        self.assist = (
            AssistEngine(ai, self._send_assist, on_busy=self._send_assist_busy) if ai is not None else None
        )
        self.segmenter = Segmenter(segmenter_config)
        self.finals: deque[Event] = deque()
        self.partial: Event | None = None
        self.last_final_id = 0
        self.work = asyncio.Event()
        self.received_samples = 0
        self.send_lock = asyncio.Lock()
        self.stat_rms: list[float] = []
        self.stat_zero = 0
        self.stat_since = time.monotonic()

    async def send(self, message: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(message, ensure_ascii=False))

    async def _send_assist(self, result: Assist) -> None:
        await self.send({"type": "assist", "title": result.title, "text": result.text})

    async def _send_assist_busy(self, busy: bool) -> None:
        await self.send({"type": "assist_status", "busy": busy})

    async def run(self) -> None:
        worker = asyncio.create_task(self._transcribe_loop())
        try:
            await self._receive_loop()
        except WebSocketDisconnect:
            pass
        finally:
            worker.cancel()
            if self.assist is not None:
                self.assist.close()
            await self._stop_live()

    async def _receive_loop(self) -> None:
        last_level = 0.0
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("bytes") is not None:
                pcm = message["bytes"]
                self.received_samples += len(pcm) // 2
                self._track_audio(pcm)
                if self.live is not None:
                    if time.monotonic() > self.live_until:
                        await self._stop_live()
                    else:
                        await self.live.feed(pcm)
                for event in self.segmenter.feed(pcm):
                    self._enqueue(event)
                now = time.monotonic()
                if now - last_level >= LEVEL_INTERVAL_S:
                    last_level = now
                    await self.send(
                        {
                            "type": "level",
                            "rms": round(self.segmenter.last_rms, 1),
                            "speech": self.segmenter._state.in_speech,
                            "received_ms": self.received_samples * 1000 // SAMPLE_RATE,
                        }
                    )
            elif message.get("text") is not None:
                await self._on_text(message["text"])

    def _track_audio(self, pcm: bytes) -> None:
        """Logs what actually arrives every 5 s: seconds of audio, max/mean RMS, speech or not.
        The main tool for telling "no audio" from "audio too quiet" when testing on the glasses."""
        samples = np.frombuffer(pcm[: len(pcm) - len(pcm) % 2], dtype="<i2").astype(np.float32)
        if samples.size:
            rms = float(np.sqrt(np.mean(samples**2)))
            self.stat_rms.append(rms)
            self.stat_zero += int(np.count_nonzero(samples) == 0)
        now = time.monotonic()
        if now - self.stat_since >= AUDIO_LOG_INTERVAL_S and self.stat_rms:
            log.info(
                "audio: total %.1fs, last %.1fs: chunks %d (all-zero %d), rms max %.0f mean %.0f, "
                "noise floor %.0f, in speech %s",
                self.received_samples / SAMPLE_RATE, now - self.stat_since, len(self.stat_rms),
                self.stat_zero, max(self.stat_rms), sum(self.stat_rms) / len(self.stat_rms),
                self.segmenter._noise_floor, self.segmenter._state.in_speech,
            )
            self.stat_rms.clear()
            self.stat_zero = 0
            self.stat_since = now

    async def _on_text(self, text: str) -> None:
        log.info("text from glasses: %s", text[:200])
        try:
            msg = json.loads(text)
        except json.JSONDecodeError:
            await self.send({"type": "error", "message": "invalid json"})
            return
        kind = msg.get("type")
        if kind == "hello":
            if msg.get("sample_rate", SAMPLE_RATE) != SAMPLE_RATE or msg.get(
                "format", "pcm_s16le"
            ) != "pcm_s16le" or msg.get("channels", 1) != 1:
                await self.send(
                    {"type": "error", "message": "expected 16 kHz mono pcm_s16le audio"}
                )
                return
            await self.send({"type": "ready"})
        elif kind == "mic":
            pass  # diagnostics from the glasses (audio source); already logged above
        elif kind == "stop":
            for event in self.segmenter.flush():
                self._enqueue(event)
        else:
            await self.send({"type": "error", "message": f"unknown message type: {kind}"})

    def _enqueue(self, event: Event) -> None:
        if event.kind == "final":
            self.finals.append(event)
            self.partial = None
        else:
            # Only the newest partial matters; an older one still waiting is stale.
            self.partial = event
        self.work.set()

    async def _transcribe_loop(self) -> None:
        while True:
            await self.work.wait()
            self.work.clear()
            while self.finals or self.partial:
                if self.finals:
                    event = self.finals.popleft()
                else:
                    event, self.partial = self.partial, None
                    if event.segment_id <= self.last_final_id:
                        continue
                try:
                    text = await asyncio.to_thread(
                        self.stt.transcribe, event.audio, partial=event.kind == "partial"
                    )
                except Exception as exc:  # keep the session alive if one call fails
                    log.exception("transcription failed")
                    await self.send({"type": "error", "message": f"transcription failed: {exc}"})
                    continue
                final = event.kind == "final"
                if final:
                    self.last_final_id = event.segment_id
                elif event.segment_id <= self.last_final_id:
                    continue  # the final for this utterance overtook us
                foreign = bool(final and text and needs_translation(text))
                if self.live is not None and (not final or foreign):
                    # Live translation is on: it already shows foreign speech as it is spoken.
                    # Skip the Korean-only Whisper partials and this foreign final (duplicates).
                    if foreign:
                        self.live_until = time.monotonic() + LIVE_IDLE_S
                else:
                    if text or final:
                        await self.send(
                            {"type": "caption", "id": event.segment_id, "text": text, "final": final}
                        )
                    if foreign and self.translator is not None:
                        asyncio.create_task(self._translate(event.segment_id, text))
                    if foreign:
                        await self._start_live()
                if final and text and self.assist is not None:
                    self.assist.on_final(text)

    async def _start_live(self) -> None:
        self.live_until = time.monotonic() + LIVE_IDLE_S
        if self.live is not None or self.live_factory is None:
            return
        live = self.live_factory(on_line=self._send_live_line)
        try:
            await live.start()
        except Exception:
            log.exception("live translation: could not open a session")
            return
        self.live = live

    async def _stop_live(self) -> None:
        live, self.live = self.live, None
        if live is not None:
            await live.stop()

    async def _send_live_line(self, source: str, korean: str, final: bool) -> None:
        if needs_translation(source):
            self.live_until = time.monotonic() + LIVE_IDLE_S
        message = {"type": "caption", "id": self.live_id, "text": source, "final": final}
        if korean:
            message["translation"] = korean
        await self.send(message)
        if final:
            self.live_id += 1

    async def _translate(self, segment_id: int, text: str) -> None:
        """Foreign speech: send the same caption again with a Korean "translation" field."""
        try:
            korean = await asyncio.to_thread(self.translator.translate, text)
        except Exception:
            log.exception("translation failed")
            return
        if korean:
            await self.send(
                {"type": "caption", "id": segment_id, "text": text, "final": True, "translation": korean}
            )


def app() -> FastAPI:
    """Factory for `uvicorn conversate.main:app --factory`."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    if not os.environ.get("CONVERSATE_TOKEN"):
        log.warning("CONVERSATE_TOKEN is not set: anyone who can reach this port can connect")
    return create_app()
