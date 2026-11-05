from conversate.translate import needs_translation


def test_needs_translation():
    assert needs_translation("Where is the station?")
    assert needs_translation("駅はどこですか")
    assert needs_translation("这个多少钱")
    assert not needs_translation("역이 어디예요?")
    assert not needs_translation("오늘 AI 회의는 3시에")  # mostly Korean with an English word
    assert not needs_translation("OK")  # too short to bother
    assert not needs_translation("123 456")
