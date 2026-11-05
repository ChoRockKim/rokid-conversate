"""Glasses app OTA: keeps the latest APK on disk (a Railway volume) and serves it.

  GET /app/latest     -> {versionCode, versionName, size, sha256, uploadedAt}   glasses token
  GET /app/download   -> the APK                                               glasses token
  PUT /app/upload?versionCode=2&versionName=0.2.0   body = APK                 OTA_UPLOAD_TOKEN

Same shape as the navigation server's ota.ts. The glasses token (CONVERSATE_TOKEN) guards the
download too, since a release APK may carry a baked-in server token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

log = logging.getLogger(__name__)

MAX_APK = 80 * 1024 * 1024


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else ""


def check(given: str, expected: str) -> bool:
    return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())


def ota_router(directory: Path, read_token: str, upload_token: str) -> APIRouter:
    router = APIRouter()
    apk = directory / "app.apk"
    meta = directory / "app.json"

    def latest() -> dict | None:
        if not apk.exists() or not meta.exists():
            return None
        return json.loads(meta.read_text())

    def require_reader(request: Request) -> None:
        # With no CONVERSATE_TOKEN set (local dev) the read side is open, like /ws.
        if read_token and not check(bearer(request), read_token):
            raise HTTPException(401, "unauthorized")

    @router.get("/app/latest")
    def get_latest(request: Request) -> dict:
        require_reader(request)
        m = latest()
        if m is None:
            raise HTTPException(404, "no APK uploaded")
        return m

    @router.get("/app/download")
    def download(request: Request) -> FileResponse:
        require_reader(request)
        m = latest()
        if m is None:
            raise HTTPException(404, "no APK uploaded")
        log.info("ota download: %s", m["versionName"])
        return FileResponse(
            apk,
            media_type="application/vnd.android.package-archive",
            headers={"x-sha256": m["sha256"]},
        )

    @router.put("/app/upload")
    async def upload(request: Request, versionCode: int = 0, versionName: str = "") -> dict:
        if not check(bearer(request), upload_token):
            raise HTTPException(401, "unauthorized")
        if versionCode <= 0 or not versionName:
            raise HTTPException(400, "versionCode and versionName are required")
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / "app.apk.part"
        digest = hashlib.sha256()
        size = 0
        with tmp.open("wb") as out:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_APK:
                    out.close()
                    tmp.unlink(missing_ok=True)
                    raise HTTPException(413, "too large")
                digest.update(chunk)
                out.write(chunk)
        # No signature check here (Android does that on install); just make sure it is a zip.
        with tmp.open("rb") as f:
            if f.read(2) != b"PK":
                tmp.unlink(missing_ok=True)
                raise HTTPException(400, "not an APK (zip)")
        tmp.replace(apk)
        m = {
            "versionCode": versionCode,
            "versionName": versionName,
            "size": size,
            "sha256": digest.hexdigest(),
            "uploadedAt": datetime.now(timezone.utc).isoformat(),
        }
        meta.write_text(json.dumps(m))
        log.info("ota upload: %s (%d) %dB", versionName, versionCode, size)
        return m

    return router


def ota_router_from_env(read_token: str) -> APIRouter:
    directory = Path(os.environ.get("OTA_DIR", "/data/ota"))
    upload_token = os.environ.get("OTA_UPLOAD_TOKEN", "")
    if not upload_token:
        log.info("OTA_UPLOAD_TOKEN not set: /app/upload is closed")
    return ota_router(directory, read_token, upload_token)
