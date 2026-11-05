import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conversate.main import create_app
from conversate.segmenter import SegmenterConfig
from tests.test_segmenter import silence, tone


class FakeTranscriber:
    def __init__(self):
        self.calls = []

    def transcribe(self, audio: np.ndarray, *, partial: bool) -> str:
        self.calls.append((len(audio), partial))
        return f"{'부분' if partial else '완성'} {len(audio) / 16000:.1f}초"


AUTH = {"Authorization": "Bearer secret"}


def client(stt=None, assistant=None, translator=None, live_factory=None):
    return TestClient(
        create_app(
            transcriber=stt or FakeTranscriber(), token="secret", assistant=assistant,
            # synthetic tones: energy detector (WebRTC VAD is checked with real speech separately)
            segmenter_config=SegmenterConfig(use_webrtc=False),
            translator=translator,
            live_factory=live_factory,
            history=None,
        )
    )


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


def test_assist_message_after_enough_speech():
    from conversate.assist import Assist

    class FakeAssistant:
        def suggest(self, transcript, shown):
            return Assist("요약", f"{len(transcript)}줄")

    class Wordy:
        def transcribe(self, audio, *, partial):
            return "검색 광고 예산을 줄이는 방안을 논의합니다"  # 22 chars

    with client(stt=Wordy(), assistant=FakeAssistant()).websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        ws.receive_text()
        # Two utterances pass the 40-char bar that triggers an ask.
        for _ in range(2):
            ws.send_bytes(silence(300) + tone(600) + silence(900))
        busy = read_until(ws, lambda m: m["type"] == "assist_status")
        assert busy["busy"] is True
        msg = read_until(ws, lambda m: m["type"] == "assist")
        assert msg["title"] == "요약"
        assert msg["text"].endswith("줄")


def test_mic_diagnostics_are_accepted_quietly():
    with client().websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        ws.receive_text()
        ws.send_text(json.dumps({"type": "mic", "source": "MIC", "silent": False}))
        ws.send_text(json.dumps({"type": "hello"}))
        assert json.loads(ws.receive_text()) == {"type": "ready"}  # no error in between


def test_foreign_speech_gets_a_translation():
    class English:
        def transcribe(self, audio, *, partial):
            return "Where is the station?"

    class Ko:
        def translate(self, text):
            return "역이 어디예요?"

    with client(stt=English(), translator=Ko()).websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        ws.receive_text()
        ws.send_bytes(silence(500) + tone(800))
        ws.send_text(json.dumps({"type": "stop"}))
        first = read_until(ws, lambda m: m["type"] == "caption" and m["final"])
        assert first["text"] == "Where is the station?" and "translation" not in first
        second = read_until(ws, lambda m: m["type"] == "caption" and "translation" in m)
        assert second["id"] == first["id"]
        assert second["translation"] == "역이 어디예요?"


def test_foreign_speech_opens_live_translation():
    """After the first foreign final, live Korean is attached to our own captions of the next
    utterance (original from our STT, translation as it streams), then replaced by the text
    translation of the final."""
    opened = []

    class FakeLive:
        def __init__(self, on_korean):
            self.on_korean, self.fed = on_korean, 0
            opened.append(self)

        async def start(self):
            pass

        async def feed(self, pcm):
            self.fed += 1
            if self.fed == 2:
                await self.on_korean("이거")
                await self.on_korean(" 얼마예요?")

        async def stop(self):
            pass

    class English:
        def transcribe(self, audio, *, partial):
            return "How much is this?"

        def transcribe_any_language(self, audio):
            return "How much"

    class Tr:
        def translate(self, text):
            return "이것은 얼마입니까?"

    with client(stt=English(), live_factory=FakeLive, translator=Tr()).websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        ws.receive_text()
        ws.send_bytes(silence(500) + tone(800))
        ws.send_text(json.dumps({"type": "stop"}))
        first = read_until(ws, lambda m: m["type"] == "caption" and m["final"])
        # live session is open now; the next utterance streams into it
        ws.send_bytes(silence(500))
        ws.send_bytes(tone(800))
        ws.send_text(json.dumps({"type": "stop"}))
        live = read_until(ws, lambda m: m["type"] == "caption" and m.get("translation") == "이거 얼마예요?")
        assert live["id"] > first["id"]
        final = read_until(ws, lambda m: m["type"] == "caption" and m["id"] == live["id"] and m["final"]
                           and m.get("translation") == "이것은 얼마입니까?")
        assert final["text"] == "How much is this?"
    assert len(opened) == 1
