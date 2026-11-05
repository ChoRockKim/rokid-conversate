"""Speech-to-text behind a small interface, so the Whisper backend can be swapped or faked."""

from __future__ import annotations

import logging
import os
import threading
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


class Transcriber(Protocol):
    def transcribe(self, audio: np.ndarray, *, partial: bool) -> str:
        """audio: float32 mono 16 kHz in [-1, 1]. Blocking; call from a worker thread."""
        ...


class FasterWhisperTranscriber:
    """Local Whisper via faster-whisper (CTranslate2). The model downloads on first use.

    Settings come from the environment:
      WHISPER_MODEL         tiny | base | small | medium | large-v3 | turbo ... (default: small)
      WHISPER_DEVICE        cpu | cuda | auto (default: auto)
      WHISPER_COMPUTE_TYPE  int8 | float16 | ... (default: int8 on CPU, float16 on CUDA)
      WHISPER_LANGUAGE      default: ko (empty = auto-detect)
    """

    def __init__(self) -> None:
        self.model_name = os.environ.get("WHISPER_MODEL", "small")
        self.device = os.environ.get("WHISPER_DEVICE", "auto")
        self.compute_type = os.environ.get("WHISPER_COMPUTE_TYPE", "default")
        self.language = os.environ.get("WHISPER_LANGUAGE", "ko") or None
        self._model = None
        self._load_lock = threading.Lock()
        # One CTranslate2 model is not meant to be driven by many threads at once.
        self._run_lock = threading.Lock()

    def _get_model(self):
        with self._load_lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                log.info("loading whisper model=%s device=%s", self.model_name, self.device)
                self._model = WhisperModel(
                    self.model_name, device=self.device, compute_type=self.compute_type
                )
            return self._model

    def transcribe(self, audio: np.ndarray, *, partial: bool) -> str:
        model = self._get_model()
        audio = normalize(audio)
        with self._run_lock:
            segments, _info = model.transcribe(
                audio,
                language=self.language,
                # Partials are thrown away a moment later, so trade accuracy for speed there.
                beam_size=1 if partial else 5,
                # The segmenter already cut on silence.
                vad_filter=False,
                # Each utterance is independent; carrying context makes Whisper repeat itself.
                condition_on_previous_text=False,
            )
            return " ".join(s.text.strip() for s in segments).strip()


def normalize(audio: np.ndarray, peak: float = 0.9, max_gain: float = 20.0) -> np.ndarray:
    """Raise a quiet utterance so its peak sits near [peak] (gain capped at [max_gain]).
    The glasses deliver speech far below full scale; Whisper does better on a normal level."""
    top = float(np.max(np.abs(audio))) if audio.size else 0.0
    if top <= 0.0:
        return audio
    gain = min(max_gain, peak / top)
    return (audio * gain).astype(np.float32) if gain > 1.0 else audio


def to_wav(audio: np.ndarray, sample_rate: int = 16_000) -> bytes:
    """float32 mono in [-1, 1] -> 16-bit PCM WAV bytes."""
    import io
    import wave

    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype("<i2").tobytes()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


class OpenAITranscriber:
    """OpenAI speech-to-text (default gpt-transcribe, $0.0045/min) for finished utterances.

    Environment: OPENAI_API_KEY, STT_MODEL (default gpt-transcribe), WHISPER_LANGUAGE (ko).
    """

    def __init__(self) -> None:
        from openai import OpenAI

        self.model = os.environ.get("STT_MODEL", "gpt-transcribe")
        self.language = os.environ.get("WHISPER_LANGUAGE", "ko") or None
        self.client = OpenAI(timeout=20.0, max_retries=1)

    def transcribe(self, audio: np.ndarray, *, partial: bool) -> str:
        extra = {}
        if self.language:
            # gpt-transcribe takes a list ("languages"); older models a single "language".
            extra = {"languages": [self.language]} if self.model == "gpt-transcribe" else {"language": self.language}
        result = self.client.audio.transcriptions.create(
            model=self.model,
            file=("utterance.wav", to_wav(normalize(audio)), "audio/wav"),
            extra_body=extra,
        )
        return (result.text or "").strip()


class HybridTranscriber:
    """Partials (every ~1.5 s while someone talks) from local Whisper: fast and free. Finals from
    the more accurate [final] backend, falling back to local Whisper if it fails. Whisper small
    was weak on Korean (e.g. "분기" -> "붕기" every time)."""

    def __init__(self, local: Transcriber, final: Transcriber) -> None:
        self.local = local
        self.final = final

    def transcribe(self, audio: np.ndarray, *, partial: bool) -> str:
        if partial:
            return self.local.transcribe(audio, partial=True)
        try:
            return self.final.transcribe(audio, partial=False)
        except Exception:
            log.exception("final transcription failed, falling back to local whisper")
            return self.local.transcribe(audio, partial=False)


def transcriber_from_env() -> Transcriber:
    """STT_FINAL=openai (default when OPENAI_API_KEY is set) or whisper."""
    local = FasterWhisperTranscriber()
    choice = os.environ.get("STT_FINAL", "openai" if os.environ.get("OPENAI_API_KEY") else "whisper")
    if choice == "openai":
        log.info("finals: OpenAI %s, partials: local whisper", os.environ.get("STT_MODEL", "gpt-transcribe"))
        return HybridTranscriber(local, OpenAITranscriber())
    log.info("finals and partials: local whisper")
    return local
