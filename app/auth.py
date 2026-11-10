"""Вход через аккаунт Claude (OAuth в браузере через официальный Anthropic CLI `ant`).

После `ant auth login` токен хранится в ~/.config/anthropic/, SDK подхватывает его сам
и обновляет. Ключ в .env не нужен. Оплата — по тарифам Claude API аккаунта
platform.claude.com (подписки Pro/Max сторонним приложениям недоступны).
"""
from __future__ import annotations

import asyncio
import os
import re
import shutil

import anthropic

from . import llm
from .config import CLAUDE_MODEL

ANT = shutil.which("ant") or next(
    (p for p in ("/opt/homebrew/bin/ant", "/usr/local/bin/ant") if os.path.exists(p)), None
)


class Auth:
    def __init__(self) -> None:
        self.ok = False
        self.method: str | None = None   # "claude" | "api_key"
        self.account = ""                 # email аккаунта Claude
        self.billing_error = False        # вход есть, но на балансе нет средств
        self.error = ""
        self.busy = False                 # идёт вход в браузере
        self.checked = False              # первая проверка завершена
        self._proc: asyncio.subprocess.Process | None = None

    def public(self) -> dict:
        return {"ok": self.ok, "method": self.method, "error": self.error, "busy": self.busy,
                "checked": self.checked, "cli": bool(ANT), "account": self.account,
                "billing_error": self.billing_error}

    async def check(self) -> dict:
        """Проверяем доступ настоящим запросом к API (дёшево: метаданные модели)."""
        self.method = "api_key" if os.getenv("ANTHROPIC_API_KEY") else "claude"
        llm.reset_client()
        self.billing_error = False
        try:
            await llm.get_client().models.retrieve(CLAUDE_MODEL)
            self.ok, self.error = True, ""
            self.billing_error = await self._billing_probe()
            self.account = await self._account_email()
        except anthropic.AuthenticationError:
            self.ok, self.error = False, "Сессия истекла — войди снова."
        except anthropic.PermissionDeniedError as e:
            self.ok, self.error = False, f"Нет доступа к модели {CLAUDE_MODEL}: {e.message}"
        except anthropic.APIConnectionError:
            self.ok, self.error = False, "Нет соединения с Anthropic API."
        except Exception as e:  # noqa: BLE001 — нет учётных данных вовсе
            self.ok = False
            msg = str(e)
            self.error = "" if "auth" in msg.lower() or "credential" in msg.lower() else msg
        self.checked = True
        return self.public()

    async def _billing_probe(self) -> bool:
        """Есть ли средства на балансе: запрос на 1 токен (стоит меньше сотой цента).
        Без средств API сразу отвечает 400 «credit balance is too low» — и это бесплатно."""
        try:
            await llm.get_client().messages.create(
                model=CLAUDE_MODEL, max_tokens=1, output_config={"effort": "low"},
                messages=[{"role": "user", "content": "ok"}],
            )
        except anthropic.BadRequestError as e:
            return "credit balance" in str(e.message).lower()
        except Exception:  # noqa: BLE001 — любая другая ошибка не означает пустой баланс
            return False
        return False

    async def _account_email(self) -> str:
        if self.method != "claude" or not ANT:
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                ANT, "auth", "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            m = re.search(r"Logged in to .+? as (\S+@\S+)", out.decode(errors="replace"))
            return m.group(1) if m else ""
        except Exception:  # noqa: BLE001
            return ""

    async def login(self) -> dict:
        if not ANT:
            self.error = "Не найден Anthropic CLI. Установи: brew install anthropics/tap/ant"
            return self.public()
        if self.busy:
            return self.public()
        self.busy, self.error = True, ""
        try:
            env = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
            self._proc = await asyncio.create_subprocess_exec(
                ANT, "auth", "login", "--timeout", "5m",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env,
            )
            out, _ = await self._proc.communicate()
            if self._proc.returncode != 0:
                tail = out.decode(errors="replace").strip().splitlines()[-1:] or ["вход отменён"]
                self.error = f"Вход не выполнен: {tail[0]}"
        finally:
            self._proc = None
            self.busy = False
        if not self.error:
            await self.check()
        return self.public()

    def cancel(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.kill()

    async def logout(self) -> dict:
        if ANT:
            proc = await asyncio.create_subprocess_exec(
                ANT, "auth", "logout", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await proc.wait()
        self.ok, self.method, self.error, self.account = False, None, "", ""
        llm.reset_client()
        return await self.check()


auth = Auth()
