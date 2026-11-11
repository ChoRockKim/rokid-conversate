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

import asyncio
import base64
import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from typing import Awaitable, Callable

from fastapi import APIRouter, HTTPException, Request, Response

log = logging.getLogger(__name__)

MAX_PHOTO = 4 * 1024 * 1024
PER_IP_PER_MINUTE = 20
PER_DAY = 1000
# Tried in order: the better model first, the lighter one when it is missing or out of quota.
DEFAULT_MODELS = "gemini-3.5-flash,gemini-3.5-flash-lite"
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
        "Read all legible text in it. Group it into paragraphs in natural reading order "
        "(a heading, a menu item with its price, or one or more complete sentences). "
        "Never split a sentence across paragraphs: text that wraps onto the next line, or continues "
        "in the next column, belongs to the same sentence, so join it (without the line breaks) "
        "into one paragraph. Every paragraph must start at the start of a sentence or item and end "
        "at its end. "
        "Translate only real content a person would want to read (a sign, a notice, a menu, an "
        "article, a message). Skip interface chrome: browser or app menus, tabs, toolbars, "
        "buttons, address bars and URLs, status bars, timestamps, page numbers, ads, cookie "
        "banners, navigation links and other repeated UI labels. "
        "The photo is cropped to what the wearer is aiming at: put the paragraph nearest the "
        "center of the photo first, then the others in reading order. "
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
    models = [m.strip() for m in (os.environ.get("GEMINI_MODELS") or DEFAULT_MODELS).split(",") if m.strip()]

    async def call(jpeg: bytes, target: str) -> dict:
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
        # Standard library only (the deployed image has no extra HTTP client); runs in a thread.
        status, text, model = 0, "", models[0]
        for model in models:
            status, text = await asyncio.to_thread(post_json, GEMINI_URL.format(model=model) + "?key=" + key, body)
            if status == 200:
                break
            log.warning("gemini %s %s: %s", model, status, text[:300])
            # Missing model, quota or a server error: try the next one.
            if status not in (404, 429, 500, 503):
                break
        if status == 429:
            raise HTTPException(429, "AI quota reached")
        if status != 200:
            raise HTTPException(502, f"AI error {status}")
        try:
            answer = json.loads(text)["candidates"][0]["content"]["parts"][0]["text"]
        except (ValueError, KeyError, IndexError, TypeError):
            return {"model": model, "paragraphs": []}
        return {"model": model, **parse(answer)}

    return call


def post_json(url: str, body: dict, timeout: float = 30) -> tuple[int, str]:
    """POST JSON with urllib; returns (status, text). Network failures become HTTP 502."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        log.warning("gemini: %s", type(e).__name__)
        raise HTTPException(502, "AI unreachable")


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


def lens_router(gemini: Gemini | None, limits: Limits | None = None, upload_token: str = "") -> APIRouter:
    router = APIRouter()
    lim = limits or Limits()
    # For diagnosing recognition: only the last photo, only in memory, only with the upload token.
    last: dict[str, bytes] = {}

    @router.get("/lens/last")
    def last_photo(request: Request) -> Response:
        from .ota import bearer, check

        if not check(bearer(request), upload_token):
            raise HTTPException(401, "unauthorized")
        if "jpeg" not in last:
            raise HTTPException(404, "no photo yet")
        return Response(last["jpeg"], media_type="image/jpeg")

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
        last["jpeg"] = body
        result = await gemini(body, target)
        log.info("lens: %s, %d paragraphs (%d bytes)", result.get("model"), len(result.get("paragraphs", [])), len(body))
        return result

    return router


def lens_router_from_env() -> APIRouter:
    gemini = gemini_from_env()
    if gemini is None:
        log.info("GEMINI_API_KEY not set: /lens/translate is closed")
    return lens_router(gemini, upload_token=os.environ.get("OTA_UPLOAD_TOKEN", ""))
