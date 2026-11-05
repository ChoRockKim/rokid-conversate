import asyncio

from conversate.assist import Assist, AssistConfig, AssistEngine, parse_reply


def test_parse_reply():
    assert parse_reply('{"title":"CPC","text":"클릭당 비용"}') == Assist("CPC", "클릭당 비용")
    assert parse_reply('설명: {"title":"A","text":"B"} 끝') == Assist("A", "B")
    assert parse_reply('{"skip": true}') is None
    assert parse_reply("모르겠습니다") is None
    assert parse_reply('{"title":"A","text":""}') is None
    assert parse_reply("{broken") is None


class Recorder:
    def __init__(self, result=Assist("주제", "내용")):
        self.calls = []
        self.result = result

    def suggest(self, transcript, shown):
        self.calls.append((list(transcript), list(shown)))
        return self.result


def run(coro):
    return asyncio.run(coro)


def test_waits_for_enough_new_speech():
    async def main():
        rec, sent = Recorder(), []

        async def send(a):
            sent.append(a)

        eng = AssistEngine(rec, send, AssistConfig(min_line_chars=1, min_interval_s=0, min_new_chars=20))
        eng.on_final("짧은 말")
        await asyncio.sleep(0.05)
        assert rec.calls == []
        eng.on_final("이번에는 충분히 긴 문장이 들어왔습니다 정말로")
        await asyncio.sleep(0.05)
        assert len(rec.calls) == 1
        assert rec.calls[0][0] == ["[새] 짧은 말", "[새] 이번에는 충분히 긴 문장이 들어왔습니다 정말로"]
        assert sent == [Assist("주제", "내용")]

    run(main())


def test_respects_interval_and_remembers_titles():
    async def main():
        now = [100.0]
        rec, sent = Recorder(), []

        async def send(a):
            sent.append(a)

        eng = AssistEngine(rec, send, AssistConfig(min_line_chars=1, min_interval_s=0.2, min_new_chars=1), clock=lambda: now[0])
        eng.on_final("첫 문장")
        await asyncio.sleep(0.05)
        assert len(rec.calls) == 1
        eng.on_final("두 번째 문장")  # too soon: scheduled after the interval
        await asyncio.sleep(0.05)
        assert len(rec.calls) == 1
        await asyncio.sleep(0.3)
        assert len(rec.calls) == 2
        assert rec.calls[1][1] == ["주제"]  # previous title passed so it is not repeated

    run(main())


def test_skip_and_errors_send_nothing():
    async def main():
        sent = []

        async def send(a):
            sent.append(a)

        class Broken:
            def suggest(self, transcript, shown):
                raise RuntimeError("api down")

        for assistant in (Recorder(result=None), Broken()):
            eng = AssistEngine(assistant, send, AssistConfig(min_line_chars=1, min_interval_s=0, min_new_chars=1))
            eng.on_final("무언가")
            await asyncio.sleep(0.05)
        assert sent == []

    run(main())


def test_retry_after_parsing():
    from conversate.assist import retry_after_seconds

    msg = "Rate limit reached ... Please try again in 22.482s. Visit https://..."
    assert retry_after_seconds(msg, None) == 22.482
    assert retry_after_seconds("try again in 350ms", None) == 0.35
    assert retry_after_seconds(msg, "7") == 7.0
    assert retry_after_seconds("no hint", None) == 60.0


def test_rate_limit_pushes_next_ask_out():
    from conversate.assist import RateLimited

    async def main():
        now = [1000.0]

        class Limited:
            calls = 0

            def suggest(self, transcript, shown):
                Limited.calls += 1
                raise RateLimited(30)

        async def send(a):
            pass

        eng = AssistEngine(Limited(), send, AssistConfig(min_line_chars=1, min_interval_s=15, min_new_chars=1), clock=lambda: now[0])
        eng.on_final("첫 문장")
        await asyncio.sleep(0.05)
        assert Limited.calls == 1
        # Next ask may not happen before the 30 s the API asked for.
        assert eng.cooldown_until == 1030.0

    run(main())


def test_strip_links_and_reply_cleanup():
    from conversate.assist import strip_links

    t = "방탄소년단은 2013년 6월 13일 데뷔했어요. ([hybecorp.com](https://hybecorp.com/attachment/download?x=1))"
    assert strip_links(t) == "방탄소년단은 2013년 6월 13일 데뷔했어요."
    assert strip_links("자세한 건 https://example.com/a?b=c 참고") == "자세한 건 참고"
    assert strip_links("[위키](https://ko.wikipedia.org) 기준 1위") == "기준 1위"
    reply = '{"title":"방탄소년단 데뷔","text":"2013년 6월 13일 데뷔. ([hybecorp.com](https://hybecorp.com/x))"}'
    assert parse_reply(reply) == Assist("방탄소년단 데뷔", "2013년 6월 13일 데뷔.")


def test_question_detection():
    from conversate.assist import looks_like_question

    for q in ["방탄소년단이 언제 데뷔했더라", "잘 들려?", "그거 누가 만들었지", "몇 명이나 와요?", "what is CPC"]:
        assert looks_like_question(q), q
    for s in ["안녕", "그러니까 삼각대는 아반떼 AD의 페이스리프트 모델", "오늘 회의를 시작하겠습니다"]:
        assert not looks_like_question(s), s


def test_question_is_asked_at_once_even_when_short():
    async def main():
        rec, sent = Recorder(), []

        async def send(a):
            sent.append(a)

        eng = AssistEngine(rec, send, AssistConfig(min_line_chars=1, min_interval_s=60, min_new_chars=40, question_interval_s=0))
        eng.on_final("안녕")
        await asyncio.sleep(0.05)
        assert rec.calls == []
        eng.on_final("방탄소년단이 언제 데뷔했더라")  # short, but a question
        await asyncio.sleep(0.05)
        assert len(rec.calls) == 1
        assert rec.calls[0][0][-1] == "[새] 방탄소년단이 언제 데뷔했더라"

    run(main())


def test_question_jumps_a_scheduled_ask():
    async def main():
        now = [0.0]
        rec = Recorder()

        async def send(a):
            pass

        cfg = AssistConfig(min_line_chars=1, min_interval_s=30, min_new_chars=1, question_interval_s=0)
        eng = AssistEngine(rec, send, cfg, clock=lambda: now[0])
        eng.on_final("첫 문장입니다")  # asked at once (never asked before)
        await asyncio.sleep(0.05)
        eng.on_final("두 번째 문장입니다")  # scheduled 30 s out
        await asyncio.sleep(0.05)
        assert len(rec.calls) == 1
        eng.on_final("그게 언제였지?")  # question: cancels the wait, asks now
        await asyncio.sleep(0.05)
        assert len(rec.calls) == 2
        assert rec.calls[1][0][-1] == "[새] 그게 언제였지?"

    run(main())


def test_busy_is_reported_around_the_request():
    async def main():
        events = []

        async def send(a):
            events.append(("assist", a.title))

        async def busy(b):
            events.append(("busy", b))

        eng = AssistEngine(Recorder(), send, AssistConfig(min_line_chars=1, min_interval_s=0, min_new_chars=1), on_busy=busy)
        eng.on_final("무언가")
        await asyncio.sleep(0.05)
        assert events == [("busy", True), ("busy", False), ("assist", "주제")]

    run(main())


class TwoStep:
    def __init__(self, topic):
        self.topic, self.answered = topic, []

    def triage(self, transcript, shown):
        return self.topic

    def answer(self, topic, transcript):
        self.answered.append(topic)
        return Assist("BTS", "2013년 데뷔")


def test_two_step_skip_shows_nothing():
    async def main():
        events = []

        async def send(a):
            events.append("assist")

        async def busy(b):
            events.append(("busy", b))

        a = TwoStep(None)
        eng = AssistEngine(a, send, AssistConfig(min_line_chars=1, min_interval_s=0, min_new_chars=1), on_busy=busy)
        eng.on_final("안녕 잘 들려?")
        await asyncio.sleep(0.05)
        assert events == []  # no "thinking" flash for a skip
        assert a.answered == []

    run(main())


def test_two_step_answer_shows_thinking_then_result():
    async def main():
        events = []

        async def send(a):
            events.append(("assist", a.title))

        async def busy(b):
            events.append(("busy", b))

        a = TwoStep("방탄소년단 데뷔일은?")
        eng = AssistEngine(a, send, AssistConfig(min_line_chars=1, min_interval_s=0, min_new_chars=1), on_busy=busy)
        eng.on_final("방탄소년단이 언제 데뷔했더라")
        await asyncio.sleep(0.05)
        assert events == [("busy", True), ("busy", False), ("assist", "BTS")]
        assert a.answered == ["방탄소년단 데뷔일은?"]

    run(main())


def test_parse_topic():
    from conversate.assist import parse_topic

    assert parse_topic('{"topic": "방탄소년단 데뷔일은?"}') == "방탄소년단 데뷔일은?"
    assert parse_topic('{"skip": true}') is None
    assert parse_topic('{"topic": ""}') is None
    assert parse_topic("음") is None


def test_similar():
    from conversate.assist import similar

    assert similar("방탄소년단 데뷔일은?", "방탄소년단의 데뷔일은?")
    assert similar("방탄소년단 데뷔", "방탄소년단 데뷔일")
    # a short title inside a longer, different one is not a repeat (was wrongly dropped)
    assert not similar("손가락 인식", "근전도와 손가락 인식")
    assert not similar("방탄소년단 데뷔일은?", "CPC 뜻은?")
    assert not similar("", "아무거나")


def test_new_lines_are_marked_and_repeats_are_dropped():
    async def main():
        seen_transcripts, sent = [], []

        class Same:
            def triage(self, transcript, shown):
                seen_transcripts.append(list(transcript))
                return "방탄소년단 데뷔일은?"

            def answer(self, topic, transcript):
                return Assist("방탄소년단 데뷔", "2013년 6월 13일이다.")

        async def send(a):
            sent.append(a.title)

        eng = AssistEngine(Same(), send, AssistConfig(min_line_chars=1, min_interval_s=0, min_new_chars=1))
        eng.on_final("방탄소년단이 언제 데뷔했더라")
        await asyncio.sleep(0.05)
        eng.on_final("아 그렇구나")
        await asyncio.sleep(0.05)
        assert seen_transcripts[0] == ["[새] 방탄소년단이 언제 데뷔했더라"]
        assert seen_transcripts[1] == ["방탄소년단이 언제 데뷔했더라", "[새] 아 그렇구나"]
        assert sent == ["방탄소년단 데뷔"]  # the same topic again is not shown twice

    run(main())


def test_restates_detects_answers_that_add_nothing():
    from conversate.assist import restates

    convo = ["[새] 이 신발 110달러더라", "회의는 3시에 시작해요"]
    assert restates("회의는 3시에 시작합니다.", convo)  # a line of the conversation again
    assert restates("신발 110달러", convo)  # only numbers already said
    assert not restates("110달러는 약 15만 원이다(1달러 약 1,380원).", convo)  # adds the conversion
    assert not restates("방탄소년단은 2013년 6월 13일 데뷔했다.", ["방탄소년단 언제 데뷔했더라"])


def test_parse_triage_and_quote_check():
    from conversate.assist import Pick, parse_triage, quote_heard

    p = parse_triage('{"quote": "이 운동화 110달러였어", "kind": "conversion", "why": "가격 가늠", "topic": "110달러는 원화로?"}')
    assert p == Pick(topic="110달러는 원화로?", kind="conversion", quote="이 운동화 110달러였어", why="가격 가늠")
    assert parse_triage('{"skip": true}') is None
    heard = ["어제 산 이 운동화 110달러였어. 생각보다 좀 비싸더라."]
    assert quote_heard("이 운동화 110달러였어", heard)
    assert quote_heard("이 운동화 110 달러 였어", heard)  # spacing differences are fine
    assert not quote_heard("방탄소년단 데뷔", heard)  # not something just heard


class Picker:
    def __init__(self, picks):
        self.picks, self.answered = list(picks), []

    def triage(self, transcript, shown):
        return self.picks.pop(0) if self.picks else None

    def answer(self, topic, transcript):
        self.answered.append(topic)
        facts = ["근전도는 근육의 전기 신호를 재는 기술이다.", "약 110만 원이다.", "2025년 9월 출시됐다."]
        titles = ["근전도", "원화 환산", "출시일"]
        n = len(self.answered) - 1
        return Assist(titles[n % 3], facts[n % 3])


def test_invented_quote_is_rejected_before_anything_shows():
    from conversate.assist import Pick

    async def main():
        events = []

        async def send(a):
            events.append("assist")

        async def busy(b):
            events.append(("busy", b))

        a = Picker([Pick(topic="BTS 데뷔일은?", kind="fact", quote="방탄소년단 데뷔")])
        eng = AssistEngine(a, send, AssistConfig(min_interval_s=0, min_new_chars=1), on_busy=busy)
        eng.on_final("오늘 점심 뭐 먹을까 고민이네")
        await asyncio.sleep(0.05)
        assert events == [] and a.answered == []

    run(main())


def test_volunteered_facts_are_rationed_but_questions_are_not():
    from conversate.assist import Pick

    async def main():
        now = [0.0]
        sent = []

        async def send(a):
            sent.append(a.title)

        a = Picker([
            Pick(topic="EMG란?", kind="term", quote="근전도 방식이래"),
            Pick(topic="뉴럴 밴드 가격?", kind="fact", quote="뉴럴 밴드도 있대"),
            Pick(topic="800달러는 원화로?", kind="conversion", quote="800달러래"),
        ])
        cfg = AssistConfig(min_interval_s=0, min_new_chars=1, question_interval_s=0, proactive_interval_s=25)
        eng = AssistEngine(a, send, cfg, clock=lambda: now[0])
        for line in ["손가락은 근전도 방식이래", "손목에 차는 뉴럴 밴드도 있대", "가격은 800달러래"]:
            now[0] += 3
            eng.on_final(line)
            await asyncio.sleep(0.05)
        # term shown, the next volunteered fact 3 s later rationed, the conversion still shown
        assert a.answered == ["EMG란?\n(근거가 된 말: \"근전도 방식이래\")", "800달러는 원화로?\n(근거가 된 말: \"800달러래\")"]
        assert len(sent) == 2

    run(main())


def test_very_short_captions_are_not_material():
    async def main():
        rec = Recorder()

        async def send(a):
            pass

        eng = AssistEngine(rec, send, AssistConfig(min_interval_s=0, min_new_chars=1))
        eng.on_final("네")
        eng.on_final("어")
        await asyncio.sleep(0.05)
        assert rec.calls == []

    run(main())


def test_suggestion_is_shown_directly_and_rationed():
    from conversate.assist import Pick

    async def main():
        now = [0.0]
        sent = []

        async def send(a):
            sent.append((a.title, a.text))

        a = Picker([
            Pick(topic="납기", kind="suggest", quote="단가는 45만 원", text="확인할 점: 납기 일정이 아직 안 나옴"),
            Pick(topic="배송", kind="suggest", quote="200대 주문", text="확인할 점: 배송비 부담 주체"),
        ])
        cfg = AssistConfig(min_interval_s=0, min_new_chars=1, proactive_interval_s=25)
        eng = AssistEngine(a, send, cfg, clock=lambda: now[0])
        eng.on_final("단가는 45만 원입니다")
        await asyncio.sleep(0.05)
        now[0] += 5
        eng.on_final("200대 주문하겠습니다")
        await asyncio.sleep(0.05)
        assert sent == [("확인할 점", "납기 일정이 아직 안 나옴")]  # second one too soon
        assert a.answered == []  # no answer/search call for suggestions

    run(main())


def test_instant_conversion_and_llm_does_not_repeat_it():
    from conversate.assist import Pick
    from conversate.convert import Conversion

    async def main():
        sent = []

        async def send(a):
            sent.append(a.text)

        def converter(text):
            return [Conversion("110달러", "110달러 환산", "110달러 ≈ 14만 7,500원 (1달러 1,341원)")] if "110달러" in text else []

        a = Picker([Pick(topic="110달러는 원화로?", kind="conversion", quote="운동화 110달러였어")])
        eng = AssistEngine(a, send, AssistConfig(min_interval_s=0, min_new_chars=1), converter=converter)
        eng.on_final("어제 산 운동화 110달러였어")
        await asyncio.sleep(0.05)
        assert sent == ["110달러 ≈ 14만 7,500원 (1달러 1,341원)"]
        assert a.answered == []  # the LLM's own conversion was skipped as already shown

    run(main())
