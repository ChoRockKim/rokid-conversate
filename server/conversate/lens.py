"""Photo translation for the rokid-lens glasses app (github.com/ChoRockKim/rokid-lens).

  POST /lens/translate?target=ko   body = JPEG    -> {"model": ..., "paragraphs": [{"source", "translated", "from"}]}

The glasses take one photo per tap and send it here; Gemini reads the text and translates it in
one call, which reads and translates far better than on-device ML Kit (the app falls back to
ML Kit when this fails). The key stays on the server (GEMINI_API_KEY); without it the route
answers 503.

The Lens APK carries no server token, so the free Gemini quota is protected by limits instead:
[PER_IP_PER_MINUTE] photos per client address per minute and [PER_DAY] photos per day overall.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import time
from collections import defaultdict, deque
from typing import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request

log = logging.getLogger(__name__)

MAX_PHOTO = 4 * 1024 * 1024
PER_IP_PER_MINUTE = 20
PER_DAY = 1000
DEFAULT_MODEL = "gemini-2.5-flash-lite"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

LANG_NAMES = {"ko": "Korean", "en": "English", "ja": "Japanese", "zh": "Chinese"}

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "paragraphs": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "source": {"type": "STRING"},
                    "translated": {"type": "STRING"},
                    "from": {"type": "STRING"},
                },
                "required": ["source", "translated", "from"],
            },
        }
    },
    "required": ["paragraphs"],
}


def prompt(target: str) -> str:
    to = LANG_NAMES.get(target, target)
    return (
        "This photo was taken by smart glasses looking at text (a sign, a menu, a page, a screen). "
        "Read all legible text in it. Group it into short paragraphs in natural reading order "
        "(a heading, a menu item with its price, a sentence or a few lines that belong together). "
        f"Translate each paragraph into natural {to}; if a paragraph is already in {to}, translate "
        f"it into {'English' if target != 'en' else 'Korean'} instead. Keep numbers, prices and "
        "names. Give `from` as the ISO 639-1 code of the original language. Skip text you cannot "
        "read reliably. If there is no text, return an empty list."
    )


Gemini = Callable[[bytes, str], Awaitable[dict]]


def gemini_from_env() -> Gemini | None:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    model = os.environ.get("GEMINI_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL

    async def call(jpeg: bytes, target: str) -> dict:
        import httpx  # comes with the openai package

        body = {
            "contents": [{
                "parts": [
                    {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
                    {"text": prompt(target)},
                ]
            }],
            "generationConfig": {
                "responseMimeType": "application/json",
                "responseSchema": SCHEMA,
                "temperature": 0.2,
            },
        }
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(GEMINI_URL.format(model=model), params={"key": key}, json=body)
        except httpx.HTTPError as e:
            log.warning("gemini: %s", type(e).__name__)
            raise HTTPException(502, "AI unreachable")
        if r.status_code == 429:
            raise HTTPException(429, "AI quota reached")
        if r.status_code != 200:
            log.warning("gemini %s: %s", r.status_code, r.text[:300])
            raise HTTPException(502, f"AI error {r.status_code}")
        data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            return {"model": model, "paragraphs": []}
        return {"model": model, **parse(text)}

    return call


def parse(text: str) -> dict:
    """Gemini's JSON answer -> {"paragraphs": [...]}, dropping malformed or empty entries."""
    try:
        obj = json.loads(text)
    except ValueError:
        return {"paragraphs": []}
    out = []
    for p in obj.get("paragraphs", []) if isinstance(obj, dict) else []:
        if not isinstance(p, dict):
            continue
        source = str(p.get("source", "")).strip()
        translated = str(p.get("translated", "")).strip()
        if source and translated:
            out.append({"source": source, "translated": translated, "from": str(p.get("from", "")).strip().lower()[:8]})
    return {"paragraphs": out}


class Limits:
    """Photos per client address per minute, and photos per day overall."""

    def __init__(self, per_ip_per_minute: int = PER_IP_PER_MINUTE, per_day: int = PER_DAY,
                 clock: Callable[[], float] = time.time):
        self.per_ip = per_ip_per_minute
        self.per_day = per_day
        self.clock = clock
        self.recent: dict[str, deque] = defaultdict(deque)
        self.day = ""
        self.today = 0

    def allow(self, ip: str) -> bool:
        now = self.clock()
        day = time.strftime("%Y-%m-%d", time.gmtime(now))
        if day != self.day:
            self.day, self.today = day, 0
        q = self.recent[ip]
        while q and now - q[0] >= 60:
            q.popleft()
        if len(q) >= self.per_ip or self.today >= self.per_day:
            return False
        q.append(now)
        self.today += 1
        return True


def client_ip(request: Request) -> str:
    # Railway puts the real client first in X-Forwarded-For.
    forwarded = request.headers.get("x-forwarded-for", "")
    return forwarded.split(",")[0].strip() or (request.client.host if request.client else "?")


def lens_router(gemini: Gemini | None, limits: Limits | None = None) -> APIRouter:
    router = APIRouter()
    lim = limits or Limits()

    @router.post("/lens/translate")
    async def translate(request: Request, target: str = "ko") -> dict:
        if gemini is None:
            raise HTTPException(503, "photo translation is not configured")
        if target not in LANG_NAMES:
            raise HTTPException(400, "unsupported target")
        body = await request.body()
        if not body or len(body) > MAX_PHOTO:
            raise HTTPException(400, "empty or too large")
        if body[:2] != b"\xff\xd8":
            raise HTTPException(400, "not a JPEG")
        if not lim.allow(client_ip(request)):
            raise HTTPException(429, "too many photos")
        result = await gemini(body, target)
        log.info("lens: %d paragraphs (%d bytes)", len(result.get("paragraphs", [])), len(body))
        return result

    return router


def lens_router_from_env() -> APIRouter:
    gemini = gemini_from_env()
    if gemini is None:
        log.info("GEMINI_API_KEY not set: /lens/translate is closed")
    return lens_router(gemini)
