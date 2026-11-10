"""Потоковое распознавание речи через Deepgram (WebSocket) с автопереподключением."""
from __future__ import annotations

import asyncio
import json
from typing import Awaitable, Callable
from urllib.parse import urlencode

from websockets.asyncio.client import connect

from .config import DEEPGRAM_API_KEY, DEEPGRAM_MODEL

OnEvent = Callable[[dict], Awaitable[None]]


async def run_deepgram(
    queue: "asyncio.Queue[bytes]",
    sample_rate: int,
    language: str,
    on_event: OnEvent,
    on_error: Callable[[str], Awaitable[None]],
    key: str = DEEPGRAM_API_KEY,
) -> None:
    params = {
        "model": DEEPGRAM_MODEL,
        "language": language or "ru",
        "encoding": "linear16",
        "sample_rate": sample_rate,
        "channels": 1,
        "interim_results": "true",   # промежуточный текст — видно, что слышим
        "smart_format": "true",
        "punctuate": "true",
        "endpointing": 300,          # конец фразы после 300 мс тишины
        "utterance_end_ms": 1000,    # надёжный сигнал «человек договорил»
        "vad_events": "true",
    }
    url = "wss://api.deepgram.com/v1/listen?" + urlencode(params)
    headers = {"Authorization": f"Token {key}"}
    backoff = 1.0

    while True:
        try:
            async with connect(url, additional_headers=headers, max_size=None, ping_interval=10) as ws:
                backoff = 1.0

                async def sender() -> None:
                    while True:
                        chunk = await queue.get()
                        await ws.send(chunk)

                send_task = asyncio.create_task(sender())
                try:
                    async for raw in ws:
                        if isinstance(raw, bytes):
                            continue
                        await on_event(json.loads(raw))
                finally:
                    send_task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # сеть, неверный ключ и т.п. — сообщаем и переподключаемся
            msg = str(e)
            if "401" in msg or "403" in msg:
                await on_error("Deepgram отклонил ключ — проверь его в ⋯ → Настройки")
                return
            await on_error(f"Распознавание речи: {msg}. Переподключение…")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 10)
            # выбрасываем накопившийся звук, чтобы после переподключения не отставать
            while not queue.empty():
                queue.get_nowait()
