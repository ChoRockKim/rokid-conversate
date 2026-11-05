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
from contextlib import asynccontextmanager
from typing import Callable

import numpy as np
from fastapi import APIRouter, FastAPI, WebSocket, WebSocketDisconnect

from .assist import Assist, AssistEngine, Assistant, assistant_from_env
from .convert import Rates
from .convert import find as find_conversions
from .history import ConversationLog, history_from_env, history_router
from .notes import notes_router, notes_store_from_env
from .ota import ota_router_from_env
from .segmenter import SAMPLE_RATE, Event, Segmenter, SegmenterConfig
from .transcriber import Transcriber, transcriber_from_env
from .translate import Translator, needs_translation, translator_from_env
from .live_translate import LiveTranslator
from .images import ImageFinder

log = logging.getLogger("conversate")

LEVEL_INTERVAL_S = 0.25
# Live translation closes after this long without foreign speech (it is billed per minute open).
LIVE_IDLE_S = 60.0
# Audio kept for replay into a live translation session that opens mid-utterance.
REPLAY_MAX_S = 8.0
# Live pieces held for an utterance with no partial yet are dropped after this long.
LIVE_HOLD_S = 4.0
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
    history: ConversationLog | None | object = _FROM_ENV,
) -> FastAPI:
    notes = notes_store_from_env()
    hist = history_from_env(notes.text) if history is _FROM_ENV else history

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Closes conversations that went quiet and has them summarized.
        task = asyncio.create_task(hist.run()) if hist is not None else None
        yield
        if task is not None:
            task.cancel()

    app = FastAPI(title="rokid-conversate", lifespan=lifespan)
    stt = transcriber or transcriber_from_env()
    # Instant money/unit conversions (CONVERT=0 turns them off); one rate cache per server.
    rates = Rates()
    converter = None if os.environ.get("CONVERT", "1") == "0" else (lambda text: find_conversions(text, rates))
    ai = assistant_from_env(notes.text) if assistant is _FROM_ENV else assistant
    tr = translator_from_env() if translator is _FROM_ENV else translator
    # Wikipedia pictures on AI cards (IMAGES=0 turns them off).
    images = None if os.environ.get("IMAGES", "1") == "0" else ImageFinder()
    if live_factory is _FROM_ENV:
        live_on = os.environ.get("OPENAI_API_KEY") and os.environ.get("LIVE_TRANSLATE", "1") != "0"
        live_factory = LiveTranslator if live_on else None
    local = getattr(stt, "local", stt)
    if os.environ.get("WHISPER_PRELOAD") == "1" and hasattr(local, "_get_model"):
        # Load the model in the background at startup, not on the first caption.
        threading.Thread(target=local._get_model, daemon=True, name="whisper-preload").start()
        if hasattr(local, "language_id"):
            threading.Thread(target=local.language_id._get_model, daemon=True, name="langid-preload").start()
    expected = token if token is not None else os.environ.get("CONVERSATE_TOKEN", "")
    app.include_router(ota or ota_router_from_env(expected))
    app.include_router(notes_router(notes, expected))
    if hist is not None:
        app.include_router(history_router(hist, expected))

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
        await Session(websocket, stt, ai, segmenter_config, tr, live_factory, hist, converter, images).run()
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
        history: ConversationLog | None = None,
        converter: Callable[[str], list] | None = None,
        images: ImageFinder | None = None,
    ) -> None:
        self.images = images
        self.history = history
        self.ws = websocket
        self.stt = stt
        self.translator = translator
        # Live translation: opened when foreign speech is heard, closed after LIVE_IDLE_S without.
        self.live_factory = live_factory
        self.live: LiveTranslator | None = None
        self.live_until = 0.0
        # Live Korean is attached to our own captions: [live_seg] is the segment it is being
        # written under, [live_text] what arrived for it so far.
        self.live_seg: int | None = None
        self.live_text = ""
        self.partial_text: dict[int, str] = {}   # newest partial text of a segment in progress
        self.final_text: dict[int, str] = {}     # foreign finals waiting for their text translation
        self.recent: deque[bytes] = deque()      # last REPLAY_MAX_S of audio
        self.recent_bytes = 0
        self.recent_total = 0                    # bytes ever added (to find what came later)
        self.partial_language: dict[int, str | None] = {}
        self.live_starting = False
        self.live_piece_at = 0.0
        self.live_claimed: int | None = None
        self.live_only: set[int] = set()         # foreign finals translated by the live stream
        self.assist = (
            AssistEngine(ai, self._send_assist, on_busy=self._send_assist_busy, converter=converter)
            if ai is not None else None
        )
        self.segmenter = Segmenter(segmenter_config)
        self.finals: deque[Event] = deque()
        self.partial: Event | None = None
        self.last_final_id = 0
        self.final_work = asyncio.Event()
        self.partial_work = asyncio.Event()
        self.received_samples = 0
        self.send_lock = asyncio.Lock()
        self.stat_rms: list[float] = []
        self.stat_zero = 0
        self.stat_since = time.monotonic()

    async def send(self, message: dict) -> None:
        async with self.send_lock:
            await self.ws.send_text(json.dumps(message, ensure_ascii=False))

    async def _record(self, coro) -> None:
        """History writes must never break captions."""
        try:
            await coro
        except Exception:
            log.exception("history write failed")

    async def _send_assist(self, result: Assist) -> None:
        if self.history is not None:
            await self._record(self.history.add_card(result.title, result.text))
        await self.send({"type": "assist", "title": result.title, "text": result.text})
        if result.image and self.images is not None:
            asyncio.create_task(self._send_image(result.title, result.image))

    async def _send_image(self, title: str, article: str) -> None:
        """After the card: its text must not wait on Wikipedia (~0.5-1 s)."""
        try:
            png = await asyncio.to_thread(self.images.find, article)
        except Exception:
            log.exception("image lookup failed")
            return
        if png:
            await self.send({"type": "assist_image", "title": title, "png": png})

    async def _send_assist_busy(self, busy: bool) -> None:
        await self.send({"type": "assist_status", "busy": busy})

    async def run(self) -> None:
        worker = asyncio.create_task(self._transcribe_loop())
        watchdog = asyncio.create_task(self._live_watchdog())
        try:
            await self._receive_loop()
        except WebSocketDisconnect:
            pass
        finally:
            worker.cancel()
            watchdog.cancel()
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
                self._remember(pcm)
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

    async def _live_watchdog(self) -> None:
        """The glasses send no audio while it is quiet (battery), so the idle check in the
        receive loop may never run: close an idle live translation session from here too."""
        while True:
            await asyncio.sleep(5)
            if self.live is not None and time.monotonic() > self.live_until:
                await self._stop_live()

    def _remember(self, pcm: bytes) -> None:
        self.recent.append(pcm)
        self.recent_bytes += len(pcm)
        self.recent_total += len(pcm)
        while self.recent and self.recent_bytes - len(self.recent[0]) >= REPLAY_MAX_S * SAMPLE_RATE * 2:
            self.recent_bytes -= len(self.recent.popleft())

    def _recent_audio(self, seconds: float, since_total: int | None = None) -> bytes:
        """The last [seconds] of audio, or (with [since_total]) everything after that point."""
        data = b"".join(self.recent)
        if since_total is not None:
            return data[len(data) - (self.recent_total - since_total):] if self.recent_total > since_total else b""
        n = int(seconds * SAMPLE_RATE) * 2
        return data[-n:]

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
            self.final_work.set()
        else:
            # Only the newest partial matters; an older one still waiting is stale.
            self.partial = event
            self.partial_work.set()

    async def _transcribe_loop(self) -> None:
        """Finals and partials run as two independent workers. With one loop, a final waiting on
        the OpenAI call (1-2 s) held up every partial behind it, and a slow local partial on a
        long utterance held up the final: captions stalled, then arrived in a burst."""
        await asyncio.gather(self._final_loop(), self._partial_loop())

    async def _final_loop(self) -> None:
        while True:
            await self.final_work.wait()
            self.final_work.clear()
            while self.finals:
                event = self.finals.popleft()
                text = await self._run_stt(event)
                if text is None:
                    continue
                self.last_final_id = max(self.last_final_id, event.segment_id)
                await self._deliver(event, text, final=True)

    async def _partial_loop(self) -> None:
        while True:
            await self.partial_work.wait()
            self.partial_work.clear()
            while self.partial is not None:
                event, self.partial = self.partial, None
                if event.segment_id <= self.last_final_id:
                    continue
                text = await self._run_stt(event)
                if text is None or event.segment_id <= self.last_final_id:
                    continue  # the final for this utterance overtook us
                await self._deliver(event, text, final=False)

    async def _run_stt(self, event: Event) -> str | None:
        started = time.monotonic()
        try:
            partial = event.kind == "partial"
            detect = getattr(self.stt, "detect_language", None)
            seg = event.segment_id
            if partial and detect is not None:
                # The language is found on an utterance's first partial (0.8 s in, ~0.3 s with
                # a small model) and reused. A foreign one starts live translation right away,
                # before this partial is even transcribed.
                language = self.partial_language.get(seg)
                if language is None:
                    language = await asyncio.to_thread(detect, event.audio)
                    self.partial_language[seg] = language
                    for old in [k for k in self.partial_language if k < seg - 5]:
                        del self.partial_language[old]
                    if language != "ko" and self.live is None and self.live_factory is not None:
                        asyncio.create_task(self._start_live(replay_s=len(event.audio) / SAMPLE_RATE + 0.5))
                text = await asyncio.to_thread(self.stt.partial_in, event.audio, language)
            else:
                text = await asyncio.to_thread(self.stt.transcribe, event.audio, partial=partial)
        except Exception as exc:  # keep the session alive if one call fails
            log.exception("transcription failed")
            await self.send({"type": "error", "message": f"transcription failed: {exc}"})
            return None
        log.info(
            "stt %s #%d: %.1fs audio in %.2fs", event.kind, event.segment_id,
            len(event.audio) / SAMPLE_RATE, time.monotonic() - started,
        )
        return text

    async def _deliver(self, event: Event, text: str, *, final: bool) -> None:
        seg = event.segment_id
        foreign = bool(text and needs_translation(text))
        if not final:
            # Kept even before live translation is up: it opens in the background, and its
            # first pieces attach to this caption at once.
            self.partial_text[seg] = text
            if self.live is not None:
                await self._live_claim(seg)
                await self._send_caption(seg, text, False, self._live_for(seg, text))
            elif text:
                await self._send_caption(seg, text, False, None)
            return
        self.partial_text.pop(seg, None)
        if text and self.history is not None:
            await self._record(self.history.add_line(text))
        live = self._live_for(seg, text) if self.live is not None else None
        await self._send_caption(seg, text, True, live)
        if foreign:
            if self.live is not None:
                self.live_until = time.monotonic() + LIVE_IDLE_S
            self.final_text[seg] = text
            if live:
                # The live stream already translated it: keep that (the late tail still
                # streams in) instead of swapping in the sentence translation a second later,
                # which made the text jump.
                self.live_only.add(seg)
            elif self.translator is not None:
                asyncio.create_task(self._translate(seg, text))
            await self._start_live()
        elif self.live_seg == seg:
            # Korean speech: whatever the translator made of it is not wanted.
            self.live_seg, self.live_text = None, ""
        if text and self.assist is not None:
            self.assist.on_final(text)

    async def _send_caption(self, seg: int, text: str, final: bool, translation: str | None) -> None:
        message = {"type": "caption", "id": seg, "text": text, "final": final}
        if translation:
            message["translation"] = translation
        await self.send(message)

    async def _live_claim(self, seg: int) -> None:
        """A new utterance started: live Korean from now on belongs to it. Pieces arriving after
        the previous utterance's final (the translation lags speech) went to that one."""
        if self.live_claimed == seg:
            return
        self.live_claimed = seg
        # Pieces held for an utterance that had no partial yet carry over to this one, unless
        # they are from long ago (an English video's last words ended up under the first
        # Japanese line, 2026-11-05).
        held = self.live_seg is not None and self.live_seg > self.last_final_id
        if held and time.monotonic() - self.live_piece_at > LIVE_HOLD_S:
            held = False
        if not held:
            if self.live_seg is not None and self.live_seg < seg:
                await self._settle_live()
            self.live_text = ""
        self.live_seg = seg

    async def _settle_live(self) -> None:
        """The live stream is done with [live_seg]: if its translation is the one shown, record
        it in the history (the sentence translation path records its own)."""
        seg = self.live_seg
        if seg is None or seg not in self.live_only:
            return
        self.live_only.discard(seg)
        text = self.final_text.pop(seg, None)
        korean = self.live_text.strip()
        if text and korean and self.history is not None:
            await self._record(self.history.add_translation(text, korean))

    def _live_for(self, seg: int, text: str) -> str | None:
        if self.live_seg != seg or not self.live_text.strip():
            return None
        if text and not needs_translation(text):
            return None  # Korean speech
        return self.live_text.strip()

    async def _on_live_korean(self, piece: str) -> None:
        self.live_until = time.monotonic() + LIVE_IDLE_S
        if (self.live_seg is not None and self.live_seg > self.last_final_id
                and self.live_seg not in self.partial_text
                and time.monotonic() - self.live_piece_at > LIVE_HOLD_S):
            self.live_text = ""  # stale held pieces: drop them (see _live_claim)
        self.live_piece_at = time.monotonic()
        finished = self.live_seg is not None and self.live_seg <= self.last_final_id
        if self.live_seg is None or (finished and (
                self.live_seg not in self.final_text or self.segmenter._state.in_speech)):
            # Translation before the utterance's first partial (language detection makes that
            # ~1 s late): it belongs to the next utterance, not the finished one. A finished
            # one only takes "tail" pieces until its sentence translation arrives, and not
            # once someone is talking again. (Its start was dropped, 2026-11-05.)
            await self._settle_live()
            self.live_seg, self.live_text = self.last_final_id + 1, ""
        if not self.live_text:
            piece = piece.lstrip()
        self.live_text += piece
        seg = self.live_seg
        if seg in self.partial_text:
            text = self.partial_text[seg]
            if not text or needs_translation(text):
                await self._send_caption(seg, text, False, self.live_text.strip())
        elif seg > self.last_final_id and self.partial_language.get(seg) not in (None, "ko"):
            # Foreign speech whose first partial is still being transcribed (Whisper on CPU,
            # ~1 s): show the translation now, the original joins it when ready. (It waited for
            # the partial, which made Japanese start at 3.3 s.)
            await self._send_caption(seg, "", False, self.live_text.strip())
        elif seg in self.final_text:
            # Tail of a finished utterance (until its sentence translation replaces it, if any).
            await self._send_caption(seg, self.final_text[seg], True, self.live_text.strip())

    async def _start_live(self, replay_s: float = 0.0) -> None:
        self.live_until = time.monotonic() + LIVE_IDLE_S
        if self.live is not None or self.live_factory is None or self.live_starting:
            return
        self.live_starting = True
        try:
            await self._open_live(replay_s)
        finally:
            self.live_starting = False
        self.live_piece_at = 0.0
        self.live_claimed: int | None = None

    async def _open_live(self, replay_s: float) -> None:
        started = time.monotonic()
        live = self.live_factory(on_korean=self._on_live_korean)
        try:
            await live.start()
            if replay_s > 0:
                # The utterance so far plus what arrived while connecting, in order; then any
                # chunk that came in during that send. Only then does the receive loop feed
                # (self.live is set below).
                mark = self.recent_total
                await live.feed(self._recent_audio(replay_s + time.monotonic() - started))
                while self.recent_total > mark:
                    newer, mark = self._recent_audio(0, since_total=mark), self.recent_total
                    await live.feed(newer)
        except Exception:
            log.exception("live translation: could not open a session")
            return
        self.live = live
        log.info("live translation ready in %.2fs (replayed %.1fs)", time.monotonic() - started, replay_s)

    async def _stop_live(self) -> None:
        live, self.live = self.live, None
        await self._settle_live()
        self.live_seg, self.live_text = None, ""
        if live is not None:
            await live.stop()

    async def _translate(self, segment_id: int, text: str) -> None:
        """Foreign speech: send the same caption again with a Korean "translation" field."""
        try:
            korean = await asyncio.to_thread(self.translator.translate, text)
        except Exception:
            log.exception("translation failed")
            return
        finally:
            self.final_text.pop(segment_id, None)
        if korean:
            if self.history is not None:
                await self._record(self.history.add_translation(text, korean))
            await self.send(
                {"type": "caption", "id": segment_id, "text": text, "final": True, "translation": korean}
            )


def app() -> FastAPI:
    """Factory for `uvicorn conversate.main:app --factory`."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    if not os.environ.get("CONVERSATE_TOKEN"):
        log.warning("CONVERSATE_TOKEN is not set: anyone who can reach this port can connect")
    return create_app()
