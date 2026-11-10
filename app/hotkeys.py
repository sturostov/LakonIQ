"""Глобальные горячие клавиши macOS в стиле Cluely.

Комбинации перехватываются через Quartz event tap и НЕ передаются другим приложениям
(как глобальные шорткаты в Electron). Нужен доступ: Системные настройки →
Конфиденциальность и безопасность → Универсальный доступ (и «Мониторинг ввода»)
для приложения, из которого запущен Python (Терминал / PyCharm).
"""
from __future__ import annotations

import threading
import time
from typing import Callable

from .logs import log

CMD, SHIFT, CTRL, ALT = 1 << 20, 1 << 17, 1 << 18, 1 << 19
MODS = CMD | SHIFT | CTRL | ALT

# Коды клавиш macOS (kVK_*)
K_RETURN, K_BACKSLASH, K_R, K_L, K_D, K_F, K_K = 36, 42, 15, 37, 2, 3, 40
K_LEFT, K_RIGHT, K_DOWN, K_UP = 123, 124, 125, 126

# (клавиша, модификаторы) → (действие, повторять при удержании, подпись для справки)
SHORTCUTS: dict[tuple[int, int], tuple[str, bool, str]] = {
    (K_RETURN, CMD): ("assist", False, "⌘↵  Подсказка: ответ на вопрос / анализ экрана и разговора"),
    (K_RETURN, CMD | SHIFT): ("screenshot", False, "⌘⇧↵  Решить задачу с экрана"),
    (K_BACKSLASH, CMD): ("toggle_window", False, "⌘\\  Показать / скрыть окно"),
    (K_R, CMD): ("reset", False, "⌘R  Сбросить (очистить экран, остановить ответ)"),
    (K_L, CMD | SHIFT): ("toggle", False, "⌘⇧L  Начать / остановить прослушивание"),
    (K_D, CMD | SHIFT): ("save_last", False, "⌘⇧D  Сохранить последний ответ в базу ★"),
    (K_F, CMD | SHIFT): ("followups", False, "⌘⇧F  Уточняющие вопросы интервьюеру"),
    (K_K, CMD | SHIFT): ("recap", False, "⌘⇧K  Кратко о разговоре"),
    (K_UP, CMD): ("move_up", True, "⌘↑↓←→  Двигать окно"),
    (K_DOWN, CMD): ("move_down", True, ""),
    (K_LEFT, CMD): ("move_left", True, ""),
    (K_RIGHT, CMD): ("move_right", True, ""),
    (K_UP, CMD | SHIFT): ("scroll_up", True, "⌘⇧↑↓  Прокрутить ответ"),
    (K_DOWN, CMD | SHIFT): ("scroll_down", True, ""),
}


def help_lines() -> list[str]:
    return [label for _, _, label in SHORTCUTS.values() if label]


def start(dispatch: Callable[[str], None]) -> bool:
    """Запускает перехват. dispatch(action) вызывается из фонового потока."""
    try:
        import Quartz
        from pynput import keyboard
    except Exception as e:  # noqa: BLE001
        print("Горячие клавиши недоступны:", e)
        return False

    def trusted(prompt: bool = False) -> bool:
        """Есть ли «Универсальный доступ». prompt=True — macOS сама покажет окно с запросом."""
        try:
            from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt

            return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))
        except Exception:  # noqa: BLE001
            return True

    swallowed_up: set[int] = set()

    def intercept(event_type, event):
        try:
            code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
            if event_type == Quartz.kCGEventKeyUp:
                if code in swallowed_up:
                    swallowed_up.discard(code)
                    return None
                return event
            if event_type != Quartz.kCGEventKeyDown:
                return event
            flags = Quartz.CGEventGetFlags(event) & MODS
            hit = SHORTCUTS.get((code, flags))
            if not hit:
                return event
            action, repeatable, _ = hit
            repeat = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
            if repeatable or not repeat:
                dispatch(action)
            swallowed_up.add(code)
            return None  # глотаем: другие приложения комбинацию не увидят
        except Exception:  # noqa: BLE001 — нельзя ронять системный перехват клавиатуры
            return event

    def launch() -> None:
        listener = keyboard.Listener(darwin_intercept=intercept)
        listener.daemon = True
        listener.start()

    if trusted(prompt=True):
        launch()
        return True

    # Разрешения пока нет: ждём, пока его включат в Системных настройках, и включаем клавиши без перезапуска
    def wait_for_access() -> None:
        while not trusted():
            time.sleep(2)
        log.info("«Универсальный доступ» получен — горячие клавиши включены")
        launch()

    log.info("нет «Универсального доступа»: горячие клавиши включатся, как только его разрешат")
    threading.Thread(target=wait_for_access, daemon=True).start()
    return True
