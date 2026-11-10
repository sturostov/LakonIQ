"""Распознавание речи через Groq Whisper — бесплатно, тот же ключ Groq, что и для ответов.

Groq не умеет потоковое распознавание, поэтому звук режем на фразы сами: детектор речи
по громкости (с адаптивным порогом шума) ловит начало фразы и паузу ≥0.7 с в конце —
готовая фраза уходит в Whisper (~0.3–0.6 с). События отдаём в том же формате, что Deepgram,
поэтому движку всё равно, кто распознаёт.
"""
from __future__ import annotations

import asyncio
import io
import re
import wave
from collections import deque
from typing import Awaitable, Callable

import numpy as np
import openai

GROQ_URL = "https://api.groq.com/openai/v1"
MODEL = "whisper-large-v3-turbo"
FRAME_MS = 30
START_FRAMES = 3          # 90 мс речи подряд — начало фразы
END_SILENCE_MS = 700      # пауза, после которой фраза считается законченной
MAX_SEGMENT_S = 15        # длинный монолог режем на куски, чтобы не ждать конца
PREROLL_MS = 300          # захватываем немного звука до начала речи
MIN_SPEECH_MS = 300       # короче — щелчок или шум, не отправляем
MIN_THRESHOLD = 300.0     # минимальный порог громкости (int16)
TARGET_RATE = 16000       # Whisper работает на 16 кГц — шлём меньше данных

# Известные «галлюцинации» Whisper на тишине и шуме (особенно в русском)
HALLUCINATIONS = re.compile(
    r"субтитр|dimatorzok|продолжение следует|спасибо за просмотр|подпис\w* на (наш )?канал|"
    r"редактор субтитров|корректор|amara\.org|ставьте лайк|благодарю за внимание\.?$",
    re.I,
)

OnEvent = Callable[[dict], Awaitable[None]]


class LimitExceeded(Exception):
    pass


def _resample(x: np.ndarray, rate: int) -> np.ndarray:
    if rate == TARGET_RATE:
        return x
    if rate % TARGET_RATE == 0:  # 48 кГц → 16 кГц: усредняем по 3 отсчёта (заодно сглаживаем)
        k = rate // TARGET_RATE
        n = len(x) // k * k
        return x[:n].reshape(-1, k).mean(axis=1)
    n_out = int(len(x) * TARGET_RATE / rate)
    return np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x)


def to_wav(samples: np.ndarray, rate: int) -> bytes:
    data = np.clip(_resample(samples.astype(np.float32), rate), -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(TARGET_RATE)
        w.writeframes(data.tobytes())
    return buf.getvalue()


class Segmenter:
    """Режет поток на фразы. feed() возвращает события ("start",) / ("end", audio, final)."""

    def __init__(self, rate: int, end_silence_ms: int = END_SILENCE_MS, max_segment_s: int = MAX_SEGMENT_S):
        self.rate = rate
        self.end_silence_ms = end_silence_ms
        self.max_segment_s = max_segment_s
        self.frame = max(1, int(rate * FRAME_MS / 1000))
        self.noise = 100.0
        self.rest = np.zeros(0, np.int16)
        self.preroll: deque = deque(maxlen=PREROLL_MS // FRAME_MS)
        self.buf: list[np.ndarray] = []
        self.in_speech = False
        self.run = 0
        self.silence_ms = 0
        self.speech_ms = 0

    def feed(self, chunk: bytes) -> list[tuple]:
        events: list[tuple] = []
        x = np.concatenate([self.rest, np.frombuffer(chunk, np.int16)])
        n = len(x) // self.frame * self.frame
        self.rest = x[n:]
        for f in x[:n].reshape(-1, self.frame):
            rms = float(np.sqrt(np.mean(f.astype(np.float32) ** 2)))
            speech = rms > max(MIN_THRESHOLD, self.noise * 3)
            if not self.in_speech:
                if not speech:  # подстраиваемся под фоновый шум
                    self.noise = 0.95 * self.noise + 0.05 * rms
                self.preroll.append(f)
                self.run = self.run + 1 if speech else 0
                if self.run >= START_FRAMES:
                    self.in_speech = True
                    self.buf = list(self.preroll)
                    self.preroll.clear()
                    self.silence_ms, self.speech_ms = 0, self.run * FRAME_MS
                    events.append(("start",))
                continue
            self.buf.append(f)
            if speech:
                self.silence_ms = 0
                self.speech_ms += FRAME_MS
            else:
                self.silence_ms += FRAME_MS
            too_long = len(self.buf) * FRAME_MS >= self.max_segment_s * 1000
            if self.silence_ms >= self.end_silence_ms or too_long:
                audio = np.concatenate(self.buf)
                finished = self.silence_ms >= self.end_silence_ms  # False — монолог разрезан посередине
                enough = self.speech_ms >= MIN_SPEECH_MS
                self.buf, self.run = [], 0
                self.in_speech = not finished  # монолог продолжается
                if not finished:
                    self.silence_ms, self.speech_ms = 0, 0
                if enough:
                    events.append(("end", audio, finished))
                elif finished:
                    events.append(("drop",))
        return events


def clean_text(resp) -> str:
    """Отбрасываем куски, где Whisper скорее «додумал» текст на тишине."""
    segments = getattr(resp, "segments", None)
    if segments:
        parts = []
        for s in segments:
            get = s.get if isinstance(s, dict) else lambda k, d=None, s=s: getattr(s, k, d)
            text = (get("text") or "").strip()
            if get("no_speech_prob", 0) > 0.6 and get("avg_logprob", 0) < -0.5:
                continue
            if HALLUCINATIONS.search(text):
                continue
            parts.append(text)
        text = " ".join(parts)
    else:
        text = (getattr(resp, "text", "") or "").strip()
        if HALLUCINATIONS.search(text):
            text = ""
    return re.sub(r"\s+", " ", text).strip()


async def transcribe(client: openai.AsyncOpenAI, wav: bytes, language: str, prompt: str) -> str:
    kwargs: dict = {"model": MODEL, "file": ("speech.wav", wav, "audio/wav"),
                    "response_format": "verbose_json", "temperature": 0}
    if language and language != "multi":
        kwargs["language"] = language
    if prompt:
        kwargs["prompt"] = prompt[:600]
    for attempt in range(4):
        try:
            return clean_text(await client.audio.transcriptions.create(**kwargs))
        except openai.RateLimitError as e:
            wait = 2.0
            try:
                wait = float(e.response.headers.get("retry-after", wait))
            except Exception:  # noqa: BLE001
                pass
            if wait > 15:  # не минутный, а дневной лимит — ждать бессмысленно
                raise LimitExceeded(
                    f"Groq: исчерпан лимит распознавания речи (снова доступно через ~{int(wait // 60) + 1} мин). "
                    "Переключись на Deepgram: ⋯ → Настройки → Распознавание речи.") from e
            await asyncio.sleep(max(wait, 0.5))
        except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError):
            await asyncio.sleep(0.5 * (attempt + 1))
    return ""


async def run_whisper(queue: "asyncio.Queue[bytes]", sample_rate: int, language: str,
                      on_event: OnEvent, on_error: Callable[[str], Awaitable[None]],
                      key: str, prompt: str = "", relaxed: bool = False) -> None:
    """relaxed=True — для своего микрофона: длинные паузы и куски, в разы меньше запросов к Groq."""
    client = openai.AsyncOpenAI(base_url=GROQ_URL, api_key=key, timeout=30, max_retries=0)
    seg = Segmenter(sample_rate, 1500, 30) if relaxed else Segmenter(sample_rate)
    ready: asyncio.Queue = asyncio.Queue()
    gap = np.zeros(int(sample_rate * 0.25), np.int16)

    async def worker() -> None:
        while True:
            items = [await ready.get()]
            while not ready.empty():  # пока распознавали — накопилось: шлём одним запросом
                items.append(ready.get_nowait())
            audio = np.concatenate([part for a, _ in items for part in (a, gap)])
            try:
                text = await transcribe(client, to_wav(audio, sample_rate), language, prompt)
            except openai.AuthenticationError:
                await on_error("Groq: неверный ключ для распознавания речи — войди через Groq заново.")
                return
            except LimitExceeded as e:
                await on_error(str(e))
                continue
            except Exception as e:  # noqa: BLE001
                await on_error(f"Распознавание речи: {e}")
                continue
            # конец реплики, только если человек не начал говорить снова, пока шло распознавание
            speech_final = items[-1][1] and not seg.in_speech and ready.empty()
            await on_event({
                "type": "Results", "is_final": True, "speech_final": speech_final,
                "trigger_delay": 0.2,  # пауза уже выдержана при нарезке — отвечаем быстрее
                "channel": {"alternatives": [{"transcript": text}]},
            })

    wtask = asyncio.create_task(worker())
    try:
        while True:
            chunk = await queue.get()
            for ev in seg.feed(chunk):
                if ev[0] == "start":
                    await on_event({"type": "SpeechStarted"})
                elif ev[0] == "end":
                    ready.put_nowait((ev[1], ev[2]))
            if wtask.done():  # воркер остановился (например, неверный ключ)
                return
    finally:
        wtask.cancel()
