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
        assert eng.last_ask + eng.config.min_interval_s == 1030.0

    run(main())
