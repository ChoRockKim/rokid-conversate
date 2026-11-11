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
    # Wikipedia article whose picture helps (a person, place, animal, dish, product), or None.
    image: str | None = None


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
- 회의·협상·상담·면접처럼 무언가 정해지는 대화에서, 빠진 조건이나 지금 되물으면 좋을 점이 분명할 때 (kind: suggest).
  예: 가격은 나왔는데 납기·배송비 이야기가 없을 때, 상대가 말한 수치가 노트와 다를 때
- 사전 노트가 있으면: 대화가 노트에 적힌 사람·안건·숫자·용어에 닿을 때 그 노트 내용을 꺼내 준다 (kind: note).
  예: 노트에 "단가 45만 원"이 있고 상대가 가격을 물으면 그 숫자를 띄운다
핵심: 대화에 이미 나온 내용을 되풀이하거나 요약하는 것은 정보가 아니다. 대화에 없는 새 사실을 더할 수 있을 때만 답한다.
다음은 건너뛴다:
- 인사, 잡담, 감탄, "잘 들려?" 같은 확인, 의견·감정 표현만 있을 때
- 회의, 예산, 밥 같은 일상 단어 자체의 뜻
- 받아쓰기가 너무 이상해서 무슨 말인지 알 수 없을 때
- 바로 전에 보여준 주제와 같을 때
- 웹 검색으로 답할 수 없는 것: 사용자의 현재 위치가 필요한 것("가장 가까운 ~", "근처 ~"),
  대화 속 사람의 개인 사정, 이 안경 앱이나 기기의 설정 방법, 화면에 무엇이 보이는지"""

TRIAGE_INSTRUCTIONS = f"""너는 스마트 안경을 쓴 사용자의 대화를 옆에서 듣는 보조다.
아래 대화 받아쓰기(음성 인식이라 틀린 글자가 있을 수 있음)를 보고, 지금 화면에 정보를 띄울지만 판단한다.
"[새]" 표시가 붙은 줄만 새로 들은 말이다. 거기서만 고른다. 표시 없는 줄은 이미 지나간 대화라 맥락으로만 쓰고,
그 안의 질문이나 주제는 이미 처리했으므로 다시 고르지 않는다.
{_CRITERIA}

대화 흐름과 상관없는 사실, 단어 하나에만 기대어 뜬금없이 끼어드는 정보는 띄우지 않는다.
지금 이 대화를 하는 사람이 "마침 궁금했는데" 하고 반길 정보만 띄운다.

반드시 JSON 하나만 출력한다.
띄울 때:
{{"quote": "근거가 된 [새] 줄의 말을 글자 그대로 (10~40자)",
  "kind": "question | conversion | term | fact | correction | note | suggest 중 하나",
  "text": "kind 가 suggest 일 때만: 화면에 띄울 메모 한 줄 (\"확인할 점: 납기 일정이 아직 안 나옴\" 처럼 20~50자, 명령·말 걸기 없이)",
  "why": "이 정보가 지금 대화에 왜 도움이 되는지 한 줄",
  "topic": "대화만 모르는 사람도 이해할 완전한 질문. 대명사·생략은 풀어 쓰고, 끝에 (상황: 지금 무슨 이야기 중인지 한 줄)",
  "search": "웹 검색이 꼭 필요하면 true: 가격·환율·일정·영업시간·날씨·경기 결과·최근 소식처럼 바뀌는 정보, 또는 잘 알려지지 않은 사람·가게·제품. 역사·정의·과학·지리·방법·단위 환산처럼 이미 아는 일반 지식이면 false"}}
예: {{"quote": "이 운동화 110달러였어", "kind": "conversion", "why": "산 물건 가격을 원화로 가늠하려 함",
     "topic": "운동화 가격 110달러는 원화로 얼마인가? (상황: 해외 직구한 운동화가 비싸다고 이야기 중)", "search": true}}
건너뛸 때: {{"skip": true}}"""

ANSWER_INSTRUCTIONS = """너는 스마트 안경 화면에 짧은 정보를 띄우는 보조다.
주어진 질문에 정확히 답한다. 빠른 답이 중요하다: 이미 확실히 아는 일반 지식(역사, 정의, 과학 등)은
검색하지 않고 바로 답하고, 가격·환율·일정·최근 소식처럼 바뀌는 정보만 웹 검색한다.
사전 노트가 있으면 먼저 참고하고, 노트에 답이 있으면 노트 내용을 쓴다 (노트가 웹보다 우선).
질문 끝의 (상황)과 대화 흐름을 보고, 이 대화를 하는 사람에게 지금 가장 쓸모 있는 각도로 답한다.
예: 산 물건 가격이 비싸다는 이야기라면 원화 환산에 국내 가격과의 비교를 붙이고,
약속 장소 이야기라면 위치·영업시간을, 사람 이야기라면 대화 주제와 관련된 경력을 고른다.
대화에 이미 나온 내용은 쓰지 않는다. 대화에 없는 새 사실(숫자, 날짜, 환산 결과 등)만 쓴다.
환산은 결과를 바로 쓴다 (예: "110달러는 약 15만 원이다(1달러 약 1,380원)").
백과사전처럼 사실만 쓴다. 사용자에게 말을 걸지 않는다:
인사, 되묻기, 제안("~해 보세요"), 공감("궁금하시군요"), 감탄, 대화 내용 요약은 쓰지 않는다.
문장은 "~이다" 또는 "~입니다"로 끝낸다.
안경 화면이 아주 작다. 반드시 JSON 하나만 출력한다:
{"title": "15자 이내 주제", "text": "한국어 80자 이내, 핵심 사실만 한두 문장", "image": "위키백과 문서 제목 또는 null"}
image: 눈으로 보면 바로 알 수 있는 대상(사람, 장소, 건물, 동물, 식물, 음식, 제품, 작품)일 때만
그 대상의 위키백과 문서 제목(한국어, 없으면 영어)을 쓴다. 숫자·날짜·환산·개념·방법 설명이면 null.
링크, URL, 출처 표시, 괄호 속 사이트 이름은 절대 넣지 않는다.
정확한 답이 없어도 질문과 관련해 쓸모 있는 일반 사실이 있으면 그것을 답한다.
관련된 사실을 하나도 찾지 못했을 때만 {"skip": true}"""

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

    def __init__(self, notes: Callable[[], str] | None = None) -> None:
        from openai import OpenAI

        # Prep notes the wearer wrote on the phone (notes.py); read fresh on every ask.
        self.notes = notes or (lambda: "")
        self.model = os.environ.get("ASSIST_MODEL", "gpt-6.1-terra")
        # Triage runs every ~15 s of talk and mostly says "skip". It was gpt-6.1-terra because
        # the cheapest model picked off-topic facts more often; moved back to luna for cost
        # (2026-11-06; the quote check and dedupe still filter its picks).
        # ASSIST_TRIAGE_MODEL=gpt-6.1-terra brings the stronger one back.
        self.triage_model = os.environ.get("ASSIST_TRIAGE_MODEL", "gpt-6-luna")
        self.web_search = os.environ.get("ASSIST_WEB_SEARCH", "1") != "0"
        if os.environ.get("ASSIST_TRIAGE", "1") == "0":
            self.triage = None  # type: ignore[assignment]  # engine falls back to suggest()
        # No SDK retries: a late answer is useless on the glasses, and retrying a rate-limited
        # request only holds up the next one. AssistEngine backs off instead.
        self.client = OpenAI(timeout=30.0, max_retries=0)
        # Speed: little hidden reasoning for these short jobs (ASSIST_REASONING=none|low|medium|"").
        self.reasoning_effort = os.environ.get("ASSIST_REASONING", "low")
        # Triage only decides whether to show something: no hidden reasoning (it counted as
        # output tokens on every call, ~140 per triage on 2026-11-08). "low" if refused.
        self.triage_reasoning = os.environ.get("ASSIST_TRIAGE_REASONING", "none")
        self._no_reasoning: set[str] = set()
        self._refused_efforts: set[tuple[str, str]] = set()

    def _conversation(self, transcript: list[str], shown: list[str]) -> str:
        prompt = ""
        notes = self.notes().strip()
        if notes:
            prompt += f"사전 노트 (사용자가 대화 전에 적어 둔 것):\n{notes[:4000]}\n\n"
        prompt += "대화:\n" + "\n".join(f"- {line}" for line in transcript)
        if shown:
            prompt += "\n\n이미 보여준 주제: " + ", ".join(shown)
        return prompt

    def _call(self, step: str, model: str, instructions: str, prompt: str, search: bool) -> str:
        from openai import BadRequestError, RateLimitError

        try:
            started = time.monotonic()
            kwargs = dict(
                model=model,
                instructions=instructions,
                input=prompt,
                tools=[{"type": "web_search", "search_context_size": "low",
                        "user_location": {"type": "approximate", "country": "KR"}}]
                if search else [],
            )
            effort = self.triage_reasoning if step == "triage" else self.reasoning_effort
            if (model, effort) in self._refused_efforts:
                effort = self.reasoning_effort
            if effort and model not in self._no_reasoning:
                kwargs["reasoning"] = {"effort": effort}
            try:
                raw = self.client.responses.with_raw_response.create(**kwargs)
            except BadRequestError as e:
                if "reasoning" not in kwargs or "reason" not in str(e).lower():
                    raise
                raw = None
                if effort != self.reasoning_effort:
                    # This effort level is not offered by this model: use the usual one.
                    log.info("assist: %s rejects reasoning effort %r, using %r", model, effort, self.reasoning_effort)
                    self._refused_efforts.add((model, effort))
                    kwargs["reasoning"] = {"effort": self.reasoning_effort}
                    try:
                        raw = self.client.responses.with_raw_response.create(**kwargs)
                    except BadRequestError as e2:
                        if "reason" not in str(e2).lower():
                            raise
                if raw is None:
                    # This model takes no reasoning setting: remember and call it plainly.
                    log.info("assist: %s rejects reasoning effort, calling without it", model)
                    self._no_reasoning.add(model)
                    kwargs.pop("reasoning")
                    raw = self.client.responses.with_raw_response.create(**kwargs)
        except RateLimitError as e:
            raise RateLimited(retry_after_seconds(str(e), e.response.headers.get("retry-after"))) from e
        response = raw.parse()
        usage = getattr(response, "usage", None)
        text = response.output_text or ""
        # Rate-limit headers show the account's real per-minute limit (it rises with the tier).
        log.info(
            "assist %s: %s %.1fs, tokens in/out %s/%s, TPM limit %s remaining %s, reply %r",
            step, model, time.monotonic() - started,
            getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"),
            raw.headers.get("x-ratelimit-limit-tokens"), raw.headers.get("x-ratelimit-remaining-tokens"),
            text[:120],
        )
        return text

    def triage(self, transcript: list[str], shown: list[str]) -> "Pick | None":
        reply = self._call("triage", self.triage_model, TRIAGE_INSTRUCTIONS,
                           self._conversation(transcript, shown), search=False)
        return parse_triage(reply)

    def answer(self, topic: str, transcript: list[str], search: bool | None = None) -> Assist | None:
        """[search]: triage's call. Web search was the biggest cost (each answer with it read
        ~9,500 tokens of results, plus the tool call), even for general knowledge."""
        prompt = f"질문: {topic}\n\n" + self._conversation(transcript, [])
        use_search = self.web_search and (search is None or search)
        return parse_reply(self._call("answer", self.model, ANSWER_INSTRUCTIONS, prompt, use_search))

    def suggest(self, transcript: list[str], shown: list[str]) -> Assist | None:
        reply = self._call("one-step", self.model, INSTRUCTIONS,
                           self._conversation(transcript, shown), self.web_search)
        return parse_reply(reply)


def _norm(text: str) -> str:
    return re.sub(r"[\W_]+", "", text or "").lower()


def similar(a: str, b: str, threshold: float = 0.75) -> bool:
    """Same topic or answer, ignoring spaces and punctuation."""
    import difflib

    x, y = _norm(a), _norm(b)
    if not x or not y:
        return False
    short, long_ = sorted((x, y), key=len)
    # Containment only counts when the two are about the same length: "손가락인식" inside
    # "근전도와손가락인식" is a different, new answer (it was wrongly dropped).
    if short in long_ and len(short) >= 0.75 * len(long_):
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


@dataclass(frozen=True)
class Pick:
    """What triage chose: the question to answer and why, anchored to words just heard."""

    topic: str
    kind: str = "fact"
    quote: str | None = None  # None: not checked (one-step or test assistants)
    why: str = ""
    text: str = ""  # suggest: shown as is, no answer step
    search: bool | None = None  # triage: does the answer need web search (None: not said)

    @property
    def prompt(self) -> str:
        extra = f"\n(근거가 된 말: \"{self.quote}\"{' — ' + self.why if self.why else ''})" if self.quote else ""
        return self.topic + extra


# Kinds answered as soon as they come up; the rest (term, fact) are volunteered information
# and rationed, so the card does not keep popping up with things nobody asked for.
URGENT_KINDS = {"question", "conversion", "correction", "note"}
SUGGEST_TITLE = "확인할 점"


def parse_triage(text: str) -> Pick | None:
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
    if not topic:
        return None
    return Pick(
        topic=topic,
        kind=str(data.get("kind", "fact")).strip().lower() or "fact",
        quote=str(data.get("quote", "")).strip(),
        why=str(data.get("why", "")).strip(),
        text=str(data.get("text", "")).strip(),
        search=_flag(data.get("search")),
    )


def _flag(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


def quote_heard(quote: str, new_lines: list[str]) -> bool:
    """The quote must be (close to) something actually just heard; otherwise triage invented it
    or reached back into old conversation, which is where off-topic cards came from."""
    q = _norm(quote)
    if len(q) < 2:
        return False
    heard = _norm(" ".join(new_lines))
    if q in heard:
        return True
    import difflib

    m = difflib.SequenceMatcher(None, q, heard)
    block = sum(b.size for b in m.get_matching_blocks())
    return block >= 0.8 * len(q)


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
    image = data.get("image")
    image = str(image).strip()[:80] if isinstance(image, str) and image.strip().lower() not in ("", "null", "none") else None
    return Assist(title[:40], body[:200], image)


def assistant_from_env(notes: Callable[[], str] | None = None) -> Assistant | None:
    if not os.environ.get("OPENAI_API_KEY"):
        log.info("OPENAI_API_KEY not set: AI panel (assist) is off")
        return None
    return OpenAIAssistant(notes)


@dataclass
class AssistConfig:
    # Ask at most this often ...
    min_interval_s: float = 15.0  # was 8; halved the triage calls (cost), questions still go at 2 s
    # ... and only once this much new speech has been transcribed since the last ask.
    min_new_chars: int = 20
    # A finished caption that looks like a question is asked right away (only this gap).
    question_interval_s: float = 2.0
    # How much of the conversation the model sees.
    context_lines: int = 20
    # Triage sees only the last this many (cost: it runs every ~15 s of talk; the answer
    # step still gets context_lines).
    triage_lines: int = 12
    # Titles remembered to avoid repeating a topic.
    remember_titles: int = 6
    # Volunteered information (not asked for): at most one card per this many seconds.
    proactive_interval_s: float = 25.0
    # Captions this short ("네", "어") are not material for the assistant.
    min_line_chars: int = 4
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
        converter: Callable[[str], list] | None = None,
    ) -> None:
        self.assistant = assistant
        self.send = send
        # Told when a request to the LLM starts (True) and ends (False), for a "thinking" hint.
        self.on_busy = on_busy
        # Instant conversions (convert.py): money/units shown at once, without the LLM.
        self.converter = converter
        self.converted: deque[str] = deque(maxlen=10)
        self.config = config or AssistConfig()
        self.default_config = self.config
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
        # When volunteered information was last shown (rationed by proactive_interval_s).
        self.last_proactive = -1e9
        # Listening mode (modes.py): a line telling the AI the setting, and kinds not to show.
        self.situation = ""
        self.blocked_kinds: frozenset = frozenset()

    def on_final(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.lines.append(text)
        if len(_norm(text)) < self.config.min_line_chars:
            return  # context only
        self.new_lines.append(text)
        self.new_chars += len(text)
        if self.converter is not None:
            asyncio.create_task(self._convert(text))
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
        new_list = list(self.new_lines)
        new = set(new_list)
        self.new_lines = []
        transcript = [f"[새] {line}" if line in new else line for line in self.lines]
        if self.situation:
            transcript = [f"(상황: {self.situation})"] + transcript
        busy = False
        topic = None
        pick = None
        triage = getattr(self.assistant, "triage", None)
        try:
            if triage is not None:
                # Step 1 shows nothing; "thinking" only once there is something to answer.
                picked = await asyncio.to_thread(triage, self._triage_view(transcript), list(self.shown))
                pick = Pick(topic=picked, kind="question") if isinstance(picked, str) else picked
                pick = self._vet(pick, new_list)
                topic = pick.topic if pick is not None else None
                if pick is None:
                    result = None
                elif pick.kind == "suggest":
                    # A note about the conversation itself: nothing to look up, show it directly.
                    note = re.sub(r"^\s*확인할\s*점\s*[:：]\s*", "", strip_links(pick.text))[:80]
                    result = Assist(SUGGEST_TITLE, note) if note else None
                else:
                    busy = await self._busy(True)
                    if pick.search is None:
                        result = await asyncio.to_thread(self.assistant.answer, pick.prompt, transcript)
                    else:
                        result = await asyncio.to_thread(
                            self.assistant.answer, pick.prompt, transcript, search=pick.search)
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
        if result is not None and (pick is None or pick.kind != "suggest") and restates(result.text, list(self.lines)):
            log.info("assist: answer %r only restates the conversation, dropped", result.text)
            result = None
        if result is not None and (self._seen(result.title) or self._seen(result.text)):
            log.info("assist: answer %r repeats a recent one, dropped", result.title)
            result = None
        if result is not None:
            if result.title:
                self.shown.append(result.title)
            self.recent.extend([result.title, result.text])
            if pick is not None and pick.kind not in URGENT_KINDS:
                self.last_proactive = self.clock()
            await self.send(result)
        if triage is not None and topic is not None:
            self.recent.append(topic)
        self.task = None
        self._maybe_ask()

    async def _convert(self, text: str) -> None:
        try:
            found = await asyncio.to_thread(self.converter, text)
        except Exception:
            log.exception("conversion failed")
            return
        for conv in found:
            if self._seen(conv.text) or any(similar(conv.span, s) for s in self.converted):
                continue
            self.converted.append(conv.span)
            self.recent.extend([conv.title, conv.text])
            self.shown.append(conv.title)
            log.info("assist: instant conversion %s", conv.text)
            await self.send(Assist(conv.title, conv.text))

    def _triage_view(self, transcript: list[str]) -> list[str]:
        """The last [triage_lines] lines (and the situation line, if any); new lines always."""
        head = [line for line in transcript[:1] if line.startswith("(상황:")]
        body = transcript[len(head):]
        keep = max(self.config.triage_lines, sum(1 for line in body if line.startswith("[새]")))
        return head + body[-keep:]

    def _vet(self, pick: "Pick | None", new_lines: list[str]) -> "Pick | None":
        """Checks on what triage picked, before anything shows on screen."""
        if pick is None:
            return None
        if pick.kind in self.blocked_kinds:
            log.info("assist: %s picks are off in this mode (%s)", pick.kind, pick.topic)
            return None
        if pick.kind == "conversion" and pick.quote and any(_norm(s) in _norm(pick.quote) for s in self.converted):
            log.info("assist: conversion %r already shown instantly, skipped", pick.topic)
            return None
        if pick.quote is not None and not quote_heard(pick.quote, new_lines):
            log.info("assist: quote %r not in what was just heard, skipped (%s)", pick.quote, pick.topic)
            return None
        if self._seen(pick.topic):
            log.info("assist: topic %r repeats a recent one, skipped", pick.topic)
            return None
        if pick.kind not in URGENT_KINDS and self.clock() - self.last_proactive < self.config.proactive_interval_s:
            log.info("assist: volunteered %s %r too soon after the last one, skipped", pick.kind, pick.topic)
            return None
        return pick

    def _seen(self, text: str) -> bool:
        return any(similar(text, old) for old in self.recent)

    async def _busy(self, value: bool) -> bool:
        if self.on_busy is not None:
            await self.on_busy(value)
        return value

    def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
