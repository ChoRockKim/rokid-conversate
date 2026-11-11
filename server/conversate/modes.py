"""Listening modes, picked on the glasses' main menu: each takes in sound differently.

  daily    two people talking (the wearer and someone close): the default.
  lecture  one person at a distance talking at length (a class, a talk, a film).
  media    sound that keeps going (videos, podcasts, songs).

The glasses pick their microphones and how eagerly they send sound (SpeechGate) per mode;
the server here picks how utterances are cut (Segmenter) and what the AI card does.
None of the values below were checked on the glasses yet.
"""

from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT = "daily"


@dataclass(frozen=True)
class Mode:
    name: str
    label: str
    # SegmenterConfig fields to override (keeps the rest, e.g. use_webrtc in tests).
    segmenter: dict = field(default_factory=dict)
    assist: bool = True
    # Told to the AI with the conversation, so it picks what helps in this setting.
    situation: str = ""
    # Triage picks of these kinds are not shown.
    blocked_kinds: frozenset = frozenset()
    # Volunteered cards at most this often (None: the default).
    proactive_interval_s: float | None = None


MODES: dict[str, Mode] = {
    "daily": Mode("daily", "일상"),
    "lecture": Mode(
        "lecture", "수업·영화",
        segmenter=dict(
            # A distant voice is quieter and a speaker pauses to think: more lenient, and a
            # longer pause before the line ends; long talk still comes out in pieces.
            webrtc_min_rms=20.0, webrtc_floor_ratio=1.5,
            end_silence_ms=900, long_after_ms=9_000, long_end_silence_ms=500, max_segment_ms=15_000,
            min_speech_ms=400,
        ),
        situation="사용자는 지금 수업·강의나 영화를 듣고 있다. 대화 상대가 아니라 멀리서 한 사람이 말한다. "
                  "낯선 용어·개념·인물·사건·작품 설명을 띄운다. 말하는 사람이 던지는 질문은 대부분 수사 의문문이니 "
                  "사실을 묻는 경우만 답한다.",
        blocked_kinds=frozenset({"suggest"}),
        proactive_interval_s=30.0,
    ),
    "media": Mode(
        "media", "미디어",
        segmenter=dict(
            # Music and voices over music: WebRTC VAD at its most lenient, barely above the
            # background, and lines cut often (songs and podcasts hardly pause).
            vad_mode=0, webrtc_min_rms=20.0, webrtc_floor_ratio=1.2,
            end_silence_ms=500, long_after_ms=4_000, long_end_silence_ms=300, max_segment_ms=8_000,
        ),
        assist=False,
    ),
}


def mode_for(name: str | None) -> Mode:
    return MODES.get((name or "").strip().lower(), MODES[DEFAULT])
