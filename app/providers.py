"""Бесплатные и дешёвые ИИ-провайдеры через OpenAI-совместимый API.

Groq, Google Gemini, DeepSeek, OpenRouter и Ollama принимают один и тот же формат
запросов (/chat/completions), поэтому работают через один клиент `openai` с разными base_url.
Список моделей берём у самого провайдера (/models), чтобы названия не устаревали.
"""
from __future__ import annotations

import re
from typing import AsyncIterator

import openai

PROVIDERS: dict[str, dict] = {
    "gemini": {
        "name": "Google Gemini",
        "title": "Войти через Google",
        "subtitle": "Gemini · бесплатно · отличный русский",
        "icon": "G",
        "account": True,          # показывать среди кнопок «Войти через аккаунт»
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_url": "https://aistudio.google.com/apikey",
        "key_pattern": r"AIza[0-9A-Za-z_\-]{30,}",
        "key_hint": "AIza…",
        "steps": [
            "Откроется Google AI Studio — войди своим Google-аккаунтом, если ещё не вошёл.",
            "Нажми «Create API key».",
            "Нажми «Copy» — приложение подхватит ключ само.",
        ],
        "prefer": [r"gemini-[\d.]+-flash$", r"flash(?!.*(lite|image|tts|live|audio|thinking|exp))", r"flash"],
        "exclude": r"embedding|imagen|veo|tts|audio|live|aqa|image-generation|learnlm",
        "vision": "same",
        "note": "Бесплатный тариф AI Studio. Запросы бесплатного тарифа Google может использовать для обучения.",
    },
    "openrouter": {
        "name": "OpenRouter",
        "title": "Войти через OpenRouter",
        "subtitle": "Google, GitHub или почта · бесплатные модели",
        "icon": "⇄",
        "account": True,
        "oauth": True,            # настоящий вход через аккаунт (OAuth PKCE), ключ создаётся сам
        "base_url": "https://openrouter.ai/api/v1",
        "key_url": "https://openrouter.ai/keys",
        "key_pattern": r"sk-or-v1-[a-f0-9]{64}",
        "key_hint": "sk-or-v1-…",
        "prefer": [r"gemma-4-31b.*:free$", r"gemma.*:free$", r"deepseek.*:free$", r"gpt-oss.*:free$",
                   r"llama.*:free$", r"qwen.*:free$", r"nemotron.*ultra.*:free$", r":free$"],
        "filter": r":free$",
        "exclude": r"$^",
        "vision": "same",
        "note": "Бесплатно ~50 запросов в день (1000 после пополнения на $10). Скорость нестабильна.",
    },
    "groq": {
        "name": "Groq",
        "title": "Войти через Groq",
        "subtitle": "бесплатно · самый быстрый",
        "icon": "⚡",
        "account": True,
        "base_url": "https://api.groq.com/openai/v1",
        "key_url": "https://console.groq.com/keys",
        "key_pattern": r"gsk_[A-Za-z0-9]{20,}",
        "key_hint": "gsk_…",
        "steps": [
            "Откроется консоль Groq — войди через Google, GitHub или почту.",
            "Нажми «Create API Key» и придумай любое название.",
            "Скопируй ключ — приложение подхватит его само.",
        ],
        "prefer": [r"^openai/gpt-oss-120b$", r"llama-3\.3-70b", r"gpt-oss", r"llama"],
        "exclude": r"whisper|guard|tts|playai|orpheus|prompt-guard|compound",
        "vision": "qwen/qwen3.8-27b",  # понимает изображения; llama-4-scout у Groq больше нет
        # у каждой модели свой дневной лимит: кончился у основной — отвечает запасная
        "fallback": ["openai/gpt-oss-20b", "qwen/qwen3.8-27b"],
        "note": "Бесплатно без карты, ~1000 запросов в день.",
    },
    "deepseek": {
        "name": "DeepSeek",
        "title": "DeepSeek",
        "subtitle": "почти бесплатно · без скриншотов",
        "icon": "🐋",
        "account": False,
        "base_url": "https://api.deepseek.com",
        "key_url": "https://platform.deepseek.com/api_keys",
        "key_pattern": r"sk-[a-f0-9]{32}",
        "key_hint": "sk-…",
        "prefer": [r"^deepseek-chat$", r"chat", r"flash"],
        "exclude": r"reasoner",
        "vision": None,
        "note": "Платно, но копейки (пополнение от $2). Изображения не поддерживает. Серверы в Китае.",
    },
    "ollama": {
        "name": "Ollama (локально)",
        "title": "Ollama на этом Mac",
        "subtitle": "бесплатно · офлайн · без ключа",
        "icon": "🦙",
        "account": False,
        "base_url": "http://localhost:11434/v1",
        "key_url": "https://ollama.com/download",
        "prefer": [r"qwen", r"gemma", r"llama", r"."],
        "exclude": r"embed",
        "vision": "same",
        "no_key": True,
        "note": "Модель работает на твоём Mac: brew install ollama && ollama pull qwen3:8b",
    },
}


class LLMError(Exception):
    def __init__(self, message: str, auth: bool = False, billing: bool = False):
        super().__init__(message)
        self.auth = auth
        self.billing = billing


def _client(provider: str, key: str) -> openai.AsyncOpenAI:
    p = PROVIDERS[provider]
    return openai.AsyncOpenAI(
        base_url=p["base_url"],
        api_key=key or ("ollama" if p.get("no_key") else "missing"),
        timeout=60,
        max_retries=1,
        default_headers={"X-Title": "LakonIQ"} if provider == "openrouter" else None,
    )


def _msg(e: Exception) -> str:
    return str(getattr(e, "message", e))


def _daily(msg: str) -> bool:
    """Дневной лимит (а не минутный): ждать долго — лучше переключиться на другую модель."""
    return bool(re.search(r"per day|\(TPD\)|\(RPD\)|per-day", msg, re.I))


def _wait(msg: str) -> str:
    """«try again in 21m12.24s» → «≈21 мин»."""
    m = re.search(r"try again in (?:(\d+)h)?(?:(\d+)m)?(?:([\d.]+)s)?", msg)
    if not m or not any(m.groups()):
        return ""
    h, mi, sec = (float(x) if x else 0.0 for x in m.groups())
    total = h * 3600 + mi * 60 + sec
    if total >= 3600:
        return f"≈{total / 3600:.1f} ч".replace(".0 ", " ")
    return f"≈{round(total / 60)} мин" if total >= 60 else f"≈{max(1, round(total))} с"


def describe(e: Exception, provider: str) -> LLMError:
    name = PROVIDERS.get(provider, {}).get("name", provider)
    if isinstance(e, openai.AuthenticationError) or (
        isinstance(e, openai.BadRequestError) and "api key" in str(e).lower()
    ):
        return LLMError(f"{name}: неверный API-ключ.", auth=True)
    if isinstance(e, openai.RateLimitError):
        msg, wait = _msg(e), _wait(_msg(e))
        if _daily(msg) or "free-models-per-day" in msg:
            return LLMError(f"{name}: дневной лимит модели исчерпан" + (f", освободится через {wait}" if wait else "")
                            + ". Выбери другую модель: ⋯ → Настройки → ИИ-модель.")
        if re.search(r"per minute|\(TPM\)|\(RPM\)|per-min", msg, re.I):
            return LLMError(f"{name}: лимит запросов в минуту — повтори " + (f"через {wait}." if wait else "чуть позже."))
        if "provider returned error" in msg.lower():
            return LLMError(f"{name}: бесплатная модель сейчас перегружена у поставщика — повтори через минуту "
                            "или выбери другую модель.")
        return LLMError(f"{name}: исчерпан лимит бесплатного тарифа, подожди или выбери другой способ входа.")
    if isinstance(e, openai.APIConnectionError):
        if provider == "ollama":
            return LLMError("Ollama не запущена. Установи и запусти: brew install ollama && ollama serve")
        return LLMError(f"{name}: нет соединения.")
    if isinstance(e, openai.APIStatusError):
        msg = str(getattr(e, "message", e))
        if e.status_code == 402 or "balance" in msg.lower():
            return LLMError(f"{name}: на балансе нет средств — пополни аккаунт.", billing=True)
        return LLMError(f"{name} ({e.status_code}): {msg[:300]}")
    return LLMError(f"{name}: {e}")


def _pick(models: list[str], patterns: list[str]) -> str:
    for pat in patterns:
        hits = sorted((m for m in models if re.search(pat, m)), reverse=True)
        if hits:
            return hits[0]
    return models[0] if models else ""


async def list_models(provider: str, key: str) -> dict:
    """Проверяет ключ и возвращает {"models": [...], "default": "..."}; бросает LLMError."""
    p = PROVIDERS[provider]
    if not key and not p.get("no_key"):
        raise LLMError("Вставь API-ключ.", auth=True)
    try:
        if provider == "openrouter":  # список моделей у OpenRouter открыт всем — ключ проверяем отдельно
            r = await _client(provider, key)._client.get(
                p["base_url"] + "/key", headers={"Authorization": f"Bearer {key}"})
            if r.status_code == 401:
                raise LLMError("OpenRouter: неверный API-ключ.", auth=True)
        page = await _client(provider, key).models.list()
        ids = [m.id.removeprefix("models/") async for m in page]
    except LLMError:
        raise
    except Exception as e:  # noqa: BLE001
        raise describe(e, provider) from e
    if p.get("filter"):
        ids = [m for m in ids if re.search(p["filter"], m)]
    ids = sorted({m for m in ids if not re.search(p["exclude"], m, re.I)})
    if not ids:
        hint = " Скачай модель: ollama pull qwen3:8b" if provider == "ollama" else ""
        raise LLMError(f"{p['name']}: не найдено подходящих моделей.{hint}")
    return {"models": ids, "default": _pick(ids, p["prefer"])}


def supports_images(provider: str) -> bool:
    return PROVIDERS[provider]["vision"] is not None


def _to_openai(system: list[dict], content: list[dict]) -> tuple[list[dict], bool]:
    sys_text = "\n\n".join(b["text"] for b in system)
    has_image = any(b["type"] == "image" for b in content)
    if has_image:
        parts = []
        for b in content:
            if b["type"] == "text":
                parts.append({"type": "text", "text": b["text"]})
            else:
                src = b["source"]
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{src['media_type']};base64,{src['data']}"}})
        user = parts
    else:
        user = "\n\n".join(b["text"] for b in content)
    return [{"role": "system", "content": sys_text}, {"role": "user", "content": user}], has_image


async def stream(provider: str, key: str, model: str, system: list[dict], content: list[dict],
                 max_tokens: int) -> AsyncIterator[str]:
    p = PROVIDERS[provider]
    messages, has_image = _to_openai(system, content)
    if has_image and p["vision"] not in (None, "same"):
        model = p["vision"]  # у Groq изображения понимает отдельная модель
    # дневной лимит основной модели → пробуем запасные (у картинок своя модель — без запасных)
    candidates = [model] + ([] if has_image else [m for m in p.get("fallback", []) if m != model])
    for i, current in enumerate(candidates):
        extra = {"reasoning_effort": "low"} if provider == "groq" and "gpt-oss" in current else {}
        try:
            resp = await _client(provider, key).chat.completions.create(
                model=current, messages=messages, max_tokens=max_tokens, stream=True, extra_body=extra or None,
            )
        except openai.RateLimitError as e:
            if i + 1 < len(candidates) and _daily(_msg(e)):
                continue
            raise describe(e, provider) from e
        except Exception as e:  # noqa: BLE001
            raise describe(e, provider) from e
        try:
            async for chunk in resp:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:  # noqa: BLE001
            raise describe(e, provider) from e
        if i:  # пометка в конце, а не в начале: начало ответа проверяет фильтр «не вопрос»
            yield (f"\n\n_(Ответила запасная модель {current}: у {model} кончился дневной лимит "
                   f"{p['name']}.)_")
        return
