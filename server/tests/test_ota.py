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


def apps_client(tmp_path):
    app = FastAPI()
    app.include_router(ota_router(tmp_path, read_token="glass", upload_token="up", public_apps=("rokid-youtube",)))
    return TestClient(app)


def test_other_apps_are_public_to_read_and_kept_apart(tmp_path):
    c = apps_client(tmp_path)
    assert c.get("/apps/rokid-youtube/latest").status_code == 404
    other = b"PK\x03\x04" + b"youtube" * 50
    r = c.put("/apps/rokid-youtube/upload?versionCode=200&versionName=0.2.0", headers=UPLOAD, content=other)
    assert r.status_code == 200, r.text

    # No token needed to read; the main app is untouched.
    assert c.get("/apps/rokid-youtube/latest").json()["versionCode"] == 200
    d = c.get("/apps/rokid-youtube/download")
    assert d.content == other
    assert d.headers["x-sha256"] == hashlib.sha256(other).hexdigest()
    assert c.get("/app/latest", headers=READ).status_code == 404

    # Uploading the main app does not touch the other one.
    c.put("/app/upload?versionCode=3&versionName=0.3.0", headers=UPLOAD, content=APK)
    assert c.get("/app/download", headers=READ).content == APK
    assert c.get("/apps/rokid-youtube/download").content == other


def test_other_apps_need_the_upload_token_and_a_known_name(tmp_path):
    c = apps_client(tmp_path)
    url = "/apps/rokid-youtube/upload?versionCode=2&versionName=x"
    assert c.put(url, content=APK).status_code == 401
    assert c.put(url, headers=READ, content=APK).status_code == 401
    assert c.put("/apps/something-else/upload?versionCode=2&versionName=x", headers=UPLOAD, content=APK).status_code == 404
    assert c.get("/apps/something-else/latest").status_code == 404
    assert c.get("/apps/..%2Fapp/latest").status_code == 404
