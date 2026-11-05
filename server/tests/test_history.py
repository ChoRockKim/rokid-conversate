import asyncio

from fastapi import FastAPI
from fastapi.testclient import TestClient

from conversate.history import ConversationLog, history_router


class FakeSummarizer:
    def summarize(self, lines, notes):
        return {"title": "납품 미팅", "summary": [f"{len(lines)}줄 대화"], "todos": ["견적서 보내기"]}


def run(coro):
    return asyncio.run(coro)


def test_conversation_splits_on_idle_and_is_summarized(tmp_path):
    async def main():
        now = [0.0]
        log = ConversationLog(tmp_path, FakeSummarizer(), idle_s=180, clock=lambda: now[0])
        await log.add_line("단가는 45만 원입니다")
        now[0] += 60
        await log.add_line("Where is it?", "어디예요?")
        await log.add_card("환산", "약 14만 원이다.")
        first_id = log.current["id"]
        now[0] += 200  # quiet for more than 3 minutes
        await log.tick()
        await asyncio.sleep(0.05)  # summary task
        conv = log.get(first_id)
        assert conv["endedAt"] and conv["summary"]["title"] == "납품 미팅"
        assert conv["summary"]["todos"] == ["견적서 보내기"]
        assert [l["text"] for l in conv["lines"]] == ["단가는 45만 원입니다", "Where is it?"]
        assert conv["lines"][1]["translation"] == "어디예요?"
        assert conv["cards"][0]["title"] == "환산"
        assert log.current is None

    run(main())


def test_reconnects_within_idle_stay_in_one_conversation(tmp_path):
    async def main():
        now = [0.0]
        log = ConversationLog(tmp_path, None, idle_s=180, clock=lambda: now[0])
        await log.add_line("하나")
        now[0] += 100
        await log.add_line("둘")
        assert len(log.current["lines"]) == 2
        assert len(log.list()) == 1

    run(main())


def test_translation_attaches_to_its_line(tmp_path):
    async def main():
        log = ConversationLog(tmp_path, None)
        await log.add_line("How much is this?")
        await log.add_translation("How much is this?", "이거 얼마예요?")
        assert log.current["lines"][0]["translation"] == "이거 얼마예요?"

    run(main())


def test_api_requires_token_and_lists(tmp_path):
    async def seed():
        log = ConversationLog(tmp_path, None)
        await log.add_line("안녕하세요")
        return log

    log = run(seed())
    app = FastAPI()
    app.include_router(history_router(log, token="glass"))
    c = TestClient(app)
    assert c.get("/history").status_code == 200
    assert c.get("/api/conversations").status_code == 401
    items = c.get("/api/conversations", headers={"Authorization": "Bearer glass"}).json()
    assert len(items) == 1 and items[0]["title"] == "안녕하세요"
    one = c.get(f"/api/conversations/{items[0]['id']}", headers={"Authorization": "Bearer glass"}).json()
    assert one["lines"][0]["text"] == "안녕하세요"
    assert c.get("/api/conversations/../../etc", headers={"Authorization": "Bearer glass"}).status_code == 404
