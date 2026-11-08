"""Glasses app OTA: keeps the latest APK on disk (a Railway volume) and serves it.

  GET /app/latest     -> {versionCode, versionName, size, sha256, uploadedAt}   glasses token
  GET /app/download   -> the APK                                               glasses token
  PUT /app/upload?versionCode=2&versionName=0.2.0   body = APK                 OTA_UPLOAD_TOKEN

Other glasses apps share this server for their updates, each in its own folder (OTA_DIR/<name>):

  GET /apps/<name>/latest, /apps/<name>/download    public (these APKs carry no server token)
  PUT /apps/<name>/upload?versionCode=..&versionName=..   OTA_UPLOAD_TOKEN

Only names listed in OTA_PUBLIC_APPS (comma-separated, default "rokid-youtube") exist.

Same shape as the navigation server's ota.ts. The glasses token (CONVERSATE_TOKEN) guards the
/app download too, since a release APK may carry a baked-in server token.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
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


class _Store:
    """One app's latest APK + its metadata in [directory]."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.apk = directory / "app.apk"
        self.meta = directory / "app.json"

    def latest(self) -> dict | None:
        if not self.apk.exists() or not self.meta.exists():
            return None
        return json.loads(self.meta.read_text())

    def latest_or_404(self) -> dict:
        m = self.latest()
        if m is None:
            raise HTTPException(404, "no APK uploaded")
        return m

    def download(self) -> FileResponse:
        m = self.latest_or_404()
        log.info("ota download: %s %s", self.directory.name, m["versionName"])
        return FileResponse(
            self.apk,
            media_type="application/vnd.android.package-archive",
            headers={"x-sha256": m["sha256"]},
        )

    async def upload(self, request: Request, versionCode: int, versionName: str) -> dict:
        return await _save(self.directory, self.apk, self.meta, request, versionCode, versionName)


def ota_router(
    directory: Path, read_token: str, upload_token: str, public_apps: tuple[str, ...] = ()
) -> APIRouter:
    router = APIRouter()
    main = _Store(directory)
    others = {name: _Store(directory / name) for name in public_apps if _NAME.fullmatch(name)}

    def require_reader(request: Request) -> None:
        # With no CONVERSATE_TOKEN set (local dev) the read side is open, like /ws.
        if read_token and not check(bearer(request), read_token):
            raise HTTPException(401, "unauthorized")

    def require_uploader(request: Request) -> None:
        if not check(bearer(request), upload_token):
            raise HTTPException(401, "unauthorized")

    def store(name: str) -> _Store:
        s = others.get(name)
        if s is None:
            raise HTTPException(404, "unknown app")
        return s

    @router.get("/app/latest")
    def get_latest(request: Request) -> dict:
        require_reader(request)
        return main.latest_or_404()

    @router.get("/app/download")
    def download(request: Request) -> FileResponse:
        require_reader(request)
        return main.download()

    @router.put("/app/upload")
    async def upload(request: Request, versionCode: int = 0, versionName: str = "") -> dict:
        require_uploader(request)
        return await main.upload(request, versionCode, versionName)

    @router.get("/apps/{name}/latest")
    def get_app_latest(name: str) -> dict:
        return store(name).latest_or_404()

    @router.get("/apps/{name}/download")
    def download_app(name: str) -> FileResponse:
        return store(name).download()

    @router.put("/apps/{name}/upload")
    async def upload_app(request: Request, name: str, versionCode: int = 0, versionName: str = "") -> dict:
        require_uploader(request)
        return await store(name).upload(request, versionCode, versionName)

    return router


_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,40}")


async def _save(directory: Path, apk: Path, meta: Path, request: Request, versionCode: int, versionName: str) -> dict:
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
    log.info("ota upload: %s %s (%d) %dB", directory.name, versionName, versionCode, size)
    return m


def ota_router_from_env(read_token: str) -> APIRouter:
    directory = Path(os.environ.get("OTA_DIR", "/data/ota"))
    upload_token = os.environ.get("OTA_UPLOAD_TOKEN", "")
    if not upload_token:
        log.info("OTA_UPLOAD_TOKEN not set: /app/upload is closed")
    public_apps = tuple(n.strip() for n in os.environ.get("OTA_PUBLIC_APPS", "rokid-youtube").split(",") if n.strip())
    return ota_router(directory, read_token, upload_token, public_apps)
