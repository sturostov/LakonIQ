"""Банк готовых ответов (вопросы с собеседований QA → ответы в формате LakonIQ).

Вопросы лежат в kb/qa_questions.txt (в проекте), ответы генерирует tools/build_kb.py
в DATA_DIR/kb/qa_kb.json — они учитывают резюме, поэтому хранятся у пользователя, а не в проекте.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .config import DATA_DIR, RESOURCE_DIR

QUESTIONS_FILE = RESOURCE_DIR / "kb" / "qa_questions.txt"
KB_FILE = DATA_DIR / "kb" / "qa_kb.json"


def question_id(question: str) -> str:
    """Стабильный id по тексту вопроса: вставка новых вопросов не сдвигает старые."""
    return "q" + hashlib.sha1(question.strip().lower().encode()).hexdigest()[:12]


def parse_questions(text: str) -> list[dict]:
    """«## Тема» + строки «вопрос || синоним; синоним» → [{id, cat, q, aka}]."""
    items, cat = [], ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("## "):
            cat = line[3:].strip()
            continue
        if not line or line.startswith("#"):
            continue
        q, _, aka = line.partition("||")
        q = q.strip()
        items.append({"id": question_id(q), "cat": cat, "q": q,
                      "aka": [a.strip() for a in aka.split(";") if a.strip()]})
    return items


def load_questions(path: Path = QUESTIONS_FILE) -> list[dict]:
    return parse_questions(path.read_text(encoding="utf-8"))


def load(path: Path = KB_FILE) -> list[dict]:
    """Готовые ответы; пустой список, если базу ещё не собирали."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [i for i in data if i.get("answer")]


def save(items: list[dict], path: Path = KB_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------------- поиск готового ответа ----------------
import math  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402

import snowballstemmer  # noqa: E402

_RU = snowballstemmer.stemmer("russian")
_EN = snowballstemmer.stemmer("english")
_TOKEN = re.compile(r"[a-zа-я0-9]+")
# Служебные и «разговорные» слова: на смысл вопроса не влияют
_STOP = set("""
а и в во на по с со к ко о об обо от до из у за для про при без над под через же ли бы не ни то это этот эта эти
тот та те что чем как какой какая какое какие каких каким каков кто где когда куда зачем почему сколько чей
вы ты я мы он она оно они его ее их вам вас вами тебе тебя твой твоя твое твои ваш ваша ваше ваши наш мой свой себя
есть быть был была было были будет будут является являются
расскажите расскажи объясните объясни скажите скажи опишите опиши назовите назови приведите приведи поясните
можете можешь могли могу мог бы пожалуйста хорошо ладно окей ок так ну вот давайте давай понятно ага угу
такое такой такая такие вообще именно просто еще уже очень все всё весь вся также тоже либо или ну-ка
нужно надо можно сейчас знаете знаешь думаете думаешь например примеры пример какую каком
чего нужен нужна нужны бывают бывает существуют существует используете используешь будешь будете py
слушай слушайте смотри смотрите значит означает допустим кстати должно должен должна должны входит входят включает
the a an what how why which
""".split())
# Как Whisper пишет термины по-русски → как они записаны в базе
_TRANSLIT = {  # ключи — основы слов после русского стеммера
    "скоуп": "scope", "фреймворк": "framework", "локатор": "locator", "эндпоинт": "endpoint", "токен": "token",
    "пэйдж": "page", "пейдж": "page", "обжект": "object", "айфрейм": "iframe", "фикстур": "фикстур",
    "пайплайн": "pipeline", "джоб": "job", "джобы": "job", "девопс": "devops", "фронтенд": "frontend", "бэкенд": "backend",
    "апи": "api", "рест": "rest", "пайтест": "pytest", "пайтон": "python", "питон": "python", "селениум": "selenium",
    "селен": "selenium", "плейрайт": "playwright", "гит": "git", "гитхаб": "github", "гитлаб": "gitlab", "докер": "docker",
    "джейсон": "json", "постман": "postman", "джира": "jira", "смоук": "smoke", "санити": "sanity", "сваггер": "swagger",
    "кафка": "kafka", "джанго": "django", "джейваскрипт": "javascript", "аллюр": "allure", "дженкинс": "jenkins",
    "эскьюэль": "sql", "сиквел": "sql", "кьюэй": "qa", "иксмл": "xml", "аппиум": "appium", "андроид": "android",
}


def _stem(token: str) -> str:
    if token.isdigit():
        return token
    if token.isascii():
        return _EN.stemWord(token)
    st = _RU.stemWord(token)
    latin = _TRANSLIT.get(st) or _TRANSLIT.get(token)
    return _EN.stemWord(latin) if latin else _CANON.get(st, st)


# «разница», «отличие», «чем лучше» — один смысл: сравнение
_CANON = {"разниц": "отлича", "отлич": "отлича", "различ": "отлича", "лучш": "отлича", "вэб": "веб"}
# Обороты, которые не несут смысла вопроса
_IDIOMS = re.compile(r"в первую очередь|в целом|на ваш взгляд|на твой взгляд|по вашему мнению|по-твоему|"
                     r"с вашей точки зрения|как вы думаете|как ты думаешь|если коротко|в двух словах", re.I)
# одно такое слово не может само по себе определять вопрос (в синонимах)
_GENERIC_WORDS = ("отличается тестирование тестировать тест testing test проверка проверять работать использовать "
                  "сделать основной главный важный тестировщик тестировщика")


# Уточнения «в питоне», «в API», «тестирование»: если в вопросе базы их нет, почти не штрафуем
_QUALIFIERS = {_stem(w) for w in "python pytest selenium playwright sql http api web веб тестирование тестировщику "
               "автотесты тесты приложение проекте работе компании".split()}


_COMPOUND = re.compile(r"\b([a-zа-я0-9])\s*/\s*([a-zа-я0-9])\b")  # «A/B» → «ab» (одиночные буквы иначе выпадают)


def stems(text: str) -> list[str]:
    text = _COMPOUND.sub(lambda m: m.group(1) + m.group(2), _IDIOMS.sub(" ", text.lower().replace("ё", "е")))
    words = _TOKEN.findall(text)
    return [_stem(w) for w in words if w not in _STOP and len(w) > 1]


def _same(a: str, b: str) -> bool:
    """Одна основа или однокоренные: «регресс» ~ «регрессион», «запуст» ~ «запуска», «тестирова» ~ «протестирова»."""
    if a == b:
        return True
    short = min(len(a), len(b))
    if short < 5:
        return False
    common = len(os.path.commonprefix([a, b]))
    if common == short and common >= 5:
        return True  # одно — начало другого: «регресс» ~ «регрессион»
    if common >= max(5, 0.8 * short) and max(len(a), len(b)) - common <= 2:
        return True  # разные окончания: «запуст(ить)» ~ «запуска(ть)», но не «тестирован(ие)» ~ «тестировщ(ик)»
    longer, shorter = (a, b) if len(a) > len(b) else (b, a)
    return short >= 7 and len(longer) - short <= 4 and longer.endswith(shorter)  # приставка: про-, пере-, за-


_GENERIC = {_stem(w) for w in _GENERIC_WORDS.split()}


class Index:
    """Ищет в банке вопрос, который задал интервьюер. Уверенность 0–1: насколько слова вопроса из базы
    прозвучали (полнота) и насколько вопрос интервьюера ими объясняется (точность), с весом по редкости слова."""

    HIT = 0.71      # с такой уверенностью показываем готовый ответ сразу (подобрано по живым формулировкам)
    MARGIN = 0.06   # и он заметно лучше второго кандидата

    def __init__(self, items: list[dict]):
        self.items = items
        # фразы: сам вопрос и каждый синоним — совпасть достаточно с любой
        self.phrases = []
        for i in items:
            first = re.split(r"(?<=[?.!])\s+", i["q"])[0]
            # первое предложение — отдельно, только если это вопрос («Что такое генератор? Чем он…»);
            # «Напишите баг-репорт.» после условия задачи слишком общее само по себе
            parts = [i["q"]] + ([first] if first != i["q"] and first.endswith("?") else []) + i.get("aka", [])
            n_own = 2 if first != i["q"] and first.endswith("?") else 1
            phrases = [(set(stems(p)), 1.0 if k < n_own else 0.97) for k, p in enumerate(parts)]
            phrases = [(ph, w) for k, (ph, w) in enumerate(phrases)
                       if ph and (k < n_own or not (len(ph) == 1 and next(iter(ph)) in _GENERIC))]
            self.phrases.append(phrases or [(set(), 1.0)])
        df: dict[str, int] = {}
        for ph in self.phrases:
            for t in set().union(*(p for p, _ in ph)):
                df[t] = df.get(t, 0) + 1
        n = max(1, len(items))
        self.idf = {t: math.log(1 + n / c) for t, c in df.items()}
        self.unknown_idf = math.log(1 + n)  # незнакомое слово — самое «редкое»: снижает точность совпадения

    def _w(self, t: str) -> float:
        return self.idf.get(t, self.unknown_idf)

    def _score(self, q: set[str], phrase: set[str]) -> float:
        if not q or not phrase:
            return 0.0
        hit_p = {p for p in phrase if any(_same(p, t) for t in q)}
        hit_q = {t for t in q if any(_same(t, p) for p in phrase)}
        recall = sum(map(self._w, hit_p)) / sum(map(self._w, phrase))
        missed = sum(self._w(t) * (0.35 if t in _QUALIFIERS else 1.0) for t in q - hit_q)
        matched = sum(map(self._w, hit_q))
        precision = matched / (matched + missed) if matched else 0.0
        return math.sqrt(recall * precision)

    def search(self, question: str, k: int = 3) -> list[tuple[float, dict]]:
        q = set(stems(question))
        scored = []
        for item, phrases in zip(self.items, self.phrases):
            best = max(self._score(q, ph) * w for ph, w in phrases)
            # общий словарь вопроса и синонимов — для длинных вопросов интервьюера
            joint = self._score(q, set().union(*(ph for ph, _ in phrases))) * 0.9
            scored.append((max(best, joint), item))
        scored.sort(key=lambda x: -x[0])
        return scored[:k]

    def match(self, question: str) -> dict | None:
        top = self.search(question, 2)
        if not top or top[0][0] < self.HIT:
            return None
        if len(top) > 1 and top[0][0] - top[1][0] < self.MARGIN and top[1][1]["cat"] != top[0][1]["cat"]:
            return None  # два разных вопроса одинаково похожи — пусть отвечает модель
        return {**top[0][1], "score": round(top[0][0], 3)}


_index: Index | None = None
_mtime: float | None = None


def index() -> Index | None:
    """Индекс банка ответов; перестраивается, если файл обновился (например, сборка закончилась на ходу)."""
    global _index, _mtime
    try:
        mtime = os.stat(KB_FILE).st_mtime
    except OSError:
        _index, _mtime = None, None
        return None
    if _index is None or mtime != _mtime:
        items = load()
        try:  # свежие синонимы из файла вопросов: правки поиска без новой генерации ответов
            aka = {i["id"]: i["aka"] for i in load_questions()}
            items = [{**i, "aka": aka.get(i["id"], i.get("aka", []))} for i in items]
        except OSError:
            pass
        _index, _mtime = (Index(items) if items else None), mtime
    return _index


def match(question: str) -> dict | None:
    idx = index()
    return idx.match(question) if idx else None
