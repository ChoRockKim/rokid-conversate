"""Small pictures for AI cards: a Wikipedia thumbnail for things that are easier seen than
described (a person, a place, an animal, a dish, a product).

The answer model names a Wikipedia article ("image": "경복궁"); we fetch the article summary's
thumbnail (ko first, then en), shrink it to at most 96 px and make it greyscale (the HUD is
green only, the glasses tint it), and send it as base64 PNG, ~5-10 KB.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import threading
import time
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

UA = "rokid-conversate/0.1 (https://github.com/ChoRockKim/rokid-conversate)"
MAX_PX = 96
LANGS = ("ko", "en")


def _get(url: str, timeout: float = 3.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def to_png(data: bytes, max_px: int = MAX_PX) -> bytes:
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode in ("P", "LA", "RGBA") or "transparency" in img.info:
        # Transparent parts on black: black is "off" on the HUD.
        rgba = img.convert("RGBA")
        img = Image.new("RGBA", rgba.size, (0, 0, 0, 255))
        img.alpha_composite(rgba)
    img = img.convert("L")
    img.thumbnail((max_px, max_px))
    img = ImageOps.autocontrast(img, cutoff=1)
    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


class ImageFinder:
    """Article title -> base64 PNG (or None). Cached, failures included."""

    TTL_S = 24 * 3600.0

    def __init__(self, get=None) -> None:
        self._get = get or _get
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._lock = threading.Lock()

    def find(self, title: str) -> str | None:
        title = title.strip()
        if not title:
            return None
        with self._lock:
            hit = self._cache.get(title)
            if hit and time.monotonic() - hit[1] < self.TTL_S:
                return hit[0]
        result = None
        for lang in LANGS:
            try:
                url = f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"))
                summary = json.loads(self._get(url))
                if summary.get("type") == "disambiguation":
                    continue
                src = (summary.get("thumbnail") or {}).get("source")
                if not src:
                    continue
                result = base64.b64encode(to_png(self._get(src))).decode()
                log.info("image for %r from %s.wikipedia (%d bytes b64)", title, lang, len(result))
                break
            except Exception as e:  # not found (404), network, bad image: try the next
                log.info("image for %r from %s failed: %s", title, lang, e)
        with self._lock:
            self._cache[title] = (result, time.monotonic())
        return result
