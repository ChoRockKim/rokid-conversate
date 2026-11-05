import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conversate.main import create_app
from tests.test_segmenter import silence, tone


class FakeTranscriber:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio: np.ndarray, *, partial: bool) -> str:
        self.calls.append((len(audio), partial))
        return f"{'부분' if partial else '완성'} {len(audio) / 16000:.1f}초"


AUTH = {"Authorization": "Bearer secret"}


def client(stt=None):
    return TestClient(create_app(transcriber=stt or FakeTranscriber(), token="secret"))


def read_until(ws, predicate, limit=200):
    for _ in range(limit):
        msg = json.loads(ws.receive_text())
        if predicate(msg):
            return msg
    raise AssertionError("message not received")


def test_rejects_missing_token():
    with pytest.raises(WebSocketDisconnect) as err:
        with client().websocket_connect("/ws") as ws:
            ws.receive_text()
    assert err.value.code == 1008


def test_rejects_wrong_token():
    with pytest.raises(WebSocketDisconnect):
        with client().websocket_connect("/ws", headers={"Authorization": "Bearer nope"}) as ws:
            ws.receive_text()


def test_hello_then_ready():
    with client().websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello", "sample_rate": 16000, "format": "pcm_s16le"}))
        assert json.loads(ws.receive_text()) == {"type": "ready"}


def test_wrong_sample_rate_is_an_error():
    with client().websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello", "sample_rate": 48000}))
        assert json.loads(ws.receive_text())["type"] == "error"


def test_speech_produces_final_caption_and_levels():
    stt = FakeTranscriber()
    with client(stt).websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        assert json.loads(ws.receive_text())["type"] == "ready"
        audio = silence(500) + tone(1000) + silence(1000)
        step = 3200  # 100 ms
        for i in range(0, len(audio), step):
            ws.send_bytes(audio[i : i + step])
        level = read_until(ws, lambda m: m["type"] == "level")
        assert level["received_ms"] > 0
        caption = read_until(ws, lambda m: m["type"] == "caption" and m["final"])
        assert caption["id"] == 1
        assert caption["text"].startswith("완성")


def test_stop_flushes_open_utterance():
    with client().websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        ws.receive_text()
        ws.send_bytes(silence(500) + tone(800))
        ws.send_text(json.dumps({"type": "stop"}))
        caption = read_until(ws, lambda m: m["type"] == "caption" and m["final"])
        assert caption["id"] == 1


def test_transcriber_failure_keeps_session():
    class Broken:
        def transcribe(self, audio, *, partial):
            raise RuntimeError("boom")

    with client(Broken()).websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        ws.receive_text()
        ws.send_bytes(silence(500) + tone(800))
        ws.send_text(json.dumps({"type": "stop"}))
        err = read_until(ws, lambda m: m["type"] == "error")
        assert "boom" in err["message"]
        ws.send_text(json.dumps({"type": "hello"}))
        assert read_until(ws, lambda m: m["type"] == "ready")
