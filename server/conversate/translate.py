"""Korean translation for captions spoken in another language.

The finished caption is shown at once; if it is mostly not Korean, a translation follows a moment
later as the same caption id with a "translation" field, and the glasses show it on the next line.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Protocol

log = logging.getLogger(__name__)

_HANGUL = re.compile(r"[가-힣ㄱ-ㆎ]")
_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)


def needs_translation(text: str) -> bool:
    """True when [text] has at least 3 letters and less than 30% of its letters are Hangul."""
    letters = _LETTER.findall(text)
    if len(letters) < 3:  # "OK", "Hi": not worth a translation line
        return False
    hangul = len(_HANGUL.findall(text))
    return hangul / len(letters) < 0.3


class Translator(Protocol):
    def translate(self, text: str) -> str | None:
        """Blocking; Korean translation, or None when there is nothing to translate."""
        ...


INSTRUCTIONS = """다음 문장을 자연스러운 한국어로 번역한다. 대화 중 들린 말을 받아쓴 것이다.
번역문만 출력한다. 설명, 따옴표, 원문은 넣지 않는다."""


class OpenAITranslator:
    """TRANSLATE_MODEL (default gpt-6-luna: cheap and fast)."""

    def __init__(self) -> None:
        from openai import OpenAI

        self.model = os.environ.get("TRANSLATE_MODEL", "gpt-6-luna")
        self.client = OpenAI(timeout=15.0, max_retries=1)

    def translate(self, text: str) -> str | None:
        response = self.client.responses.create(model=self.model, instructions=INSTRUCTIONS, input=text)
        out = (response.output_text or "").strip().strip('"').strip()
        log.info("translate: %r -> %r", text[:80], out[:80])
        return out or None


def translator_from_env() -> Translator | None:
    if not os.environ.get("OPENAI_API_KEY") or os.environ.get("TRANSLATE", "1") == "0":
        return None
    return OpenAITranslator()
