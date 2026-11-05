"""WebSocket server the glasses talk to directly (the phone only provides a hotspot).

Protocol (see docs/protocol.md):
  glasses -> server  text   {"type":"hello","sample_rate":16000,"format":"pcm_s16le","channels":1}
                     binary raw PCM, little-endian int16, mono, 16 kHz (any chunk size)
                     text   {"type":"stop"}     finish the current utterance
  server -> glasses  {"type":"ready"}
                     {"type":"level","rms":123.4,"speech":false,"received_ms":5000}
                     {"type":"caption","id":3,"text":"...","final":false}
                     {"type":"assist","title":"...","text":"..."}   AI panel (needs OPENAI_API_KEY)
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

from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect

from .assist import Assist, AssistEngine, Assistant, assistant_from_env
from .ota import ota_router_from_env
from .segmenter import SAMPLE_RATE, Event, Segmenter
from .transcriber import FasterWhisperTranscriber, Transcriber

log = logging.getLogger("conversate")

LEVEL_INTERVAL_S = 0.25


_FROM_ENV = object()


def create_app(
    transcriber: Transcriber | None = None,
    token: str | None = None,
    assistant: Assistant | None | object = _FROM_ENV,
    ota: APIRouter | None = None,
) -> FastAPI:
    app = FastAPI(title="rokid-conversate")
    stt = transcriber or FasterWhisperTranscriber()
    ai = assistant_from_env() if assistant is _FROM_ENV else assistant
    if os.environ.get("WHISPER_PRELOAD") == "1" and hasattr(stt, "_get_model"):
        # Load the model in the background at startup, not on the first caption.
        threading.Thread(target=stt._get_model, daemon=True, name="whisper-preload").start()
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
        await Session(websocket, stt, ai).run()

    return app


def _authorized(websocket: WebSocket, expected: str) -> bool:
    header = websocket.headers.get("authorization", "")
    given = header[7:] if header.lower().startswith("bearer ") else ""
    return hmac.compare_digest(given.encode(), expected.encode())


class Session:
    """One glasses connection: audio in, captions out."""

    def __init__(self, websocket: WebSocket, stt: Transcriber, ai: Assistant | None = None) -> None:
        self.ws = websocket
        self.stt = stt
        self.assist = AssistEngine(ai, self._send_assist) if ai is not None else None
        self.segmenter = Segmenter()
        self.finals: deque[Event] = deque()
        self.partial: Event | None = None
        self.last_final_id = 0
        self.work = asyncio.Event()
        self.received_samples = 0
        self.send_lock = asyncio.Lock()

    async def send(self, message: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(message, ensure_ascii=False))

    async def _send_assist(self, result: Assist) -> None:
        await self.send({"type": "assist", "title": result.title, "text": result.text})

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

    async def _receive_loop(self) -> None:
        last_level = 0.0
        while True:
            message = await self.ws.receive()
            if message["type"] == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            if message.get("bytes") is not None:
                pcm = message["bytes"]
                self.received_samples += len(pcm) // 2
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

    async def _on_text(self, text: str) -> None:
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
                if text or final:
                    await self.send(
                        {"type": "caption", "id": event.segment_id, "text": text, "final": final}
                    )
                if final and text and self.assist is not None:
                    self.assist.on_final(text)


def app() -> FastAPI:
    """Factory for `uvicorn conversate.main:app --factory`."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    if not os.environ.get("CONVERSATE_TOKEN"):
        log.warning("CONVERSATE_TOKEN is not set: anyone who can reach this port can connect")
    return create_app()
