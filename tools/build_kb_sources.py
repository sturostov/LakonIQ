"""Ответы банка из открытых источников — почти без изменений.

Для вопросов из kb/sources_map.json берёт статью из QA-Theory (KazeKaze93) или главу «Библии QA»
(VladislavEremeev/QA_bible), чистит разметку GitBook, длинные главы обрезает по границе абзаца
и, если в тексте нет примера, добавляет «Пример-задача» / «Пример в коде» из сгенерированного ответа.
Остальные вопросы сохраняют сгенерированный ответ (tools/build_kb.py).
Если tools/build_kb.py не запускали, банк собирается бесплатно из одних источников:
вопросы без статьи в него не попадают, примеры не добавляются.

Тексты источников принадлежат авторам (QA Bible — GPL-3.0; у QA-Theory лицензия не указана), поэтому
скачиваются и хранятся только у пользователя: ~/Library/Application Support/LakonIQ/kb/.

    .venv/bin/python tools/build_kb_sources.py
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import kb  # noqa: E402

SOURCES_DIR = kb.KB_FILE.parent / "sources"
REPOS = {
    "qa_theory": ("https://github.com/KazeKaze93/QA-Theory.git", "QA-Theory", "https://kaze.gitbook.io/qa-theory/"),
    "qa_bible": ("https://github.com/VladislavEremeev/QA_bible.git", "QA Bible", "https://vladislaveremeev.gitbook.io/qa_bible/"),
}
LIMIT = 3500  # длиннее — обрезаем по абзацу: в карточке на собеседовании нужна суть
EXAMPLE = re.compile(r"```|^\s*\|?\s*:?-{3,}|(?<![а-яё])(пример(?!но)|например)|e\.g\.|for example", re.I | re.M)
TAIL = re.compile(r"(?mi)^\W{0,4}(источник|источники|доп\.? ?материал|дополнительные материалы|полезные ссылки|литература|ссылки)\b")


def ensure_sources() -> None:
    SOURCES_DIR.mkdir(parents=True, exist_ok=True)
    for name, (url, _, _) in REPOS.items():
        if not (SOURCES_DIR / name).exists():
            print(f"скачиваю {url}")
            subprocess.run(["git", "clone", "-q", "--depth", "1", url, str(SOURCES_DIR / name)], check=True)


def _unescape(text: str) -> str:
    return re.sub(r"\\([\\`*_{}\[\]()#+\-.!>|~])", r"\1", text)


def clean(md: str) -> str:
    """Разметка GitBook → то, что умеет карточка LakonIQ."""
    md = md.replace("\r\n", "\n")
    md = re.sub(r"\A\s*#\s+[^\n]*\n", "", md)                      # заголовок статьи — в карточке уже есть вопрос
    md = re.sub(r"<!--.*?-->", "", md, flags=re.S)
    md = re.sub(r"\{%[^%]*%\}", "", md)                              # теги GitBook (hint, embed, tabs…)
    md = re.sub(r"<figure>.*?</figure>", "", md, flags=re.S)
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)                     # картинки
    md = re.sub(r"<img[^>]*>", "", md)
    md = re.sub(r"<br\s*/?>", "\n", md)
    md = re.sub(r'<a [^>]*href="(https?://[^"]+)"[^>]*>(.*?)</a>', r"[\2](\1)", md, flags=re.S)
    md = re.sub(r"</?(mark|details|summary|p|div|span|strong|em|b|i|u|sup|sub)[^>]*>", "", md)
    md = re.sub(r"\[([^\]]+)\]\((?!https?:)[^)]*\)", r"\1", md)       # относительные ссылки — просто текст
    m = TAIL.search(md)
    if m and m.start() > 200:
        md = md[:m.start()]                                          # хвост «Источники / доп. материал»
    parts = re.split(r"(```.*?```)", md, flags=re.S)                 # вне блоков кода:
    for k in range(0, len(parts), 2):
        p = parts[k]
        p = _unescape(p)
        p = re.sub(r"(?m)^>\s?", "", p)                              # цитаты
        p = re.sub(r"(?m)^\s*(\*\*\*|---|___)\s*$", "", p)           # горизонтальные линии
        p = re.sub(r"(?m)^#{1,6}\s+", "### ", p)                     # заголовки — компактные (h4)
        parts[k] = p
    md = "".join(parts)
    md = re.sub(r"[ \t]+\n", "\n", md)
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def trim(md: str, limit: int = LIMIT) -> tuple[str, bool]:
    """Обрезка по границе абзаца; блок кода и таблица не разрываются."""
    if len(md) <= limit:
        return md, False
    paras, buf, inside = [], [], False
    for p in md.split("\n\n"):
        buf.append(p)
        if p.count("```") % 2:
            inside = not inside
        if not inside:
            paras.append("\n\n".join(buf))
            buf = []
    if buf:
        paras.append("\n\n".join(buf))
    out, size = [], 0
    for para in paras:
        if size + len(para) > limit and out:
            break
        out.append(para)
        size += len(para) + 2
    return "\n\n".join(out).strip(), True


def examples_from(generated: str) -> str:
    """Блоки «Пример-задача» и «Пример в коде» из сгенерированного ответа."""
    blocks = re.findall(r"(\*\*Пример(?:-задача| в коде):\*\*.*?)(?=\n\*\*[^*\n]{2,40}:\*\*|\Z)", generated, re.S)
    return "\n\n".join(b.strip() for b in blocks)


def compose(entry: dict, generated: str) -> dict:
    parts, cut, refs = [], False, []
    for s in entry["sources"]:
        name, base = REPOS[s["src"]][1], REPOS[s["src"]][2]
        text, was_cut = trim(clean((SOURCES_DIR / s["src"] / s["path"]).read_text(encoding="utf-8")))
        cut |= was_cut
        if len(entry["sources"]) > 1:
            text = f"### {s['title']}\n\n{text}"
        parts.append(text)
        refs.append({"name": name, "title": s["title"], "url": base + re.sub(r"(/README)?\.md$", "", s["path"])})
    body = "\n\n".join(parts)
    added = False
    if not EXAMPLE.search(body):
        ex = examples_from(generated)
        if ex:
            body += "\n\n" + ex
            added = True
    links = " · ".join(f"[{r['name']} — «{r['title']}»]({r['url']})" for r in refs)
    body += f"\n\n📖 {'Полный текст' if cut else 'Источник'}: {links}"
    return {"answer": body, "sources": refs, "example_added": added, "trimmed": cut}


def main() -> None:
    ensure_sources()
    mapping = json.loads((ROOT / "kb" / "sources_map.json").read_text(encoding="utf-8"))
    items = kb.load()
    if not items:
        print("Сгенерированных ответов нет — собираю банк только из открытых источников (бесплатно).")
        items = [dict(it, answer_gen="") for it in kb.load_questions(ROOT / "kb" / "qa_questions.txt")]
    stats = {"source": 0, "example_added": 0, "trimmed": 0, "generated": 0}
    out = []
    for it in items:
        if "answer_gen" not in it:
            it["answer_gen"] = it["answer"]
        entry = mapping.get(it["id"])
        if not entry:
            if not it["answer_gen"]:
                continue  # ни статьи, ни сгенерированного ответа
            it["answer"], it["sources"] = it["answer_gen"], []
            stats["generated"] += 1
            out.append(it)
            continue
        it.update(compose(entry, it["answer_gen"]))
        stats["source"] += 1
        stats["example_added"] += it["example_added"]
        stats["trimmed"] += it["trimmed"]
        out.append(it)
    kb.save(out)
    print(f"Ответов из источников: {stats['source']} (пример добавлен в {stats['example_added']}, "
          f"обрезано длинных глав: {stats['trimmed']}); сгенерированных: {stats['generated']} → {kb.KB_FILE}")


if __name__ == "__main__":
    main()
