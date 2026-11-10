"""Настройки и пути.

Ресурсы (static/, помощник захвата звука) лежат рядом с кодом или внутри LakonIQ.app.
Данные пользователя (база, логи, .env) — всегда в ~/Library/Application Support/LakonIQ:
внутрь .app писать нельзя, а так у собранного приложения и запуска из исходников одни и те же данные.
"""
import os
import shutil
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

APP_NAME = "LakonIQ"
FROZEN = bool(getattr(sys, "frozen", False))          # запущено как собранное .app
ROOT = Path(__file__).resolve().parent.parent          # папка проекта (при запуске из исходников)
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", ROOT))   # static/ и native/bin — внутри .app или в проекте

APP_SUPPORT = Path.home() / "Library" / "Application Support"
# INTERVIEW_DATA — другая папка данных (например, для тестов), чтобы не трогать рабочие
DATA_DIR = Path(os.getenv("INTERVIEW_DATA") or APP_SUPPORT / APP_NAME)
DATA_DIR.mkdir(parents=True, exist_ok=True)
# Где данные лежали раньше (по порядку): до переименования в LakonIQ и до сборки в .app
LEGACY_DIRS = [APP_SUPPORT / "Interview Copilot", ROOT / "data"]


def _migrate_legacy_db(target: Path) -> None:
    """Один раз переносим базу и логи из старой папки (копией через backup — даже если база открыта)."""
    if target.exists() or os.getenv("INTERVIEW_DATA"):
        return
    for old_dir in LEGACY_DIRS:
        old_db = old_dir / "interview.db"
        if not old_db.exists() or (FROZEN and old_dir == ROOT / "data"):
            continue
        src = sqlite3.connect(old_db)
        dst = sqlite3.connect(target)
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        if (old_dir / "logs").exists() and not (DATA_DIR / "logs").exists():
            shutil.copytree(old_dir / "logs", DATA_DIR / "logs")
        if (old_dir / ".env").exists() and not (DATA_DIR / ".env").exists():
            shutil.copy2(old_dir / ".env", DATA_DIR / ".env")
        return


load_dotenv(DATA_DIR / ".env")
if not FROZEN:
    load_dotenv(ROOT / ".env")

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "").strip()
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-opus-5-5").strip()
CLAUDE_EFFORT = os.getenv("CLAUDE_EFFORT", "low").strip()
DEEPGRAM_MODEL = os.getenv("DEEPGRAM_MODEL", "nova-3").strip()
HOST = "127.0.0.1"  # только локально, наружу сервер не торчит
PORT = int(os.getenv("PORT", "8765"))  # может смениться при запуске, если порт занят (см. main.py)

# INTERVIEW_DB — отдельная база (например, для тестов), чтобы не трогать рабочие данные
DB_PATH = Path(os.getenv("INTERVIEW_DB") or DATA_DIR / "interview.db")
if not os.getenv("INTERVIEW_DB"):
    _migrate_legacy_db(DB_PATH)
STATIC_DIR = RESOURCE_DIR / "static"
