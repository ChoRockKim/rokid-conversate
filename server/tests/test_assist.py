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

        eng = AssistEngine(rec, send, AssistConfig(min_interval_s=0, min_new_chars=20))
        eng.on_final("짧은 말")
        await asyncio.sleep(0.05)
        assert rec.calls == []
        eng.on_final("이번에는 충분히 긴 문장이 들어왔습니다 정말로")
        await asyncio.sleep(0.05)
        assert len(rec.calls) == 1
        assert rec.calls[0][0] == ["짧은 말", "이번에는 충분히 긴 문장이 들어왔습니다 정말로"]
        assert sent == [Assist("주제", "내용")]

    run(main())


def test_respects_interval_and_remembers_titles():
    async def main():
        now = [100.0]
        rec, sent = Recorder(), []

        async def send(a):
            sent.append(a)

        eng = AssistEngine(rec, send, AssistConfig(min_interval_s=0.2, min_new_chars=1), clock=lambda: now[0])
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
            eng = AssistEngine(assistant, send, AssistConfig(min_interval_s=0, min_new_chars=1))
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

        eng = AssistEngine(Limited(), send, AssistConfig(min_interval_s=15, min_new_chars=1), clock=lambda: now[0])
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

        eng = AssistEngine(rec, send, AssistConfig(min_interval_s=60, min_new_chars=40, question_interval_s=0))
        eng.on_final("안녕")
        await asyncio.sleep(0.05)
        assert rec.calls == []
        eng.on_final("방탄소년단이 언제 데뷔했더라")  # short, but a question
        await asyncio.sleep(0.05)
        assert len(rec.calls) == 1
        assert rec.calls[0][0][-1] == "방탄소년단이 언제 데뷔했더라"

    run(main())


def test_question_jumps_a_scheduled_ask():
    async def main():
        now = [0.0]
        rec = Recorder()

        async def send(a):
            pass

        cfg = AssistConfig(min_interval_s=30, min_new_chars=1, question_interval_s=0)
        eng = AssistEngine(rec, send, cfg, clock=lambda: now[0])
        eng.on_final("첫 문장입니다")  # asked at once (never asked before)
        await asyncio.sleep(0.05)
        eng.on_final("두 번째 문장입니다")  # scheduled 30 s out
        await asyncio.sleep(0.05)
        assert len(rec.calls) == 1
        eng.on_final("그게 언제였지?")  # question: cancels the wait, asks now
        await asyncio.sleep(0.05)
        assert len(rec.calls) == 2
        assert rec.calls[1][0][-1] == "그게 언제였지?"

    run(main())


def test_busy_is_reported_around_the_request():
    async def main():
        events = []

        async def send(a):
            events.append(("assist", a.title))

        async def busy(b):
            events.append(("busy", b))

        eng = AssistEngine(Recorder(), send, AssistConfig(min_interval_s=0, min_new_chars=1), on_busy=busy)
        eng.on_final("무언가")
        await asyncio.sleep(0.05)
        assert events == [("busy", True), ("busy", False), ("assist", "주제")]

    run(main())
