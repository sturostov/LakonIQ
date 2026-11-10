"""Собрать банк ответов для собеседований: вопросы из kb/qa_questions.txt → ответы Claude в формате LakonIQ
(с учётом роли и резюме из настроек) → ~/Library/Application Support/LakonIQ/kb/qa_kb.json.

Пакетный режим Claude API (Message Batches): −50% к цене, результат обычно за 15–60 минут.
Генерирует только вопросы без готового ответа, так что повторный запуск дешёвый.

    .venv/bin/python tools/build_kb.py            # отправить недостающие вопросы и дождаться ответов
    .venv/bin/python tools/build_kb.py --resume   # дождаться последнего отправленного пакета
    .venv/bin/python tools/build_kb.py --dry-run  # только посчитать, что будет отправлено
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import anthropic  # noqa: E402

from app import config, kb, llm  # noqa: E402  (app.db не импортируем: он открывает базу на запись)

MODEL = "claude-opus-5-5"
EFFORT = "high"  # в пакете скорость не важна — берём качество
BATCH_FILE = kb.KB_FILE.parent / "last_batch.json"


def user_settings() -> dict:
    """Настройки пользователя — только чтение базы."""
    con = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    s = {k: json.loads(v) for k, v in con.execute("SELECT key, value FROM settings")}
    con.close()
    s.update(answer_style="live", answer_depth="auto")  # формат как у живых подсказок
    return s


def request_for(item: dict, s: dict) -> dict:
    """Тот же запрос, что делает приложение для живого ответа (stream_answer), без контекста разговора."""
    q = item["q"]
    text = f"<interviewer_question>\n{q}\n</interviewer_question>"
    if llm.EXPERIENCE_Q.search(q) and not (s.get("resume") or "").strip():
        text += llm.NO_RESUME_NOTE
    text += llm.format_note(q, s)
    return {
        "custom_id": item["id"],
        "params": {
            "model": MODEL,
            "max_tokens": 16000,
            "system": llm._system(llm.answer_rules(s), s),
            "messages": [{"role": "user", "content": text}],
            "output_config": {"effort": EFFORT},
        },
    }


def wait(client: anthropic.Anthropic, batch_id: str):
    while True:
        b = client.messages.batches.retrieve(batch_id)
        c = b.request_counts
        print(f"{datetime.now():%H:%M:%S} {b.processing_status}: готово {c.succeeded}, в работе {c.processing}, "
              f"ошибок {c.errored}", flush=True)
        if b.processing_status == "ended":
            return b
        time.sleep(30)


def collect(client: anthropic.Anthropic, batch_id: str, items: list[dict], existing: dict) -> None:
    by_id = {i["id"]: i for i in items}
    failed, usage = [], {"in": 0, "cache": 0, "out": 0}
    for r in client.messages.batches.results(batch_id):
        item = by_id.get(r.custom_id)
        if not item:
            continue
        if r.result.type != "succeeded":
            failed.append((item["q"], r.result.type))
            continue
        msg = r.result.message
        usage["in"] += msg.usage.input_tokens
        usage["cache"] += (msg.usage.cache_read_input_tokens or 0) + (msg.usage.cache_creation_input_tokens or 0)
        usage["out"] += msg.usage.output_tokens
        text = "".join(b.text for b in msg.content if b.type == "text").strip()
        if msg.stop_reason != "end_turn" or not text:
            failed.append((item["q"], msg.stop_reason))
            continue
        existing[item["id"]] = {**item, "answer": text, "model": MODEL, "effort": EFFORT,
                                "created": datetime.now().isoformat(timespec="seconds")}
    order = {i["id"]: n for n, i in enumerate(items)}
    kb.save(sorted(existing.values(), key=lambda i: order.get(i["id"], 1 << 30)))
    print(f"\nВ базе ответов: {len(existing)} из {len(items)} → {kb.KB_FILE}")
    print(f"Токены: вход {usage['in']}, кэш {usage['cache']}, выход {usage['out']}")
    for q, why in failed:
        print(f"  не получилось ({why}): {q}")
    if failed:
        print("Запусти скрипт ещё раз — он отправит только эти вопросы.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    items = kb.load_questions(ROOT / "kb" / "qa_questions.txt")
    existing = {i["id"]: i for i in kb.load()}
    client = anthropic.Anthropic()

    if args.resume:
        batch_id = json.loads(BATCH_FILE.read_text())["id"]
    else:
        todo = [i for i in items if i["id"] not in existing]
        print(f"Вопросов: {len(items)}, уже с ответом: {len(existing)}, отправляю: {len(todo)}")
        if not todo or args.dry_run:
            return
        s = user_settings()
        batch = client.messages.batches.create(requests=[request_for(i, s) for i in todo])
        batch_id = batch.id
        BATCH_FILE.parent.mkdir(parents=True, exist_ok=True)
        BATCH_FILE.write_text(json.dumps({"id": batch_id, "created": datetime.now().isoformat(), "count": len(todo)}))
        print(f"Пакет {batch_id} отправлен")
    wait(client, batch_id)
    collect(client, batch_id, items, existing)


if __name__ == "__main__":
    main()
