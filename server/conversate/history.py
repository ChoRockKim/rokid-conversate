"""Conversation history: transcripts, AI cards, and an after-the-fact summary with to-dos.

A conversation is everything said until no final caption arrives for IDLE_S (3 min); glasses
reconnects in between (screen off, app pause) stay in the same conversation. When one ends, an
LLM writes a title, a few summary points and the to-dos, like the Even G2's AI Summary.

Text only, no audio. Stored as JSON files on the Railway volume (HISTORY_DIR, default
/data/conversations). HISTORY=0 turns it off. Viewed on the phone:
  GET /history                  page (token asked once, kept on the device)
  GET /api/conversations        [{id, title, startedAt, endedAt, lines, summarized}]
  GET /api/conversations/{id}   the whole record
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

log = logging.getLogger(__name__)

IDLE_S = 180.0
_ID = re.compile(r"^[0-9]{8}-[0-9]{6}$")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Summarizer(Protocol):
    def summarize(self, lines: list[str], notes: str) -> dict | None:
        """Blocking. -> {"title", "summary": [..], "todos": [..]} or None."""
        ...


SUMMARY_INSTRUCTIONS = """너는 대화를 정리해 주는 비서다. 아래는 스마트 안경이 받아쓴 대화다 (음성 인식이라 틀린 글자가 있을 수 있다).
반드시 JSON 하나만 출력한다:
{"title": "20자 이내 대화 제목",
 "summary": ["핵심 내용 한 줄씩, 3~6개, 결정된 것과 숫자를 우선"],
 "todos": ["누가 무엇을 해야 하는지 한 줄씩 (없으면 빈 배열)"]}
대화에 없는 내용은 지어내지 않는다. 잡담뿐이면 summary 1~2줄, todos 는 빈 배열."""


class OpenAISummarizer:
    def __init__(self) -> None:
        from openai import OpenAI

        self.model = os.environ.get("SUMMARY_MODEL", "gpt-6.1-terra")
        self.client = OpenAI(timeout=60.0, max_retries=1)

    def summarize(self, lines: list[str], notes: str) -> dict | None:
        prompt = ""
        if notes.strip():
            prompt += f"사전 노트 (맥락):\n{notes[:3000]}\n\n"
        prompt += "대화:\n" + "\n".join(f"- {line}" for line in lines[-400:])
        response = self.client.responses.create(model=self.model, instructions=SUMMARY_INSTRUCTIONS, input=prompt)
        match = re.search(r"\{.*\}", response.output_text or "", re.DOTALL)
        if not match:
            return None
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return {
            "title": str(data.get("title", "")).strip()[:40] or "대화",
            "summary": [str(x).strip() for x in data.get("summary", []) if str(x).strip()][:8],
            "todos": [str(x).strip() for x in data.get("todos", []) if str(x).strip()][:10],
        }


class ConversationLog:
    """Shared by all glasses sessions of the server."""

    def __init__(
        self,
        directory: Path,
        summarizer: Summarizer | None,
        notes: Callable[[], str] = lambda: "",
        idle_s: float = IDLE_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.dir = directory
        self.summarizer = summarizer
        self.notes = notes
        self.idle_s = idle_s
        self.clock = clock
        self.current: dict | None = None
        self.last_activity = 0.0
        self.lock = asyncio.Lock()

    # region recording

    def _path(self, conv_id: str) -> Path:
        return self.dir / f"{conv_id}.json"

    def _save(self, conv: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self._path(conv["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(conv, ensure_ascii=False))
        tmp.replace(self._path(conv["id"]))

    async def _current(self) -> dict:
        if self.current is not None and self.clock() - self.last_activity > self.idle_s:
            await self._finish()
        if self.current is None:
            started = datetime.now(timezone.utc)
            self.current = {
                "id": started.strftime("%Y%m%d-%H%M%S"), "startedAt": started.isoformat(),
                "endedAt": None, "lines": [], "cards": [], "summary": None,
            }
            log.info("history: conversation %s started", self.current["id"])
        self.last_activity = self.clock()
        return self.current

    async def add_line(self, text: str, translation: str | None = None) -> None:
        async with self.lock:
            conv = await self._current()
            entry = {"t": _now_iso(), "text": text}
            if translation:
                entry["translation"] = translation
            conv["lines"].append(entry)
            self._save(conv)

    async def add_translation(self, text: str, translation: str) -> None:
        """A translation for a caption already recorded: attach it to that line."""
        async with self.lock:
            if self.current is None:
                return
            for entry in reversed(self.current["lines"][-10:]):
                if entry["text"] == text:
                    entry["translation"] = translation
                    self._save(self.current)
                    return

    async def add_card(self, title: str, text: str) -> None:
        async with self.lock:
            conv = await self._current()
            conv["cards"].append({"t": _now_iso(), "title": title, "text": text})
            self._save(conv)

    # endregion

    # region ending and summarizing

    async def tick(self) -> None:
        """Called periodically: closes a conversation that has gone quiet."""
        async with self.lock:
            if self.current is not None and self.clock() - self.last_activity > self.idle_s:
                await self._finish()

    async def _finish(self) -> None:
        conv, self.current = self.current, None
        if conv is None:
            return
        conv["endedAt"] = _now_iso()
        self._save(conv)
        log.info("history: conversation %s ended (%d lines)", conv["id"], len(conv["lines"]))
        if self.summarizer is not None and conv["lines"]:
            asyncio.create_task(self._summarize(conv))

    async def _summarize(self, conv: dict) -> None:
        lines = [
            e["text"] + (f" (번역: {e['translation']})" if e.get("translation") else "") for e in conv["lines"]
        ]
        try:
            summary = await asyncio.to_thread(self.summarizer.summarize, lines, self.notes())
        except Exception:
            log.exception("history: summary failed for %s", conv["id"])
            return
        if summary:
            conv["summary"] = summary
            self._save(conv)
            log.info("history: %s summarized: %s", conv["id"], summary.get("title"))

    async def run(self, every_s: float = 30.0) -> None:
        while True:
            await asyncio.sleep(every_s)
            try:
                await self.tick()
            except Exception:
                log.exception("history tick failed")

    # endregion

    # region reading

    def list(self) -> list[dict]:
        out = []
        for path in sorted(self.dir.glob("*.json"), reverse=True)[:200]:
            try:
                conv = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            summary = conv.get("summary") or {}
            first = conv["lines"][0]["text"] if conv.get("lines") else ""
            out.append({
                "id": conv["id"], "title": summary.get("title") or first[:30] or "(빈 대화)",
                "startedAt": conv.get("startedAt"), "endedAt": conv.get("endedAt"),
                "lines": len(conv.get("lines", [])), "todos": len(summary.get("todos", [])),
                "summarized": bool(conv.get("summary")),
            })
        return out

    def get(self, conv_id: str) -> dict | None:
        if not _ID.match(conv_id):
            return None
        try:
            return json.loads(self._path(conv_id).read_text())
        except (OSError, ValueError):
            return None

    # endregion


def history_from_env(notes: Callable[[], str]) -> ConversationLog | None:
    if os.environ.get("HISTORY", "1") == "0":
        return None
    summarizer = OpenAISummarizer() if os.environ.get("OPENAI_API_KEY") else None
    return ConversationLog(Path(os.environ.get("HISTORY_DIR", "/data/conversations")), summarizer, notes)


def history_router(store: ConversationLog, token: str) -> APIRouter:
    router = APIRouter()

    def require(request: Request) -> None:
        header = request.headers.get("authorization", "")
        given = header[7:] if header.lower().startswith("bearer ") else ""
        if token and not hmac.compare_digest(given.encode(), token.encode()):
            raise HTTPException(401, "unauthorized")

    @router.get("/history", response_class=HTMLResponse)
    def page() -> str:
        return PAGE

    @router.get("/api/conversations")
    def list_conversations(request: Request) -> list[dict]:
        require(request)
        return store.list()

    @router.get("/api/conversations/{conv_id}")
    def get_conversation(conv_id: str, request: Request) -> dict:
        require(request)
        conv = store.get(conv_id)
        if conv is None:
            raise HTTPException(404, "not found")
        return conv

    return router


PAGE = """<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>대화 기록</title>
<style>
  :root { color-scheme: light dark; --bg:#fff; --fg:#111; --muted:#666; --line:#e2e2e2; --card:#f6f7f6; --accent:#0a7d3c; }
  @media (prefers-color-scheme: dark) { :root { --bg:#0f1210; --fg:#e8efe9; --muted:#9aa59c; --line:#2a332c; --card:#161b17; --accent:#3ddc84; } }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.5 -apple-system, system-ui, sans-serif; }
  main { max-width:680px; margin:0 auto; padding:16px; }
  h1 { font-size:20px; margin:8px 0 12px; } h2 { font-size:17px; margin:20px 0 8px; }
  a { color:inherit; text-decoration:none; }
  .item { display:block; padding:12px 14px; border:1px solid var(--line); border-radius:12px; margin-bottom:8px; }
  .item .meta, .muted { color:var(--muted); font-size:13px; }
  ul { padding-left:20px; margin:4px 0; } li { margin:3px 0; }
  .card { background:var(--card); border-radius:10px; padding:8px 12px; margin:6px 0; font-size:14px; }
  .line { padding:4px 0; border-bottom:1px solid var(--line); font-size:15px; }
  .line .tr { color:var(--muted); }
  .line time { color:var(--muted); font-size:12px; margin-right:6px; }
  input { width:100%; box-sizing:border-box; padding:10px 12px; font:inherit; color:var(--fg);
          background:transparent; border:1px solid var(--line); border-radius:10px; }
  button { padding:10px 18px; font:inherit; font-weight:600; border:0; border-radius:10px; background:var(--accent); color:#fff; margin-top:10px; }
  .back { color:var(--accent); font-size:15px; }
  [hidden] { display:none !important; }
</style></head>
<body><main>
  <section id="login" hidden>
    <h1>대화 기록</h1>
    <input id="token" type="password" placeholder="서버 토큰 (CONVERSATE_TOKEN)" autocomplete="off">
    <button id="saveToken">확인</button>
  </section>
  <section id="list" hidden><h1>대화 기록</h1><div id="items"></div></section>
  <section id="detail" hidden></section>
</main>
<script>
const KEY = "conversate-token";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function token() { try { return localStorage.getItem(KEY) || ""; } catch { return ""; } }
function setToken(t) { try { localStorage.setItem(KEY, t); } catch {} }
async function api(path) {
  const r = await fetch(path, { headers: { "Authorization": "Bearer " + token() } });
  if (r.status === 401) throw new Error("unauthorized");
  if (!r.ok) throw new Error("HTTP " + r.status);
  return r.json();
}
const fmt = (iso) => iso ? new Date(iso).toLocaleString("ko-KR", {month:"numeric", day:"numeric", hour:"2-digit", minute:"2-digit"}) : "";
const hm = (iso) => iso ? new Date(iso).toLocaleTimeString("ko-KR", {hour:"2-digit", minute:"2-digit"}) : "";
function show(id) { for (const s of ["login","list","detail"]) $(s).hidden = s !== id; }
async function route() {
  const id = location.hash.slice(1);
  try {
    if (id) {
      const c = await api("/api/conversations/" + encodeURIComponent(id));
      const s = c.summary;
      let h = `<a class="back" href="#">← 목록</a><h1>${esc(s?.title || "대화")}</h1>`;
      h += `<div class="muted">${fmt(c.startedAt)} · 자막 ${c.lines.length}줄${c.endedAt ? "" : " · 진행 중"}</div>`;
      if (s) {
        h += `<h2>요약</h2><ul>${s.summary.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`;
        if (s.todos.length) h += `<h2>할 일</h2><ul>${s.todos.map(x => `<li>${esc(x)}</li>`).join("")}</ul>`;
      } else {
        h += `<p class="muted">${c.endedAt ? "요약을 만드는 중이거나 만들지 못했어요." : "대화가 끝나고 3분이 지나면 요약이 만들어져요."}</p>`;
      }
      if (c.cards.length) h += `<h2>AI 카드</h2>` + c.cards.map(k => `<div class="card"><b>${esc(k.title)}</b><br>${esc(k.text)}</div>`).join("");
      h += `<h2>자막</h2>` + c.lines.map(l => `<div class="line"><time>${hm(l.t)}</time>${esc(l.text)}${l.translation ? `<div class="tr">${esc(l.translation)}</div>` : ""}</div>`).join("");
      $("detail").innerHTML = h; show("detail");
    } else {
      const list = await api("/api/conversations");
      $("items").innerHTML = list.length ? list.map(c => `<a class="item" href="#${esc(c.id)}"><b>${esc(c.title)}</b>
        <div class="meta">${fmt(c.startedAt)} · 자막 ${c.lines}줄${c.todos ? " · 할 일 " + c.todos : ""}${c.endedAt ? "" : " · 진행 중"}</div></a>`).join("")
        : `<p class="muted">아직 기록이 없어요.</p>`;
      show("list");
    }
  } catch (e) { show("login"); }
}
$("saveToken").onclick = () => { setToken($("token").value.trim()); route(); };
window.addEventListener("hashchange", route);
route();
</script>
</body></html>
"""
