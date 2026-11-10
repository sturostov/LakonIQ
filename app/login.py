"""Авторизация при запуске: вход через существующие аккаунты и по API-ключу.

- Claude      — OAuth в браузере через официальный Anthropic CLI (`ant auth login`).
- OpenRouter  — OAuth PKCE: вход через Google / GitHub / почту, ключ создаётся автоматически.
- Google, Groq — у них нет входа для сторонних приложений, поэтому открываем страницу ключей
                (где человек уже вошёл своим аккаунтом) и подхватываем ключ из буфера обмена,
                как только его скопируют. Буфер читается только во время такого входа (до 5 минут)
                и только ради строки, похожей на ключ этого провайдера.
- По API-ключу — вручную для любого провайдера; Ollama — без ключа.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import secrets
import subprocess
import time
import webbrowser
from typing import Awaitable, Callable
from urllib.parse import urlencode

import httpx2 as httpx  # HTTP-клиент, на котором работают SDK anthropic и openai

from . import db, providers
from .auth import auth
from . import config

OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_EXCHANGE_URL = "https://openrouter.ai/api/v1/auth/keys"
CALLBACK_PATH = "/oauth/openrouter"
CLIPBOARD_TIMEOUT = 300  # секунд ждём, пока человек скопирует ключ


# ---------- подключение провайдера (общее для всех способов входа) ----------
async def connect(pid: str, key: str = "", model: str = "") -> dict:
    s = db.get_settings()
    if pid == "claude":
        if not auth.ok or auth.billing_error:
            await auth.check()
        if not auth.ok:
            return {"ok": False, "error": "Сначала войди через Claude."}
        if auth.billing_error:
            return {"ok": False, "error": "Вход выполнен, но на балансе Claude нет средств. Пополни: "
                                          "platform.claude.com → Settings → Billing — или выбери бесплатный способ."}
        db.update_settings({"provider": "claude", "model": model})
        return {"ok": True, "models": [], "model": model}
    if pid not in providers.PROVIDERS:
        return {"ok": False, "error": "Неизвестный провайдер."}
    keys = dict(s.get("api_keys") or {})
    key = (key or "").strip()
    if key.startswith("••••"):
        key = ""
    key = key or keys.get(pid, "")
    try:
        info = await providers.list_models(pid, key)
    except providers.LLMError as e:
        return {"ok": False, "error": str(e)}
    chosen = model if model in info["models"] else info["default"]
    if key:
        keys[pid] = key
    db.update_settings({"provider": pid, "model": chosen, "api_keys": keys})
    return {"ok": True, "models": info["models"], "model": chosen}


def account_label(s: dict) -> str:
    """Подпись «вошли как …» для меню и экрана входа."""
    pid = s.get("provider") or ""
    if pid == "claude":
        return f"Claude · {auth.account}" if auth.account else "Claude"
    if pid not in providers.PROVIDERS:
        return ""
    p = providers.PROVIDERS[pid]
    if p.get("no_key"):
        return f"{p['name']} · {s.get('model') or ''}".rstrip(" ·")
    key = (s.get("api_keys") or {}).get(pid, "")
    return f"{p['name']} · ключ ••••{key[-4:]}" if key else p["name"]


async def logout() -> None:
    """Выйти из текущего способа входа: Claude — выход из аккаунта, остальные — удалить ключ."""
    s = db.get_settings()
    pid = s.get("provider") or ""
    if pid == "claude":
        await auth.logout()
    elif pid in providers.PROVIDERS:
        keys = {k: v for k, v in (s.get("api_keys") or {}).items() if k != pid}
        db.update_settings({"api_keys": keys})
    db.update_settings({"provider": "none", "model": ""})


def _pbpaste() -> str:
    try:
        return subprocess.run(["pbpaste"], capture_output=True, text=True, timeout=2).stdout[:4000]
    except Exception:  # noqa: BLE001
        return ""


# ---------- процессы входа ----------
class Login:
    def __init__(self) -> None:
        self.provider: str | None = None
        self.status = "idle"   # idle | waiting | waiting_key | checking | done | error
        self.message = ""
        self.task: asyncio.Task | None = None
        self.notify: Callable[[], Awaitable[None]] | None = None  # ставит engine
        self._verifier = ""
        self._state = ""

    def public(self) -> dict:
        return {"provider": self.provider, "status": self.status, "message": self.message}

    async def _set(self, status: str, message: str = "") -> None:
        self.status, self.message = status, message
        if self.notify:
            await self.notify()

    async def start(self, pid: str) -> None:
        await self.cancel(silent=True)
        self.provider = pid
        if pid == "claude":
            self.task = asyncio.create_task(self._claude())
        elif pid == "openrouter":
            await self._openrouter_start()
        elif pid in providers.PROVIDERS and providers.PROVIDERS[pid].get("key_pattern"):
            self.task = asyncio.create_task(self._clipboard(pid))
        else:
            self.task = asyncio.create_task(self._finish(pid, ""))

    async def with_key(self, pid: str, key: str) -> None:
        """Вход по API-ключу, вставленному вручную."""
        await self.cancel(silent=True)
        self.provider = pid
        await self._set("checking", "Проверяю ключ…")
        await self._finish(pid, key)

    async def cancel(self, silent: bool = False) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
        if self.provider == "claude":
            auth.cancel()
        self.task, self._verifier, self._state = None, "", ""
        self.provider, self.status, self.message = None, "idle", ""
        if not silent and self.notify:
            await self.notify()

    async def _finish(self, pid: str, key: str) -> bool:
        r = await connect(pid, key)
        if r["ok"]:
            name = "Claude" if pid == "claude" else providers.PROVIDERS[pid]["name"]
            await self._set("done", f"Вход выполнен: {name}" + (f" · {r['model']}" if r.get("model") else ""))
            return True
        await self._set("error", r["error"])
        return False

    async def _claude(self) -> None:
        await self._set("checking", "Проверяю вход в Claude…")
        await auth.check()
        if auth.ok:
            await self._finish("claude", "")
            return
        await self._set("waiting", "Подтверди вход в браузере…")
        await auth.login()
        if auth.ok:
            await self._finish("claude", "")
        else:
            await self._set("error", auth.error or "Вход не выполнен.")

    # --- OpenRouter: OAuth PKCE ---
    async def _openrouter_start(self) -> None:
        self._verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(self._verifier.encode()).digest()).rstrip(b"=").decode()
        self._state = secrets.token_urlsafe(16)
        url = OPENROUTER_AUTH_URL + "?" + urlencode({
            "callback_url": f"http://localhost:{config.PORT}{CALLBACK_PATH}",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": self._state,
            "key_label": "LakonIQ",
        })
        await self._set("waiting", "Войди в OpenRouter в браузере и нажми «Authorize»…")
        await asyncio.to_thread(webbrowser.open, url)

    async def openrouter_callback(self, code: str, state: str, error: str) -> tuple[bool, str]:
        if self.provider != "openrouter" or not self._verifier:
            return False, "Вход не ожидался — начни заново в приложении."
        if error or not code:
            await self._set("error", "Вход в OpenRouter отменён.")
            return False, "Вход отменён."
        if state and state != self._state:
            await self._set("error", "Ответ OpenRouter не прошёл проверку — попробуй ещё раз.")
            return False, "Ответ не прошёл проверку безопасности."
        await self._set("checking", "Получаю доступ…")
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                r = await client.post(OPENROUTER_EXCHANGE_URL, json={
                    "code": code, "code_verifier": self._verifier, "code_challenge_method": "S256"})
            key = r.json().get("key") if r.status_code == 200 else ""
        except Exception as e:  # noqa: BLE001
            key, r = "", None
            await self._set("error", f"OpenRouter: нет соединения ({e}).")
            return False, "Нет соединения с OpenRouter."
        if not key:
            msg = "Код входа устарел или неверен — попробуй ещё раз." if r is not None and r.status_code == 403 \
                else f"OpenRouter не выдал ключ (код {r.status_code if r is not None else '?'})."
            await self._set("error", msg)
            return False, msg
        self._verifier = ""
        ok = await self._finish("openrouter", key)
        return ok, self.message

    # --- Google / Groq / DeepSeek: страница ключей + автоподхват из буфера обмена ---
    async def _clipboard(self, pid: str) -> None:
        p = providers.PROVIDERS[pid]
        pattern = re.compile(p["key_pattern"])
        await asyncio.to_thread(webbrowser.open, p["key_url"])
        await self._set("waiting_key", "Жду, пока ты скопируешь ключ…")
        tried: set[str] = set()
        deadline = time.monotonic() + CLIPBOARD_TIMEOUT
        while time.monotonic() < deadline:
            m = pattern.search(await asyncio.to_thread(_pbpaste))
            if m and m.group(0) not in tried:
                tried.add(m.group(0))
                await self._set("checking", "Нашёл ключ, проверяю…")
                r = await connect(pid, m.group(0))
                if r["ok"]:
                    await self._set("done", f"Вход выполнен: {p['name']} · {r['model']}")
                    return
                await self._set("waiting_key", f"Ключ не подошёл ({r['error']}). Скопируй другой.")
            await asyncio.sleep(1)
        await self._set("error", "Не дождался ключа — вставь его вручную ниже.")


login = Login()
