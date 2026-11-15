from fastapi import FastAPI
from fastapi.testclient import TestClient

from conversate import relay
from conversate.relay import RelayStore, relay_router


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def client():
    clock = Clock()
    store = RelayStore(clock=clock)
    app = FastAPI()
    app.include_router(relay_router(store))
    return TestClient(app), store, clock


def bearer(secret):
    return {"Authorization": f"Bearer {secret}"}


def test_full_flow_hands_secrets_over_once():
    c, store, _ = client()
    s = c.post("/relay/new").json()
    assert len(s["code"]) == 6 and s["code"].isdigit() and s["secret"]
    assert c.get(f"/api/relay/{s['code']}/status").json() == {"state": "waiting", "message": ""}

    r = c.post(f"/api/relay/{s['code']}/send", json={"kind": "credentials", "username": " me ", "password": "pw"})
    assert r.status_code == 204
    assert c.get(f"/api/relay/{s['code']}/status").json()["state"] == "logging_in"

    first = c.get("/relay/poll", headers=bearer(s["secret"])).json()
    assert first["messages"] == [{"kind": "credentials", "username": "me", "password": "pw"}]
    # Handed over once, then gone from the server.
    assert c.get("/relay/poll", headers=bearer(s["secret"])).json()["messages"] == []

    assert c.post("/relay/status", headers=bearer(s["secret"]), json={"state": "need_code", "message": "문자로 받은 코드"}).status_code == 200
    assert c.get(f"/api/relay/{s['code']}/status").json() == {"state": "need_code", "message": "문자로 받은 코드"}
    c.post(f"/api/relay/{s['code']}/send", json={"kind": "code", "text": "123456"})
    assert c.get("/relay/poll", headers=bearer(s["secret"])).json()["messages"] == [{"kind": "code", "text": "123456"}]

    c.post("/relay/status", headers=bearer(s["secret"]), json={"state": "done"})
    assert c.get(f"/api/relay/{s['code']}/status").json()["state"] == "done"
    assert c.post(f"/api/relay/{s['code']}/send", json={"kind": "code", "text": "1"}).status_code == 409


def test_glasses_need_their_secret():
    c, _, _ = client()
    c.post("/relay/new")
    assert c.get("/relay/poll").status_code == 401
    assert c.get("/relay/poll", headers=bearer("guess")).status_code == 401
    assert c.post("/relay/status", headers=bearer("guess"), json={"state": "done"}).status_code == 401


def test_bad_input_is_refused():
    c, _, _ = client()
    s = c.post("/relay/new").json()
    assert c.post(f"/api/relay/{s['code']}/send", json={"kind": "credentials", "username": "me"}).status_code == 400
    assert c.post(f"/api/relay/{s['code']}/send", json={"kind": "other"}).status_code == 400
    assert c.post("/relay/status", headers=bearer(s["secret"]), json={"state": "hacked"}).status_code == 400


def test_sessions_expire_and_finish():
    c, _, clock = client()
    s = c.post("/relay/new").json()
    clock.now += relay.SESSION_TTL + 1
    assert c.get(f"/api/relay/{s['code']}/status").status_code == 404
    assert c.get("/relay/poll", headers=bearer(s["secret"])).status_code == 401

    s2 = c.post("/relay/new").json()
    c.post("/relay/status", headers=bearer(s2["secret"]), json={"state": "done"})
    clock.now += 31
    assert c.get(f"/api/relay/{s2['code']}/status").status_code == 404


def test_too_many_wrong_codes_from_one_address_must_wait():
    c, _, clock = client()
    s = c.post("/relay/new").json()
    wrong = "000000" if s["code"] != "000000" else "111111"
    for _ in range(relay.MAX_MISSES):
        assert c.get(f"/api/relay/{wrong}/status").status_code == 404
    # Even the right code is refused for a while from that address.
    assert c.get(f"/api/relay/{s['code']}/status").status_code == 429
    clock.now += relay.MISS_WINDOW + 1
    fresh = c.post("/relay/new").json()
    assert c.get(f"/api/relay/{fresh['code']}/status").status_code == 200


def test_page_is_served_and_has_no_secrets():
    c, _, _ = client()
    r = c.get("/login/123456")
    assert r.status_code == 200 and "안경 로그인" in r.text
    assert c.get("/login").status_code == 200


def test_values_are_never_logged(caplog):
    c, _, _ = client()
    s = c.post("/relay/new").json()
    with caplog.at_level("INFO"):
        c.post(f"/api/relay/{s['code']}/send", json={"kind": "credentials", "username": "someone", "password": "hunter2"})
        c.get("/relay/poll", headers=bearer(s["secret"]))
    assert "hunter2" not in caplog.text and "someone" not in caplog.text and s["secret"] not in caplog.text


JPEG = b"\xff\xd8\xff\xe0" + b"0" * 100


def test_captcha_mirror_screen_and_inputs():
    c, store, _ = client()
    s = c.post("/relay/new").json()
    code, auth = s["code"], bearer(s["secret"])
    # Not mirroring yet: the glasses cannot push a picture and the phone gets nothing.
    assert c.post("/relay/screen", headers=auth, content=JPEG).status_code == 409
    assert c.get(f"/api/relay/{code}/screen").status_code == 204

    c.post("/relay/status", headers=auth, json={"state": "captcha", "message": "로봇 확인"})
    assert c.post("/relay/screen", headers=auth, content=b"not a jpeg").status_code == 400
    assert c.post("/relay/screen", content=JPEG).status_code == 401
    assert c.post("/relay/screen", headers=auth, content=JPEG).json() == {"seq": 1}
    r = c.get(f"/api/relay/{code}/screen?after=0")
    assert r.status_code == 200 and r.content == JPEG and r.headers["X-Seq"] == "1" and r.headers["content-type"] == "image/jpeg"
    assert c.get(f"/api/relay/{code}/screen?after=1").status_code == 204  # nothing newer

    # Phone inputs are passed on as they are (and do not change the state).
    assert c.post(f"/api/relay/{code}/send", json={"kind": "tap", "x": 0.25, "y": 1}).status_code == 204
    assert c.post(f"/api/relay/{code}/send", json={"kind": "scroll", "dy": -5}).status_code == 204
    assert c.post(f"/api/relay/{code}/send", json={"kind": "type", "text": "ab c"}).status_code == 204
    assert c.post(f"/api/relay/{code}/send", json={"kind": "tap", "x": 1.5, "y": 0}).status_code == 400
    assert c.post(f"/api/relay/{code}/send", json={"kind": "tap", "x": "nan", "y": 0}).status_code == 400
    assert c.get(f"/api/relay/{code}/status").json()["state"] == "captcha"
    assert c.get("/relay/poll", headers=auth).json()["messages"] == [
        {"kind": "tap", "x": 0.25, "y": 1.0}, {"kind": "scroll", "dy": -1.0}, {"kind": "type", "text": "ab c"},
    ]

    # Leaving the mirror (or finishing) drops the picture.
    c.post("/relay/status", headers=auth, json={"state": "logging_in"})
    assert c.get(f"/api/relay/{code}/screen").status_code == 204


def test_page_shows_the_mirror():
    c, _, _ = client()
    html = c.get("/login").text
    assert 'id="screen"' in html and "kind: 'tap'" in html
