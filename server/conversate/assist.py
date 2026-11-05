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
- 대화에 나온 사실 질문에 대한 답 (필요하면 웹 검색). 마지막 문장이 질문이면 그 답이 최우선
- 언급된 사람·회사·제품의 배경
도움이 될 게 없거나, 이미 보여준 주제와 겹치면 건너뛴다.

안경 화면이 아주 작다. 반드시 JSON 하나만 출력한다:
{"title": "15자 이내 주제", "text": "한국어 80자 이내, 핵심 사실만 한두 문장"}
링크, URL, 출처 표시, 괄호 속 사이트 이름은 절대 넣지 않는다.
건너뛸 때는 {"skip": true}"""

# Korean/English cues that a finished caption is a question worth answering right away.
_QUESTION = re.compile(
    r"\?|？|언제|어디|누구|누가|왜|뭐|무엇|무슨|몇|얼마|어떻게|어떤|어느"
    r"|더라|나요|까요|ㄹ까|을까|를까|니\s*$|냐\s*$|지\s*$|죠\s*$|는지|인지"
    r"|\b(what|when|where|who|why|how|which)\b",
    re.IGNORECASE,
)


def looks_like_question(text: str) -> bool:
    return bool(_QUESTION.search(text))


_MD_LINK = re.compile(r"\(?\[([^\]]*)\]\([^)]*\)\)?")
_URL = re.compile(r"\(?https?://\S+\)?")


def strip_links(text: str) -> str:
    """Web search answers come with citations like "([site](https://...))"; drop them."""
    text = _MD_LINK.sub("", text)
    text = _URL.sub("", text)
    text = re.sub(r"\(\s*\)", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


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
        # No SDK retries: a late answer is useless on the glasses, and retrying a rate-limited
        # request only holds up the next one. AssistEngine backs off instead.
        self.client = OpenAI(timeout=30.0, max_retries=0)

    def suggest(self, transcript: list[str], shown: list[str]) -> Assist | None:
        from openai import RateLimitError

        prompt = "대화:\n" + "\n".join(f"- {line}" for line in transcript)
        if shown:
            prompt += "\n\n이미 보여준 주제: " + ", ".join(shown)
        try:
            raw = self.client.responses.with_raw_response.create(
                model=self.model,
                instructions=INSTRUCTIONS,
                input=prompt,
                tools=[{"type": "web_search", "search_context_size": "low",
                        "user_location": {"type": "approximate", "country": "KR"}}]
                if self.web_search else [],
            )
        except RateLimitError as e:
            raise RateLimited(retry_after_seconds(str(e), e.response.headers.get("retry-after"))) from e
        response = raw.parse()
        usage = getattr(response, "usage", None)
        # Rate-limit headers show the account's real per-minute limit (it rises with the tier).
        log.info(
            "assist: %s tokens in/out %s/%s, TPM limit %s remaining %s, reply %r",
            self.model,
            getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"),
            raw.headers.get("x-ratelimit-limit-tokens"), raw.headers.get("x-ratelimit-remaining-tokens"),
            (response.output_text or "")[:120],
        )
        return parse_reply(response.output_text)


class RateLimited(Exception):
    """The LLM said "too many requests"; wait [seconds] before asking again."""

    def __init__(self, seconds: float) -> None:
        super().__init__(f"rate limited, retry in {seconds:.0f}s")
        self.seconds = seconds


def retry_after_seconds(message: str, header: str | None) -> float:
    """From the Retry-After header, or OpenAI's "Please try again in 22.482s"; 60 s otherwise."""
    if header:
        try:
            return float(header)
        except ValueError:
            pass
    match = re.search(r"try again in ([\d.]+)\s*(ms|s)", message)
    if match:
        value = float(match.group(1))
        return value / 1000 if match.group(2) == "ms" else value
    return 60.0


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
    title = strip_links(str(data.get("title", "")))
    body = strip_links(str(data.get("text", "")))
    if not body:
        return None
    return Assist(title[:40], body[:200])


def assistant_from_env() -> Assistant | None:
    if not os.environ.get("OPENAI_API_KEY"):
        log.info("OPENAI_API_KEY not set: AI panel (assist) is off")
        return None
    return OpenAIAssistant()


@dataclass
class AssistConfig:
    # Ask at most this often ...
    min_interval_s: float = 8.0
    # ... and only once this much new speech has been transcribed since the last ask.
    min_new_chars: int = 20
    # A finished caption that looks like a question is asked right away (only this gap).
    question_interval_s: float = 2.0
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
        on_busy: Callable[[bool], Awaitable[None]] | None = None,
    ) -> None:
        self.assistant = assistant
        self.send = send
        # Told when a request to the LLM starts (True) and ends (False), for a "thinking" hint.
        self.on_busy = on_busy
        self.config = config or AssistConfig()
        self.clock = clock
        self.lines: deque[str] = deque(maxlen=self.config.context_lines)
        self.shown: deque[str] = deque(maxlen=self.config.remember_titles)
        self.new_chars = 0
        self.last_ask = -1e9
        self.task: asyncio.Task | None = None
        # True while self.task is only sleeping before its ask (safe to cancel and restart).
        self.waiting = False
        # A question arrived; ask as soon as possible.
        self.question = False
        # After a rate-limit answer, nothing is asked before this time.
        self.cooldown_until = -1e9

    def on_final(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.lines.append(text)
        self.new_chars += len(text)
        if looks_like_question(text):
            self.question = True
        self._maybe_ask()

    def _maybe_ask(self) -> None:
        cfg = self.config
        if self.task is not None and not self.task.done():
            if not (self.question and self.waiting):
                return  # re-checked when the running ask finishes
            self.task.cancel()  # a question jumps the queue of a scheduled, not yet sent ask
        if not self.question and self.new_chars < cfg.min_new_chars:
            return
        gap = cfg.question_interval_s if self.question else cfg.min_interval_s
        now = self.clock()
        wait = max(0.0, gap - (now - self.last_ask), self.cooldown_until - now)
        self.task = asyncio.create_task(self._ask_after(wait))

    async def _ask_after(self, delay: float) -> None:
        self.waiting = True
        try:
            if delay > 0:
                await asyncio.sleep(delay)
        finally:
            self.waiting = False
        self.last_ask = self.clock()
        self.new_chars = 0
        self.question = False
        transcript = list(self.lines)
        if self.on_busy is not None:
            await self.on_busy(True)
        try:
            result = await asyncio.to_thread(self.assistant.suggest, transcript, list(self.shown))
        except RateLimited as e:
            log.warning("assist: %s", e)
            # No ask (not even for a question) until the limit resets.
            self.cooldown_until = self.clock() + e.seconds
            result = None
        except Exception:
            log.exception("assist failed")
            result = None
        if self.on_busy is not None:
            await self.on_busy(False)
        if result is not None:
            if result.title:
                self.shown.append(result.title)
            await self.send(result)
        self.task = None
        self._maybe_ask()

    def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
