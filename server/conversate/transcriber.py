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
