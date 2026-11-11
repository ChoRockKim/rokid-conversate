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
        self.language_id = LanguageId()
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
        return self._run(audio, partial=partial, language=self.language)

    def partial_in(self, audio: np.ndarray, language: str | None) -> str:
        """Partial in a language found earlier in the same utterance ([detect_partial])."""
        return self._run(audio, partial=True, language=language)

    def detect_language(self, audio: np.ndarray) -> str:
        return self.language_id.detect(audio)

    def _run(self, audio: np.ndarray, *, partial: bool, language: str | None, info: bool = False):
        model = self._get_model()
        audio = normalize(audio)
        with self._run_lock:
            segments, _info = model.transcribe(
                audio,
                language=language,
                # Partials are thrown away a moment later, so trade accuracy for speed there.
                beam_size=1 if partial else 5,
                # The segmenter already cut on silence.
                vad_filter=False,
                # Each utterance is independent; carrying context makes Whisper repeat itself.
                condition_on_previous_text=False,
            )
            text = " ".join(s.text.strip() for s in segments).strip()
        if info:
            return text, _info.language, float(_info.language_probability or 0.0)
        return text


class LanguageId:
    """Spoken language of the first ~1 s of an utterance, with a small separate Whisper model.

    Measured on our clips (0.8 s and 1.2 s of speech): "base" got all 5 right in ~0.3 s;
    "tiny" took a Korean one for Chinese; the "small" model's own detection cost ~1.5 s per
    partial. Korean wins unless another language is clearly ahead, since a wrong "foreign"
    opens a paid translation session and writes the partial in the wrong language.

    Environment: LANGID_MODEL (default tiny: with the rule below it got all 8 of our clips
    right, 3 Korean, 2 English, 3 Japanese, 2026-11-11; ~200 MB instead of base's ~370 MB).
    """

    def __init__(self) -> None:
        self.model_name = os.environ.get("LANGID_MODEL", "tiny")
        self._model = None
        self._lock = threading.Lock()

    def _get_model(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8")
            return self._model

    def detect(self, audio: np.ndarray) -> str:
        model = self._get_model()
        with self._lock:
            language, prob, all_probs = model.detect_language(normalize(audio))
        ko = dict(all_probs or []).get("ko", 0.0)
        # Stricter since Korean partials kept opening live translation (2026-11-11).
        if language != "ko" and prob >= 0.7 and ko < 0.05:
            log.info("language id: %s %.2f (ko %.2f)", language, prob, ko)
            return language
        if language != "ko":
            log.info("language id: %s %.2f but ko %.2f -> ko", language, prob, ko)
        return "ko"


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

    Environment: OPENAI_API_KEY, STT_MODEL (default gpt-transcribe), STT_LANGUAGES (ko,en,ja,zh).
    """

    def __init__(self) -> None:
        from openai import OpenAI

        self.model = os.environ.get("STT_MODEL", "gpt-transcribe")
        # Expected languages: Korean first, plus what a foreign speaker is likely to use, so
        # foreign speech is written in its own language (and then translated) instead of being
        # forced into Korean. STT_LANGUAGES="" lets the model detect freely.
        self.languages = [x for x in os.environ.get("STT_LANGUAGES", "ko,en,ja,zh").split(",") if x.strip()]
        self.client = OpenAI(timeout=20.0, max_retries=1)

    def transcribe(self, audio: np.ndarray, *, partial: bool, languages: list[str] | None = None) -> str:
        extra = {}
        languages = languages or self.languages
        if languages:
            # gpt-transcribe takes a list ("languages"); older models a single "language".
            extra = {"languages": languages} if self.model == "gpt-transcribe" else {"language": languages[0]}
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

    def detect_language(self, audio: np.ndarray) -> str:
        detect = getattr(self.local, "detect_language", None)
        return detect(audio) if detect else "ko"

    def partial_in(self, audio: np.ndarray, language: str | None) -> str:
        partial_in = getattr(self.local, "partial_in", None)
        return partial_in(audio, language) if partial_in else self.local.transcribe(audio, partial=True)

    def transcribe_korean(self, audio: np.ndarray) -> str:
        """Again, as Korean: short Korean utterances came back as English or Japanese
        ("Groits.", "お尻が見たいよ。") and got a made-up translation."""
        try:
            return self.final.transcribe(audio, partial=False, languages=["ko"])
        except TypeError:  # a final backend without the languages option
            pass
        except Exception:
            log.exception("Korean re-transcription failed")
        partial_in = getattr(self.local, "partial_in", None)
        return partial_in(audio, "ko") if partial_in else ""

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
