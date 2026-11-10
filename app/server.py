"""HTTP/WebSocket API для интерфейса."""
from __future__ import annotations

import asyncio
import html
import logging
import re

import httpx2 as httpx  # тот же HTTP-клиент, что у SDK
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import config, db, hotkeys, kb, providers
from .audio import list_input_devices, refresh_devices
from .config import STATIC_DIR
from .auth import auth
from .engine import engine
from .login import account_label, connect, login
from .logs import log


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine.loop = asyncio.get_running_loop()
    db.close_orphan_sessions()  # прошлый запуск закрыли во время прослушивания

    async def initial_auth_check():
        await auth.check()
        await engine.broadcast(engine.state())

    asyncio.create_task(initial_auth_check())
    yield
    await engine.stop()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# Защита локального сервера. Браузер даёт любой странице подключиться к 127.0.0.1 (WebSocket — даже без CORS),
# поэтому: 1) Host — только 127.0.0.1/localhost (сайт, «переназначивший» свой домен на 127.0.0.1, не пройдёт);
# 2) WebSocket и изменяющие запросы — только со страницы самого приложения (заголовок Origin).
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


def origin_ok(origin: str | None) -> bool:
    """Своё окно — http://127.0.0.1:<порт приложения>. Нет заголовка — не браузер (локальная программа)."""
    if not origin:
        return True
    return origin in (f"http://127.0.0.1:{config.PORT}", f"http://localhost:{config.PORT}")


@app.middleware("http")
async def same_origin_only(request, call_next):
    if request.method not in ("GET", "HEAD", "OPTIONS") and not origin_ok(request.headers.get("origin")):
        log.warning("отклонён запрос %s %s с чужого сайта: %s", request.method, request.url.path,
                    request.headers.get("origin"))
        return JSONResponse({"error": "запрос с чужого сайта отклонён"}, status_code=403)
    return await call_next(request)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    if not origin_ok(ws.headers.get("origin")):
        log.warning("отклонено подключение к /ws с чужого сайта: %s", ws.headers.get("origin"))
        await ws.close(code=1008)  # до accept — браузер получит отказ (403)
        return
    await ws.accept()
    engine.clients.add(ws)
    await ws.send_json(engine.state())
    try:
        while True:
            msg = await ws.receive_json()
            asyncio.create_task(engine.handle(msg))
    except WebSocketDisconnect:
        pass
    finally:
        engine.clients.discard(ws)


@app.post("/api/log")
def client_log(body: dict = Body(...)):
    """События и ошибки JavaScript из окна приложения."""
    level = logging.WARNING if body.get("level") == "error" else logging.INFO
    log.log(level, "окно: %s", str(body.get("message", ""))[:2000])
    return {"ok": True}


@app.get("/api/hotkeys")
def hotkey_list():
    return hotkeys.help_lines()


@app.post("/api/window/show")
def window_show():
    """Повторный запуск приложения: вместо второго окна показываем уже открытое."""
    if engine.show_window:
        engine.show_window()
    return {"ok": True}


@app.post("/api/window/hide")
def window_hide():
    if engine.hide_window:
        engine.hide_window()
    return {"ok": True}


@app.get("/api/devices")
def devices():
    if not engine.listening:
        refresh_devices()  # подхватить наушники, подключённые после запуска
    return list_input_devices()


def _masked(s: dict) -> dict:
    """Ключи в интерфейс не отдаём целиком."""
    s = dict(s)
    s["api_keys"] = {k: f"••••{v[-4:]}" for k, v in (s.get("api_keys") or {}).items() if v}
    return s


@app.get("/api/settings")
def get_settings():
    return _masked(db.get_settings())


@app.put("/api/settings")
async def put_settings(values: dict = Body(...)):
    dg_key = str(values.pop("deepgram_key", "") or "").strip()
    values = {k: v for k, v in values.items() if k not in ("api_keys", "provider")}  # ИИ — только через вход
    if dg_key and not dg_key.startswith("••••"):
        # проверяем ключ Deepgram до сохранения
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get("https://api.deepgram.com/v1/projects",
                                     headers={"Authorization": f"Token {dg_key}"})
        except Exception:  # noqa: BLE001
            raise HTTPException(400, "Deepgram: нет соединения, ключ не проверен.")
        if r.status_code in (401, 403):
            raise HTTPException(400, "Deepgram: неверный ключ.")
        keys = dict(db.get_settings().get("api_keys") or {})
        keys["deepgram"] = dg_key
        values["api_keys"] = keys
    s = db.update_settings(values)
    await engine.broadcast(engine.state())
    return _masked(s)


# ---------- вход и ИИ-провайдеры ----------
@app.get("/api/providers")
def provider_list():
    s = db.get_settings()
    keys = s.get("api_keys") or {}
    out = [{
        "id": "claude", "name": "Claude", "title": "Войти через Claude",
        "subtitle": "лучшее качество · по тарифам API", "icon": "✳", "account": True, "oauth": True,
        "note": "Вход через аккаунт Claude (оплата по тарифам Claude API).",
        "key_url": "https://platform.claude.com", "no_key": True, "vision": True,
        "configured": auth.ok, "billing_error": auth.billing_error,
    }]
    for pid, p in providers.PROVIDERS.items():
        out.append({
            "id": pid, "name": p["name"], "title": p["title"], "subtitle": p["subtitle"], "icon": p["icon"],
            "account": bool(p.get("account")), "oauth": bool(p.get("oauth")), "steps": p.get("steps", []),
            "note": p["note"], "key_url": p["key_url"], "key_hint": p.get("key_hint", ""),
            "no_key": bool(p.get("no_key")), "vision": p["vision"] is not None, "configured": bool(keys.get(pid)),
        })
    return {"current": s.get("provider") or "none", "model": s.get("model") or "",
            "account": account_label(s) if engine.ai_ready(s) else "", "providers": out}


@app.post("/api/providers/connect")
async def provider_connect(body: dict = Body(...)):
    r = await connect(body.get("provider", ""), body.get("key", ""), body.get("model", ""))
    await engine.broadcast(engine.state())
    return r


_DONE_PAGE = """<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>LakonIQ</title>
<style>body{{margin:0;min-height:100vh;display:grid;place-items:center;font:16px/1.5 -apple-system,sans-serif;
background:radial-gradient(120% 90% at 50% 0%,#73a8e8 0%,#1c38ea 55%,#0b1a6b 100%);color:#f4f4f5}}
.c{{background:rgba(24,24,27,.9);border:1px solid rgba(255,255,255,.1);border-radius:16px;padding:32px 40px;
text-align:center;max-width:420px}}h1{{font-size:22px;margin:8px 0}}p{{color:#a1a1aa;margin:0}}</style></head>
<body><div class="c"><div style="font-size:40px">{icon}</div><h1>{title}</h1><p>{text}</p></div></body></html>"""


@app.get("/oauth/openrouter", response_class=HTMLResponse)
async def openrouter_callback(code: str = "", state: str = "", error: str = ""):
    ok, msg = await login.openrouter_callback(code, state, error)
    if ok:
        page = _DONE_PAGE.format(icon="✓", title="Вход выполнен",
                                 text="Можно закрыть эту вкладку и вернуться в LakonIQ.")
    else:
        page = _DONE_PAGE.format(icon="⚠️", title="Не получилось войти", text=html.escape(msg))
    return HTMLResponse(page)


@app.get("/api/qa")
def list_qa(saved: bool = False, q: str = "", session_id: int | None = None):
    return db.list_qa(saved_only=saved, query=q, session_id=session_id)


@app.get("/api/kb")
def kb_list(q: str = "", cat: str = ""):
    """Банк ответов: всё или поиск — сначала похожие по смыслу вопросы, потом совпадения по тексту."""
    everything = kb.load()
    cats: dict[str, int] = {}
    for i in everything:
        cats[i["cat"]] = cats.get(i["cat"], 0) + 1
    items = [i for i in everything if not cat or i["cat"] == cat]
    q = q.strip()
    if q:
        idx = kb.index()
        similar = [it for s, it in (idx.search(q, 8) if idx else []) if s >= 0.45]
        word = re.compile(r"(?<![\w])" + re.escape(q.lower()))  # с начала слова: «put» не найдёт «input»
        text = [i for i in items if word.search((i["q"] + " " + i["answer"] + " " + i["cat"]).lower())]
        seen, items = set(), []
        for i in similar + text:
            if i["id"] not in seen:
                seen.add(i["id"])
                items.append(i)
    if q and cat:
        items = [i for i in items if i["cat"] == cat]
    return {"total": len(everything), "found": len(items), "cats": list(cats.items()),
            "items": [{k: i[k] for k in ("id", "cat", "q", "answer")} for i in items[:120]]}


@app.post("/api/kb/{item_id}/save")
async def kb_save(item_id: str):
    """Ответ из банка → в личную базу (★) — он попадёт в «Заучивание»."""
    item = next((i for i in kb.load() if i["id"] == item_id), None)
    if not item:
        raise HTTPException(404)
    qa_id = db.create_qa(item["q"], "kb", None, True)
    qa = db.update_qa(qa_id, {"answer": item["answer"], "tags": item["cat"], "saved": True})
    await engine.broadcast({"type": "qa_updated", "qa": qa})
    return qa


@app.patch("/api/qa/{qa_id}")
async def patch_qa(qa_id: int, fields: dict = Body(...)):
    item = db.update_qa(qa_id, fields)
    if not item:
        raise HTTPException(404)
    await engine.broadcast({"type": "qa_updated", "qa": item})
    return item


@app.delete("/api/qa/{qa_id}")
async def delete_qa(qa_id: int):
    task = engine.answer_tasks.get(qa_id)
    if task:
        task.cancel()
    db.delete_qa(qa_id)
    await engine.broadcast({"type": "answer_removed", "id": qa_id})
    return {"ok": True}


@app.get("/api/study")
def study():
    return db.study_queue()


@app.post("/api/study/{qa_id}")
def study_grade(qa_id: int, body: dict = Body(...)):
    result = body.get("result", "good")
    if result not in ("again", "hard", "good"):
        raise HTTPException(400, "result: again | hard | good")
    item = db.grade(qa_id, result)
    if not item:
        raise HTTPException(404)
    return item


@app.get("/api/sessions")
def sessions():
    return db.list_sessions()


@app.get("/api/sessions/{session_id}")
def session(session_id: int):
    s = db.get_session(session_id)
    if not s:
        raise HTTPException(404)
    return s


@app.get("/api/export.md", response_class=PlainTextResponse)
def export_md(saved: bool = True):
    items = db.list_qa(saved_only=saved, limit=100000)[::-1]
    parts = ["# База ответов к собеседованиям\n"]
    for i, q in enumerate(items, 1):
        parts.append(f"## {i}. {q['question']}\n")
        if q["tags"]:
            parts.append(f"_Теги: {q['tags']}_\n")
        parts.append(q["answer"].strip() + "\n")
        if q["notes"].strip():
            parts.append(f"> **Мои заметки:** {q['notes'].strip()}\n")
    return PlainTextResponse(
        "\n".join(parts),
        headers={"Content-Disposition": 'attachment; filename="interview-answers.md"'},
    )
