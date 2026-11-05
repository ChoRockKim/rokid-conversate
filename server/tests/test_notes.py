from fastapi import FastAPI
from fastapi.testclient import TestClient

from conversate.assist import OpenAIAssistant
from conversate.notes import NotesStore, notes_router

AUTH = {"Authorization": "Bearer glass"}


def client(tmp_path):
    app = FastAPI()
    store = NotesStore(tmp_path / "notes.json")
    app.include_router(notes_router(store, token="glass"))
    return TestClient(app), store


def test_page_is_served_without_token(tmp_path):
    c, _ = client(tmp_path)
    r = c.get("/notes")
    assert r.status_code == 200 and "사전 노트" in r.text


def test_save_and_read(tmp_path):
    c, store = client(tmp_path)
    assert c.get("/api/notes", headers=AUTH).json()["text"] == ""
    r = c.put("/api/notes", headers=AUTH, json={"text": "단가 45만 원, MOQ 200대"})
    assert r.status_code == 200 and r.json()["updatedAt"]
    assert c.get("/api/notes", headers=AUTH).json()["text"] == "단가 45만 원, MOQ 200대"
    assert store.text() == "단가 45만 원, MOQ 200대"


def test_token_required_and_limits(tmp_path):
    c, _ = client(tmp_path)
    assert c.get("/api/notes").status_code == 401
    assert c.put("/api/notes", json={"text": "x"}).status_code == 401
    assert c.put("/api/notes", headers=AUTH, json={"nope": 1}).status_code == 400
    assert c.put("/api/notes", headers=AUTH, json={"text": "가" * 9000}).status_code == 413


def test_notes_go_into_the_prompt(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    a = OpenAIAssistant(notes=lambda: "오늘 김 팀장 미팅, 단가 45만 원")
    prompt = a._conversation(["[새] 가격이 얼마였죠?"], [])
    assert prompt.startswith("사전 노트")
    assert "단가 45만 원" in prompt and "[새] 가격이 얼마였죠?" in prompt
    assert OpenAIAssistant()._conversation(["안녕"], []).startswith("대화:")
