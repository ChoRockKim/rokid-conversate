from conversate.convert import Rates, find, won

FIXED = {"USD": 1341.0, "JPY": 8.47, "EUR": 1506.0, "CNY": 188.0}


def rates():
    return Rates(fetch=lambda url: {"rates": {"KRW": FIXED[url.split("latest/")[1].split("?")[0] if "latest/" in url else "USD"]}})


def texts(sentence):
    return [c.text for c in find(sentence, rates())]


def test_won_format():
    assert won(147_510) == "14만 7,500원"
    assert won(1_071_459) == "107만 1,500원"
    assert won(150_000) == "15만 원"
    assert won(9_870) == "9,870원"
    assert won(1_234_567_890) == "12억 3,456만 원"


def test_dollars_in_korean_speech():
    assert texts("어제 산 이 운동화 110달러였어") == ["110달러 ≈ 14만 7,500원 (1달러 1,341원)"]
    assert texts("총 1,169달러에 배송비 207달러") == [
        "1,169달러 ≈ 156만 7,600원 (1달러 1,341원)",
        "207달러 ≈ 27만 7,600원 (1달러 1,341원)",
    ]
    assert texts("이거 3.5달러야") == ["3.5달러 ≈ 4,690원 (1달러 1,341원)"]
    assert texts("백만 달러는 아니고 100만 달러") == ["100만 달러 ≈ 13억 4,100만 원 (1달러 1,341원)"]


def test_other_currencies_and_symbols():
    assert texts("호텔이 5천 엔이래") == ["5천 엔 ≈ 4만 2,400원 (1엔 8.47원)"]
    assert texts("It costs $799") == ["$799 ≈ 107만 1,500원 (1달러 1,341원)"]
    assert texts("20 euros for lunch") == ["20 euros ≈ 3만 100원 (1유로 1,506원)"]


def test_units_and_temperature():
    assert texts("거기까지 5마일이야") == ["5마일 ≈ 8.05km"]
    assert texts("키가 6피트래") == ["6피트 ≈ 1.83m"]
    assert texts("오늘 화씨 70도래") == ["화씨 70도 ≈ 섭씨 21.1도"]


def test_no_false_positives():
    assert texts("그 동네 사람이야") == []
    assert texts("불 좀 꺼 줘") == []
    assert texts("오늘 회의는 3시") == []
    assert texts("달러 환율이 올랐대") == []  # no amount


def test_rate_failure_means_no_card():
    r = Rates(fetch=lambda url: (_ for _ in ()).throw(OSError("offline")))
    assert find("110달러", r) == []
