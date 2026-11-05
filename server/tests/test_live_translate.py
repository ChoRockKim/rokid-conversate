import asyncio
import json

import numpy as np

from conversate.live_translate import LiveLine, LiveTranslator, resample_16k_to_24k


def test_resample_length_and_shape():
    x = (np.sin(np.arange(1600) / 10) * 1000).astype("<i2").tobytes()
    y = np.frombuffer(resample_16k_to_24k(x), dtype="<i2")
    assert len(y) == 2400
    assert abs(int(y.max()) - 1000) < 20


def test_live_line_closes_on_sentence_end():
    line = LiveLine()
    line.add_source("Where is ")
    line.add_korean("어디")
    assert not line.should_close()
    line.add_source("the station?")
    line.add_korean("역이 어디예요?")
    assert line.should_close()
    assert line.take() == ("Where is the station?", "어디역이 어디예요?")


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
        lines = []

        async def on_line(src, ko, final):
            lines.append((src, ko, final))

        ws = FakeWs([
            {"type": "session.input_transcript.delta", "delta": "Hello"},
            {"type": "session.output_transcript.delta", "delta": "안녕하세요"},
            {"type": "session.input_transcript.delta", "delta": "."},
            {"type": "session.output_transcript.delta", "delta": "."},
            {"type": "session.closed"},
        ])

        async def connect():
            return ws

        lt = LiveTranslator(on_line, connect=connect)
        await lt.start()
        await lt.feed(np.zeros(1600, dtype="<i2").tobytes())
        await asyncio.wait_for(lt.closed.wait(), 1)
        await lt.stop()
        kinds = [m["type"] for m in ws.sent]
        assert kinds[0] == "session.update"
        assert ws.sent[0]["session"]["audio"]["output"]["language"] == "ko"
        assert "session.input_audio_buffer.append" in kinds
        assert lines[-1] == ("Hello.", "안녕하세요.", True)

    asyncio.run(main())
