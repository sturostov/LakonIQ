"""Логи для отладки: data/logs/app.log (события) и data/logs/stacks.log (стеки потоков при зависании).

Если приложение повисло, стеки всех потоков можно снять командой:  kill -USR1 <pid>
"""
from __future__ import annotations

import faulthandler
import logging
import logging.handlers
import signal

from .config import DATA_DIR

LOG_DIR = DATA_DIR / "logs"
APP_LOG = LOG_DIR / "app.log"
STACKS_LOG = LOG_DIR / "stacks.log"
_stacks_file = None

log = logging.getLogger("copilot")


def setup_logging() -> None:
    global _stacks_file
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(APP_LOG, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)-7s %(threadName)-18s %(name)s: %(message)s",
                                           "%H:%M:%S"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "httpx2", "websockets", "uvicorn.access", "openai", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _stacks_file = open(STACKS_LOG, "a", encoding="utf-8")  # noqa: SIM115 — держим открытым для faulthandler
    faulthandler.enable(file=_stacks_file, all_threads=True)          # падения
    faulthandler.register(signal.SIGUSR1, file=_stacks_file, all_threads=True)  # kill -USR1 <pid>
    log.info("=" * 20 + " запуск приложения " + "=" * 20)


def arm_hang_watchdog(seconds: float, reason: str) -> None:
    """Если процесс не завершится за `seconds`, записать стеки всех потоков — увидим, где завис."""
    if _stacks_file:
        _stacks_file.write(f"\n--- watchdog: {reason}; стеки, если через {seconds:.0f} с процесс ещё жив ---\n")
        _stacks_file.flush()
        faulthandler.dump_traceback_later(seconds, repeat=False, file=_stacks_file)
