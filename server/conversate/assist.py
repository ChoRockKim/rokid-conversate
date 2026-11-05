"""AI results for the top ~30% of the glasses screen (`assist` messages).

Every so often the server hands the recent finished captions to an LLM and asks for the one thing
that would help the wearer right now: an unfamiliar term or name explained, a factual question
answered (with web search), background on a person or company. Like the AI cues on the Even G2,
it stays quiet when there is nothing useful to say.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Assist:
    title: str
    text: str


class Assistant(Protocol):
    def suggest(self, transcript: list[str], shown: list[str]) -> Assist | None:
        """Blocking; called from a worker thread. `shown` = titles already on screen recently."""
        ...


INSTRUCTIONS = """너는 스마트 안경을 쓴 사용자의 대화를 옆에서 돕는 보조다.
아래 대화 받아쓰기(음성 인식이라 틀린 글자가 있을 수 있음)를 보고, 지금 사용자에게 가장 도움이 될 것 딱 하나를 고른다:
- 낯선 용어·약어·고유명사 설명
- 대화에 나온 사실 질문에 대한 답 (필요하면 웹 검색)
- 언급된 사람·회사·제품의 배경
도움이 될 게 없거나, 이미 보여준 주제와 겹치면 건너뛴다.

안경 화면이 아주 작다. 반드시 JSON 하나만 출력한다:
{"title": "20자 이내 주제", "text": "한국어 120자 이내, 핵심만"}
건너뛸 때는 {"skip": true}"""


class OpenAIAssistant:
    """OpenAI Responses API, optionally with the built-in web_search tool.

    Environment:
      OPENAI_API_KEY       required
      ASSIST_MODEL         default gpt-6.1-terra (fast); gpt-6-luna is cheaper, gpt-6.1-sol smarter
      ASSIST_WEB_SEARCH    1 (default) to let the model search the web, 0 to turn it off
    """

    def __init__(self) -> None:
        from openai import OpenAI

        self.model = os.environ.get("ASSIST_MODEL", "gpt-6.1-terra")
        self.web_search = os.environ.get("ASSIST_WEB_SEARCH", "1") != "0"
        self.client = OpenAI(timeout=30.0)

    def suggest(self, transcript: list[str], shown: list[str]) -> Assist | None:
        prompt = "대화:\n" + "\n".join(f"- {line}" for line in transcript)
        if shown:
            prompt += "\n\n이미 보여준 주제: " + ", ".join(shown)
        response = self.client.responses.create(
            model=self.model,
            instructions=INSTRUCTIONS,
            input=prompt,
            tools=[{"type": "web_search", "search_context_size": "low",
                    "user_location": {"type": "approximate", "country": "KR"}}]
            if self.web_search else [],
        )
        return parse_reply(response.output_text)


def parse_reply(text: str) -> Assist | None:
    """Pull the JSON object out of the model's reply. Anything unusable counts as a skip."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("skip"):
        return None
    title = str(data.get("title", "")).strip()
    body = str(data.get("text", "")).strip()
    if not body:
        return None
    return Assist(title[:40], body[:300])


def assistant_from_env() -> Assistant | None:
    if not os.environ.get("OPENAI_API_KEY"):
        log.info("OPENAI_API_KEY not set: AI panel (assist) is off")
        return None
    return OpenAIAssistant()


@dataclass
class AssistConfig:
    # Ask at most this often ...
    min_interval_s: float = 15.0
    # ... and only once this much new speech has been transcribed since the last ask.
    min_new_chars: int = 40
    # How much of the conversation the model sees.
    context_lines: int = 12
    # Titles remembered to avoid repeating a topic.
    remember_titles: int = 6


class AssistEngine:
    """Per connection: collects final captions, decides when to ask, sends results."""

    def __init__(
        self,
        assistant: Assistant,
        send: Callable[[Assist], Awaitable[None]],
        config: AssistConfig | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.assistant = assistant
        self.send = send
        self.config = config or AssistConfig()
        self.clock = clock
        self.lines: deque[str] = deque(maxlen=self.config.context_lines)
        self.shown: deque[str] = deque(maxlen=self.config.remember_titles)
        self.new_chars = 0
        self.last_ask = -1e9
        self.task: asyncio.Task | None = None

    def on_final(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.lines.append(text)
        self.new_chars += len(text)
        self._maybe_ask()

    def _maybe_ask(self) -> None:
        cfg = self.config
        if self.task is not None and not self.task.done():
            return  # re-checked when the running ask finishes
        if self.new_chars < cfg.min_new_chars:
            return
        wait = cfg.min_interval_s - (self.clock() - self.last_ask)
        if wait > 0:
            self.task = asyncio.create_task(self._ask_after(wait))
        else:
            self.task = asyncio.create_task(self._ask_after(0))

    async def _ask_after(self, delay: float) -> None:
        if delay > 0:
            await asyncio.sleep(delay)
        self.last_ask = self.clock()
        self.new_chars = 0
        transcript = list(self.lines)
        try:
            result = await asyncio.to_thread(self.assistant.suggest, transcript, list(self.shown))
        except Exception:
            log.exception("assist failed")
            result = None
        if result is not None:
            if result.title:
                self.shown.append(result.title)
            await self.send(result)
        self.task = None
        self._maybe_ask()

    def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
