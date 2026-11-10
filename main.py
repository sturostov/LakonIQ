"""Точка входа.

    python main.py            — плавающая панель поверх всех окон (как Cluely)
    python main.py --browser  — только сервер, интерфейс в браузере
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
import urllib.request
import webbrowser

import uvicorn

from app import hotkeys
from app.logs import arm_hang_watchdog, log, setup_logging
from app import config
from app.config import HOST
from app.engine import engine
from app.server import app

URL = f"http://{HOST}:{config.PORT}"
WIDTH = 760  # панель ~720 + поля под тени
MOVE_STEP = 60


def run_server() -> None:
    uvicorn.run(app, host=HOST, port=config.PORT, log_level="warning")


def already_running(port: int) -> bool:
    """На порту уже отвечает LakonIQ? Тогда показываем его окно вместо второго экземпляра."""
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/hotkeys", timeout=1) as r:
            if r.status != 200:
                return False
        urllib.request.urlopen(urllib.request.Request(f"http://{HOST}:{port}/api/window/show", method="POST"),
                               timeout=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def free_port(preferred: int) -> int:
    import socket

    for port in [preferred, *range(preferred + 1, preferred + 50)]:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            if sock.connect_ex((HOST, port)) != 0:
                return port
    return preferred


def wait_server(timeout: float = 20) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(URL, timeout=1)
            return
        except Exception:
            time.sleep(0.1)
    raise SystemExit(f"Сервер не запустился на {URL}")


def primary_screen():
    """Основной монитор (с панелью меню) — окно появляется сверху по центру именно на нём."""
    import webview

    screens = webview.screens
    return screens[0] if screens else None


def stop_listening() -> None:
    if engine.loop and engine.listening:
        try:
            asyncio.run_coroutine_threadsafe(engine.stop(), engine.loop).result(timeout=5)
            log.info("прослушивание остановлено, сессия сохранена")
        except Exception as e:  # noqa: BLE001
            log.warning("не удалось остановить прослушивание за 5 с: %r", e)


def screen_size() -> tuple[int, int]:
    scr = primary_screen()
    return (int(scr.width), int(scr.height)) if scr else (1440, 900)


class Overlay:
    """Управление плавающим окном: видимость, перемещение, подгонка высоты под контент."""

    def __init__(self, window) -> None:
        self.w = window
        self.visible = True
        self.screen_w, self.screen_h = screen_size()

    def toggle(self) -> None:
        (self.w.hide if self.visible else self.w.show)()
        self.visible = not self.visible

    def show(self) -> None:
        self.w.show()
        self.visible = True

    def hide(self) -> None:
        self.w.hide()
        self.visible = False

    def move(self, dx: int, dy: int) -> None:
        x = max(-WIDTH + 120, min(self.screen_w - 120, self.w.x + dx))
        y = max(0, min(self.screen_h - 60, self.w.y + dy))
        self.w.move(x, y)

    # --- функции, вызываемые из JS (window.pywebview.api.*) ---
    def fit(self, height: float) -> None:
        h = int(max(60, min(height, self.screen_h - 40)))
        if abs(h - self.w.height) > 1:
            self.w.resize(WIDTH, h)

    def set_on_top(self, value: bool) -> None:
        self.w.on_top = bool(value)

    def quit(self) -> None:
        # Сначала даём мосту pywebview вернуть ответ в JS, и только потом закрываем окно:
        # закрытие прямо внутри вызова из JS вешает pywebview на macOS.
        log.info("кнопка «Закрыть приложение»: вызов дошёл до Python, закрываю окно через 0.15 с")
        arm_hang_watchdog(10, "нажата «Закрыть приложение»")
        t = threading.Timer(0.15, self._destroy)
        t.daemon = True
        t.start()

    def _destroy(self) -> None:
        log.info("закрываю окно (destroy)")
        self.w.destroy()
        log.info("окно закрыто (destroy вернулся)")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", action="store_true", help="открыть в браузере вместо плавающего окна")
    args = parser.parse_args()

    setup_logging()
    if already_running(config.PORT):
        log.info("приложение уже запущено — показал его окно и выхожу")
        return
    global URL
    config.PORT = free_port(config.PORT)
    URL = f"http://{HOST}:{config.PORT}"
    threading.Thread(target=run_server, daemon=True).start()
    wait_server()
    print(f"LakonIQ: {URL}")
    print("Горячие клавиши:\n  " + "\n  ".join(hotkeys.help_lines()))

    overlay: Overlay | None = None

    def dispatch(action: str) -> None:
        if action == "toggle_window":
            if overlay:
                overlay.toggle()
            return
        if action.startswith("move_"):
            if overlay:
                dx, dy = {"up": (0, -MOVE_STEP), "down": (0, MOVE_STEP),
                          "left": (-MOVE_STEP, 0), "right": (MOVE_STEP, 0)}[action[5:]]
                overlay.move(dx, dy)
            return
        if action.startswith("scroll_"):
            engine.run_soon(engine.broadcast({"type": "scroll", "dir": action[7:]}))
            return
        if overlay and action in {"assist", "screenshot", "followups", "recap"}:
            overlay.show()  # ответ должен быть виден
        engine.run_soon(engine.handle({"action": action}))

    if not hotkeys.start(dispatch):
        print("⚠️  Глобальные горячие клавиши не запущены.")

    if args.browser:
        webbrowser.open(URL)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            shutdown()

    import webview

    sw, _ = screen_size()
    window = webview.create_window(
        screen=primary_screen(),
        title="LakonIQ",
        url=URL + "/?overlay=1",
        width=WIDTH,
        height=140,
        x=(sw - WIDTH) // 2,
        y=30,
        frameless=True,
        transparent=True,
        easy_drag=False,
        on_top=True,
        shadow=False,
        text_select=True,
        focus=True,
    )
    overlay = Overlay(window)
    engine.hide_window = overlay.hide
    engine.show_window = overlay.show
    window.expose(overlay.fit, overlay.set_on_top, overlay.quit)
    webview.start()
    log.info("цикл окна завершён (webview.start вернулся)")
    shutdown()  # окно закрыли


def shutdown() -> None:
    """Корректно завершаем сессию (расшифровка уже в базе) и выходим наверняка.

    pywebview ждёт ответа от окна без тайм-аута (размер, позиция, evaluate_js); если такой вызов попал
    в момент закрытия, его поток висит вечно и не даёт Python завершиться. Поэтому выходим через os._exit.
    """
    arm_hang_watchdog(10, "завершение работы")
    log.info("завершение: останавливаю прослушивание (если идёт)")
    stop_listening()
    log.info("завершение: выход из процесса")
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
