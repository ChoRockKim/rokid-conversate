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
        ws.send_text(json.dumps({"type": "hello", "levels": True}))
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
    utterance (original from our STT, translation as it streams) and kept when it is final."""
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
        seen = []
        live = read_until(ws, lambda m: seen.append(m) or (
            m["type"] == "caption" and m["final"] and m.get("translation") == "이거 얼마예요?"))
        assert live["id"] > first["id"] and live["text"] == "How much is this?"
        # The live translation stays (no swap to the sentence translation: the text jumped).
        assert not any(m.get("translation") == "이것은 얼마입니까?" and m["id"] == live["id"] for m in seen)
    assert len(opened) == 1


def test_foreign_partial_opens_live_early_and_replays_audio():
    """A foreign first partial opens live translation before the sentence ends, and the
    utterance so far is replayed into it; a Korean partial does not."""
    opened = []

    class FakeLive:
        def __init__(self, on_korean):
            self.fed = []
            opened.append(self)

        async def start(self):
            pass

        async def feed(self, pcm):
            self.fed.append(len(pcm))

        async def stop(self):
            pass

    class Detecting:
        def __init__(self, text, language):
            self.text, self.language = text, language

        def transcribe(self, audio, *, partial):
            return self.text

        def detect_language(self, audio):
            return self.language

        def partial_in(self, audio, language):
            assert language == self.language
            return self.text

    with client(stt=Detecting("Where is the", "en"), live_factory=FakeLive).websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        ws.receive_text()
        ws.send_bytes(silence(300) + tone(2500))
        read_until(ws, lambda m: m["type"] == "caption" and not m["final"])
        assert len(opened) == 1
        assert opened[0].fed and opened[0].fed[0] >= 16000 * 2  # at least a second replayed

    opened.clear()
    with client(stt=Detecting("어디로 가", "ko"), live_factory=FakeLive).websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        ws.receive_text()
        ws.send_bytes(silence(300) + tone(2500))
        read_until(ws, lambda m: m["type"] == "caption" and not m["final"])
    assert opened == []


class _Ws:
    def __init__(self):
        self.sent = []

    async def send_text(self, t):
        self.sent.append(json.loads(t))


class _Stt:
    def transcribe(self, audio, *, partial):
        return ""


def _session():
    from conversate.main import Session

    return Session(_Ws(), _Stt())


def test_live_pieces_go_to_the_utterance_of_their_audio_time():
    """The end of sentence #3's translation arrives while #4 is being spoken: by its audio time
    it still goes under #3 (by arrival order it used to start #4's line)."""
    import asyncio

    async def main():
        s = _session()
        s.live = object()
        s.segmenter._next_id = 4
        s.last_final_id = 3
        s.live_bounds = {3: 4000.0}
        s.final_text[3] = "I'm from Indonesia."
        s.partial_text[4] = "Sending greetings"
        s.partial_language[4] = "en"
        await s._on_live_korean("인도네시아에서 왔어요.", 3600)
        await s._on_live_korean("모두에게", 4800)
        sent = s.ws.sent
        assert sent[0] == {"type": "caption", "id": 3, "text": "I'm from Indonesia.", "final": True,
                           "translation": "인도네시아에서 왔어요."}
        assert sent[1] == {"type": "caption", "id": 4, "text": "Sending greetings", "final": False,
                           "translation": "모두에게"}

    asyncio.run(main())


def test_foreign_translation_shows_before_its_first_partial_text():
    """Language known (foreign) but the partial not transcribed yet: the translation goes out
    with an empty original. Unknown or Korean: held back."""
    import asyncio

    async def main():
        s = _session()
        s.live = object()
        s.segmenter._next_id = 4
        s.last_final_id = 3
        await s._on_live_korean("오늘은")          # language of #4 not known yet
        assert s.ws.sent == []
        s.partial_language[4] = "ja"
        await s._on_live_korean(" 평소보다")
        assert s.ws.sent[-1] == {"type": "caption", "id": 4, "text": "", "final": False,
                                 "translation": "오늘은 평소보다"}

    asyncio.run(main())


def test_korean_speech_written_as_foreign_is_transcribed_again():
    import asyncio
    import numpy as np
    from conversate.main import Session
    from conversate.segmenter import Event

    class Stt:
        def __init__(self, language):
            self.language = language

        def transcribe(self, audio, *, partial):
            return ""

        def detect_language(self, audio):
            return self.language

        def transcribe_korean(self, audio):
            return "오시리가 보고 싶어"

    event = Event("final", np.zeros(16000, dtype=np.float32), 5)

    async def main():
        s = Session(_Ws(), Stt("ko"))
        assert await s._check_foreign(event, "お尻が見たいよ。") == "오시리가 보고 싶어"
        s = Session(_Ws(), Stt("ja"))
        assert await s._check_foreign(event, "とてもいい。") == "とてもいい。"
        assert await s._check_foreign(event, "어제 산 운동화") == "어제 산 운동화"

    asyncio.run(main())


def test_sentence_translation_replaces_a_shifted_live_one_only():
    import asyncio

    class Tr:
        def __init__(self, korean):
            self.korean = korean

        def translate(self, text):
            return self.korean

    async def main():
        s = _session()
        s.translator = Tr("실례합니다, 가장 가까운 지하철역이 어디인가요?")
        s.final_text[2] = "Excuse me, where is the nearest subway station?"
        s.live_texts[2] = "실례합니다, 제일 가까운 지하철역이 어디예요?"
        await s._translate(2, s.final_text[2])
        assert s.ws.sent == []  # same meaning: the live one stays

        s.translator = Tr("모두에게 인사를 전합니다.")
        s.final_text[3] = "Sending greetings to everyone."
        s.live_texts[3] = "인도네시아에서 왔고 이 팟캐스트 보면서"
        await s._translate(3, s.final_text[3])
        assert s.ws.sent[-1]["translation"] == "모두에게 인사를 전합니다."
        assert 3 not in s.final_text  # late pieces no longer overwrite it

    asyncio.run(main())


def test_live_pieces_follow_the_logged_production_timing():
    """Production, 2026-11-08 (English clip): sentence #1 was cut around 4100 ms of session
    audio, its translation's last words came at 4200-4600 ms, the next sentence's at 8200 ms.
    "역이 어디인가요?" used to land under #2."""
    import asyncio

    async def main():
        s = _session()
        s.live = object()
        s.segmenter._next_id = 2
        s.last_final_id = 1
        s.live_bounds = {1: 4100.0}
        s.final_text[1] = "Excuse me, where is the nearest subway station?"
        s.partial_language[2] = "en"
        for piece, at in [("실", 1000), ("례", 1000), ("합니다", 1200), (",", 1600), (" 가", 2400),
                          ("장", 2600), (" 가까", 3000), ("운", 3200), (" 지", 3400), ("하", 3400),
                          ("철", 3600), ("역", 3800), ("이", 4000), ("인가", 4200), (" 어디", 4200),
                          ("요", 4600), ("?", 4600), (" 저", 8200), ("는", 8400), (" 근", 8800)]:
            await s._on_live_korean(piece, at)
        assert s.live_texts[1] == "실례합니다, 가장 가까운 지하철역이인가 어디요?"
        assert s.live_texts[2] == "저는 근"

    asyncio.run(main())


def test_no_level_messages_unless_asked():
    """Battery: the glasses do not ask for levels, so none are sent."""
    with client(FakeTranscriber()).websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello"}))
        assert json.loads(ws.receive_text())["type"] == "ready"
        audio = silence(500) + tone(1000) + silence(1000)
        for i in range(0, len(audio), 3200):
            ws.send_bytes(audio[i : i + 3200])
        seen = []
        read_until(ws, lambda m: seen.append(m["type"]) or (m["type"] == "caption" and m["final"]))
        assert "level" not in seen


def test_a_two_sentence_line_keeps_its_second_sentence():
    """Production, 2026-11-08 (Japanese): "…元気ですね。ちょっと走ってきました。" was closed after
    the first translated sentence, and "조금 뛰고 왔어요" went to the next line."""
    import asyncio

    async def main():
        s = _session()
        s.live = object()
        s.segmenter._next_id = 2
        s.last_final_id = 1
        s.live_bounds = {1: 5800.0}
        s.final_text[1] = "今日はいつもより元気ですね。ちょっと走ってきました。"
        for piece, at in [("오늘은", 3000), (" 평소보다", 3600), (" 힘차네요", 4400), (".", 4600),
                          (" 조금", 5800), (" 뛰고", 6000), (" 왔어요", 6400), (".", 6800), (" 어제", 12000)]:
            await s._on_live_korean(piece, at)
        assert s.live_texts[1] == "오늘은 평소보다 힘차네요. 조금 뛰고 왔어요."
        assert s.live_texts[2] == "어제"

    asyncio.run(main())


def test_mode_changes_how_sound_is_cut_and_the_ai():
    import asyncio
    from conversate.assist import AssistEngine, Pick

    class Ai:
        def triage(self, transcript, shown):
            return None

    async def main():
        s = _session()
        s.assist = AssistEngine(Ai(), lambda a: None)
        assert s.mode.name == "daily"
        s.segmenter._next_id = 7
        s._apply_mode("lecture")
        assert s.segmenter.config.end_silence_ms == 900
        assert s.segmenter._next_id == 7  # caption ids keep counting up
        assert s.assist.config.proactive_interval_s == 30.0
        assert s.assist._vet(Pick("t", kind="suggest", text="확인할 점"), []) is None
        s._apply_mode("media")
        assert s.segmenter.config.max_segment_ms == 8_000 and s.segmenter.config.vad_mode == 0
        assert not s.mode.assist
        s._apply_mode("nonsense")  # unknown: back to daily
        assert s.mode.name == "daily" and s.segmenter.config.end_silence_ms == 700
        assert s.assist.config.proactive_interval_s == 25.0

    asyncio.run(main())


def test_hello_carries_the_mode():
    with client().websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello", "mode": "media"}))
        assert json.loads(ws.receive_text())["type"] == "ready"
        ws.send_text(json.dumps({"type": "mode", "mode": "lecture"}))
        ws.send_text(json.dumps({"type": "stop"}))  # no reply to mode: no error either


def test_opus_audio_is_decoded_like_pcm():
    """The glasses send Opus (battery); the server decodes it into the usual PCM."""
    import av
    import numpy as np
    from conversate.opus import frame_packets

    tone_pcm = np.frombuffer(silence(500) + tone(1200) + silence(1200), dtype="<i2")
    enc = av.CodecContext.create("libopus", "w")
    enc.sample_rate, enc.layout, enc.format, enc.bit_rate = 16000, "mono", "s16", 32000
    enc.open()
    messages = []
    for i in range(0, len(tone_pcm) - 1600, 1600):  # 100 ms per message, 5 packets each
        packets = []
        for j in range(i, i + 1600, 320):
            frame = av.AudioFrame.from_ndarray(tone_pcm[j:j + 320].reshape(1, -1), format="s16", layout="mono")
            frame.sample_rate, frame.pts = 16000, j
            packets += [bytes(p) for p in enc.encode(frame)]
        messages.append(frame_packets(packets))
    stt = FakeTranscriber()
    with client(stt).websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello", "format": "opus", "levels": True}))
        assert json.loads(ws.receive_text())["type"] == "ready"
        for m in messages:
            ws.send_bytes(m)
        caption = read_until(ws, lambda m: m["type"] == "caption" and m["final"])
        assert caption["text"].startswith("완성")
    assert sum(len(m) for m in messages) < len(tone_pcm) * 2 / 4  # well under PCM size


def test_hallucinated_outro_lines_are_dropped_but_real_talk_is_kept():
    from conversate.main import is_noise_text

    for made_up in ["구독과 좋아요는 저에게 큰 힘이 됩니다.", "시청해 주셔서 감사합니다.", "MBC 뉴스 김철수입니다.",
                    "좋아요와 구독 부탁드려요", "ご視聴ありがとうございました", "Thank you for watching!", "음..."]:
        assert is_noise_text(made_up), made_up
    for real in ["감사합니다", "오늘 구독료 얘기 좀 하자", "유튜브 구독자가 많네", "이 영상 좋아요 많이 받았네"]:
        assert not is_noise_text(real), real


def test_korean_captions_off_sends_only_foreign_speech():
    """Setting on the glasses: Korean speech is still transcribed (AI, history) but not sent;
    foreign speech and its translation are."""

    class Mixed:
        def __init__(self):
            self.n = 0

        def transcribe(self, audio, *, partial):
            if partial:
                return "부분"
            self.n += 1
            return "어제 산 운동화 이야기" if self.n == 1 else "Where is the station?"

    class Tr:
        def translate(self, text):
            return "역이 어디예요?"

    with client(stt=Mixed(), translator=Tr()).websocket_connect("/ws", headers=AUTH) as ws:
        ws.send_text(json.dumps({"type": "hello", "korean_captions": False}))
        assert json.loads(ws.receive_text())["type"] == "ready"
        for _ in range(2):
            ws.send_bytes(silence(500) + tone(1000) + silence(1000))
            ws.send_text(json.dumps({"type": "stop"}))
        seen = []
        read_until(ws, lambda m: seen.append(m) or (m["type"] == "caption" and m.get("translation") == "역이 어디예요?"))
        texts = [m["text"] for m in seen if m["type"] == "caption"]
        assert "어제 산 운동화 이야기" not in texts and "부분" not in texts
        assert "Where is the station?" in texts


def test_opening_live_translation_keeps_the_session_settings():
    """_start_live used to reset korean_captions, the Opus decoder and more (stray lines)."""
    import asyncio

    class Live:
        def __init__(self, on_korean):
            pass

        async def start(self):
            pass

        async def feed(self, pcm):
            pass

        async def stop(self):
            pass

    async def main():
        s = _session()
        s.live_factory = Live
        s.korean_captions = False
        s.opus = "decoder"
        await s._start_live()
        assert s.live is not None
        assert s.korean_captions is False and s.opus == "decoder"

    asyncio.run(main())


def test_live_translation_opened_on_a_korean_partial_closes_at_its_final():
    import asyncio
    import numpy as np
    from conversate.segmenter import Event

    stopped = []

    class Live:
        async def stop(self):
            stopped.append(True)

    async def main():
        s = _session()
        s.live = Live()
        s.live_opened_by, s.live_confirmed = 4, False
        await s._deliver(Event("final", np.zeros(1600, dtype=np.float32), 4), "어제 산 운동화 이야기", final=True)
        assert stopped and s.live is None
        # A confirmed session (a foreign final came) stays open on Korean finals.
        s.live, s.live_opened_by, s.live_confirmed = Live(), 5, True
        await s._deliver(Event("final", np.zeros(1600, dtype=np.float32), 6), "응 맞아", final=True)
        assert s.live is not None

    asyncio.run(main())
