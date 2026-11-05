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
    """Either one-step [suggest], or two-step [triage] + [answer] (preferred: the "thinking"
    hint is shown only once triage decided there is something worth answering)."""

    def suggest(self, transcript: list[str], shown: list[str]) -> Assist | None:
        """Blocking; called from a worker thread. `shown` = titles already on screen recently."""
        ...


_CRITERIA = """대화를 듣는 사람에게 도움이 될 새 정보가 있으면 적극적으로 알려준다. 이런 경우에 답한다:
- 금액·단위·시간 환산: 외화 금액은 현재 환율로 원화(예: "110달러" -> 약 몇 원), 마일·파운드·화씨 등은
  미터법으로, 다른 나라 시각은 한국 시각으로
- 누군가 묻거나 궁금해하는 사실 (날짜, 수치, 가격, 인물, 장소, 정의, 방법 등). 혼잣말 같은 질문도 포함
- 전문 용어·약어·신조어, 또는 한 번 설명해 주면 좋을 개념
- 대화에 나온 사람·회사·제품·작품·장소·사건에 대한 유용한 최신 사실 (가격, 출시일, 위치, 영업시간, 평가 등)
- 대화 속 수치나 사실 주장이 틀렸거나 확인이 필요할 때
핵심: 대화에 이미 나온 내용을 되풀이하거나 요약하는 것은 정보가 아니다. 대화에 없는 새 사실을 더할 수 있을 때만 답한다.
다음은 건너뛴다:
- 인사, 잡담, 감탄, "잘 들려?" 같은 확인, 의견·감정 표현만 있을 때
- 회의, 예산, 밥 같은 일상 단어 자체의 뜻
- 받아쓰기가 너무 이상해서 무슨 말인지 알 수 없을 때
- 바로 전에 보여준 주제와 같을 때"""

TRIAGE_INSTRUCTIONS = f"""너는 스마트 안경을 쓴 사용자의 대화를 옆에서 듣는 보조다.
아래 대화 받아쓰기(음성 인식이라 틀린 글자가 있을 수 있음)를 보고, 지금 화면에 정보를 띄울지만 판단한다.
"[새]" 표시가 붙은 줄만 새로 들은 말이다. 거기서만 고른다. 표시 없는 줄은 이미 지나간 대화라 맥락으로만 쓰고,
그 안의 질문이나 주제는 이미 처리했으므로 다시 고르지 않는다.
{_CRITERIA}

반드시 JSON 하나만 출력한다.
띄울 때: {{"topic": "찾아서 알려줄 내용을 한 문장 질문으로 (예: 방탄소년단 데뷔일은?)"}}
건너뛸 때: {{"skip": true}}"""

ANSWER_INSTRUCTIONS = """너는 스마트 안경 화면에 짧은 정보를 띄우는 보조다.
주어진 질문에 정확히 답한다. 환율·가격·최신 사실은 웹 검색으로 확인한다. 대화는 맥락으로만 참고한다.
대화에 이미 나온 내용은 쓰지 않는다. 대화에 없는 새 사실(숫자, 날짜, 환산 결과 등)만 쓴다.
환산은 결과를 바로 쓴다 (예: "110달러는 약 15만 원이다(1달러 약 1,380원)").
백과사전처럼 사실만 쓴다. 사용자에게 말을 걸지 않는다:
인사, 되묻기, 제안("~해 보세요"), 공감("궁금하시군요"), 감탄, 대화 내용 요약은 쓰지 않는다.
문장은 "~이다" 또는 "~입니다"로 끝낸다.
안경 화면이 아주 작다. 반드시 JSON 하나만 출력한다:
{"title": "15자 이내 주제", "text": "한국어 80자 이내, 핵심 사실만 한두 문장"}
링크, URL, 출처 표시, 괄호 속 사이트 이름은 절대 넣지 않는다.
확실한 답을 찾지 못하면 {"skip": true}"""

# One-step prompt (kept for ASSIST_TRIAGE=0).
INSTRUCTIONS = f"""너는 스마트 안경을 쓴 사용자의 대화를 옆에서 돕는 보조다.
아래 대화 받아쓰기(음성 인식이라 틀린 글자가 있을 수 있음)를 보고 필요하면 웹 검색으로 답한다.
{_CRITERIA}

안경 화면이 아주 작다. 반드시 JSON 하나만 출력한다:
{{"title": "15자 이내 주제", "text": "한국어 80자 이내, 핵심 사실만 한두 문장"}}
링크, URL, 출처 표시, 괄호 속 사이트 이름은 절대 넣지 않는다.
건너뛸 때는 {{"skip": true}}"""

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
    """OpenAI Responses API, in two steps:
      1. triage (ASSIST_TRIAGE_MODEL, default gpt-6-luna, no tools): is there anything worth
         showing? Cheap and fast; most conversation ends here, with nothing on screen.
      2. answer (ASSIST_MODEL, default gpt-6.1-terra, web_search): only for what triage picked.
    Before this split every ask showed "thinking" and then mostly skipped (15 of 17 asks on
    2026-11-05), which looked like the AI giving up.

    Environment: OPENAI_API_KEY, ASSIST_MODEL, ASSIST_TRIAGE_MODEL,
    ASSIST_WEB_SEARCH (1 default / 0), ASSIST_TRIAGE (1 default / 0 = one-step).
    """

    def __init__(self) -> None:
        from openai import OpenAI

        self.model = os.environ.get("ASSIST_MODEL", "gpt-6.1-terra")
        self.triage_model = os.environ.get("ASSIST_TRIAGE_MODEL", "gpt-6-luna")
        self.web_search = os.environ.get("ASSIST_WEB_SEARCH", "1") != "0"
        if os.environ.get("ASSIST_TRIAGE", "1") == "0":
            self.triage = None  # type: ignore[assignment]  # engine falls back to suggest()
        # No SDK retries: a late answer is useless on the glasses, and retrying a rate-limited
        # request only holds up the next one. AssistEngine backs off instead.
        self.client = OpenAI(timeout=30.0, max_retries=0)

    @staticmethod
    def _conversation(transcript: list[str], shown: list[str]) -> str:
        prompt = "대화:\n" + "\n".join(f"- {line}" for line in transcript)
        if shown:
            prompt += "\n\n이미 보여준 주제: " + ", ".join(shown)
        return prompt

    def _call(self, step: str, model: str, instructions: str, prompt: str, search: bool) -> str:
        from openai import RateLimitError

        try:
            raw = self.client.responses.with_raw_response.create(
                model=model,
                instructions=instructions,
                input=prompt,
                tools=[{"type": "web_search", "search_context_size": "low",
                        "user_location": {"type": "approximate", "country": "KR"}}]
                if search else [],
            )
        except RateLimitError as e:
            raise RateLimited(retry_after_seconds(str(e), e.response.headers.get("retry-after"))) from e
        response = raw.parse()
        usage = getattr(response, "usage", None)
        text = response.output_text or ""
        # Rate-limit headers show the account's real per-minute limit (it rises with the tier).
        log.info(
            "assist %s: %s tokens in/out %s/%s, TPM limit %s remaining %s, reply %r",
            step, model,
            getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"),
            raw.headers.get("x-ratelimit-limit-tokens"), raw.headers.get("x-ratelimit-remaining-tokens"),
            text[:120],
        )
        return text

    def triage(self, transcript: list[str], shown: list[str]) -> str | None:
        reply = self._call("triage", self.triage_model, TRIAGE_INSTRUCTIONS,
                           self._conversation(transcript, shown), search=False)
        return parse_topic(reply)

    def answer(self, topic: str, transcript: list[str]) -> Assist | None:
        prompt = f"질문: {topic}\n\n" + self._conversation(transcript, [])
        return parse_reply(self._call("answer", self.model, ANSWER_INSTRUCTIONS, prompt, self.web_search))

    def suggest(self, transcript: list[str], shown: list[str]) -> Assist | None:
        reply = self._call("one-step", self.model, INSTRUCTIONS,
                           self._conversation(transcript, shown), self.web_search)
        return parse_reply(reply)


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text or "").lower()


def similar(a: str, b: str, threshold: float = 0.6) -> bool:
    """Same topic or answer, ignoring spaces and punctuation."""
    import difflib

    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    if x in y or y in x:
        return True
    return difflib.SequenceMatcher(None, x, y).ratio() >= threshold


def restates(answer: str, conversation: list[str], threshold: float = 0.7) -> bool:
    """True when the answer adds nothing: it is (nearly) a line of the conversation, or every
    number in it was already said."""
    lines = [re.sub(r"^\[새\]\s*", "", line) for line in conversation]
    if any(similar(answer, line, threshold) for line in lines):
        return True
    numbers = set(re.findall(r"\d[\d,.]*", answer))
    said = set(re.findall(r"\d[\d,.]*", " ".join(lines)))
    words = set(re.findall(r"[가-힣A-Za-z]{2,}", answer))
    said_words = set(re.findall(r"[가-힣A-Za-z]{2,}", " ".join(lines)))
    new_words = words - said_words
    return bool(numbers) and numbers <= said and len(new_words) <= 2


def parse_topic(text: str) -> str | None:
    """Triage reply -> the question to answer, or None for skip / anything unusable."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("skip"):
        return None
    topic = str(data.get("topic", "")).strip()
    return topic or None


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
    # Recent topics/answers kept to drop repeats (a topic once answered came back again and
    # again because the same question stayed in the context window).
    remember_answers: int = 10


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
        self.recent: deque[str] = deque(maxlen=self.config.remember_answers)
        self.new_lines: list[str] = []
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
        self.new_lines.append(text)
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
        # Mark what is new since the last ask; the rest is context only.
        new = set(self.new_lines)
        self.new_lines = []
        transcript = [f"[새] {line}" if line in new else line for line in self.lines]
        busy = False
        topic = None
        triage = getattr(self.assistant, "triage", None)
        try:
            if triage is not None:
                # Step 1 shows nothing; "thinking" only once there is something to answer.
                topic = await asyncio.to_thread(triage, transcript, list(self.shown))
                if topic is not None and self._seen(topic):
                    log.info("assist: topic %r repeats a recent one, skipped", topic)
                    topic = None
                if topic is None:
                    result = None
                else:
                    busy = await self._busy(True)
                    result = await asyncio.to_thread(self.assistant.answer, topic, transcript)
            else:
                busy = await self._busy(True)
                result = await asyncio.to_thread(self.assistant.suggest, transcript, list(self.shown))
        except RateLimited as e:
            log.warning("assist: %s", e)
            # No ask (not even for a question) until the limit resets.
            self.cooldown_until = self.clock() + e.seconds
            result = None
        except Exception:
            log.exception("assist failed")
            result = None
        if busy:
            await self._busy(False)
        if result is not None and restates(result.text, list(self.lines)):
            log.info("assist: answer %r only restates the conversation, dropped", result.text)
            result = None
        if result is not None and (self._seen(result.title) or self._seen(result.text)):
            log.info("assist: answer %r repeats a recent one, dropped", result.title)
            result = None
        if result is not None:
            if result.title:
                self.shown.append(result.title)
            self.recent.extend([result.title, result.text])
            await self.send(result)
        if triage is not None and topic is not None:
            self.recent.append(topic)
        self.task = None
        self._maybe_ask()

    def _seen(self, text: str) -> bool:
        return any(similar(text, old) for old in self.recent)

    async def _busy(self, value: bool) -> bool:
        if self.on_busy is not None:
            await self.on_busy(value)
        return value

    def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
