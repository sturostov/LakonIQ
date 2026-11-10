"""Источники звука → asyncio-очередь, 16-bit PCM моно.

- «Звук системы» (по умолчанию): всё, что слышно в наушниках, через ScreenCaptureKit
  (помощник native/syscap). Работает с любыми наушниками, без BlackHole и настройки Audio-MIDI.
- Микрофон или любое устройство ввода (в т.ч. BlackHole) — через PortAudio (sounddevice).
"""
from __future__ import annotations

import asyncio
import os
import select
import shutil
import subprocess
import threading

import numpy as np
import sounddevice as sd

from .config import FROZEN, RESOURCE_DIR, ROOT

SYSTEM = "system"      # специальное значение: звук системы
DEFAULT_MIC = "default"  # специальное значение: микрофон по умолчанию (тот же, что выбран в Zoom/macOS)

SYSCAP_SRC = ROOT / "native" / "syscap.swift"
# в собранном приложении помощник уже лежит внутри .app; из исходников — собирается при первом запуске
SYSCAP_BIN = (RESOURCE_DIR if FROZEN else ROOT) / "native" / "bin" / "syscap"


class SourceError(Exception):
    pass


def refresh_devices() -> None:
    """PortAudio запоминает список устройств при старте — обновляем, чтобы находились AirPods,
    подключённые после запуска приложения. Вызывать только когда ни один поток не открыт."""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:  # noqa: BLE001
        pass


def list_input_devices() -> list[dict]:
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append({"index": i, "name": d["name"], "channels": d["max_input_channels"]})
    return out


def find_device(name: str) -> int | None:
    """Ищет входное устройство по имени (индексы меняются при подключении AirPods и т.п.)."""
    if not name:
        return None
    if name == DEFAULT_MIC:
        idx = sd.default.device[0]
        return idx if idx is not None and idx >= 0 else None
    devices = list_input_devices()
    for d in devices:
        if d["name"] == name:
            return d["index"]
    for d in devices:
        if name.lower() in d["name"].lower():
            return d["index"]
    return None


def open_source(name: str, loop: asyncio.AbstractEventLoop):
    """Открывает и запускает источник по значению из настроек. Бросает SourceError с понятным текстом."""
    if name == SYSTEM:
        src = SystemAudioSource(loop)
        src.start()
        return src
    idx = find_device(name)
    if idx is None:
        label = "Микрофон по умолчанию" if name == DEFAULT_MIC else f"«{name}»"
        raise SourceError(f"Не найдено устройство {label}. Проверь подключение наушников и ⋯ → Настройки.")
    try:
        src = AudioSource(idx, loop)
        src.start()
    except Exception as e:  # noqa: BLE001
        raise SourceError(f"Не удалось открыть «{name}»: {e}") from e
    return src


class _Base:
    sample_rate = 48000

    def __init__(self, loop: asyncio.AbstractEventLoop):
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self.level = 0.0  # громкость 0..1 для индикатора в интерфейсе
        self._loop = loop

    def _push(self, data: bytes) -> None:
        if self.queue.full():  # сеть подвисла — выбрасываем самое старое, чтобы не копить задержку
            self.queue.get_nowait()
        self.queue.put_nowait(data)

    def _emit(self, mono: np.ndarray) -> None:
        rms = float(np.sqrt(np.mean(mono.astype(np.float32) ** 2))) / 32768.0 if len(mono) else 0.0
        self.level = max(rms * 4, self.level * 0.6)
        self._loop.call_soon_threadsafe(self._push, mono.astype(np.int16).tobytes())


class AudioSource(_Base):
    """Микрофон или другое устройство ввода через PortAudio."""

    def __init__(self, device: int, loop: asyncio.AbstractEventLoop):
        super().__init__(loop)
        info = sd.query_devices(device, "input")
        self.device = device
        self.name = info["name"]
        self.sample_rate = int(info["default_samplerate"])
        self.channels = min(2, int(info["max_input_channels"]))
        self._stream: sd.InputStream | None = None

    def _callback(self, indata, frames, time_info, status) -> None:
        mono = indata.mean(axis=1) if indata.shape[1] > 1 else indata[:, 0]
        self._emit(mono)

    def start(self) -> None:
        self._stream = sd.InputStream(
            device=self.device,
            channels=self.channels,
            samplerate=self.sample_rate,
            dtype="int16",
            blocksize=int(self.sample_rate * 0.05),  # 50 мс — минимальная задержка
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None


def ensure_syscap() -> str:
    """Собирает помощник захвата звука, если его нет или исходник новее (один раз, ~10 с)."""
    if FROZEN:
        if SYSCAP_BIN.exists():
            return str(SYSCAP_BIN)
        raise SourceError("Внутри приложения нет помощника захвата звука — переустанови LakonIQ.")
    if SYSCAP_BIN.exists() and SYSCAP_BIN.stat().st_mtime >= SYSCAP_SRC.stat().st_mtime:
        return str(SYSCAP_BIN)
    swiftc = shutil.which("swiftc")
    if not swiftc:
        raise SourceError("Для захвата звука системы нужны инструменты Xcode: выполни в терминале "
                          "`xcode-select --install`, или выбери BlackHole в ⋯ → Настройки.")
    SYSCAP_BIN.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run([swiftc, "-O", "-swift-version", "5", str(SYSCAP_SRC), "-o", str(SYSCAP_BIN)],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise SourceError(f"Не удалось собрать захват звука системы: {r.stderr.strip()[-300:]}")
    return str(SYSCAP_BIN)


class SystemAudioSource(_Base):
    """Звук системы (всё, что слышно в наушниках) через помощник на ScreenCaptureKit."""

    name = "Звук системы"
    sample_rate = 48000

    def __init__(self, loop: asyncio.AbstractEventLoop):
        super().__init__(loop)
        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        path = ensure_syscap()
        self._proc = subprocess.Popen([path], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE, bufsize=0)
        ready, _, _ = select.select([self._proc.stderr], [], [], 15)
        line = self._proc.stderr.readline().decode(errors="replace").strip() if ready else ""
        if line != "ready":
            self.stop()
            low = line.lower()
            if "declined" in low or "tcc" in low or "not authorized" in low or "permission" in low:
                raise SourceError("Нет доступа к записи звука системы: Системные настройки → Конфиденциальность "
                                  "и безопасность → «Запись экрана и системного звука» → включи для Cursor/Терминала "
                                  "и перезапусти приложение.")
            raise SourceError(f"Захват звука системы не запустился: {line or 'нет ответа'}")
        self._thread = threading.Thread(target=self._reader, daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        fd = self._proc.stdout.fileno()
        rest = b""
        chunk = int(self.sample_rate * 0.05) * 2  # 50 мс
        while self._proc and self._proc.poll() is None:
            try:
                data = os.read(fd, chunk)
            except OSError:
                break
            if not data:
                break
            data = rest + data
            cut = len(data) // 2 * 2
            rest = data[cut:]
            if cut:
                self._emit(np.frombuffer(data[:cut], np.int16))

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if not proc:
            return
        try:
            proc.stdin.close()  # помощник сам завершится, увидев конец stdin
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            proc.kill()
