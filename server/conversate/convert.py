"""Instant conversions without an LLM: foreign money to won, imperial units to metric.

"어제 산 운동화 110달러였어" -> "110달러 ≈ 14만 7,500원 (1달러 1,341원)" in well under a second,
where the LLM path (triage + answer with web search) took 8-9 s for the same card.

Rates: open.er-api.com (free, no key, daily), fallback api.frankfurter.dev; cached for an hour.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import urllib.request
from dataclasses import dataclass

log = logging.getLogger(__name__)

# word -> ISO code; longer names first so "홍콩달러" wins over "달러".
CURRENCIES: list[tuple[str, str]] = [
    ("홍콩달러", "HKD"), ("홍콩 달러", "HKD"), ("대만달러", "TWD"), ("대만 달러", "TWD"),
    ("캐나다달러", "CAD"), ("캐나다 달러", "CAD"), ("호주달러", "AUD"), ("호주 달러", "AUD"),
    ("싱가포르달러", "SGD"), ("싱가포르 달러", "SGD"),
    ("달러", "USD"), ("불", "USD"), ("dollars", "USD"), ("dollar", "USD"), ("bucks", "USD"), ("usd", "USD"),
    ("엔", "JPY"), ("yen", "JPY"), ("jpy", "JPY"),
    ("유로", "EUR"), ("euros", "EUR"), ("euro", "EUR"), ("eur", "EUR"),
    ("위안", "CNY"), ("yuan", "CNY"), ("cny", "CNY"),
    ("파운드", "GBP"), ("pounds", "GBP"), ("gbp", "GBP"),
    ("바트", "THB"), ("동", "VND"), ("페소", "PHP"), ("루피", "INR"),
]
SYMBOLS = {"$": "USD", "€": "EUR", "¥": "JPY", "£": "GBP"}
NAMES_KO = {"USD": "달러", "JPY": "엔", "EUR": "유로", "CNY": "위안", "GBP": "파운드", "HKD": "홍콩달러",
            "TWD": "대만달러", "CAD": "캐나다달러", "AUD": "호주달러", "SGD": "싱가포르달러", "THB": "바트",
            "VND": "동", "PHP": "페소", "INR": "루피"}

# unit word -> (metric name, factor, offset)  value_metric = value * factor + offset
UNITS: list[tuple[str, str, float, float]] = [
    ("마일", "km", 1.609344, 0.0), ("miles", "km", 1.609344, 0.0), ("mile", "km", 1.609344, 0.0),
    ("피트", "m", 0.3048, 0.0), ("feet", "m", 0.3048, 0.0), ("foot", "m", 0.3048, 0.0), ("ft", "m", 0.3048, 0.0),
    ("인치", "cm", 2.54, 0.0), ("inches", "cm", 2.54, 0.0), ("inch", "cm", 2.54, 0.0),
    ("야드", "m", 0.9144, 0.0), ("yards", "m", 0.9144, 0.0),
    ("온스", "g", 28.3495, 0.0), ("ounces", "g", 28.3495, 0.0), ("oz", "g", 28.3495, 0.0),
    ("갤런", "L", 3.78541, 0.0), ("gallons", "L", 3.78541, 0.0),
    ("lbs", "kg", 0.453592, 0.0), ("lb", "kg", 0.453592, 0.0),
]

_KO_MULT = {"천": 1_000, "만": 10_000, "억": 100_000_000}
_NUM = r"(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s*(천|만|억)?\s*"
_FAHRENHEIT = re.compile(r"(?:화씨\s*" + _NUM + r"도|" + _NUM + r"(?:°\s*F|degrees?\s*fahrenheit|도\s*화씨))", re.I)


def _end(word: str) -> str:
    """English unit words must end there ("lb" not "lbs..."); Korean ones take particles
    ("110달러였어", "5마일이야")."""
    return r"(?![a-z])" if word.isascii() else ""


@dataclass(frozen=True)
class Conversion:
    span: str   # the words that triggered it, e.g. "110달러"
    title: str
    text: str


def _value(num: str, mult: str | None) -> float:
    return float(num.replace(",", "")) * (_KO_MULT.get(mult or "", 1))


def won(amount: float) -> str:
    """1474500 -> "147만 4,500원" style (rounded to a sensible precision)."""
    n = int(round(amount))
    if n >= 100_000_000:
        eok, rest = divmod(n, 100_000_000)
        man = rest // 10_000
        return f"{eok:,}억 {man:,}만 원" if man else f"{eok:,}억 원"
    if n >= 10_000:
        man, rest = divmod(n, 10_000)
        rest = int(round(rest, -2))  # to 100 won
        if rest == 10_000:
            man, rest = man + 1, 0
        return f"{man:,}만 {rest:,}원" if rest else f"{man:,}만 원"
    return f"{int(round(n, -1)):,}원"


def _num(x: float) -> str:
    if x >= 100:
        return f"{x:,.0f}"
    if x >= 10:
        return f"{x:,.1f}".rstrip("0").rstrip(".")
    return f"{x:,.2f}".rstrip("0").rstrip(".")


class Rates:
    """KRW per unit of a currency, fetched lazily and cached."""

    TTL_S = 3600.0
    SOURCES = (
        ("https://open.er-api.com/v6/latest/{code}", lambda d: d["rates"]["KRW"]),
        ("https://api.frankfurter.dev/v1/latest?from={code}&to=KRW", lambda d: d["rates"]["KRW"]),
    )

    def __init__(self, fetch=None) -> None:
        self._fetch = fetch or self._http
        self._cache: dict[str, tuple[float, float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _http(url: str) -> dict:
        req = urllib.request.Request(url, headers={"User-Agent": "rokid-conversate"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode())

    def krw(self, code: str) -> float | None:
        with self._lock:
            hit = self._cache.get(code)
            if hit and time.monotonic() - hit[1] < self.TTL_S:
                return hit[0]
        for url, pick in self.SOURCES:
            try:
                rate = float(pick(self._fetch(url.format(code=code))))
            except Exception as e:  # network, format: try the next source
                log.info("rate %s from %s failed: %s", code, url.split("/")[2], e)
                continue
            with self._lock:
                self._cache[code] = (rate, time.monotonic())
            return rate
        return None


def find(text: str, rates: Rates) -> list[Conversion]:
    """All conversions worth showing for one caption (at most 2)."""
    out: list[Conversion] = []
    lower = text.lower()
    taken: list[tuple[int, int]] = []

    def free(a: int, b: int) -> bool:
        return all(b <= s or a >= e for s, e in taken)

    # money: number + currency word, or symbol + number
    for word, code in CURRENCIES:
        for m in re.finditer(_NUM + re.escape(word) + _end(word), lower):
            if word == "동" and not re.search(r"(베트남|동화|vnd)", lower):
                continue  # "동" alone is too common a word
            if word == "불" and not m.group(1):
                continue
            if not free(m.start(), m.end()):
                continue
            taken.append((m.start(), m.end()))
            amount = _value(m.group(1), m.group(2))
            rate = rates.krw(code)
            if rate:
                span = text[m.start():m.end()].strip()
                out.append(Conversion(span, f"{span} 환산",
                                      f"{span} ≈ {won(amount * rate)} (1{NAMES_KO.get(code, code)} {_num(rate)}원)"))
    for sym, code in SYMBOLS.items():
        for m in re.finditer(re.escape(sym) + r"\s*(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)", text):
            if not free(m.start(), m.end()):
                continue
            taken.append((m.start(), m.end()))
            rate = rates.krw(code)
            if rate:
                span = m.group(0).strip()
                amount = _value(m.group(1), None)
                out.append(Conversion(span, f"{span} 환산", f"{span} ≈ {won(amount * rate)} (1{NAMES_KO.get(code, code)} {_num(rate)}원)"))

    # temperature
    for m in _FAHRENHEIT.finditer(text):
        num, mult = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        f = _value(num, mult)
        span = m.group(0).strip()
        out.append(Conversion(span, "온도 환산", f"{span} ≈ 섭씨 {_num((f - 32) * 5 / 9)}도"))

    # units
    for word, metric, factor, offset in UNITS:
        for m in re.finditer(_NUM + re.escape(word) + _end(word), lower):
            if not free(m.start(), m.end()):
                continue
            taken.append((m.start(), m.end()))
            span = text[m.start():m.end()].strip()
            out.append(Conversion(span, "단위 환산", f"{span} ≈ {_num(_value(m.group(1), m.group(2)) * factor + offset)}{metric}"))
    return out[:2]
