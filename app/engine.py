"""Оркестратор: звук → распознавание → определение вопроса → ответ Claude → интерфейс."""
from __future__ import annotations

import asyncio
import base64
import io
import re
import time
from typing import AsyncIterator, Callable


from . import db, kb, llm, providers, qdetect
from .audio import SourceError, open_source, refresh_devices
from .auth import auth
from .login import account_label, login
from .logs import log
from .config import DEEPGRAM_API_KEY
from .stt import run_deepgram
from .stt_whisper import run_whisper

SPEAKER_NAMES = {"interviewer": "Интервьюер", "me": "Я"}
TRIGGER_DELAY = 0.6     # пауза после конца фразы перед автоответом, сек
MERGE_WINDOW = 6.0      # дозаданный вопрос, пока ответ ещё пишется, — пересобрать в один
MIN_QUESTION_CHARS = 6
STT_LABELS = {"deepgram": "Deepgram", "groq": "Groq Whisper"}
GATE_NO = "НЕТ"  # ответ модели, если реплика оказалась не вопросом
# Подсказка для Whisper: так он пишет термины правильно («GIL в Python», а не «гил в пайтоне»)
WHISPER_TERMS = ("Python, GIL, asyncio, async/await, Django, FastAPI, Flask, PostgreSQL, SQL, Redis, Kafka, "
                 "RabbitMQ, Docker, Kubernetes, REST API, ORM, SQLAlchemy, Celery, pytest, git, CI/CD, "
                 "микросервисы, многопоточность, JavaScript, TypeScript, React, Java, Go, Linux, AWS")


def whisper_prompt(s: dict) -> str:
    role = (s.get("role") or "разработчик").strip()
    return f"Техническое собеседование. Позиция: {role}. Термины: {WHISPER_TERMS}."


async def _instant(text: str) -> AsyncIterator[str]:
    """Готовый текст как «поток» из одного куска — карточка появляется сразу."""
    yield text


def _kb_text(hit: dict) -> str:
    return f"_📚 Из банка ответов: «{hit['q']}»_\n\n{hit['answer']}"


class Engine:
    def __init__(self) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.clients: set = set()
        self.listening = False
        self.session_id: int | None = None
        self.sources: dict = {}  # роль → источник звука
        self.tasks: list[asyncio.Task] = []
        self.lines: list[tuple[str, str]] = []      # (speaker, text) — финальные фразы
        self.pending: list[str] = []                # фразы интервьюера, ещё не отвеченные
        self.pending_start = 0
        self._trigger: asyncio.TimerHandle | None = None
        self.answer_tasks: dict[int, asyncio.Task] = {}
        self.last_live: dict | None = None          # {"id", "question", "started", "context_end"}
        # хуки окна (ставит desktop.py), чтобы на скриншоте не было нашего окна
        self.hide_window: Callable[[], None] | None = None
        self.show_window: Callable[[], None] | None = None

    # ---------- связь с интерфейсом ----------
    async def broadcast(self, event: dict) -> None:
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(event)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def state(self) -> dict:
        s = db.get_settings()
        return {
            "type": "state",
            "listening": self.listening,
            "session_id": self.session_id,
            "auto_answer": s["auto_answer"],
            "keys": {"anthropic": auth.ok, "deepgram": bool(self.stt_choice(s)[0] == "deepgram")},
            "stt": {"kind": self.stt_choice(s)[0], "error": "" if self.stt_choice(s)[0] else self.stt_choice(s)[1],
                    "label": STT_LABELS.get(self.stt_choice(s)[0], "")},
            "auth": auth.public(),
            "ai": {"provider": llm.provider_of(s), "model": s.get("model") or "", "ready": self.ai_ready(s),
                   "account": account_label(s) if self.ai_ready(s) else "", "checked": auth.checked,
                   "effort": s.get("claude_effort") or "auto"},
            "login": login.public(),
            "sources": {k: v.name for k, v in self.sources.items()},
        }

    @staticmethod
    def stt_choice(s: dict) -> tuple[str, str]:
        """("deepgram" | "groq", ключ) или ("", текст ошибки)."""
        keys = s.get("api_keys") or {}
        deepgram, groq = keys.get("deepgram") or DEEPGRAM_API_KEY, keys.get("groq", "")
        pref = s.get("stt") or "auto"
        if pref == "deepgram":
            return ("deepgram", deepgram) if deepgram else ("", "Нет ключа Deepgram — добавь его в ⋯ → Настройки → Распознавание речи.")
        if pref == "groq":
            return ("groq", groq) if groq else ("", "Нет ключа Groq — войди через Groq на экране входа (это бесплатно).")
        if deepgram:
            return "deepgram", deepgram
        if groq:
            return "groq", groq
        return "", ("Для распознавания речи нужен бесплатный ключ Groq (⋯ → Сменить способ входа → Войти через Groq) "
                    "или ключ Deepgram (⋯ → Настройки).")

    def ai_ready(self, s: dict | None = None) -> bool:
        s = s or db.get_settings()
        provider = llm.provider_of(s)
        if provider == "none":
            return False
        if provider == "claude":
            return auth.ok and not auth.billing_error
        cfg = providers.PROVIDERS[provider]
        return bool(s.get("model")) and (cfg.get("no_key") or bool((s.get("api_keys") or {}).get(provider)))

    async def error(self, msg: str) -> None:
        log.warning("ошибка для пользователя: %s", msg)
        await self.broadcast({"type": "error", "message": msg})

    def run_soon(self, coro) -> None:
        """Запуск корутины из другого потока (горячие клавиши, окно)."""
        if self.loop:
            asyncio.run_coroutine_threadsafe(coro, self.loop)

    async def handle(self, msg: dict) -> None:
        action = msg.get("action")
        if action == "start":
            await self.start()
        elif action == "stop":
            await self.stop()
        elif action == "toggle":
            await (self.stop() if self.listening else self.start())
        elif action == "answer":
            await self.answer_now((msg.get("text") or "").strip())
        elif action == "screenshot":
            await self.screenshot(msg.get("hint", ""))
        elif action == "cancel":
            task = self.answer_tasks.get(int(msg.get("id", 0)))
            if task:
                task.cancel()
        elif action == "set_auto":
            db.update_settings({"auto_answer": bool(msg.get("value"))})
            await self.broadcast(self.state())
        elif action == "review":
            await self.review(msg.get("session_id") or self.session_id)
        elif action == "login_start":
            await login.start(str(msg.get("provider", "")))
        elif action == "login_key":
            await login.with_key(str(msg.get("provider", "")), str(msg.get("key", "")))
        elif action == "login_cancel":
            await login.cancel()
        elif action == "logout":
            await self.stop()
            await login.cancel(silent=True)
            await login.logout()
            await self.broadcast(self.state())
        elif action == "auth_check":
            await auth.check()
            await self.broadcast(self.state())
        elif action == "assist":
            await self.assist()
        elif action == "followups":
            await self._simple("💬 Уточняющие вопросы", "followups", llm.stream_followups)
        elif action == "recap":
            await self._simple("📋 Кратко о разговоре", "recap", llm.stream_recap)
        elif action == "reset":
            await self.reset()
        elif action == "save_last":
            await self.save_last()
        elif action == "rephrase":
            await self.rephrase(int(msg.get("id", 0)))
        elif action == "deeper":
            await self.deeper(int(msg.get("id", 0)))
        elif action == "fresh":
            await self.fresh(int(msg.get("id", 0)))
        elif action == "clear_pending":
            self.pending.clear()
            await self.broadcast({"type": "pending", "text": ""})

    # ---------- действия в стиле Cluely ----------
    async def assist(self) -> None:
        """⌘↵: есть неотвеченный вопрос — отвечаем; иначе смотрим на экран + разговор."""
        self._cancel_trigger()
        if self.pending:
            self.last_live = None
            return await self._answer_pending()
        if not self.ai_ready():
            return await self.error("Сначала подключи ИИ: ⋯ → Настройки → ИИ-модель.")
        image = None
        if llm.supports_images(db.get_settings()):
            try:
                image = await asyncio.to_thread(self._grab_screen)
            except Exception:
                image = None  # нет разрешения на запись экрана — подсказываем только по разговору
        context = self.transcript_text()[-4000:]
        if not image and not context:
            return await self.error("Нечего анализировать: разреши «Запись экрана» или начни слушать разговор.")
        title = "✨ Что сказать?" + (" · экран" if image else "")
        await self._start_answer(title, "assist", llm.stream_assist(image, context, db.get_settings()))

    async def _simple(self, title: str, source: str, fn) -> None:
        context = self.transcript_text()[-6000:]
        if not context:
            return await self.error("Пока ничего не прозвучало — подожди, пока интервьюер что-то скажет."
                                    if self.listening else "Разговора ещё нет — нажми «Слушать».")
        await self._start_answer(title, source, fn(context, db.get_settings()))

    async def rephrase(self, qa_id: int) -> None:
        """«Проще»: пересказать готовую подсказку живыми словами (новой карточкой)."""
        qa = db.get_qa(qa_id)
        if not qa or not qa["answer"].strip():
            return await self.error("Подожди, пока ответ допишется.")
        if not self.ai_ready():
            return await self.error("Сначала войди: ⋯ → Сменить способ входа.")
        question = llm.MODE_PREFIX.sub("", qa["question"])
        await self._start_answer(f"🗣 Проще: {question}", "rephrase", llm.stream_rephrase(
            question, qa["answer"], db.get_settings(), voice=self.voice_sample()))

    def kb_answer(self, question: str, settings: dict) -> dict | None:
        """Готовый ответ из банка вопросов — только если вопрос узнали уверенно."""
        if not settings.get("kb_answers", True):
            return None
        kind = llm.question_kind(question, settings)
        if kind == "followup":
            return None  # «А если…» — зависит от разговора, отвечает модель
        try:
            hit = kb.match(question)
        except Exception as e:  # noqa: BLE001 — поиск не должен ломать ответ
            log.warning("поиск в банке ответов: %s", e)
            return None
        if hit and kind == "behavioral" and hit["cat"] != "Поведенческие":
            return None  # «был ли опыт с Kafka» — вопрос про опыт, а не «что такое брокер»
        if hit:
            log.info("ответ из банка (%.2f): %s → %s", hit["score"], question[:80], hit["q"])
        return hit

    async def fresh(self, qa_id: int) -> None:
        """«Свежий ответ»: вместо готового из банка — новый от модели, с учётом разговора."""
        qa = db.get_qa(qa_id)
        if not qa:
            return
        if not self.ai_ready():
            return await self.error("Сначала войди: ⋯ → Сменить способ входа.")
        question = llm.MODE_PREFIX.sub("", qa["question"])
        context = self.transcript_text()[-2500:]
        await self._start_answer(question, "live", llm.stream_answer(
            question, context, db.get_settings(), voice=self.voice_sample()))

    async def deeper(self, qa_id: int) -> None:
        """«Подробнее»: новый слой поверх показанной подсказки — устройство, шпаргалка, частые ошибки, другой пример."""
        qa = db.get_qa(qa_id)
        if not qa or not qa["answer"].strip():
            return await self.error("Подожди, пока ответ допишется.")
        if not self.ai_ready():
            return await self.error("Сначала войди: ⋯ → Сменить способ входа.")
        question = llm.MODE_PREFIX.sub("", qa["question"])
        context = self.transcript_text()[-2500:]
        await self._start_answer(f"📖 Подробнее: {question}", "deeper", llm.stream_deeper(
            question, qa["answer"], context, db.get_settings(), voice=self.voice_sample()))

    async def reset(self) -> None:
        """⌘R: очистить экран и остановить генерацию (сохранённое в базе не трогаем)."""
        self._cancel_trigger()
        for task in list(self.answer_tasks.values()):
            task.cancel()
        self.pending.clear()
        self.last_live = None
        await self.broadcast({"type": "reset"})

    async def save_last(self) -> None:
        items = db.list_qa(limit=1, session_id=self.session_id) if self.session_id else db.list_qa(limit=1)
        if not items:
            return await self.error("Нет ответа для сохранения.")
        item = db.update_qa(items[0]["id"], {"saved": True})
        await self.broadcast({"type": "qa_updated", "qa": item})
        await self.broadcast({"type": "info", "message": "★ Добавлено в базу"})

    # ---------- старт/стоп прослушивания ----------
    async def start(self) -> None:
        if self.listening:
            return
        if not self.ai_ready():
            return await self.error("Сначала войди: ⋯ → Сменить способ входа.")
        s = db.get_settings()
        stt_kind, stt_key = self.stt_choice(s)
        if not stt_kind:
            return await self.error(stt_key)
        roles = {"interviewer": s["interviewer_device"], "me": s["me_device"]}
        await asyncio.to_thread(refresh_devices)  # чтобы находились наушники, подключённые после запуска
        for role, name in roles.items():
            if not name:
                continue
            try:
                src = await asyncio.to_thread(open_source, name, self.loop)
            except SourceError as e:
                await self.error(f"{SPEAKER_NAMES[role]}: {e}")
                continue
            self.sources[role] = src
            on_event = lambda ev, r=role: self.on_stt(r, ev)  # noqa: E731
            if stt_kind == "deepgram":
                stt = run_deepgram(src.queue, src.sample_rate, s["language"], on_event, self.error, stt_key)
            else:
                stt = run_whisper(src.queue, src.sample_rate, s["language"], on_event, self.error,
                                  stt_key, whisper_prompt(s), relaxed=(role == "me"))
            self.tasks.append(asyncio.create_task(stt))
        if "interviewer" not in self.sources:
            await self._teardown()
            return await self.error("Звук интервьюера не запущен — проверь источник в ⋯ → Настройки → Звук.")
        self.tasks.append(asyncio.create_task(self._levels()))
        self.lines.clear()
        self.pending.clear()
        self.session_id = db.create_session()
        self.listening = True
        log.info("прослушивание начато: сессия %s, источники %s, распознавание %s",
                 self.session_id, {k: v.name for k, v in self.sources.items()}, stt_kind)
        await self.broadcast(self.state())

    async def _teardown(self) -> None:
        for t in self.tasks:
            t.cancel()
        self.tasks.clear()
        for src in self.sources.values():
            src.stop()
        self.sources.clear()

    async def stop(self) -> None:
        if not self.listening:
            return
        log.info("останавливаю прослушивание (сессия %s)", self.session_id)
        await self._teardown()
        self.listening = False
        if self.session_id:
            db.end_session(self.session_id, self.transcript_text())
        await self.broadcast(self.state())

    async def _levels(self) -> None:
        while True:
            await asyncio.sleep(0.15)
            await self.broadcast({"type": "levels", **{k: round(v.level, 3) for k, v in self.sources.items()}})
            for v in self.sources.values():
                v.level *= 0.5

    def voice_sample(self) -> str:
        """Как говорит сам кандидат: его реплики «Я» из этого разговора, а если их мало — из прошлых."""
        mine = [t for sp, t in self.lines if sp == "me"][-12:]
        if len(mine) < 5:
            for sess in db.list_sessions()[:5]:
                transcript = (db.get_session(sess["id"]) or {}).get("transcript", "")
                old = [ln[3:].strip() for ln in transcript.splitlines() if ln.startswith("Я: ")]
                mine = old[-12:] + mine
                if len(mine) >= 12:
                    break
        return "\n".join(mine[-12:])[-1200:]

    def transcript_text(self, start: int = 0, end: int | None = None) -> str:
        return "\n".join(f"{SPEAKER_NAMES[s]}: {t}" for s, t in self.lines[start:end])

    # ---------- распознавание и определение вопроса ----------
    async def on_stt(self, role: str, ev: dict) -> None:
        kind = ev.get("type")
        if kind == "Results":
            text = ev["channel"]["alternatives"][0]["transcript"].strip()
            is_final = ev.get("is_final", False)
            speech_final = ev.get("speech_final", False)
            if text:
                await self.broadcast({"type": "transcript", "speaker": role, "text": text, "final": is_final})
                if is_final:
                    if role == "interviewer":
                        if not self.pending:
                            self.pending_start = len(self.lines)
                        self.pending.append(text)
                        await self.broadcast({"type": "pending", "text": " ".join(self.pending)})
                    self.lines.append((role, text))
                    if self.session_id:
                        db.append_transcript(self.session_id, f"{SPEAKER_NAMES[role]}: {text}")
                if role == "interviewer" and not is_final:
                    self._cancel_trigger()  # интервьюер ещё говорит
            if role == "interviewer" and speech_final:
                self._schedule_trigger(ev.get("trigger_delay", TRIGGER_DELAY))
        elif kind == "UtteranceEnd" and role == "interviewer":
            self._schedule_trigger(0)
        elif kind == "SpeechStarted" and role == "interviewer":
            self._cancel_trigger()  # интервьюер снова заговорил — ждём конца мысли

    def _cancel_trigger(self) -> None:
        if self._trigger:
            self._trigger.cancel()
            self._trigger = None

    def _schedule_trigger(self, delay: float) -> None:
        self._cancel_trigger()
        self._trigger = self.loop.call_later(delay, lambda: asyncio.ensure_future(self._auto_trigger()))

    async def _auto_trigger(self) -> None:
        self._trigger = None
        if db.get_settings()["auto_answer"]:
            await self._answer_pending()

    async def _answer_pending(self, force: bool = False) -> None:
        """Интервьюер договорил: решаем, вопрос ли это. Вопрос — отвечаем, остальное — только в память."""
        question = " ".join(self.pending).strip()
        if len(question) < MIN_QUESTION_CHARS and not force:
            self.pending.clear()
            await self.broadcast({"type": "pending", "text": ""})
            return
        context_end = self.pending_start
        self.pending.clear()
        await self.broadcast({"type": "pending", "text": ""})

        kind = qdetect.QUESTION if force or not db.get_settings().get("smart_filter", True) \
            else qdetect.classify(question)[0]
        if kind == qdetect.CONTEXT:
            # приветствие, реакция, рассказ о компании — запоминаем как контекст, без карточки
            await self.broadcast({"type": "utterance", "kind": "context", "text": question})
            return

        heard = question  # вся реплика целиком — для пометок в расшифровке
        # Из склейки «реклама + вступление + вопрос» оставляем сам вопрос; остальное — фон
        focus, background = qdetect.extract_question(question)

        # Интервьюер дозадал вопрос, пока ответ ещё пишется → один общий ответ.
        # Готовый ответ не трогаем: кандидат мог уже начать его читать.
        prev = self.last_live
        task = self.answer_tasks.get(prev["id"]) if prev else None
        if prev and task and not task.done() and time.monotonic() - prev["started"] < MERGE_WINDOW:
            task.cancel()
            focus = f"{prev['question']} {focus}"
            context_end = prev["context_end"]
            kind = qdetect.QUESTION

        context = self.transcript_text(max(0, context_end - 20), context_end)[-2500:]
        if background:
            context = (context + f"\nИнтервьюер: {background}").strip()
        settings = db.get_settings()
        hit = self.kb_answer(focus, settings)
        if hit:  # вопрос из банка — готовый ответ сразу, без запроса к модели
            qa_id = await self._start_answer(focus, "kb", _instant(_kb_text(hit)))
            await self.broadcast({"type": "utterance", "kind": "question", "text": heard})
            self.last_live = {"id": qa_id, "question": focus, "started": time.monotonic(), "context_end": context_end}
            return
        gated = kind == qdetect.MAYBE  # не уверены — модель сама скажет «НЕТ», и карточка не появится
        gen = llm.stream_answer(focus, context, settings, gated=gated, voice=self.voice_sample())
        qa_id = await self._start_answer(focus, "live", gen, gated=gated)
        if not gated:
            await self.broadcast({"type": "utterance", "kind": "question", "text": heard})
        self.last_live = {"id": qa_id, "question": focus, "started": time.monotonic(), "context_end": context_end}

    async def answer_now(self, text: str) -> None:
        """⌘↵ / кнопка: введённый вопрос или последняя реплика интервьюера — без фильтра."""
        self._cancel_trigger()
        if text:
            hit = self.kb_answer(text, db.get_settings())
            if hit:
                await self._start_answer(text, "kb", _instant(_kb_text(hit)))
                return
            context = self.transcript_text()[-4000:] if self.listening else ""
            await self._start_answer(text, "manual", llm.stream_answer(
                text, context, db.get_settings(), voice=self.voice_sample()))
            return
        if not self.pending:
            last = next((t for s, t in reversed(self.lines) if s == "interviewer"), "")
            if not last:
                return await self.error("Нечего отвечать: интервьюер ещё ничего не сказал.")
            self.pending = [last]
            self.pending_start = max(0, len(self.lines) - 1)
        self.last_live = None
        await self._answer_pending(force=True)

    # ---------- генерация ----------
    async def _start_answer(self, question: str, source: str, gen: AsyncIterator[str], gated: bool = False) -> int:
        saved = db.get_settings()["auto_save"]
        qa_id = db.create_qa(question, source, self.session_id, saved)
        if not gated:  # карточку с «непонятной» репликой покажем, только когда модель начнёт отвечать
            await self.broadcast({"type": "answer_start", "qa": db.get_qa(qa_id)})
        self.answer_tasks[qa_id] = asyncio.create_task(self._run_answer(qa_id, gen, gated))
        return qa_id

    async def _run_answer(self, qa_id: int, gen: AsyncIterator[str], gated: bool = False) -> None:
        buf = ""
        shown = not gated
        try:
            async for chunk in gen:
                buf += chunk
                if not shown:
                    head = re.sub(r"[\s*_#>:.!-]+", "", buf)[:6].upper()
                    if head.startswith(GATE_NO):  # модель решила: это не вопрос → тихо в память
                        await gen.aclose()
                        question = (db.get_qa(qa_id) or {}).get("question", "")
                        db.delete_qa(qa_id)
                        await self.broadcast({"type": "utterance", "kind": "context", "text": question})
                        return
                    if len(head) < len(GATE_NO) and GATE_NO.startswith(head):
                        continue  # пока не ясно — ждём ещё пару токенов
                    shown = True
                    qa = db.get_qa(qa_id)
                    await self.broadcast({"type": "answer_start", "qa": qa})
                    await self.broadcast({"type": "utterance", "kind": "question", "text": qa["question"]})
                    await self.broadcast({"type": "answer_delta", "id": qa_id, "text": buf})
                    continue
                await self.broadcast({"type": "answer_delta", "id": qa_id, "text": chunk})
        except asyncio.CancelledError:
            db.delete_qa(qa_id)
            if shown:
                await self.broadcast({"type": "answer_removed", "id": qa_id})
            raise
        except llm.LLMError as e:
            buf += f"\n\n⚠️ {e}"
            if llm.provider_of(db.get_settings()) == "claude" and (e.auth or e.billing):
                # сессия истекла или кончились средства → вернуть на экран входа
                if e.auth:
                    auth.ok = False
                if e.billing:
                    auth.billing_error = True
                asyncio.create_task(self.broadcast(self.state()))
        except Exception as e:  # noqa: BLE001 — непредвиденная ошибка не должна ронять ответ
            buf += f"\n\n⚠️ Ошибка: {e}"
        finally:
            self.answer_tasks.pop(qa_id, None)
        if not shown:  # ответ закончился, не успев начаться (ошибка/пусто) — показываем как есть
            if not buf.strip():
                db.delete_qa(qa_id)
                return
            await self.broadcast({"type": "answer_start", "qa": db.get_qa(qa_id)})
        db.update_qa(qa_id, {"answer": buf})
        await self.broadcast({"type": "answer_done", "qa": db.get_qa(qa_id)})

    async def screenshot(self, hint: str = "") -> None:
        if not self.ai_ready():
            return await self.error("Сначала подключи ИИ: ⋯ → Настройки → ИИ-модель.")
        if not llm.supports_images(db.get_settings()):
            return await self.error("Выбранная модель не видит изображения — для скриншотов выбери Groq, Gemini или Claude.")
        try:
            b64 = await asyncio.to_thread(self._grab_screen)
        except Exception as e:
            return await self.error(
                f"Не удалось сделать скриншот: {e}. Разреши «Запись экрана» для терминала/PyCharm "
                "в Системных настройках → Конфиденциальность."
            )
        await self._start_answer(
            "📷 Задача с экрана" + (f": {hint}" if hint else ""),
            "screenshot",
            llm.stream_screenshot(b64, "image/jpeg", hint, db.get_settings()),
        )

    def _grab_screen(self) -> str:
        import mss
        from PIL import Image

        if self.hide_window:
            self.hide_window()
            time.sleep(0.25)
        try:
            with mss.mss() as sct:
                shot = sct.grab(sct.monitors[1])
        finally:
            if self.show_window:
                self.show_window()
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        img.thumbnail((1920, 1920))  # Retina-скриншот уменьшаем: быстрее загрузка, текст читаем
        out = io.BytesIO()
        img.save(out, "JPEG", quality=85)
        return base64.b64encode(out.getvalue()).decode()

    async def review(self, session_id: int | None) -> None:
        if not session_id:
            return await self.error("Нет сессии для разбора — сначала проведи собеседование.")
        sess = db.get_session(session_id)
        transcript = self.transcript_text() if session_id == self.session_id and self.listening else (sess or {}).get("transcript", "")
        qa = [q for q in db.list_qa(session_id=session_id) if q["source"] != "screenshot"][::-1]
        if not transcript and not qa:
            return await self.error("В этой сессии нет расшифровки.")
        await self.broadcast({"type": "review_start", "session_id": session_id})
        buf = ""
        try:
            async for chunk in llm.stream_review(transcript, qa, db.get_settings()):
                buf += chunk
                await self.broadcast({"type": "review_delta", "session_id": session_id, "text": chunk})
        except llm.LLMError as e:
            buf += f"\n\n⚠️ {e}"
        db.set_session_review(session_id, buf)
        await self.broadcast({"type": "review_done", "session_id": session_id, "text": buf})


engine = Engine()


async def _login_changed() -> None:
    await engine.broadcast(engine.state())


login.notify = _login_changed
