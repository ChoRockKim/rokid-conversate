"""Cut a 16 kHz mono PCM stream into speech segments.

Whisper works on whole utterances, not on a live stream, so the server collects audio while
someone is speaking and hands each finished utterance to the transcriber. While the utterance
is still going, it can also ask for a "partial" transcript of what it has so far, so captions
appear before the speaker stops.

Speech detection uses WebRTC VAD (shape of the signal, not its loudness) plus a small absolute
level floor. The first version used only loudness against an adaptive noise floor with a fixed
minimum of 300; on the glasses (2026-11-05) speech mostly arrived at RMS 100-400, so most of it
was missed, and missed speech was then learnt as "noise", raising the bar further. The energy
mode is kept for tests and as a fallback (use_webrtc=False).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

SAMPLE_RATE = 16_000
FRAME_MS = 30
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 480 samples


@dataclass
class SegmenterConfig:
    # WebRTC VAD decides what is speech (aggressiveness 0..3, higher = stricter) ...
    use_webrtc: bool = True
    vad_mode: int = 2
    # ... as long as the frame is at least this loud (int16 RMS), so hiss stays silence.
    webrtc_min_rms: float = 30.0
    # Energy mode (use_webrtc=False): RMS this many times above the noise floor ...
    speech_ratio: float = 3.0
    # ... and above this absolute level.
    min_speech_rms: float = 300.0
    # End the utterance after this much continuous non-speech.
    end_silence_ms: int = 700
    # Drop utterances shorter than this (coughs, clicks).
    min_speech_ms: int = 300
    # Once an utterance is this long, a shorter pause is enough to end it, so long talk comes
    # out in caption-sized pieces (cut at a breath rather than mid-word) ...
    long_after_ms: int = 4_000
    long_end_silence_ms: int = 300
    # ... and a hard cut so one long monologue still produces final captions.
    max_segment_ms: int = 8_000
    # Ask for a partial transcript every this much new audio inside an utterance (0 = never).
    partial_every_ms: int = 1_500
    # Audio kept from before speech started, so the first syllable is not clipped.
    pre_roll_ms: int = 600


@dataclass
class Event:
    kind: str  # "partial" or "final"
    audio: np.ndarray  # float32 in [-1, 1]
    segment_id: int


@dataclass
class _State:
    in_speech: bool = False
    speech: list[np.ndarray] = field(default_factory=list)
    pre_roll: list[np.ndarray] = field(default_factory=list)
    speech_frames: int = 0
    silence_frames: int = 0
    frames_since_partial: int = 0


class Segmenter:
    def __init__(self, config: SegmenterConfig | None = None) -> None:
        self.config = config or SegmenterConfig()
        self._pending = np.zeros(0, dtype=np.int16)
        self._odd_byte = b""
        self._state = _State()
        self._noise_floor = self.config.min_speech_rms / self.config.speech_ratio
        self._next_id = 1
        self.last_rms = 0.0
        self._vad = None
        if self.config.use_webrtc:
            import webrtcvad

            self._vad = webrtcvad.Vad(self.config.vad_mode)

    @staticmethod
    def _frames(ms: int) -> int:
        return max(1, ms // FRAME_MS)

    def feed(self, pcm: bytes) -> list[Event]:
        """Add raw little-endian int16 samples, return any segments that became ready."""
        # A chunk may end in the middle of a sample; keep that byte for the next chunk.
        data = self._odd_byte + pcm
        cut = len(data) - len(data) % 2
        self._odd_byte = data[cut:]
        samples = np.frombuffer(data[:cut], dtype="<i2").astype(np.int16)
        self._pending = np.concatenate([self._pending, samples])
        events: list[Event] = []
        while len(self._pending) >= FRAME_SAMPLES:
            frame = self._pending[:FRAME_SAMPLES]
            self._pending = self._pending[FRAME_SAMPLES:]
            event = self._on_frame(frame)
            if event is not None:
                events.append(event)
        return events

    def flush(self) -> list[Event]:
        """End of stream: finish the current utterance, if any."""
        event = self._finish()
        return [event] if event is not None else []

    def _on_frame(self, frame: np.ndarray) -> Event | None:
        cfg = self.config
        st = self._state
        rms = float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))
        self.last_rms = rms
        if self._vad is not None:
            is_speech = rms >= cfg.webrtc_min_rms and self._vad.is_speech(
                frame.astype("<i2").tobytes(), SAMPLE_RATE
            )
        else:
            is_speech = rms >= cfg.min_speech_rms and rms >= self._noise_floor * cfg.speech_ratio

        if not st.in_speech:
            # Track the background level only while nobody is talking.
            self._noise_floor = 0.95 * self._noise_floor + 0.05 * max(rms, 1.0)
            st.pre_roll.append(frame)
            if len(st.pre_roll) > self._frames(cfg.pre_roll_ms):
                st.pre_roll.pop(0)
            if is_speech:
                st.in_speech = True
                st.speech = list(st.pre_roll)
                st.pre_roll = []
                st.speech_frames = 1
                st.silence_frames = 0
                st.frames_since_partial = 0
            return None

        st.speech.append(frame)
        st.frames_since_partial += 1
        if is_speech:
            st.speech_frames += 1
            st.silence_frames = 0
        else:
            st.silence_frames += 1

        end_silence = (
            cfg.long_end_silence_ms if len(st.speech) >= self._frames(cfg.long_after_ms) else cfg.end_silence_ms
        )
        if st.silence_frames >= self._frames(end_silence):
            return self._finish()
        if len(st.speech) >= self._frames(cfg.max_segment_ms):
            return self._finish()
        if cfg.partial_every_ms and st.frames_since_partial >= self._frames(cfg.partial_every_ms):
            st.frames_since_partial = 0
            return Event("partial", _to_float(st.speech), self._next_id)
        return None

    def _finish(self) -> Event | None:
        st = self._state
        if not st.in_speech:
            return None
        long_enough = st.speech_frames >= self._frames(self.config.min_speech_ms)
        audio = _to_float(st.speech)
        segment_id = self._next_id
        self._state = _State()
        if not long_enough:
            return None
        self._next_id += 1
        return Event("final", audio, segment_id)


def _to_float(frames: list[np.ndarray]) -> np.ndarray:
    return np.concatenate(frames).astype(np.float32) / 32768.0
