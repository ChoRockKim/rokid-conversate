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
import re
import threading
import time
from collections import deque
from dataclasses import replace
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
from .modes import Mode, mode_for
from .opus import OpusDecoder

log = logging.getLogger("conversate")

LEVEL_INTERVAL_S = 0.25
# Live translation closes after this long without foreign speech (it is billed per minute open).
LIVE_IDLE_S = 20.0  # billed per minute open; was 60
# Audio kept for replay into a live translation session that opens mid-utterance.
REPLAY_MAX_S = 8.0
# elapsed_ms runs 0.5-1.3 s behind the words it translates (it is the output's time: "station"
# came at 3800 ms although the sentence was cut before that, 2026-11-08). A finished utterance
# keeps taking pieces this long after its cut, until its translation ends a sentence.
LIVE_LAG_MS = 2000.0
_SENTENCE_DONE = re.compile(r"[.?!。？！…]\s*$")
_SENTENCE_END_ANY = re.compile(r"[。？！]+|[.?!]+(?=\s|$)")
# A finished utterance still takes late live pieces this long after its sentence translation
# was compared (and kept).
LIVE_TAIL_S = 3.0
AUDIO_LOG_INTERVAL_S = 5.0


_FROM_ENV = object()

# What speech recognition makes up from noise (YouTube outros, broadcast credits, filler):
# shown, it kept the captions up while nobody talked.
_NOISE_TEXT = re.compile(
    r"^\W*(?:시청해\s*주셔서\s*감사합니다|구독과\s*좋아요|좋아요와\s*구독|구독\s*부탁|MBC\s*뉴스|KBS\s*뉴스|SBS\s*뉴스"
    r"|다음\s*영상에서\s*만나요|자막\s*제공|thanks? for watching|thank you for watching|you|bye|음+|어+|아+|으+|흠+|"
    r"ご視聴ありがとうございました|字幕|请不吝点赞)[\W\s]*$",
    re.IGNORECASE,
)


def is_noise_text(text: str) -> bool:
    t = (text or "").strip()
    return bool(t) and (bool(_NOISE_TEXT.match(t)) or not re.search(r"\w", t))


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
        # Live Korean is attached to our own captions by audio time: [live_bounds] is where each
        # finished utterance ended in the live session's audio, [live_texts] the Korean so far.
        self.live_bounds: dict[int, float] = {}
        self.live_texts: dict[int, str] = {}
        self.partial_text: dict[int, str] = {}   # newest partial text of a segment in progress
        self.final_text: dict[int, str] = {}     # foreign finals waiting for their text translation
        self.recent: deque[bytes] = deque()      # last REPLAY_MAX_S of audio
        self.recent_bytes = 0
        self.recent_total = 0                    # bytes ever added (to find what came later)
        self.partial_language: dict[int, str | None] = {}
        self.live_starting = False
        self.want_levels = False
        self.opus: OpusDecoder | None = None
        self.assist = (
            AssistEngine(ai, self._send_assist, on_busy=self._send_assist_busy, converter=converter)
            if ai is not None else None
        )
        self.base_segmenter_config = segmenter_config or SegmenterConfig()
        self.segmenter = Segmenter(self.base_segmenter_config)
        self.mode: Mode = mode_for(None)
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
                if self.opus is not None:
                    pcm = await asyncio.to_thread(self.opus.decode, pcm)
                self.received_samples += len(pcm) // 2
                self._track_audio(pcm)
                self._remember(pcm)
                if self.live is not None:
                    if time.monotonic() > self.live_until:
                        await self._stop_live()
                    else:
                        await self.live.feed(pcm)
                for event in self.segmenter.feed(pcm):
                    if event.kind == "final":
                        self._mark_live_bound(event.segment_id)
                    self._enqueue(event)
                now = time.monotonic()
                # Battery: only for clients that ask ({"type":"hello","levels":true}, e.g. test
                # tools). The glasses no longer use them, and 4 messages a second kept their
                # Wi-Fi busy while someone talked.
                if self.want_levels and now - last_level >= LEVEL_INTERVAL_S:
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
            audio_format = msg.get("format", "pcm_s16le")
            if msg.get("sample_rate", SAMPLE_RATE) != SAMPLE_RATE or audio_format not in (
                "pcm_s16le", "opus"
            ) or msg.get("channels", 1) != 1:
                await self.send(
                    {"type": "error", "message": "expected 16 kHz mono pcm_s16le or opus audio"}
                )
                return
            # Opus (battery: an eighth of the bytes): decoded here into the usual PCM.
            self.opus = OpusDecoder() if audio_format == "opus" else None
            log.info("audio format: %s", audio_format)
            self.want_levels = bool(msg.get("levels"))
            self._apply_mode(msg.get("mode"))
            await self.send({"type": "ready"})
        elif kind == "mode":
            # Picked on the glasses' menu (also sent again in every hello).
            self._apply_mode(msg.get("mode"))
        elif kind == "battery":
            # Battery drain the glasses measured (%/h), already logged above with the mode.
            log.info("battery (mode %s): %s", self.mode.name, text[:200])
        elif kind == "mic":
            pass  # diagnostics from the glasses (audio source); already logged above
        elif kind == "stop":
            for event in self.segmenter.flush():
                self._enqueue(event)
        else:
            await self.send({"type": "error", "message": f"unknown message type: {kind}"})

    def _apply_mode(self, name: str | None) -> None:
        """Listening mode (modes.py): how utterances are cut and what the AI card does."""
        mode = mode_for(name)
        if mode.name == self.mode.name:
            return
        log.info("mode: %s -> %s", self.mode.name, mode.name)
        # Finish what was being said under the old settings; ids keep counting up (the glasses
        # key captions by id).
        for event in self.segmenter.flush():
            self._enqueue(event)
        next_id = self.segmenter._next_id
        self.segmenter = Segmenter(replace(self.base_segmenter_config, **mode.segmenter))
        self.segmenter._next_id = next_id
        self.mode = mode
        if self.assist is not None:
            self.assist.situation = mode.situation
            self.assist.blocked_kinds = mode.blocked_kinds
            self.assist.config = replace(
                self.assist.default_config,
                proactive_interval_s=mode.proactive_interval_s or self.assist.default_config.proactive_interval_s,
            )

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
                text = await self._check_foreign(event, text)
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

    async def _check_foreign(self, event: Event, text: str) -> str:
        """A final that came back foreign is checked against the sound itself (Whisper base,
        ~0.3 s): if that says Korean, it is transcribed again as Korean. In real use 282 of
        3,621 lines were "foreign", among them Korean speech written as English or Japanese
        ("Groits.", "Purification.", "お尻が見たいよ。"), which also opened live translation and
        got a made-up translation."""
        if not text or not needs_translation(text) or is_noise_text(text):
            return text
        detect = getattr(self.stt, "detect_language", None)
        korean = getattr(self.stt, "transcribe_korean", None)
        if detect is None or korean is None:
            return text
        try:
            if await asyncio.to_thread(detect, event.audio) != "ko":
                return text
            again = (await asyncio.to_thread(korean, event.audio)).strip()
        except Exception:
            log.exception("foreign check failed")
            return text
        log.info("foreign check: %r was Korean speech -> %r", text, again)
        return again

    async def _deliver(self, event: Event, text: str, *, final: bool) -> None:
        seg = event.segment_id
        if is_noise_text(text):
            log.info("dropped noise transcript: %r", text)
            text = ""
        foreign = bool(text and needs_translation(text))
        if not final:
            # Kept even before live translation is up: it opens in the background, and its
            # first pieces attach to this caption at once.
            self.partial_text[seg] = text
            if self.live is not None:
                await self._send_caption(seg, text, False, self._live_text_for(seg, text))
            elif text:
                await self._send_caption(seg, text, False, None)
            return
        self.partial_text.pop(seg, None)
        if text and self.history is not None:
            await self._record(self.history.add_line(text))
        if foreign:
            self.final_text[seg] = text
            await self._send_caption(seg, text, True, self._live_text_for(seg, text))
            if self.live is not None:
                self.live_until = time.monotonic() + LIVE_IDLE_S
            if self.translator is not None:
                asyncio.create_task(self._translate(seg, text))
            await self._start_live()
        else:
            await self._send_caption(seg, text, True, None)
            self.live_texts.pop(seg, None)  # Korean speech: the translator's version is not wanted
        if text and self.assist is not None and self.mode.assist:
            self.assist.on_final(text)

    async def _send_caption(self, seg: int, text: str, final: bool, translation: str | None) -> None:
        message = {"type": "caption", "id": seg, "text": text, "final": final}
        if translation:
            message["translation"] = translation
        await self.send(message)

    def _mark_live_bound(self, seg: int) -> None:
        """An utterance was just cut: remember where it ended in the live session's audio."""
        fed_ms = getattr(self.live, "fed_ms", None) if self.live is not None else None
        if isinstance(fed_ms, (int, float)):
            self.live_bounds[seg] = fed_ms
            for old in sorted(self.live_bounds)[:-20]:
                del self.live_bounds[old]

    def _live_seg_for(self, elapsed_ms: float | None) -> int:
        """The utterance a live piece belongs to, by its time: one that was cut after it, or
        one cut a little before it whose translation has not ended its sentence yet (the
        translation lags); else the one being spoken now. By arrival order, the end of one
        sentence's translation landed under the next one and lines read one step off."""
        if elapsed_ms is not None:
            for seg in sorted(self.live_bounds):
                end = self.live_bounds[seg]
                if elapsed_ms <= end:
                    return seg
                if elapsed_ms <= end + LIVE_LAG_MS and not self._live_done(seg):
                    return seg
        return self.segmenter._next_id

    def _live_done(self, seg: int) -> bool:
        """The translation of a finished utterance is complete: it has as many sentence ends as
        the original (once transcribed), else just ends a sentence. A two-sentence Japanese
        line was closed after its first sentence and the second went to the next line."""
        korean = self.live_texts.get(seg, "").strip()
        original = self.final_text.get(seg)
        if original:
            wanted = max(1, len(_SENTENCE_END_ANY.findall(original.strip().rstrip(".?!。？！") + "。")))
            return len(_SENTENCE_END_ANY.findall(korean)) >= wanted
        return bool(_SENTENCE_DONE.search(korean))

    def _live_text_for(self, seg: int, text: str) -> str | None:
        korean = self.live_texts.get(seg, "").strip()
        if not korean or (text and not needs_translation(text)):
            return None  # nothing yet, or Korean speech
        return korean

    async def _on_live_korean(self, piece: str, elapsed_ms: float | None = None) -> None:
        self.live_until = time.monotonic() + LIVE_IDLE_S
        seg = self._live_seg_for(elapsed_ms)
        so_far = self.live_texts.get(seg, "")
        self.live_texts[seg] = so_far + (piece if so_far else piece.lstrip())
        for old in sorted(self.live_texts)[:-20]:
            del self.live_texts[old]
        korean = self.live_texts[seg].strip()
        if not korean:
            return
        if seg in self.final_text:
            # A finished foreign utterance: its late pieces (until the sentence translation
            # replaced it, or a few seconds after it was kept).
            await self._send_caption(seg, self.final_text[seg], True, korean)
        elif seg in self.partial_text:
            text = self.partial_text[seg]
            if (not text or needs_translation(text)) and self.partial_language.get(seg) != "ko":
                await self._send_caption(seg, text, False, korean)
        elif seg > self.last_final_id and self.partial_language.get(seg) not in (None, "ko"):
            # Foreign speech whose first partial is still being transcribed (Whisper on CPU,
            # ~1 s): show the translation now, the original joins it when ready.
            await self._send_caption(seg, "", False, korean)

    async def _start_live(self, replay_s: float = 0.0) -> None:
        self.live_until = time.monotonic() + LIVE_IDLE_S
        if self.live is not None or self.live_factory is None or self.live_starting:
            return
        self.live_starting = True
        try:
            await self._open_live(replay_s)
        finally:
            self.live_starting = False
        self.want_levels = False
        self.opus: OpusDecoder | None = None

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
        self.live_bounds.clear()
        if live is not None:
            await live.stop()

    async def _translate(self, segment_id: int, text: str) -> None:
        """Sentence translation of a foreign final. Shown unless the live translation already
        on screen says much the same (then it stays, so the text does not jump); the history
        always gets this one (aligned with its original)."""
        try:
            korean = await asyncio.to_thread(self.translator.translate, text)
        except Exception:
            log.exception("translation failed")
            self.final_text.pop(segment_id, None)
            return
        if not korean:
            self.final_text.pop(segment_id, None)
            return
        if self.history is not None:
            await self._record(self.history.add_translation(text, korean))
        shown = self.live_texts.get(segment_id, "").strip()
        if shown and same_meaning(shown, korean):
            # Keep the live one; let its last pieces still come in for a moment.
            asyncio.get_running_loop().call_later(LIVE_TAIL_S, self.final_text.pop, segment_id, None)
            return
        if shown:
            log.info("live translation replaced: %r -> %r", shown[:60], korean[:60])
        self.final_text.pop(segment_id, None)
        await self.send(
            {"type": "caption", "id": segment_id, "text": text, "final": True, "translation": korean}
        )


def same_meaning(live: str, sentence: str) -> bool:
    """Two translations of the same sentence share most characters and are about as long;
    one shifted by a line (or cut short) does not."""
    import difflib

    a, b = re.sub(r"[\W_]+", "", live), re.sub(r"[\W_]+", "", sentence)
    if not a or not b or not 0.6 <= len(a) / len(b) <= 1.7:
        return False
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.5

def app() -> FastAPI:
    """Factory for `uvicorn conversate.main:app --factory`."""
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    if not os.environ.get("CONVERSATE_TOKEN"):
        log.warning("CONVERSATE_TOKEN is not set: anyone who can reach this port can connect")
    return create_app()
