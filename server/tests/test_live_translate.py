import asyncio
import json

import numpy as np

from conversate.live_translate import KoreanJoiner, LiveTranslator, resample_16k_to_24k


def test_resample_length_and_shape():
    x = (np.sin(np.arange(1600) / 10) * 1000).astype("<i2").tobytes()
    y = np.frombuffer(resample_16k_to_24k(x), dtype="<i2")
    assert len(y) == 2400
    assert abs(int(y.max()) - 1000) < 20


def test_joiner_restores_space_after_pause():
    j = KoreanJoiner()
    pieces = [j.add("써봤", 0.0), j.add("는데요", 0.05), j.add("제가", 0.6), j.add(" 여러분", 0.65),
              j.add("?", 1.5), j.add("네", 2.5)]
    assert "".join(pieces) == "써봤는데요 제가 여러분? 네"


class FakeWs:
    def __init__(self, incoming):
        self.sent = []
        self.incoming = incoming

    async def send(self, msg):
        self.sent.append(json.loads(msg))

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for m in self.incoming:
            yield json.dumps(m)

    async def close(self):
        pass


def test_session_flow():
    async def main():
        pieces = []

        async def on_korean(piece):
            pieces.append(piece)

        ws = FakeWs([
            {"type": "session.input_transcript.delta", "delta": "Hello"},
            {"type": "session.output_transcript.delta", "delta": "안녕"},
            {"type": "session.output_transcript.delta", "delta": "하세요."},
            {"type": "session.closed"},
        ])

        async def connect():
            return ws

        lt = LiveTranslator(on_korean, connect=connect)
        await lt.start()
        await lt.feed(np.zeros(1600, dtype="<i2").tobytes())
        await asyncio.wait_for(lt.closed.wait(), 1)
        await lt.stop()
        kinds = [m["type"] for m in ws.sent]
        assert kinds[0] == "session.update"
        assert ws.sent[0]["session"]["audio"]["output"]["language"] == "ko"
        assert "session.input_audio_buffer.append" in kinds
        assert "".join(pieces) == "안녕하세요."

    asyncio.run(main())
