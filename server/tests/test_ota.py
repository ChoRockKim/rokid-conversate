import hashlib

from fastapi import FastAPI
from fastapi.testclient import TestClient

from conversate.ota import ota_router

READ = {"Authorization": "Bearer glass"}
UPLOAD = {"Authorization": "Bearer up"}
APK = b"PK\x03\x04" + b"fake apk body" * 100


def client(tmp_path):
    app = FastAPI()
    app.include_router(ota_router(tmp_path, read_token="glass", upload_token="up"))
    return TestClient(app)


def test_nothing_uploaded_is_404(tmp_path):
    c = client(tmp_path)
    assert c.get("/app/latest", headers=READ).status_code == 404
    assert c.get("/app/download", headers=READ).status_code == 404


def test_upload_then_latest_and_download(tmp_path):
    c = client(tmp_path)
    r = c.put("/app/upload?versionCode=2&versionName=0.2.0", headers=UPLOAD, content=APK)
    assert r.status_code == 200, r.text
    meta = r.json()
    assert meta["versionCode"] == 2
    assert meta["size"] == len(APK)
    assert meta["sha256"] == hashlib.sha256(APK).hexdigest()

    assert c.get("/app/latest", headers=READ).json()["versionName"] == "0.2.0"
    d = c.get("/app/download", headers=READ)
    assert d.status_code == 200
    assert d.content == APK
    assert d.headers["x-sha256"] == meta["sha256"]
    assert d.headers["content-type"] == "application/vnd.android.package-archive"


def test_tokens_are_separate(tmp_path):
    c = client(tmp_path)
    assert c.put("/app/upload?versionCode=2&versionName=x", headers=READ, content=APK).status_code == 401
    assert c.put("/app/upload?versionCode=2&versionName=x", content=APK).status_code == 401
    c.put("/app/upload?versionCode=2&versionName=x", headers=UPLOAD, content=APK)
    assert c.get("/app/latest").status_code == 401
    assert c.get("/app/download", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_rejects_bad_uploads(tmp_path):
    c = client(tmp_path)
    assert c.put("/app/upload?versionCode=2&versionName=x", headers=UPLOAD, content=b"hello").status_code == 400
    assert c.put("/app/upload?versionName=x", headers=UPLOAD, content=APK).status_code == 400
    assert c.put("/app/upload?versionCode=2", headers=UPLOAD, content=APK).status_code == 400
    # A failed upload leaves no APK behind.
    assert c.get("/app/latest", headers=READ).status_code == 404


def test_upload_closed_without_upload_token(tmp_path):
    app = FastAPI()
    app.include_router(ota_router(tmp_path, read_token="glass", upload_token=""))
    c = TestClient(app)
    assert c.put("/app/upload?versionCode=2&versionName=x", headers={"Authorization": "Bearer "}, content=APK).status_code == 401
