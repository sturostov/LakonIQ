"""SQLite: сессии, пары вопрос→ответ с заметками, интервальное повторение, настройки."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from typing import Any

from .config import DB_PATH

_lock = threading.Lock()
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

_conn.executescript(
    """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        transcript TEXT NOT NULL DEFAULT '',
        review TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS qa (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id INTEGER,
        source TEXT NOT NULL DEFAULT 'live',      -- live | manual | screenshot
        question TEXT NOT NULL,
        answer TEXT NOT NULL DEFAULT '',
        notes TEXT NOT NULL DEFAULT '',
        tags TEXT NOT NULL DEFAULT '',
        saved INTEGER NOT NULL DEFAULT 0,          -- 1 = в базе для заучивания
        box INTEGER NOT NULL DEFAULT 0,            -- коробка Лейтнера
        reviews INTEGER NOT NULL DEFAULT 0,
        next_review TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS qa_saved ON qa(saved, next_review);
    """
)

DEFAULT_SETTINGS: dict[str, Any] = {
    "role": "Python backend-разработчик (Middle/Senior)",
    "resume": "",
    "vacancy": "",
    "extra": "",
    "language": "ru",
    "interviewer_device": "system",   # звук системы — всё, что слышно в наушниках
    "me_device": "",
    "auto_answer": True,
    "auto_save": False,
    "kb_answers": True,     # сначала искать готовый ответ в банке вопросов (мгновенно, без модели)
    "smart_filter": True,   # отвечать только на вопросы, остальное — в контекст
    "answer_style": "live", # live — живая речь, strict — чётко и по делу
    "answer_depth": "auto", # auto — по типу вопроса, short — коротко, full — подробно
    "claude_effort": "auto",  # глубина размышлений Claude: auto | low | medium | high | xhigh | max
    "my_voice": "",         # образец того, как кандидат говорит сам (для подстройки стиля)
    "stt": "auto",          # auto | groq | deepgram — распознавание речи
    "provider": "claude",   # claude | groq | gemini | deepseek | openrouter | ollama
    "model": "",            # пусто = модель по умолчанию провайдера
    "api_keys": {},         # ключи провайдеров (хранятся только локально, в data/interview.db)
}

# Интервалы повторения по коробкам (в днях)
BOX_DAYS = [0, 1, 3, 7, 16, 35, 75]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r else None


def _exec(sql: str, args: tuple = ()) -> sqlite3.Cursor:
    with _lock:
        cur = _conn.execute(sql, args)
        _conn.commit()
        return cur


def _all(sql: str, args: tuple = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in _conn.execute(sql, args).fetchall()]


# ---------- настройки ----------
def get_settings() -> dict:
    out = dict(DEFAULT_SETTINGS)
    for r in _all("SELECT key, value FROM settings"):
        out[r["key"]] = json.loads(r["value"])
    return out


def update_settings(values: dict) -> dict:
    for k, v in values.items():
        if k in DEFAULT_SETTINGS:
            _exec(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (k, json.dumps(v, ensure_ascii=False)),
            )
    return get_settings()


# ---------- сессии ----------
def create_session() -> int:
    title = datetime.now().strftime("Собеседование %d.%m.%Y %H:%M")
    return _exec("INSERT INTO sessions(title, started_at) VALUES(?, ?)", (title, now())).lastrowid


def end_session(session_id: int, transcript: str) -> None:
    _exec("UPDATE sessions SET ended_at = ?, transcript = ? WHERE id = ?", (now(), transcript, session_id))


def append_transcript(session_id: int, line: str) -> None:
    """Каждая реплика сразу в базу — расшифровка не теряется, даже если приложение закрыли без «Стоп»."""
    _exec("UPDATE sessions SET transcript = CASE WHEN transcript = '' THEN ? ELSE transcript || char(10) || ? END "
          "WHERE id = ?", (line, line, session_id))


def close_orphan_sessions() -> None:
    """Сессии, оставшиеся «идущими» после прошлого закрытия приложения, помечаем завершёнными."""
    _exec("UPDATE sessions SET ended_at = started_at WHERE ended_at IS NULL")


def set_session_review(session_id: int, review: str) -> None:
    _exec("UPDATE sessions SET review = ? WHERE id = ?", (review, session_id))


def get_session(session_id: int) -> dict | None:
    with _lock:
        return _row(_conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone())


def list_sessions() -> list[dict]:
    return _all(
        "SELECT s.id, s.title, s.started_at, s.ended_at, length(s.review) > 0 AS has_review, "
        "(SELECT count(*) FROM qa WHERE qa.session_id = s.id) AS qa_count "
        "FROM sessions s ORDER BY s.id DESC"
    )


# ---------- вопросы/ответы ----------
def create_qa(question: str, source: str, session_id: int | None, saved: bool) -> int:
    ts = now()
    return _exec(
        "INSERT INTO qa(session_id, source, question, saved, next_review, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?, ?, ?)",
        (session_id, source, question, int(saved), ts, ts, ts),
    ).lastrowid


def get_qa(qa_id: int) -> dict | None:
    with _lock:
        return _row(_conn.execute("SELECT * FROM qa WHERE id = ?", (qa_id,)).fetchone())


def update_qa(qa_id: int, fields: dict) -> dict | None:
    allowed = {"question", "answer", "notes", "tags", "saved"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if fields:
        if "saved" in fields:
            fields["saved"] = int(bool(fields["saved"]))
        cols = ", ".join(f"{k} = ?" for k in fields)
        _exec(f"UPDATE qa SET {cols}, updated_at = ? WHERE id = ?", (*fields.values(), now(), qa_id))
    return get_qa(qa_id)


def delete_qa(qa_id: int) -> None:
    _exec("DELETE FROM qa WHERE id = ?", (qa_id,))


def list_qa(saved_only: bool = False, query: str = "", session_id: int | None = None, limit: int = 500) -> list[dict]:
    sql = "SELECT * FROM qa WHERE 1=1"
    args: list = []
    if saved_only:
        sql += " AND saved = 1"
    if session_id is not None:
        sql += " AND session_id = ?"
        args.append(session_id)
    if query:
        sql += " AND (question LIKE ? OR answer LIKE ? OR notes LIKE ? OR tags LIKE ?)"
        args += [f"%{query}%"] * 4
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(limit)
    return _all(sql, tuple(args))


# ---------- заучивание (система Лейтнера) ----------
def study_queue() -> dict:
    ts = now()
    due = _all(
        "SELECT * FROM qa WHERE saved = 1 AND answer != '' AND (next_review IS NULL OR next_review <= ?) "
        "ORDER BY box ASC, next_review ASC LIMIT 200",
        (ts,),
    )
    total = _all("SELECT count(*) AS n FROM qa WHERE saved = 1 AND answer != ''")[0]["n"]
    return {"due": due, "total": total}


def grade(qa_id: int, result: str) -> dict | None:
    item = get_qa(qa_id)
    if not item:
        return None
    box = item["box"]
    if result == "again":
        box, delta = 0, timedelta(minutes=10)
    elif result == "hard":
        box = max(1, box)
        delta = timedelta(days=BOX_DAYS[box] / 2 or 0.5)
    else:  # good
        box = min(box + 1, len(BOX_DAYS) - 1)
        delta = timedelta(days=BOX_DAYS[box])
    nxt = (datetime.now() + delta).isoformat(timespec="seconds")
    _exec(
        "UPDATE qa SET box = ?, reviews = reviews + 1, next_review = ?, updated_at = ? WHERE id = ?",
        (box, nxt, now(), qa_id),
    )
    return get_qa(qa_id)
