"""Генерация ответов через Claude со стримингом (первые слова — меньше чем за секунду)."""
from __future__ import annotations

import re
from typing import AsyncIterator

import anthropic

from . import providers
from .config import CLAUDE_EFFORT, CLAUDE_MODEL
from .providers import LLMError

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    """Клиент создаётся лениво: учётные данные (вход через Claude) могут появиться после старта."""
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


def reset_client() -> None:
    global _client
    _client = None

LANG_NAMES = {"ru": "русском", "en": "английском", "multi": "том же языке, что и вопрос"}

ANSWER_RULES_STRICT = """Ты — помощник кандидата на собеседовании. Тебе приходит распознанная речь интервьюера \
(в ней бывают ошибки распознавания — восстанавливай смысл по контексту, особенно технические термины).

Пиши ответ от первого лица, как будто говорит сам кандидат, на {lang} языке. Строго в формате:

**Ответ:** 2–4 простых предложения, которые можно сразу произнести вслух. Без воды и вступлений. \
Отвечай только на вопрос из <interviewer_question>; разговор выше — фон, используй его, только если он относится к вопросу.

**Детали:**
- 3–6 коротких пунктов: ключевые термины, пример из практики, подводные камни, что спросят дальше

Если для ответа нужен код или SQL — добавь короткий пример в блоке кода.
Если вопрос про опыт кандидата — опирайся на резюме, не выдумывай компаний и цифр, которых там нет; \
если данных нет, дай шаблон ответа по методу STAR с местами для подстановки в [квадратных скобках].
Если реплика — не вопрос (приветствие, «меня слышно?», рассказ о компании), ответь одной строкой: \
**Ответ:** и короткая уместная реплика, без «Деталей»."""

# Живая устная речь. Правила «очеловечивания» адаптированы из скилла humanizer (blader/humanizer, MIT)
# и примет ИИ-текста из Википедии — под устный русский на собеседовании.
HUMAN_SPEECH = """Звучи как живой человек, а не учебник:
- Как опытный коллега объясняет за кофе: сначала суть простыми словами, без определений \
в духе «X — это механизм, обеспечивающий…».
- Короткие фразы разной длины. Начинай сразу с сути — без вводных «Если по-простому», «Если коротко», «По сути».
- Термины — только по необходимости и сразу простыми словами. Максимум одна оговорка («насколько помню»).
- Без канцелярита и штампов: «является», «осуществляет», «данный», «в рамках», «важно отметить», \
«ключевую роль», «таким образом», «не только…, но и…»; без триад ради ритма, пафоса, пересказа вопроса и выводов в конце.
- Личный опыт («на проекте мы…») — только из резюме, иначе «я бы сделал так…»; \
не упоминай резюме и служебные данные вслух.
- Уровень — под позицию: junior проще и честнее, senior — с компромиссами.
- Подстраивайся под манеру кандидата (реплики «Я:», <candidate_voice>), не копируя содержание.
- Только грамотный русский; английские термины целиком латиницей; к интервьюеру не на «ты».
Пример. Плохо: «Индекс — это структура данных, обеспечивающая ускорение выборки.» \
Хорошо: «Это как оглавление в книге: база не листает всю таблицу, а сразу прыгает \
к нужным строкам. Обычно ставлю индекс на поля, по которым часто ищем, например email. Минус — запись чуть медленнее.»"""

ANSWER_RULES_LIVE = """Ты подсказываешь кандидату на собеседовании, что сказать, на {lang} языке. \
Речь интервьюера распознана автоматически — восстанавливай смысл, если слова искажены. \
Кандидат глянет на подсказку и перескажет своими словами. Формат строго:

**Скажи:** живые предложения от первого лица — прямо на вопрос (сколько — см. «Глубина»).

**Шпаргалка:** если в ответе есть набор однотипных пунктов — методы, коды ответов, команды, виды, уровни, \
отличия X от Y — вынеси их в таблицу Markdown на 2–3 колонки, до 7 строк (например, «| Метод | Что делает | Пример |»). \
Тогда в «Скажи» только суть, без перечисления всех пунктов: кандидат глянет на таблицу.

**Пример-задача:** понятная учебная задача, как на собеседовании, без кода. Первая строка — «Условие:» \
с конкретными данными (например: «поле „Возраст“ принимает целые числа от 18 до 60»), дальше — решение по шагам \
и ожидаемый результат. Если вопрос про тест-дизайн (классы эквивалентности, граничные значения, таблица решений, \
переходы состояний, попарное тестирование, тест-кейсы, чек-лист) — решение обязательно таблицей, \
по одной проверке в строке:
| Что проверяем | Значение | Ожидаем |
|---|---|---|
В таблице — и валидные, и невалидные классы (пусто, не число, дробное…), и границы; допущения по условию — \
одной строкой под таблицей.

**Пример в коде:** только когда вопрос про код — программирование, автотесты и их инструменты, SQL, API/HTTP, \
git, Linux — или код прямо просят. Блок кода до 10 строк. Тестировщику (QA) — код с его стороны: запрос к API \
(requests), автотест pytest, SQL-проверка — а не реализация сервера; разработчику — рабочий код. Имена и адреса нейтральные (https://example.com), без реальных компаний. После кода ничего не пиши. \
К тест-дизайну, теории тестирования, процессам и вопросам про опыт код не нужен.

Примеры — безличные учебные ситуации (форма входа, корзина, поле «Возраст»), а не рассказ о проектах. \
Какие блоки нужны именно сейчас — сказано в <format> в конце запроса.

{depth}

Названия блоков пиши ровно так: «**Скажи:**», «**Шпаргалка:**», «**Пример-задача:**», «**Пример в коде:**».
Вопрос про опыт без данных в резюме — живой шаблон с местами для подстановки в [квадратных скобках]. \
Не вопрос — одна уместная фраза после **Скажи:**, без остальных блоков.
Отвечай по существу того, что спросили в <interviewer_question>, не уходи в соседние темы. \
<conversation_so_far> — фон: бери из него только то, что прямо относится к вопросу (например, стек компании).

""" + HUMAN_SPEECH

DEPTH_RULES = {
    "auto": """Глубина — по типу вопроса:
- Базовый вопрос о понятии, технике или технологии («что такое HTTP / REST / индекс», «в чём заключается техника…», \
«как работает…», «расскажите про…») — шанс показать понимание: «Скажи» из 3–5 фраз (суть простыми словами → \
как устроено → где пригождается на практике).
- Узкий или уточняющий вопрос («а какой код вернёте?», «а если…?», «почему не X?») — 1–3 фразы.""",
    "short": """Глубина — всегда коротко: «Скажи» из 2–3 фраз, пример — самый короткий (таблица — до 4 строк).""",
    "full": """Глубина — всегда развёрнуто: «Скажи» из 4–6 фраз (суть → как устроено → где пригождается → \
компромиссы и частые ошибки), примеры — подробнее обычного.""",
}


REPHRASE_RULES = """Сделай подсказку намного проще и короче — чтобы её можно было сказать вслух за 15 секунд, \
на {lang} языке. Формат строго:

**Скажи:** 2–3 коротких предложения, всего до 45 слов: только самая суть простыми словами, без терминов \
и перечислений; если уместно — одна бытовая аналогия («это как…»).

Больше ничего: без таблиц, кода и других блоков. Ничего не выдумывай.

""" + HUMAN_SPEECH


def answer_rules(settings: dict) -> str:
    if settings.get("answer_style") == "strict":
        full = settings.get("answer_depth") == "full"
        return ANSWER_RULES_STRICT + ("\n\nОтветь развёрнуто: подробнее обычного, с примером из практики, "
                                      "компромиссами и подводными камнями." if full else "")
    depth = DEPTH_RULES.get(settings.get("answer_depth") or "auto", DEPTH_RULES["auto"])
    # {lang} подставится позже в _system(); глубину вставляем сразу
    return ANSWER_RULES_LIVE.replace("{depth}", depth)


SCREEN_RULES = """Тебе показан экран кандидата во время собеседования — картинкой или текстом, распознанным \
с экрана (<screen_text>). Найди задачу и реши её на {lang} языке. \
Сначала определи тип задачи и отвечай в формате этого типа (тип не называй):

1. Программирование — написать или исправить код, алгоритм, SQL-запрос, автотест:
**Задача:** одной строкой, что требуется.
**Решение:** идея в 2–3 предложениях, как объяснить вслух; для алгоритма — сложность по времени и памяти.
Затем рабочий код в блоке кода (язык — как на экране, иначе Python) и 2–3 пограничных случая.

2. Тест-дизайн и тестирование — классы эквивалентности, граничные значения, тест-кейсы, чек-лист, \
«как протестировать», баг-репорт. Без кода и без «сложности»:
**Задача:** одной строкой.
**Скажи:** 2–3 предложения — как рассуждаешь (на какие классы делишь, где границы).
Таблица Markdown «| Поле | Класс | Значение | Ожидаем |»: валидные и невалидные классы и границы, одна проверка в строке.
Если просят минимальный набор данных — короткая таблица тест-кейсов, где одна строка закрывает сразу несколько \
валидных классов (невалидные — по одному в строке).
**Спорные места:** 2–4 коротких пункта — пробелы и противоречия в требованиях, что уточнить у аналитика.

3. Вопрос теста, логика, теория:
**Ответ:** коротко, затем 1–2 предложения — почему.

Код пиши только в задачах типа 1 или если на экране прямо просят код."""

REVIEW_RULES = """Ниже — расшифровка собеседования и ответы, которые подсказывал ассистент. \
Сделай разбор для подготовки к следующим собеседованиям, на {lang} языке:

## Итог
2–3 предложения: как прошло, общее впечатление.

## Вопросы, которые задавали
Нумерованный список всех вопросов интервьюера (переформулируй чётко).

## Где ответы были слабыми
Для каждого: вопрос → что прозвучало → как ответить лучше (коротко).

## Что повторить
Список тем для заучивания, от самых важных.

## Вероятные вопросы на следующем этапе
5–8 вопросов с короткими ответами."""


def _profile_block(s: dict) -> str:
    parts = [f"Роль/позиция кандидата: {s.get('role') or 'не указана'}"]
    if s.get("resume"):
        parts.append(f"<resume>\n{s['resume']}\n</resume>")
    if s.get("vacancy"):
        parts.append(f"<vacancy>\n{s['vacancy']}\n</vacancy>")
    if s.get("extra"):
        parts.append(f"Дополнительные пожелания кандидата к ответам:\n{s['extra']}")
    if s.get("my_voice"):
        parts.append(f"<candidate_voice>\nТак кандидат говорит сам:\n{s['my_voice']}\n</candidate_voice>")
    return "\n\n".join(parts)


def _system(rules: str, settings: dict) -> list[dict]:
    lang = LANG_NAMES.get(settings.get("language", "ru"), "русском")
    # Профиль стабилен в течение интервью → кэшируем, повторные запросы дешевле и быстрее
    return [
        {"type": "text", "text": rules.format(lang=lang)},
        {"type": "text", "text": _profile_block(settings), "cache_control": {"type": "ephemeral"}},
    ]


def provider_of(settings: dict) -> str:
    """claude | groq | gemini | … | none (не выполнен вход)."""
    p = settings.get("provider") or "none"
    return p if p == "claude" or p in providers.PROVIDERS else "none"


def supports_images(settings: dict) -> bool:
    p = provider_of(settings)
    return p == "claude" or (p in providers.PROVIDERS and providers.supports_images(p))


async def _stream(settings: dict, system: list[dict], content: list[dict], max_tokens: int) -> AsyncIterator[str]:
    provider = provider_of(settings)
    if provider == "none":
        raise LLMError("Сначала войди: ⋯ → Сменить способ входа.")
    if provider == "claude":
        async for t in _stream_claude(settings, system, content, max_tokens):
            yield t
        return
    if any(b["type"] == "image" for b in content) and not providers.supports_images(provider):
        content = [b for b in content if b["type"] != "image"]
        yield "_(Эта модель не видит изображения — подсказка только по разговору.)_\n\n"
    if not settings.get("model"):
        raise LLMError("Не выбрана модель — открой ⋯ → Настройки → ИИ-модель.")
    key = (settings.get("api_keys") or {}).get(provider, "")
    async for t in providers.stream(provider, key, settings["model"], system, content, max_tokens):
        yield t


EFFORTS = ("low", "medium", "high", "xhigh", "max")
AUTO_DEEP_EFFORT = "high"  # «Подробнее», задача с экрана, разбор: их и так ждут, первое слово ≈6 с вместо ≈1 с


def claude_effort(settings: dict) -> str:
    """Глубина размышлений Claude (ползунок в настройках). «Авто»: живые ответы — быстро, остальное — глубже."""
    level = settings.get("claude_effort") or "auto"
    if level in EFFORTS:
        return level
    return AUTO_DEEP_EFFORT if settings.get("_deep") else CLAUDE_EFFORT


async def _stream_claude(settings: dict, system: list[dict], content: list[dict], max_tokens: int) -> AsyncIterator[str]:
    model = settings.get("model") if settings.get("provider") == "claude" and settings.get("model") else CLAUDE_MODEL
    effort = claude_effort(settings)
    try:
        async with get_client().beta.messages.stream(
            model=model,
            # размышления у Opus 5.5 не выключаются и входят в max_tokens: даём запас, длину задаёт промпт
            # (на max простой вопрос съел 17 000 токенов размышлений)
            max_tokens=max(max_tokens, 16000 if effort in ("low", "medium") else 32000),
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": effort},
            # при отказе классификатора запрос автоматически перезапускается на запасной модели
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            async for text in stream.text_stream:
                yield text
            final = await stream.get_final_message()
            if final.stop_reason == "refusal":
                yield "\n\n_(Модель отказалась отвечать на этот запрос.)_"
            elif final.stop_reason == "max_tokens":
                yield "\n\n_(Ответ обрезан по длине.)_"
    except anthropic.AuthenticationError as e:
        raise LLMError("Сессия Claude истекла — войди снова.", auth=True) from e
    except anthropic.RateLimitError as e:
        raise LLMError("Claude: превышен лимит запросов, попробуй через несколько секунд.") from e
    except anthropic.APIStatusError as e:
        if "credit balance" in str(e.message).lower():
            raise LLMError("На балансе Claude Platform нет средств. Пополни: platform.claude.com → Settings → "
                           "Billing — или войди бесплатным способом (⋯ → Сменить способ входа).", billing=True) from e
        raise LLMError(f"Claude ({e.status_code}): {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise LLMError("Нет соединения с Anthropic API.") from e


ASSIST_RULES = """На скриншоте — экран кандидата прямо сейчас, ниже — последние реплики разговора (если есть). \
Пойми, что сейчас происходит (вопрос интервьюера, задача или код на экране, тест) и подскажи, \
что сказать или сделать прямо сейчас, на {lang} языке. Формат:

**Скажи:** 2–4 коротких живых предложения, которые легко произнести вслух (или главное действие).

Если на экране задача по программированию — дай идею решения, сложность и полный код в блоке кода. \
Если задача на тест-дизайн — короткая таблица «| Что проверяем | Значение | Ожидаем |», без кода.

""" + HUMAN_SPEECH

FOLLOWUP_RULES = """По ходу собеседования предложи кандидату 4–5 вопросов, которые уместно задать интервьюеру \
прямо сейчас, на {lang} языке: уточняющие по текущей задаче/теме и 1–2 вопроса о команде, процессах или продукте, \
связанные с услышанным. Нумерованный список, каждый вопрос одной строкой, без вступления."""

RECAP_RULES = """Сделай краткую сводку разговора на {lang} языке, чтобы кандидат быстро восстановил контекст:
- о чём шла речь и какие вопросы уже задали (5–8 пунктов)
- последний открытый вопрос или задача — отдельной строкой **Сейчас:**"""


def _conv(context: str) -> str:
    return f"<conversation_so_far>\n{context}\n</conversation_so_far>\n\n" if context else ""


GATE_RULE = """

ВАЖНО: эта реплика интервьюера может быть не вопросом. Сначала реши: есть ли в ней вопрос или задание \
кандидату? Если нет (приветствие, реакция на ответ, рассказ о компании или задаче без вопроса, проверка \
связи/звука/экрана) — ответь ровно одним словом: НЕТ. Если вопрос есть — отвечай в обычном формате."""


def _voice(voice: str) -> str:
    return f"<candidate_voice>\nНедавние реплики кандидата (его манера речи):\n{voice}\n</candidate_voice>\n\n" \
        if voice else ""


EXPERIENCE_Q = re.compile(
    r"(ваш|тво|сво|личн)\w* опыт|опыт (работы|с |в |на )|приходилось ли|доводилось ли|(есть|был[аио]?) ли у (вас|тебя)|"
    r"расскажите (о|про) (себе|проект|свой|ваш|последн|прошл)|на (прошлой|предыдущей|текущей|последней) работе|"
    r"как (вы|ты) (делали|решали|работали|внедряли|настраивали)|какие (проекты|задачи) (вы|ты)|"
    r"your experience|tell me about (yourself|a time|your)|have you ever",
    re.I,
)
NO_RESUME_NOTE = ("\n\n<note>Это вопрос про личный опыт, а резюме не заполнено. Не выдумывай проекты, компании, "
                  "технологии и цифры. Дай живой шаблон рассказа с местами для подстановки в [квадратных скобках], "
                  "например «На [проекте] я занимался [чем], самое сложное было [что]».</note>")


# Маршрутизация по типу вопроса (паттерн routing): тип определяем кодом — мгновенно и без лишних токенов —
# и в конце запроса говорим модели, какие блоки нужны. Иначе она вставляет все блоки подряд, включая
# ненужный код (например, к вопросам на тест-дизайн).
TEST_DESIGN_Q = re.compile(
    r"эквивалент|граничн|таблиц\w* (принятия )?решений|(переход|диаграмм)\w* состояни|попарн|тест-?дизайн|"
    r"как (бы )?(ты|вы) (бы )?(протестир|тестир|провер)|как (будешь|будете|стал[аи]? бы) (тестир|провер)|"
    r"какие проверки|что (бы )?(ты|вы) (бы )?(будешь |будете )?провер|"
    r"(составь|составьте|напиши|напишите|придумай|придумайте)\b.{0,40}(тест|проверк|чек-?лист)|"
    r"equivalence|boundary value|decision table|state transition|pairwise|test design|how would you test",
    re.I,
)
CODE_ASK_Q = re.compile(  # код прямо просят
    r"(напиш|покаж|приведи|набросай|реализуй)\w*.{0,30}(код|функци|скрипт|автотест|запрос|класс|метод|тест)|"
    r"\bв коде\b|\bкодом\b|автоматизир|автотест|на (python|питоне|java|sql)\b|"
    r"write (a |the )?(code|function|test|query)|in code\b",
    re.I,
)
CODE_TOPIC_Q = re.compile(  # тема — код, инструменты, данные
    r"pytest|unittest|фикстур|fixture|параметриз|selenium|селениум|playwright|appium|cypress|локатор|xpath|"
    r"селектор|page ?object|явн\w+ ожидан|неявн\w+ ожидан|\bwait\b|\bpython\b|питон|\bjava\b|javascript|"
    r"typescript|\bsql\b|\bselect\b|\bjoin\b|индекс|транзакц|\bбд\b|баз\w* данных|\bapi\b|\brest\b|http|json|"
    r"\bxml\b|curl|postman|swagger|graphql|grpc|websocket|\b(get|post|put|patch)\b|\bgit\b|\bгит\b|docker|"
    r"kubernetes|linux|линукс|bash|консол|терминал|ci ?/ ?cd|jenkins|gitlab|allure|декоратор|генератор|"
    r"итератор|lambda|лямбд|замыкан|наследован|полиморфизм|инкапсуляц|\bооп\b|исключени|exception|"
    r"async|многопоточ|алгоритм|массив|словар|кортеж|tuple|\bdict\b|regex|регулярн",
    re.I,
)
BEHAVIORAL_Q = re.compile(  # о себе и опыте: только живой рассказ
    r"о себе|почему (вы|ты) (хотите|хочешь|ушли|ушёл|ушел|уходите|уходишь|выбрали|выбрал)|сильн\w+ сторон|"
    r"слаб\w+ сторон|конфликт|зарплат|ожидани\w+ по (зп|зарплат|доход)|мотивац|где (вы|ты) (себя )?видите|"
    r"почему (мы|именно (вы|тебя|вас))|strength|weakness|why (do you want|should we)",
    re.I,
)
FOLLOWUP_Q = re.compile(  # уточнение по ходу разговора: «А если…», «А какой…»
    r"^\s*(а|и|ну|так|то есть|тогда|хорошо|окей|ок|понятно)[,\s]+(а\s+)?"
    r"(если|почему|как|какой|какая|какое|какие|что|зачем|когда|где|сколько|чем|в ч[её]м|разве)\b",
    re.I,
)
BASE_Q = re.compile(  # новый базовый вопрос, даже если начинается с «А…» («А что такое REST?»)
    r"что (вообще )?(такое|значит|означает)|как (вообще )?(работает|устроен)|расскаж|"
    r"в ч[её]м (заключается|суть|разница|отличие)|чем отлича|(зачем|для чего) (нужен|нужна|нужно|нужны)|"
    r"какие\b.{0,30}\b(бывают|есть|существуют|знаешь|знаете)",
    re.I,
)
LIST_Q = re.compile(  # просят перечислить или сравнить — нужна таблица-шпаргалка
    r"какие\b.{0,30}\b(бывают|есть|существуют|знаешь|знаете)|\bвиды\b|\bтипы\b|уровни|перечисл|назови|"
    r"чем отлича|в ч[её]м (разница|отличие)|разниц|сравни|\bvs\b|difference between|types of|kinds of",
    re.I,
)
MODE_PREFIX = re.compile(r"^((🗣 Проще|📖 Подробнее): )+")
QA_ROLE = re.compile(r"\bqa\b|\baqa\b|sdet|тестиров|тестировщ|quality|\bтест", re.I)

TABLE_HINT = ("таблица Markdown «| Что проверяем | Значение | Ожидаем |» с разделителем |---|---|---| "
              "(для проверок без значений — «| Что проверяем | Как | Ожидаем |»): валидные и невалидные классы "
              "и границы, одна проверка в строке; условие, которое уже дал интервьюер, бери как есть")
KIND_NOTES = {
    "design": f"Тип вопроса — тест-дизайн. Блоки: «**Скажи:**» и «**Пример-задача:**» — первая строка «Условие:», "
              f"дальше {TABLE_HINT}. Кода не нужно: «Пример в коде» не пиши.",
    "design_code": f"Тип вопроса — тест-дизайн с автоматизацией. Блоки: «**Скажи:**», «**Пример-задача:**» — первая "
                   f"строка «Условие:», дальше {TABLE_HINT}; затем «**Пример в коде:**» — автотест pytest "
                   f"с @pytest.mark.parametrize на значения из таблицы.",
    "code": "Тип вопроса — про код и инструменты. Блоки: «**Скажи:**», «**Шпаргалка:**» — если в ответе есть перечень однотипных пунктов, и «**Пример в коде:**» "
            "(до 10 строк); «**Пример-задача:**» — только если без неё непонятно.",
    "theory": "Тип вопроса — теория или процессы. Блоки: «**Скажи:**», «**Шпаргалка:**» — если в ответе есть перечень однотипных пунктов, и короткая «**Пример-задача:**» "
              "(первая строка «Условие:», 2–4 строки, конкретная ситуация). Кода не нужно.",
    "mixed": "Блоки: «**Скажи:**», «**Шпаргалка:**» — если в ответе есть перечень однотипных пунктов, затем «**Пример в коде:**», если по теме есть осмысленный код "
             "(до 10 строк), иначе короткая «**Пример-задача:**».",
    "followup": "Это уточняющий вопрос по ходу разговора: только «**Скажи:**» в 1–3 фразы, без других блоков.",
    "behavioral": "Вопрос про опыт или о себе: только блок «**Скажи:**» (заголовок ровно такой) — живой рассказ "
                  "от первого лица; без примеров и кода.",
}


def question_kind(question: str, settings: dict) -> str:
    """Тип вопроса → какие блоки нужны: design | design_code | code | theory | mixed | followup | behavioral."""
    code_ask = bool(CODE_ASK_Q.search(question))
    if EXPERIENCE_Q.search(question) or BEHAVIORAL_Q.search(question):
        return "behavioral"
    if FOLLOWUP_Q.search(question) and not code_ask and not BASE_Q.search(question):
        return "followup"
    if TEST_DESIGN_Q.search(question):
        return "design_code" if code_ask else "design"
    if code_ask or CODE_TOPIC_Q.search(question):
        return "code"
    # остальное: тестировщику — теория без кода, разработчику — код, если он осмысленный
    return "theory" if QA_ROLE.search(settings.get("role") or "") else "mixed"


def format_note(question: str, settings: dict) -> str:
    """Подсказка о формате — в конце запроса: длинный профиль (резюме) отодвигает правила далеко от ответа."""
    kind = question_kind(question, settings)
    must_table = kind in ("code", "theory", "mixed") and LIST_Q.search(question)
    cheat = " Вопрос просит перечислить или сравнить — «**Шпаргалка:**» обязательна." if must_table else ""
    return f"\n\n<format>{KIND_NOTES[kind]}{cheat}{_qa_code(kind, settings)} Других заголовков не делай; " \
           "после кода ничего не пиши.</format>"


def _qa_code(kind: str, settings: dict) -> str:
    """Тестировщику код нужен со своей стороны — запрос, автотест, SQL, а не сервер на Flask."""
    if kind in ("code", "mixed", "design_code") and QA_ROLE.search(settings.get("role") or ""):
        return " Код — со стороны тестировщика: запрос к API, автотест pytest или SQL-проверка, не код сервера."
    return ""


def stream_answer(question: str, context: str, settings: dict, gated: bool = False,
                  voice: str = "") -> AsyncIterator[str]:
    text = _voice(voice) + _conv(context) + f"<interviewer_question>\n{question}\n</interviewer_question>"
    if EXPERIENCE_Q.search(question) and not (settings.get("resume") or "").strip():
        text += NO_RESUME_NOTE  # без резюме опыт не выдумываем
    if settings.get("answer_style") != "strict":
        text += format_note(question, settings)
    rules = answer_rules(settings) + (GATE_RULE if gated else "")
    return _stream(settings, _system(rules, settings), [{"type": "text", "text": text}], 4000)


DEEPER_RULES = """Кандидат уже видел короткую подсказку (<hint>) и нажал «Подробнее»: ему нужно, о чём рассказать \
дальше. Ответь на {lang} языке и добавь то, чего в подсказке нет; её формулировки и примеры не повторяй. Формат строго:

**Скажи:** 3–5 новых фраз — как это устроено внутри и почему так, где это важно на практике, компромиссы.

**Шпаргалка:** таблица Markdown на 2–4 колонки — ключевые элементы темы с характеристиками \
(например, для REST: «| Метод | Что делает | Идемпотентен | Типичный ответ |»). Нечего сводить в таблицу — пропусти блок.

**Частые ошибки:** 2–4 пункта, каждый — одно короткое предложение: типичные ошибки и тонкости, о которых спрашивают.

Затем пример (какой — сказано в <format>): полнее, чем в подсказке, и на другой ситуации.

""" + HUMAN_SPEECH

DEEPER_NOTES = {
    "design": "Пример — «**Пример-задача:**»: новая задача, первая строка «Условие:», дальше таблица «| Что проверяем | "
              "Значение | Ожидаем |» — другие классы и границы, чем в подсказке. Кода не нужно.",
    "design_code": "Пример — «**Пример-задача:**» с таблицей проверок и «**Пример в коде:**» — автотест pytest "
                   "с @pytest.mark.parametrize (до 15 строк).",
    "code": "Пример — «**Пример в коде:**» до 15 строк, на другой ситуации, чем в подсказке.",
    "theory": "Пример — «**Пример-задача:**»: новая ситуация, первая строка «Условие:». Кода не нужно.",
    "mixed": "Пример — «**Пример в коде:**» до 15 строк, если по теме есть осмысленный код, иначе «**Пример-задача:**».",
    "followup": "Пример — «**Пример-задача:**»: короткая ситуация, первая строка «Условие:».",
    "behavioral": "Это вопрос про опыт: вместо всех блоков — только «**Скажи:**», подробный рассказ по схеме "
                  "ситуация → задача → что сделал → результат, 5–8 фраз, по резюме; без шпаргалки, ошибок и примеров.",
}


def stream_deeper(question: str, answer: str, context: str, settings: dict, voice: str = "") -> AsyncIterator[str]:
    """«Подробнее»: новый слой поверх уже показанной подсказки — устройство, шпаргалка, ошибки, другой пример."""
    if settings.get("answer_style") == "strict":
        return stream_answer(question, context, {**settings, "answer_depth": "full"}, voice=voice)
    kind = question_kind(question, settings)
    text = (_voice(voice) + _conv(context) + f"<interviewer_question>\n{question}\n</interviewer_question>\n\n"
            f"<hint>\n{answer}\n</hint>\n\n<format>{DEEPER_NOTES[kind]}{_qa_code(kind, settings)} Других заголовков не делай; "
            "после кода ничего не пиши.</format>")
    return _stream({**settings, "_deep": True}, _system(DEEPER_RULES, settings), [{"type": "text", "text": text}], 5000)


def stream_rephrase(question: str, answer: str, settings: dict, voice: str = "") -> AsyncIterator[str]:
    text = _voice(voice) + f"<question>\n{question}\n</question>\n\n<hint>\n{answer}\n</hint>"
    return _stream(settings, _system(REPHRASE_RULES, settings), [{"type": "text", "text": text}], 1500)


def _image(image_b64: str, media_type: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": image_b64}}


OCR_RULES = """Перепиши дословно весь текст задачи с экрана: условие, требования, данные, код (с отступами, \
в блоке кода), варианты ответа. Если на экране интерфейс — коротко перечисли его элементы (поля, кнопки, подписи). \
Ничего не решай и не комментируй."""


def _reads_separately(settings: dict) -> bool:
    """Картинки понимает отдельная, более слабая модель (Groq): пусть она только читает экран, а решает основная."""
    p = provider_of(settings)
    return p in providers.PROVIDERS and providers.PROVIDERS[p]["vision"] not in (None, "same")


async def _screenshot_two_step(image_b64: str, media_type: str, hint: str, settings: dict) -> AsyncIterator[str]:
    ask = hint or "Реши задачу с экрана."
    read = [_image(image_b64, media_type), {"type": "text", "text": "Перепиши текст с экрана."}]
    # без профиля: резюме для чтения экрана не нужно, а лимит токенов у модели зрения небольшой
    text = "".join([t async for t in _stream(settings, [{"type": "text", "text": OCR_RULES}], read, 3000)]).strip()
    if text:
        content = [{"type": "text", "text": f"<screen_text>\n{text}\n</screen_text>\n\n{ask}"}]
    else:  # прочитать не вышло — пусть решает модель зрения сама
        content = [_image(image_b64, media_type), {"type": "text", "text": ask}]
    async for t in _stream({**settings, "_deep": True}, _system(SCREEN_RULES, settings), content, 8000):
        yield t


def stream_screenshot(image_b64: str, media_type: str, hint: str, settings: dict) -> AsyncIterator[str]:
    if _reads_separately(settings):
        return _screenshot_two_step(image_b64, media_type, hint, settings)
    content = [_image(image_b64, media_type), {"type": "text", "text": hint or "Реши задачу с экрана."}]
    return _stream({**settings, "_deep": True}, _system(SCREEN_RULES, settings), content, 8000)


def stream_assist(image_b64: str | None, context: str, settings: dict) -> AsyncIterator[str]:
    content = [_image(image_b64, "image/jpeg")] if image_b64 else []
    content.append({"type": "text", "text": (_conv(context) or "Разговора пока нет.\n\n") + "Что мне сказать или сделать сейчас?"})
    return _stream(settings, _system(ASSIST_RULES, settings), content, 8000)


def stream_followups(context: str, settings: dict) -> AsyncIterator[str]:
    text = _conv(context) + "Какие вопросы мне задать интервьюеру сейчас?"
    return _stream(settings, _system(FOLLOWUP_RULES, settings), [{"type": "text", "text": text}], 2000)


def stream_recap(context: str, settings: dict) -> AsyncIterator[str]:
    return _stream(settings, _system(RECAP_RULES, settings), [{"type": "text", "text": _conv(context)}], 2000)


def stream_review(transcript: str, qa: list[dict], settings: dict) -> AsyncIterator[str]:
    hints = "\n\n".join(f"Вопрос: {q['question']}\nПодсказка: {q['answer']}" for q in qa)
    text = f"<transcript>\n{transcript}\n</transcript>\n\n<assistant_hints>\n{hints}\n</assistant_hints>"
    return _stream({**settings, "_deep": True}, _system(REVIEW_RULES, settings), [{"type": "text", "text": text}], 8000)
