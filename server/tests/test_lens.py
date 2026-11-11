import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from conversate.lens import Limits, lens_router, parse, prompt

JPEG = b"\xff\xd8\xff\xe0" + b"photo" * 100


def client(gemini, limits=None):
    app = FastAPI()
    app.include_router(lens_router(gemini, limits))
    return TestClient(app)


def test_closed_without_a_key():
    assert client(None).post("/lens/translate", content=JPEG).status_code == 503


def test_photo_goes_to_the_model_and_comes_back_as_paragraphs():
    seen = {}

    async def fake(jpeg, target):
        seen["jpeg"], seen["target"] = jpeg, target
        return {"model": "fake", "paragraphs": [{"source": "Exit", "translated": "출구", "from": "en"}]}

    r = client(fake).post("/lens/translate?target=ko", content=JPEG)
    assert r.status_code == 200, r.text
    assert r.json()["paragraphs"][0]["translated"] == "출구"
    assert seen == {"jpeg": JPEG, "target": "ko"}


def test_rejects_non_jpegs_and_unknown_targets():
    async def fake(jpeg, target):
        raise AssertionError("must not be called")

    c = client(fake)
    assert c.post("/lens/translate", content=b"PNG....").status_code == 400
    assert c.post("/lens/translate", content=b"").status_code == 400
    assert c.post("/lens/translate?target=xx", content=JPEG).status_code == 400


def test_limits_per_address_per_minute_and_per_day():
    now = [1_000_000.0]
    limits = Limits(per_ip_per_minute=2, per_day=3, clock=lambda: now[0])
    assert limits.allow("a") and limits.allow("a")
    assert not limits.allow("a")          # third in the same minute
    assert limits.allow("b")              # another address
    now[0] += 61
    assert not limits.allow("a")          # the day's 3 are used up
    now[0] += 86_400
    assert limits.allow("a")              # next day


def test_too_many_photos_is_429():
    async def fake(jpeg, target):
        return {"model": "fake", "paragraphs": []}

    c = client(fake, Limits(per_ip_per_minute=1, per_day=10))
    assert c.post("/lens/translate", content=JPEG).status_code == 200
    assert c.post("/lens/translate", content=JPEG).status_code == 429


def test_parse_keeps_only_complete_paragraphs():
    text = json.dumps({"paragraphs": [
        {"source": "味噌ラーメン 950円", "translated": "된장 라멘 950엔", "from": "JA"},
        {"source": "", "translated": "x", "from": "en"},
        {"source": "y", "translated": "", "from": "en"},
        "junk",
    ]})
    assert parse(text) == {"paragraphs": [{"source": "味噌ラーメン 950円", "translated": "된장 라멘 950엔", "from": "ja"}]}
    assert parse("not json") == {"paragraphs": []}


def test_prompt_asks_for_korean_and_english_for_korean_text():
    p = prompt("ko")
    assert "into natural Korean" in p and "into English instead" in p
    assert "Never split a sentence across paragraphs" in p


def test_gemini_call_uses_only_the_standard_library(monkeypatch):
    import conversate.lens as lens

    sent = {}

    def fake_post(url, body, timeout=30):
        sent["url"], sent["body"] = url, body
        answer = {"paragraphs": [{"source": "Exit", "translated": "출구", "from": "en"}]}
        return 200, json.dumps({"candidates": [{"content": {"parts": [{"text": json.dumps(answer)}]}}]})

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(lens, "post_json", fake_post)
    r = client(lens.gemini_from_env()).post("/lens/translate?target=ko", content=JPEG)
    assert r.status_code == 200, r.text
    assert r.json()["paragraphs"] == [{"source": "Exit", "translated": "출구", "from": "en"}]
    assert "gemini-3.5-flash-lite:generateContent?key=k" in sent["url"]
    assert sent["body"]["contents"][0]["parts"][0]["inline_data"]["mime_type"] == "image/jpeg"


def test_gemini_errors_become_502_or_429(monkeypatch):
    import conversate.lens as lens

    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setattr(lens, "post_json", lambda url, body, timeout=30: (429, "quota"))
    assert client(lens.gemini_from_env()).post("/lens/translate", content=JPEG).status_code == 429
    monkeypatch.setattr(lens, "post_json", lambda url, body, timeout=30: (500, "boom"))
    assert client(lens.gemini_from_env()).post("/lens/translate", content=JPEG).status_code == 502
