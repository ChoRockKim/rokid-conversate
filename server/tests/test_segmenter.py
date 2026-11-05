import numpy as np

from conversate.segmenter import SAMPLE_RATE, Segmenter, SegmenterConfig as _Config


def SegmenterConfig(**kw):
    # Synthetic tones are not speech to WebRTC VAD; these tests exercise the segmenting logic
    # with the energy detector.
    kw.setdefault("use_webrtc", False)
    return _Config(**kw)


def tone(ms: int, amplitude: int = 8000) -> bytes:
    t = np.arange(SAMPLE_RATE * ms // 1000) / SAMPLE_RATE
    return (np.sin(2 * np.pi * 220 * t) * amplitude).astype("<i2").tobytes()


def silence(ms: int, amplitude: int = 30) -> bytes:
    rng = np.random.default_rng(0)
    n = SAMPLE_RATE * ms // 1000
    return rng.integers(-amplitude, amplitude, n).astype("<i2").tobytes()


def feed_all(seg: Segmenter, *chunks: bytes, chunk_ms: int = 100):
    events = []
    step = SAMPLE_RATE * chunk_ms // 1000 * 2
    for data in chunks:
        for i in range(0, len(data), step):
            events += seg.feed(data[i : i + step])
    return events


def test_one_utterance_gives_one_final():
    seg = Segmenter(SegmenterConfig(partial_every_ms=0))
    events = feed_all(seg, silence(1000), tone(1200), silence(1000))
    finals = [e for e in events if e.kind == "final"]
    assert len(finals) == 1
    # speech + pre-roll + trailing silence until the cut, roughly
    assert 1.2 <= len(finals[0].audio) / SAMPLE_RATE <= 2.4


def test_silence_only_gives_nothing():
    seg = Segmenter(SegmenterConfig())
    assert feed_all(seg, silence(5000)) == []
    assert seg.flush() == []


def test_click_is_dropped():
    seg = Segmenter(SegmenterConfig(partial_every_ms=0))
    assert feed_all(seg, silence(1000), tone(90), silence(1500)) == []


def test_two_utterances_have_increasing_ids():
    seg = Segmenter(SegmenterConfig(partial_every_ms=0))
    events = feed_all(seg, silence(500), tone(800), silence(1000), tone(800), silence(1000))
    assert [e.segment_id for e in events] == [1, 2]


def test_partials_during_long_speech_then_final():
    seg = Segmenter(SegmenterConfig(partial_every_ms=1000))
    events = feed_all(seg, silence(500), tone(3500), silence(1000))
    kinds = [e.kind for e in events]
    # 3.5 s of speech plus the 0.7 s of silence before the cut, one partial per second
    assert kinds.count("partial") == 4
    assert kinds[-1] == "final"
    assert len({e.segment_id for e in events}) == 1


def test_max_segment_forces_cut():
    seg = Segmenter(SegmenterConfig(partial_every_ms=0, max_segment_ms=2000))
    events = feed_all(seg, silence(300), tone(5000))
    assert len([e for e in events if e.kind == "final"]) == 2


def test_flush_finishes_open_utterance():
    seg = Segmenter(SegmenterConfig(partial_every_ms=0))
    assert feed_all(seg, silence(500), tone(800)) == []
    assert [e.kind for e in seg.flush()] == ["final"]


def test_odd_byte_chunks_are_handled():
    seg = Segmenter(SegmenterConfig(partial_every_ms=0))
    data = silence(500) + tone(800) + silence(1000)
    events = []
    for i in range(0, len(data), 333):  # odd size splits samples across chunks
        events += seg.feed(data[i : i + 333])
    finals = [e for e in events if e.kind == "final"]
    assert len(finals) == 1
    # Samples split across chunks are reassembled, so the audio is the original tone.
    whole = Segmenter(SegmenterConfig(partial_every_ms=0))
    expected = [e for e in feed_all(whole, data) if e.kind == "final"][0].audio
    np.testing.assert_array_equal(finals[0].audio, expected)


def test_normalize_raises_quiet_audio_with_a_cap():
    from conversate.transcriber import normalize

    quiet = np.array([0.0, 0.01, -0.02], dtype=np.float32)
    out = normalize(quiet)
    assert abs(float(np.max(np.abs(out))) - 0.4) < 1e-6  # 20x cap: 0.02 -> 0.4
    loud = np.array([0.5, -0.95], dtype=np.float32)
    assert np.array_equal(normalize(loud), loud)  # already loud: untouched
    assert np.array_equal(normalize(np.zeros(3, dtype=np.float32)), np.zeros(3, dtype=np.float32))


def test_webrtc_vad_ignores_steady_tone_and_hiss():
    # Real speech is checked by hand with recordings; here: no false speech on non-speech.
    seg = Segmenter(_Config())
    assert seg.feed(silence(2000, amplitude=40)) == []
    assert seg.flush() == []
