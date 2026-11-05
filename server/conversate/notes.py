"""Prep notes: things the wearer writes down before a conversation (who they are meeting, the
purpose, terms, numbers to remember). The assistant reads them on every ask.

The glasses have no keyboard, so notes are edited on the phone in a small web page:
  GET  /notes        the page (no secrets in it; it asks for the token once and keeps it)
  GET  /api/notes    {"text", "updatedAt"}          Authorization: Bearer <CONVERSATE_TOKEN>
  PUT  /api/notes    {"text"} -> {"text", "updatedAt"}
Stored as one JSON file on the Railway volume (NOTES_PATH, default /data/notes.json).
"""

from __future__ import annotations

import hmac
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)

MAX_CHARS = 8000


class NotesStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def read(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {"text": "", "updatedAt": None}

    def text(self) -> str:
        return str(self.read().get("text") or "")

    def write(self, text: str) -> dict:
        data = {"text": text[:MAX_CHARS], "updatedAt": datetime.now(timezone.utc).isoformat()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(self.path)
        log.info("notes saved (%d chars)", len(data["text"]))
        return data


def notes_store_from_env() -> NotesStore:
    return NotesStore(Path(os.environ.get("NOTES_PATH", "/data/notes.json")))


def notes_router(store: NotesStore, token: str) -> APIRouter:
    router = APIRouter()

    def require(request: Request) -> None:
        header = request.headers.get("authorization", "")
        given = header[7:] if header.lower().startswith("bearer ") else ""
        if token and not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "unauthorized")

    @router.get("/notes", response_class=HTMLResponse)
    def page() -> str:
        return PAGE

    @router.get("/api/notes")
    def get_notes(request: Request) -> dict:
        require(request)
        return store.read()

    @router.put("/api/notes")
    async def put_notes(request: Request) -> dict:
        require(request)
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(400, "json body required")
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str):
            raise HTTPException(400, "text required")
        if len(text) > MAX_CHARS:
            raise HTTPException(413, f"at most {MAX_CHARS} characters")
        return store.write(text)

    return router


PAGE = """<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>사전 노트</title>
<style>
  :root { color-scheme: light dark; --bg:#fff; --fg:#111; --muted:#666; --line:#ddd; --accent:#0a7d3c; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0f1210; --fg:#e8efe9; --muted:#9aa59c; --line:#2a332c; --accent:#3ddc84; } }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 -apple-system, system-ui, sans-serif; }
  main { max-width:640px; margin:0 auto; padding:16px; }
  h1 { font-size:20px; margin:8px 0 4px; }
  p { color:var(--muted); margin:0 0 12px; font-size:14px; }
  textarea { width:100%; box-sizing:border-box; min-height:55vh; padding:12px; font:inherit;
             background:transparent; color:var(--fg); border:1px solid var(--line); border-radius:10px; }
  input { width:100%; box-sizing:border-box; padding:10px 12px; font:inherit; color:var(--fg);
          background:transparent; border:1px solid var(--line); border-radius:10px; }
  .row { display:flex; gap:8px; align-items:center; margin-top:10px; }
  button { padding:10px 18px; font:inherit; font-weight:600; border:0; border-radius:10px;
           background:var(--accent); color:#fff; }
  button.secondary { background:transparent; color:var(--muted); border:1px solid var(--line); }
  #status { color:var(--muted); font-size:14px; }
  [hidden] { display:none !important; }
</style></head>
<body><main>
  <h1>사전 노트</h1>
  <p>대화 전에 적어 두면 안경의 AI가 참고해요. 만날 사람, 목적, 자주 나올 용어, 알아둘 숫자 같은 것.</p>
  <section id="login" hidden>
    <input id="token" type="password" placeholder="서버 토큰 (CONVERSATE_TOKEN)" autocomplete="off">
    <div class="row"><button id="saveToken">확인</button></div>
  </section>
  <section id="editor" hidden>
    <textarea id="text" placeholder="예)&#10;- 오늘 3시 OO전자 김OO 팀장 미팅, 스마트 안경 납품 건&#10;- 단가 1대 45만 원, 최소 주문 200대&#10;- 상대 회사 경쟁사: XX, YY&#10;- 용어: MOQ = 최소 주문 수량"></textarea>
    <div class="row">
      <button id="save">저장</button>
      <button id="logout" class="secondary">토큰 바꾸기</button>
      <span id="status"></span>
    </div>
  </section>
</main>
<script>
const KEY = "conversate-token";
const $ = (id) => document.getElementById(id);
function getToken() { try { return localStorage.getItem(KEY) || ""; } catch { return ""; } }
function setToken(t) { try { localStorage.setItem(KEY, t); } catch {} }
async function api(method, body) {
  const r = await fetch("/api/notes", { method, headers: { "Authorization": "Bearer " + getToken(),
    "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  if (r.status === 401) throw new Error("unauthorized");
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
function when(iso) { return iso ? new Date(iso).toLocaleString("ko-KR") : "아직 없음"; }
async function load() {
  try {
    const d = await api("GET");
    $("text").value = d.text || ""; $("status").textContent = "마지막 저장: " + when(d.updatedAt);
    $("login").hidden = true; $("editor").hidden = false;
  } catch (e) { $("editor").hidden = true; $("login").hidden = false;
    if (getToken()) $("token").placeholder = "토큰이 맞지 않아요. 다시 입력"; }
}
$("saveToken").onclick = () => { setToken($("token").value.trim()); load(); };
$("logout").onclick = () => { setToken(""); $("token").value = ""; load(); };
$("save").onclick = async () => {
  $("status").textContent = "저장 중…";
  try { const d = await api("PUT", { text: $("text").value }); $("status").textContent = "저장됨 · " + when(d.updatedAt); }
  catch (e) { $("status").textContent = "저장 실패: " + e.message; }
};
load();
</script>
</body></html>
"""
