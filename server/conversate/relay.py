"""Phone-link login relay for the glasses apps (first user: rokid-feeds' Threads login).

The glasses have no keyboard and their web pages cannot take the phone keyboard's text, so the
wearer types a login on the phone instead, like a TV's "go to example.com/device":

  glasses  POST /relay/new                 -> {"code": "482913", "secret": "...", "expiresIn": 600}
  phone    GET  /login  (or /login/482913)   a small page: enter the code, then ID and password
  phone    POST /api/relay/{code}/send      {"kind": "credentials", "username", "password"}
                                            {"kind": "code", "text"}  (two-step code, when asked)
  phone    GET  /api/relay/{code}/status    {"state", "message"} -- the page polls this
  glasses  GET  /relay/poll                 Authorization: Bearer <secret> -> {"messages": [...]}
                                            (each message is handed over once and then dropped)
  glasses  POST /relay/status               Authorization: Bearer <secret>, {"state", "message"}
                                            states: waiting, logging_in, need_code, done, error

Secrets stay in memory only: never logged, never written to disk, dropped once the glasses take
them, and every session ends after SESSION_TTL seconds (or right after "done"). Codes are 6
random digits; a phone address that misses too many codes is told to wait (brute force).
"""

from __future__ import annotations

import hmac
import logging
import secrets
import threading
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

log = logging.getLogger(__name__)

SESSION_TTL = 600.0
MAX_SESSIONS = 50
MAX_FIELD = 256
# Wrong codes per phone address before it has to wait MISS_WINDOW seconds.
MAX_MISSES = 10
MISS_WINDOW = 600.0
STATES = {"waiting", "logging_in", "need_code", "done", "error"}


@dataclass
class Session:
    code: str
    secret: str
    created: float
    state: str = "waiting"
    message: str = ""
    messages: list[dict] = field(default_factory=list)
    done_at: float | None = None


class RelayStore:
    def __init__(self, clock=time.monotonic) -> None:
        self.clock = clock
        self.lock = threading.Lock()
        self.by_code: dict[str, Session] = {}
        self.misses: dict[str, list[float]] = {}

    def _sweep(self) -> None:
        now = self.clock()
        for code, s in list(self.by_code.items()):
            expired = now - s.created > SESSION_TTL
            finished = s.done_at is not None and now - s.done_at > 30
            if expired or finished:
                del self.by_code[code]

    def new(self) -> Session:
        with self.lock:
            self._sweep()
            if len(self.by_code) >= MAX_SESSIONS:
                raise HTTPException(503, "too many login sessions, try again later")
            while True:
                code = f"{secrets.randbelow(1_000_000):06d}"
                if code not in self.by_code:
                    break
            s = Session(code=code, secret=secrets.token_urlsafe(24), created=self.clock())
            self.by_code[code] = s
            return s

    def by_secret(self, secret: str) -> Session:
        with self.lock:
            self._sweep()
            for s in self.by_code.values():
                if hmac.compare_digest(s.secret, secret):
                    return s
        raise HTTPException(401, "unknown or expired session")

    def for_phone(self, code: str, address: str) -> Session:
        """The session for a code typed on the phone; counts misses per address."""
        with self.lock:
            self._sweep()
            now = self.clock()
            recent = [t for t in self.misses.get(address, []) if now - t < MISS_WINDOW]
            if len(recent) >= MAX_MISSES:
                self.misses[address] = recent
                raise HTTPException(429, "too many wrong codes, wait a few minutes")
            s = self.by_code.get(code)
            if s is None:
                recent.append(now)
                self.misses[address] = recent
                raise HTTPException(404, "no such code (it may have expired)")
            return s


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:] if header.lower().startswith("bearer ") else ""


def _clean(value: object) -> str:
    return str(value or "")[:MAX_FIELD]


def relay_router(store: RelayStore | None = None) -> APIRouter:
    store = store or RelayStore()
    router = APIRouter()

    @router.post("/relay/new")
    def new() -> dict:
        s = store.new()
        log.info("relay: new session")
        return {"code": s.code, "secret": s.secret, "expiresIn": int(SESSION_TTL)}

    @router.get("/relay/poll")
    def poll(request: Request) -> dict:
        s = store.by_secret(_bearer(request))
        with store.lock:
            out, s.messages = s.messages, []
        return {"messages": out, "state": s.state}

    @router.post("/relay/status")
    async def status(request: Request) -> dict:
        s = store.by_secret(_bearer(request))
        body = await request.json()
        state = str(body.get("state", ""))
        if state not in STATES:
            raise HTTPException(400, "bad state")
        with store.lock:
            s.state = state
            s.message = _clean(body.get("message"))[:200]
            if state == "done":
                s.done_at = store.clock()
                s.messages = []
        log.info("relay: glasses state %s", state)
        return {"ok": True}

    @router.post("/api/relay/{code}/send", status_code=204)
    async def send(code: str, request: Request) -> Response:
        s = store.for_phone(code, request.client.host if request.client else "?")
        body = await request.json()
        kind = body.get("kind")
        if kind == "credentials":
            username, password = _clean(body.get("username")).strip(), _clean(body.get("password"))
            if not username or not password:
                raise HTTPException(400, "username and password are required")
            message = {"kind": "credentials", "username": username, "password": password}
        elif kind == "code":
            text = _clean(body.get("text")).strip()
            if not text:
                raise HTTPException(400, "code is required")
            message = {"kind": "code", "text": text}
        else:
            raise HTTPException(400, "bad kind")
        with store.lock:
            if s.state == "done":
                raise HTTPException(409, "already logged in")
            s.messages.append(message)
            s.state = "logging_in"
            s.message = ""
        log.info("relay: phone sent %s", kind)  # never the values
        return Response(status_code=204)

    @router.get("/api/relay/{code}/status")
    def phone_status(code: str, request: Request) -> dict:
        s = store.for_phone(code, request.client.host if request.client else "?")
        return {"state": s.state, "message": s.message}

    @router.get("/login", response_class=HTMLResponse)
    def page() -> str:
        return PAGE

    @router.get("/login/{code}", response_class=HTMLResponse)
    def page_with_code(code: str) -> str:
        return PAGE

    return router


PAGE = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>안경 로그인</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 24px 16px; background: #0b0f0c; color: #e8f5ec; font: 17px/1.5 -apple-system, system-ui, sans-serif; }
  main { max-width: 420px; margin: 0 auto; }
  h1 { font-size: 22px; margin: 0 0 4px; }
  p.sub { color: #9bb5a4; margin: 0 0 20px; font-size: 15px; }
  label { display: block; font-size: 14px; color: #9bb5a4; margin: 14px 0 6px; }
  input { box-sizing: border-box; width: 100%; padding: 13px 14px; font-size: 17px; border-radius: 10px;
          border: 1px solid #2c4a37; background: #121a15; color: #e8f5ec; }
  input:focus { outline: 2px solid #00ff66; border-color: transparent; }
  button { width: 100%; margin-top: 20px; padding: 14px; font-size: 17px; font-weight: 600; border: 0;
           border-radius: 10px; background: #00ff66; color: #04120a; }
  button:disabled { opacity: .5; }
  .status { margin-top: 20px; padding: 12px 14px; border-radius: 10px; background: #121a15; border: 1px solid #2c4a37; }
  .hidden { display: none; }
  .note { margin-top: 18px; font-size: 13px; color: #7d9786; }
</style></head>
<body><main>
  <h1>안경 로그인</h1>
  <p class="sub">안경 화면의 숫자 6자리를 넣고, 로그인 정보를 보내면 안경이 대신 로그인합니다.</p>

  <form id="codeForm">
    <label for="code">안경에 보이는 코드</label>
    <input id="code" inputmode="numeric" autocomplete="one-time-code" maxlength="6" pattern="[0-9]{6}" required>
    <button>다음</button>
  </form>

  <form id="loginForm" class="hidden">
    <label for="user">아이디 (사용자 이름, 전화번호 또는 이메일)</label>
    <input id="user" autocomplete="username" autocapitalize="none" spellcheck="false" required>
    <label for="pass">비밀번호</label>
    <input id="pass" type="password" autocomplete="current-password" required>
    <button id="loginBtn">안경으로 보내기</button>
  </form>

  <form id="otpForm" class="hidden">
    <label for="otp">인증 코드 (문자·인증 앱)</label>
    <input id="otp" inputmode="numeric" autocomplete="one-time-code" required>
    <button>안경으로 보내기</button>
  </form>

  <div id="status" class="status hidden"></div>
  <p class="note">보낸 정보는 안경이 가져가면 서버에서 바로 지워지고, 10분이 지나면 코드도 사라집니다. 서버는 비밀번호를 저장하거나 기록하지 않습니다.</p>
</main>
<script>
(function () {
  var $ = function (id) { return document.getElementById(id); };
  var code = (location.pathname.match(/\\/login\\/(\\d{6})/) || [])[1] || '';
  var timer = null;
  function show(id, on) { $(id).classList.toggle('hidden', !on); }
  function status(text) { $('status').textContent = text; show('status', !!text); }
  async function api(path, body) {
    var r = await fetch('/api/relay/' + code + path, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {});
    if (!r.ok) {
      var detail = '';
      try { detail = (await r.json()).detail || ''; } catch (e) {}
      throw new Error(r.status === 404 ? '코드가 없거나 만료됐습니다. 안경에서 다시 시작해 주세요.' : r.status === 429 ? '잘못된 코드를 너무 많이 넣었습니다. 몇 분 뒤 다시 해 주세요.' : (detail || ('오류 ' + r.status)));
    }
    return r.status === 204 ? null : r.json();
  }
  var LABEL = { waiting: '안경이 기다리는 중입니다. 로그인 정보를 보내 주세요.', logging_in: '안경이 로그인하는 중…', need_code: '인증 코드가 필요합니다.', done: '로그인 완료! 안경에서 Threads를 볼 수 있습니다.', error: '로그인하지 못했습니다.' };
  async function poll() {
    try {
      var s = await api('/status');
      status(LABEL[s.state] + (s.message ? ' (' + s.message + ')' : ''));
      show('otpForm', s.state === 'need_code');
      show('loginForm', s.state === 'waiting' || s.state === 'error');
      if (s.state === 'done') { clearInterval(timer); show('loginForm', false); show('otpForm', false); }
    } catch (e) { status(e.message); clearInterval(timer); }
  }
  function start() {
    show('codeForm', false);
    poll();
    timer = setInterval(poll, 2000);
  }
  $('codeForm').addEventListener('submit', async function (e) {
    e.preventDefault();
    code = $('code').value.trim();
    try { await api('/status'); history.replaceState(null, '', '/login/' + code); start(); } catch (err) { status(err.message); }
  });
  $('loginForm').addEventListener('submit', async function (e) {
    e.preventDefault();
    $('loginBtn').disabled = true;
    try {
      await api('/send', { kind: 'credentials', username: $('user').value, password: $('pass').value });
      $('pass').value = '';
      status(LABEL.logging_in);
      show('loginForm', false);
    } catch (err) { status(err.message); }
    $('loginBtn').disabled = false;
  });
  $('otpForm').addEventListener('submit', async function (e) {
    e.preventDefault();
    try { await api('/send', { kind: 'code', text: $('otp').value }); $('otp').value = ''; status(LABEL.logging_in); show('otpForm', false); } catch (err) { status(err.message); }
  });
  if (code) { $('code').value = code; start(); }
})();
</script>
</body></html>
"""
