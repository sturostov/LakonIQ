"use strict";

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const OVERLAY = new URLSearchParams(location.search).has("overlay");
if (!OVERLAY) document.documentElement.classList.add("browser");

const api = async (method, url, body) => {
  const r = await fetch(url, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
  if (!r.ok) {
    const text = await r.text();
    let detail = text;
    try { detail = JSON.parse(text).detail || text; } catch { /* не JSON */ }
    throw new Error(typeof detail === "string" ? detail : `${r.status} ${text}`);
  }
  return r.json();
};
const native = () => window.pywebview && window.pywebview.api;
// Вызов функции окна, только если pywebview её уже подключил (до pywebviewready их ещё нет)
function callNative(name, ...args) {
  const api = native();
  if (api && typeof api[name] === "function") return api[name](...args);
  return undefined;
}

// Логи окна → data/logs/app.log (для отладки)
function clientLog(message, level = "info") {
  try {
    fetch("/api/log", { method: "POST", keepalive: true, headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message, level }) });
  } catch { /* сервер недоступен */ }
}
window.addEventListener("error", (e) => clientLog(`JS ошибка: ${e.message} @ ${e.filename}:${e.lineno}`, "error"));
window.addEventListener("unhandledrejection", (e) => clientLog(`JS promise: ${e.reason && (e.reason.message || e.reason)}`, "error"));

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch { /* хранилище недоступно */ }
  return null;
}

let toastTimer;
function toast(msg, ok = false, action = null) {
  const t = $("#toast");
  t.textContent = msg;
  if (action) {  // кнопка в уведомлении, например «Вернуть»
    const b = document.createElement("button");
    b.className = "toast-btn";
    b.textContent = action.label;
    b.onclick = () => { action.fn(); t.classList.add("hidden"); };
    t.append(b);
  }
  t.className = "toast" + (ok ? " ok" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), action ? 6000 : ok ? 1800 : 6000);
  fitSoon();
}

/* ================= Подгонка высоты окна под контент ================= */
let fitTimer;
function fitSoon() {
  if (!OVERLAY || (typeof quitting !== "undefined" && quitting)) return;
  clearTimeout(fitTimer);
  fitTimer = setTimeout(() => {
    let h = $("#app").getBoundingClientRect().bottom;
    const menu = $("#menu");
    if (!menu.classList.contains("hidden")) h = Math.max(h, menu.getBoundingClientRect().bottom + 8);
    callNative("fit", Math.ceil(h) + 4);
  }, 40);
}
new ResizeObserver(fitSoon).observe($("#app"));
document.documentElement.style.setProperty("--feed-max", `${Math.round(screen.availHeight * 0.55)}px`);

/* ================= Markdown (сначала экранируем — безопасно) ================= */
function inline(s) {
  return s
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/(^|\s)_([^_]+)_(?=\s|$|[.,!?])/g, "$1<em>$2</em>");
}

function md(src) {
  const blocks = [];
  src = String(src || "").replace(/```[\w+-]*\n?([\s\S]*?)(?:```|$)/g, (_, code) => {
    blocks.push(code.replace(/\n$/, ""));
    return `\n\u0000${blocks.length - 1}\u0000\n`;
  });
  const lines = esc(src).split("\n");
  let html = "", list = null, para = [];
  const flushPara = () => { if (para.length) { html += `<p>${inline(para.join("<br>"))}</p>`; para = []; } };
  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  const isRow = (l) => /^\s*\|.*\|\s*$/.test(l || "");
  const isSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l || "");
  const cells = (l) => l.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((c) => inline(c.trim()));
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i].trimEnd();
    let m;
    // Таблица Markdown: строка «| a | b |», под ней «|---|---|»
    if (isRow(line) && isSep(lines[i + 1])) {
      flushPara(); closeList();
      let t = `<div class="md-table"><table><thead><tr>${cells(line).map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>`;
      i += 2;
      while (i < lines.length && isRow(lines[i])) {
        t += `<tr>${cells(lines[i]).map((c) => `<td>${c}</td>`).join("")}</tr>`;
        i++;
      }
      i--;
      html += t + "</tbody></table></div>";
      continue;
    }
    if ((m = line.match(/^\u0000(\d+)\u0000$/))) {
      flushPara(); closeList();
      html += `<pre><button class="copy" type="button">копировать</button><code>${esc(blocks[+m[1]])}</code></pre>`;
    } else if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      flushPara(); closeList();
      const n = Math.min(4, m[1].length + 1);
      html += `<h${n}>${inline(m[2])}</h${n}>`;
    } else if ((m = line.match(/^\s*(?:[-*•]|(\d+)[.)])\s+(.*)$/))) {
      flushPara();
      const type = m[1] ? "ol" : "ul";
      if (list !== type) { closeList(); html += `<${type}>`; list = type; }
      html += `<li>${inline(m[2])}</li>`;
    } else if (!line.trim()) {
      flushPara(); closeList();
    } else {
      closeList(); para.push(line);
    }
  }
  flushPara(); closeList();
  return html;
}

document.addEventListener("click", (e) => {
  const btn = e.target.closest("pre .copy");
  if (!btn) return;
  navigator.clipboard.writeText(btn.nextElementSibling.textContent).then(() => toast("Код скопирован", true));
});

/* ================= Навигация: живой режим + разделы из меню ================= */
let view = "live";
function showView(name) {
  view = name;
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  $("#menu").classList.add("hidden");
  if (name === "base") loadBase();
  if (name === "study") loadStudy();
  if (name === "sessions") loadSessions();
  if (name === "settings") loadSettings();
  if (name === "keys") loadKeys();
  if (name === "login") loadLogin();
  if (name === "live") $("#askInput").focus();
  fitSoon();
}
$("#btnMenu").onclick = (e) => { e.stopPropagation(); $("#menu").classList.toggle("hidden"); fitSoon(); };
document.addEventListener("click", (e) => {
  if (!e.target.closest("#menu, #btnMenu") && !$("#menu").classList.contains("hidden")) {
    $("#menu").classList.add("hidden"); fitSoon();
  }
});
$$("#menu [data-view]").forEach((b) => (b.onclick = () => showView(b.dataset.view)));
$$(".back").forEach((b) => (b.onclick = () => (view === "sessions" && currentSession ? loadSessions() : showView("live"))));
let quitting = false;
$("#btnQuit").onclick = () => {
  clientLog(`клик «Закрыть приложение» (окно: ${native() ? "нативное" : "браузер"})`);
  quitting = true;  // больше не обращаемся к окну: оно закрывается
  clearTimeout(fitTimer);
  if (ws) ws.onclose = null;
  return typeof native()?.quit === "function" ? native().quit() : window.close();
};

/* ================= Карточка «AI Response» ================= */
const SRC = {
  live: "Ответ", manual: "Ваш вопрос", screenshot: "Экран", assist: "Подсказка",
  followups: "Вопросы интервьюеру", recap: "Сводка", rephrase: "Проще", deeper: "Подробнее",
  kb: "📚 Из банка",
};
const texts = new Map();

function cardHTML(qa, streaming) {
  return `
    <div class="head"><span class="spark">✦</span>${SRC[qa.source] || "Ответ"}</div>
    <div class="q">${esc(qa.question)}</div>
    <div class="a md ${streaming ? "cursor" : ""}">${md(qa.answer)}</div>
    ${qa.notes ? `<div class="notes-view">${esc(qa.notes)}</div>` : ""}
    <div class="actions">
      <button class="act-save ${qa.saved ? "on" : ""}" title="В базу для заучивания (⌘⇧D)">${qa.saved ? "★ В базе" : "☆ В базу"}</button>
      <button class="act-edit" title="Заметки, правка ответа, теги">✎ Заметка</button>
      <button class="act-simpler" title="Пересказать проще, как говорят вслух">🗣 Проще</button>
      <button class="act-deeper" title="Полный ответ: с примером, компромиссами и подводными камнями">📖 Подробнее</button>
      ${qa.source === "kb" ? `<button class="act-fresh" title="Не тот вопрос? Модель ответит заново с учётом разговора">✨ Свежий ответ</button>` : ""}
      <button class="act-copy" title="Скопировать ответ">Копировать</button>
      <button class="act-del" title="${streaming ? "Остановить" : "Удалить"}">${streaming ? "■ Стоп" : "✕"}</button>
      <span class="tags">${qa.tags ? "#" + esc(qa.tags).replace(/,\s*/g, " #") : ""}</span>
    </div>`;
}

function makeCard(qa, streaming = false) {
  const el = document.createElement("div");
  el.className = "card" + (streaming ? " streaming" : "") + (qa.saved ? " saved" : "");
  el.dataset.id = qa.id;
  el._qa = qa;
  el.innerHTML = cardHTML(qa, streaming);
  return el;
}

function refreshCards(qa, streaming = false) {
  $$(`.card[data-id="${qa.id}"]`).forEach((el) => {
    if (el.classList.contains("editing")) return;
    el._qa = qa;
    el.className = "card" + (streaming ? " streaming" : "") + (qa.saved ? " saved" : "");
    el.innerHTML = cardHTML(qa, streaming);
  });
}

function openEditor(el) {
  const qa = el._qa;
  el.classList.add("editing");
  el.innerHTML = `
    <div class="edit-grid">
      <input class="e-q" value="${esc(qa.question)}">
      <textarea class="e-a" rows="9">${esc(qa.answer)}</textarea>
      <textarea class="e-n" rows="3" placeholder="Мои заметки: как отвечу своими словами, что уточнить, что запомнить">${esc(qa.notes)}</textarea>
      <input class="e-t" value="${esc(qa.tags)}" placeholder="Теги через запятую: python, sql, system design">
    </div>
    <div class="actions">
      <button class="act-store on">Сохранить</button>
      <button class="act-cancel">Отмена</button>
      <label class="toggle"><input type="checkbox" class="e-s" ${qa.saved ? "checked" : ""}><span>в базу ★</span></label>
    </div>`;
  $(".e-n", el).focus();
}

document.addEventListener("click", async (e) => {
  const el = e.target.closest(".card");
  if (!el || !el._qa) return;  // карточки банка в «Базе» — со своими кнопками
  const qa = el._qa, id = qa.id;
  const is = (c) => e.target.closest(c);
  try {
    if (is(".act-save")) {
      const upd = await api("PATCH", `/api/qa/${id}`, { saved: !qa.saved });
      refreshCards(upd);
      toast(upd.saved ? "★ Добавлено в базу" : "Убрано из базы", true);
    } else if (is(".act-edit")) {
      openEditor(el);
    } else if (is(".act-fresh")) {
      send({ action: "fresh", id });
    } else if (is(".act-deeper")) {
      if (el.classList.contains("streaming")) return toast("Подожди, пока ответ допишется");
      send({ action: "deeper", id });
    } else if (is(".act-simpler")) {
      if (el.classList.contains("streaming")) return toast("Подожди, пока ответ допишется");
      send({ action: "rephrase", id });
    } else if (is(".act-copy")) {
      await navigator.clipboard.writeText(qa.answer);
      toast("Ответ скопирован", true);
    } else if (is(".act-del")) {
      if (el.classList.contains("streaming")) send({ action: "cancel", id });
      else await api("DELETE", `/api/qa/${id}`);
    } else if (is(".act-cancel")) {
      el.classList.remove("editing");
      refreshCards(qa);
    } else if (is(".act-store")) {
      const upd = await api("PATCH", `/api/qa/${id}`, {
        question: $(".e-q", el).value, answer: $(".e-a", el).value,
        notes: $(".e-n", el).value, tags: $(".e-t", el).value, saved: $(".e-s", el).checked,
      });
      el.classList.remove("editing");
      refreshCards(upd);
      toast("Сохранено", true);
    }
  } catch (err) { toast(err.message); }
});

/* ================= WebSocket ================= */
let ws, state = {};
function send(msg) {
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(msg));
  else toast("Нет связи с сервером");
}
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = (e) => onEvent(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connect, 1000);
}

const renderQueued = new Set();
function scheduleRender(id) {
  if (renderQueued.has(id)) return;
  renderQueued.add(id);
  requestAnimationFrame(() => {
    renderQueued.delete(id);
    // ответ уже завершён: финальный текст нарисовал answer_done — не затираем его
    if (!texts.has(id)) return;
    $$(`.card[data-id="${id}"] .a`).forEach((a) => { a.innerHTML = md(texts.get(id)); });
  });
}

let listenStart = 0, timerInt;
function setListening(on) {
  const b = $("#btnListen");
  b.classList.toggle("on", on);
  $("#listenLabel").textContent = on ? "Стоп" : "Слушать";
  $("#timer").classList.toggle("hidden", !on);
  $("#liveStrip").classList.toggle("hidden", !on && !$("#transcript").children.length);
  clearInterval(timerInt);
  if (on) {
    if (!listenStart) listenStart = Date.now();
    const tick = () => {
      const s = Math.floor((Date.now() - listenStart) / 1000);
      $("#timer").textContent = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
    };
    tick();
    timerInt = setInterval(tick, 1000);
  } else {
    listenStart = 0;
  }
}

function onEvent(ev) {
  switch (ev.type) {
    case "state": {
      state = ev;
      setListening(ev.listening);
      applyAuth(ev.auth || {}, ev.ai || {}, ev.login || {});
      // распознавание речи
      const stt = ev.stt || {};
      const w = $("#keyWarn");
      w.textContent = stt.kind ? "" : stt.error || "";
      w.classList.toggle("hidden", !w.textContent);
      $("#sttStatus").textContent = stt.kind ? `Сейчас используется: ${stt.label}` : stt.error || "";
      $("#sttLabel").textContent = stt.label ? `· ${stt.label}` : "";
      break;
    }
    case "levels": {
      const li = ev.interviewer || 0, lm = ev.me || 0;
      $("#lvlInterviewer").style.width = `${Math.min(100, li * 100)}%`;
      $("#lvlMe").style.width = `${Math.min(100, lm * 100)}%`;
      const lvl = Math.max(li, lm);
      $$(".wave i").forEach((bar, i) => {
        const k = [0.5, 0.8, 1, 0.75, 0.55][i];
        bar.style.height = `${3 + Math.min(13, lvl * 40 * k * (0.7 + Math.random() * 0.6))}px`;
      });
      break;
    }
    case "transcript": addTranscript(ev); break;
    case "pending":
      $("#pending").textContent = ev.text;
      $("#pending").classList.toggle("hidden", !ev.text);
      break;
    case "utterance": {
      // помечаем реплики интервьюера: ❓ — вопрос (будет ответ), бледные — ушли в контекст
      const lines = $$("#transcript .line.interviewer:not(.interim)").reverse().slice(0, 8);
      let marked = 0;
      for (const el of lines) {
        if (el.dataset.text && ev.text.includes(el.dataset.text)) {
          el.classList.toggle("is-q", ev.kind === "question");
          el.classList.toggle("is-ctx", ev.kind === "context");
          marked++;
        } else if (marked) break;
      }
      break;
    }
    case "answer_start":
      texts.set(ev.qa.id, "");
      $("#answers").prepend(makeCard(ev.qa, true));
      $("#answers").scrollTop = 0;
      if (!entered) enter();
      else if (view !== "live") showView("live");
      break;
    case "answer_delta":
      texts.set(ev.id, (texts.get(ev.id) || "") + ev.text);
      scheduleRender(ev.id);
      break;
    case "answer_done":
      texts.delete(ev.qa.id);
      refreshCards(ev.qa);
      updateDue();
      break;
    case "qa_updated": refreshCards(ev.qa); updateDue(); break;
    case "answer_removed": $$(`.card[data-id="${ev.id}"]`).forEach((c) => c.remove()); break;
    case "reset":
      $("#answers").innerHTML = "";
      $("#transcript").innerHTML = "";
      $("#pending").classList.add("hidden");
      Object.keys(interim).forEach((k) => delete interim[k]);
      setListening(state.listening);
      toast("Сброшено", true);
      break;
    case "scroll": {
      const feed = view === "live" ? $("#answers") : $(`#view-${view} .feed, #view-${view} .list, #view-${view} .reveal`);
      if (feed) feed.scrollBy({ top: ev.dir === "up" ? -180 : 180 });
      break;
    }
    case "review_start":
      if (currentSession === ev.session_id) { reviewBuf = ""; $("#sessionReviewText").innerHTML = '<p class="cursor"></p>'; }
      break;
    case "review_delta":
      if (currentSession === ev.session_id) { reviewBuf += ev.text; $("#sessionReviewText").innerHTML = md(reviewBuf); }
      break;
    case "review_done":
      if (currentSession === ev.session_id) $("#sessionReviewText").innerHTML = md(ev.text);
      $("#sessionReview").disabled = false;
      break;
    case "info": toast(ev.message, true); break;
    case "error": toast(ev.message); break;
  }
}

/* ================= Авторизация: первый экран при запуске ================= */
let authState = {}, aiState = {}, loginState = {}, providerData = null, startShown = false, entered = false;
const CLAUDE_MODELS = [["", "Claude Opus 5.5 (лучшее качество)"], ["claude-sonnet-5-5", "Claude Sonnet 5.5"], ["claude-haiku-5-5", "Claude Haiku 5.5 (быстро и дёшево)"]];
const ACCOUNT_ORDER = ["gemini", "openrouter", "groq", "claude"];

function applyAuth(a, ai, lg) {
  authState = a || {}; aiState = ai || {};
  showEffort(aiState.provider === "claude" && aiState.ready, aiState.effort);
  const prev = loginState; loginState = lg || {};
  const ready = !!aiState.ready;
  document.body.classList.toggle("locked", !(ready && entered));
  $("#accountLine").textContent = ready ? `Вход: ${aiState.account}` : "";
  $("#accountLine").classList.toggle("hidden", !ready);
  $("#btnLogout").classList.toggle("hidden", !ready);
  if (aiState.checked && !startShown) { startShown = true; showView("login"); }   // при запуске — всегда вход
  if (loginState.status === "done" && prev.status !== "done" && ready) { toast(loginState.message, true); enter(); return; }
  if (!ready && entered && aiState.checked) { entered = false; showView("login"); }   // вышли / сессия истекла
  if (view === "login") renderLoginState();
}

function enter() {
  entered = true;
  document.body.classList.remove("locked");
  showView("live");
}

async function loadLogin() {
  providerData = await api("GET", "/api/providers");
  const byId = Object.fromEntries(providerData.providers.map((p) => [p.id, p]));
  $("#accountList").innerHTML = ACCOUNT_ORDER.filter((id) => byId[id]).map((id) => {
    const p = byId[id];
    return `<div class="signin" data-id="${id}">
        <button class="signin-btn">
          <span class="ico ico-${id}">${esc(p.icon)}</span>
          <span class="s-txt"><b>${esc(p.title)}</b><small>${esc(p.subtitle)}</small></span>
          <span class="s-st"></span>
        </button>
        <div class="s-flow hidden"></div>
      </div>`;
  }).join("");
  const sel = $("#keyProvider"), keep = sel.value;
  sel.innerHTML = providerData.providers.filter((p) => p.id !== "claude")
    .map((p) => `<option value="${p.id}">${esc(p.name)}${p.configured ? " — ключ сохранён" : ""}</option>`).join("");
  if (keep) sel.value = keep;
  updateKeyForm();
  renderLoginState();
}

function flowHTML(p, lg) {
  const msg = esc(lg.message || "");
  const cancel = '<button class="link f-cancel">отмена</button>';
  const manual = p.key_hint ? `<div class="key-row f-manual">
      <input class="f-key" type="password" autocomplete="off" placeholder="или вставь ключ вручную (${esc(p.key_hint)})">
      <button class="pill ghost small f-key-go">Войти</button></div>` : "";
  switch (lg.status) {
    case "waiting":
      return `<div class="flow-line"><span class="spinner"></span><span>${msg}</span>${cancel}</div>`;
    case "waiting_key":
      return `<ol class="steps">${(p.steps || []).map((t) => `<li>${esc(t)}</li>`).join("")}</ol>
        <div class="flow-line"><span class="spinner"></span><span>${msg}</span>${cancel}</div>${manual}`;
    case "checking":
      return `<div class="flow-line"><span class="spinner"></span><span>${msg}</span></div>`;
    case "error":
      return `<p class="login-error small">${msg}</p>
        <div class="flow-line"><button class="pill ghost small f-retry">Попробовать снова</button></div>${manual}`;
    case "done":
      return `<div class="flow-line ok">✓ ${msg}</div>`;
    default:
      return "";
  }
}

function renderLoginState() {
  if (!providerData) return;
  const ready = !!aiState.ready;
  // «Продолжить», если вход уже выполнен раньше
  $("#btnContinue").classList.toggle("hidden", !ready);
  $("#continueAs").textContent = ready ? `как ${aiState.account}` : "";
  // предупреждение о балансе Claude
  const banner = $("#loginBanner");
  const billing = authState.ok && authState.billing_error && !ready;
  banner.textContent = billing ? `Claude: вход выполнен (${authState.account || "аккаунт"}), но на балансе нет средств. `
    + "Пополни на platform.claude.com → Billing или войди бесплатным способом ниже." : "";
  banner.classList.toggle("hidden", !billing);
  // строки «Войти через …»
  for (const row of $$("#accountList .signin")) {
    const id = row.dataset.id, p = providerData.providers.find((x) => x.id === id);
    const st = $(".s-st", row);
    const current = ready && aiState.provider === id;
    st.className = "s-st" + (current ? " on" : "");
    st.textContent = current ? "✓ текущий"
      : id === "claude" ? (authState.ok && authState.billing_error ? "нет средств" : authState.ok ? "вход выполнен" : "")
      : p.configured ? "ключ сохранён" : "";
    const flow = $(".s-flow", row);
    const active = loginState.provider === id && loginState.status !== "idle";
    const key = active ? `${loginState.status}|${loginState.message}` : "";
    if (flow.dataset.key !== key) {
      flow.dataset.key = key;
      flow.innerHTML = active ? flowHTML(p, loginState) : "";
      flow.classList.toggle("hidden", !active);
    }
    $(".signin-btn", row).disabled = active && ["waiting", "checking"].includes(loginState.status);
  }
  // вход по ключу
  const kp = $("#keyProvider").value, err = $("#keyError");
  const keyActive = loginState.provider === kp && !$(`#accountList .signin[data-id="${kp}"] .s-flow:not(.hidden)`);
  err.textContent = keyActive && loginState.status === "error" ? loginState.message : "";
  err.classList.toggle("hidden", !err.textContent);
  $("#keySubmit").disabled = keyActive && loginState.status === "checking";
  $("#keySubmit").textContent = keyActive && loginState.status === "checking" ? "Проверяю…" : "Войти";
  fitSoon();
}

function updateKeyForm() {
  const p = providerData?.providers.find((x) => x.id === $("#keyProvider").value);
  if (!p) return;
  $("#keyInput").classList.toggle("hidden", p.no_key);
  $("#keyInput").placeholder = p.configured ? "ключ сохранён — можно вставить новый" : `Вставь API-ключ (${p.key_hint || "…"})`;
  $("#keySubmit").textContent = p.no_key ? "Подключить" : "Войти";
  $("#keyNote").innerHTML = `${esc(p.note)} <a href="${esc(p.key_url)}" target="_blank" rel="noopener">${p.no_key ? "Скачать ↗" : "Получить ключ ↗"}</a>`;
}

$("#accountList").addEventListener("click", (e) => {
  const row = e.target.closest(".signin");
  if (!row) return;
  const id = row.dataset.id;
  if (e.target.closest(".signin-btn") || e.target.closest(".f-retry")) return send({ action: "login_start", provider: id });
  if (e.target.closest(".f-cancel")) return send({ action: "login_cancel" });
  if (e.target.closest(".f-key-go")) {
    const key = $(".f-key", row).value.trim();
    if (key) send({ action: "login_key", provider: id, key });
  }
});
$("#accountList").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.classList.contains("f-key")) $(".f-key-go", e.target.closest(".signin")).click();
});
$("#keyProvider").onchange = () => { updateKeyForm(); renderLoginState(); };
$("#keySubmit").onclick = () => {
  const pid = $("#keyProvider").value, key = $("#keyInput").value.trim();
  const p = providerData?.providers.find((x) => x.id === pid);
  if (!key && p && !p.no_key && !p.configured) return toast("Вставь API-ключ");
  send({ action: "login_key", provider: pid, key });
  $("#keyInput").value = "";
};
$("#keyInput").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#keySubmit").click(); });
$("#btnContinue").onclick = enter;
document.addEventListener("keydown", (e) => {
  if (view === "login" && e.key === "Enter" && aiState.ready && !/INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) {
    e.preventDefault(); enter();
  }
});
$("#btnSwitch").onclick = () => { $("#menu").classList.add("hidden"); showView("login"); };
$("#btnLogout").onclick = () => { $("#menu").classList.add("hidden"); entered = false; send({ action: "logout" }); showView("login"); };

// Блок «ИИ-модель» в настройках
async function loadAiSettings() {
  const data = await api("GET", "/api/providers");
  const cur = data.providers.find((p) => p.id === data.current);
  $("#aiName").textContent = cur ? cur.name : "вход не выполнен";
  const sel = $("#aiModel");
  sel.classList.toggle("hidden", !cur);
  if (!cur) return;
  let options = [];
  if (data.current === "claude") options = CLAUDE_MODELS;
  else {
    try {
      const r = await api("POST", "/api/providers/connect", { provider: data.current, model: data.model });
      options = r.ok ? r.models.map((m) => [m, m]) : [[data.model, data.model]];
    } catch { options = [[data.model, data.model]]; }
  }
  sel.innerHTML = options.map(([v, l]) => `<option value="${esc(v)}" ${v === data.model ? "selected" : ""}>${esc(l)}</option>`).join("");
  sel.dataset.provider = data.current;
}
$("#aiModel").onchange = async (e) => {
  const r = await api("POST", "/api/providers/connect", { provider: e.target.dataset.provider, model: e.target.value });
  toast(r.ok ? `Модель: ${e.target.value || "по умолчанию"}` : r.error, r.ok);
};
$("#btnChangeAi").onclick = () => showView("login");

/* ================= Глубина размышлений Claude (effort) ================= */
// Как переключатель в самом Claude. Время до первого слова замерено на Opus 5.5 с профилем кандидата.
const EFFORTS = [
  ["auto", "Авто", "Живые ответы — быстро (≈1 с), а «Подробнее», задачи с экрана и разбор думают глубже (≈6 с)."],
  ["low", "low", "Первое слово ≈1 с · ≈1,5 ¢ за ответ."],
  ["medium", "medium", "Первое слово ≈3 с · ≈2,5 ¢ за ответ."],
  ["high", "high", "Первое слово ≈6 с · ≈2,5 ¢ за ответ."],
  ["xhigh", "xhigh", "Первое слово ≈12 с · ≈4 ¢ за ответ."],
  ["max", "max", "Первое слово ≈2,5 мин · ≈35 ¢ за ответ — только для сложных задач с экрана, не для живых ответов."],
];
let effort = "auto";

function paintEffort() {
  const i = Math.max(0, EFFORTS.findIndex(([v]) => v === effort));
  $$(".effort-range").forEach((r) => (r.value = i));
  $$(".effort-val").forEach((el) => (el.textContent = EFFORTS[i][1]));
  $$(".effort-hint").forEach((el) => (el.textContent = EFFORTS[i][2]));
  $$(".effort").forEach((box) => box.classList.toggle("is-slow", effort === "max"));
  $("#effortChip").textContent = `🧠 ${EFFORTS[i][1]}`;
}

function showEffort(isClaude, value) {
  if (value) effort = value;
  $("#effortBox").classList.toggle("hidden", !isClaude);
  $("#effortChip").classList.toggle("hidden", !isClaude);
  $("#askForm").classList.toggle("has-effort", isClaude);
  if (!isClaude) $("#effortPanel").classList.add("hidden");
  paintEffort();
}

for (const box of [$("#effortBox"), $("#effortPanel")]) {
  box.innerHTML = `
    <div class="effort-head"><span>Глубина размышлений Claude</span><b class="effort-val"></b></div>
    <input type="range" class="effort-range" min="0" max="${EFFORTS.length - 1}" step="1" aria-label="Глубина размышлений Claude">
    <div class="effort-ticks">${EFFORTS.map(([, label]) => `<span>${label}</span>`).join("")}</div>
    <small class="effort-hint"></small>`;
  const range = $(".effort-range", box);
  range.oninput = () => { effort = EFFORTS[+range.value][0]; paintEffort(); };
  range.onchange = async () => {
    try {
      await api("PUT", "/api/settings", { claude_effort: effort });
      toast(effort === "max" ? "max: живые ответы будут ждать ≈2,5 мин" : `Claude думает: ${EFFORTS[+range.value][1]}`, effort !== "max");
    } catch (err) { toast(err.message); }
  };
}
$("#effortChip").onclick = () => { $("#effortPanel").classList.toggle("hidden"); fitSoon(); };
paintEffort();

/* ================= Расшифровка ================= */
const interim = {};
function addTranscript({ speaker, text, final }) {
  const box = $("#transcript");
  $("#liveStrip").classList.remove("hidden");
  let el = interim[speaker];
  if (!el) {
    el = document.createElement("div");
    el.className = `line ${speaker} interim`;
    box.appendChild(el);
    interim[speaker] = el;
  }
  el.innerHTML = `<span class="who">${speaker === "me" ? "Я" : "Интервьюер"}:</span>${esc(text)}`;
  if (final) { el.classList.remove("interim"); el.dataset.text = text; delete interim[speaker]; }
  while (box.children.length > 300) box.firstChild.remove();
  box.scrollTop = box.scrollHeight;
}
$("#btnTranscript").onclick = () => {
  const t = $("#transcript");
  t.classList.toggle("collapsed");
  $("#btnTranscript").textContent = t.classList.contains("collapsed") ? "развернуть" : "свернуть";
  t.scrollTop = t.scrollHeight;
};

/* ================= Живой режим ================= */
$("#btnListen").onclick = () => send({ action: state.listening ? "stop" : "start" });
$("#btnAssist").onclick = () => send({ action: "assist" });
$$(".chip[data-action]").forEach((c) => (c.onclick = () => send({ action: c.dataset.action })));
$("#askForm").onsubmit = (e) => {
  e.preventDefault();
  const text = $("#askInput").value.trim();
  send(text ? { action: "answer", text } : { action: "assist" });
  $("#askInput").value = "";
};
// «Скрыть»: в окне — сворачиваем панель-оверлей, вернуть можно ⌘\
$("#btnHide").onclick = () => {
  if (native()) fetch("/api/window/hide", { method: "POST" });
  else toast("В браузере скрытие не работает — запусти python main.py");
};

/* ================= Очистить все ответы ================= */
// Убирает карточки только с экрана: контекст разговора и записи в базе остаются.
function updateClearBtn() {
  $("#btnClear").classList.toggle("hidden", !$("#answers .card"));
}
new MutationObserver(updateClearBtn).observe($("#answers"), { childList: true });

$("#btnClear").onclick = () => {
  // убираем только готовые ответы; те, что ещё пишутся, остаются и дописываются
  const done = $$("#answers .card").filter((c) => !c.classList.contains("streaming"));
  if (!done.length) return toast("Ответы ещё пишутся — очищать пока нечего", true);
  done.forEach((c) => c.remove());
  fitSoon();
  const writing = $$("#answers .card").length;
  toast(`Убрано ответов: ${done.length}${writing ? ` · дописывается: ${writing}` : ""} · контекст сохранён`, true,
    { label: "Вернуть", fn: () => { $("#answers").append(...done); fitSoon(); } });
};

/* ================= База ================= */
let baseTimer;
function makeKbCard(item) {
  const el = document.createElement("div");
  el.className = "card";
  el.innerHTML = `
    <div class="head"><span class="spark">✦</span>📚 ${esc(item.cat)}</div>
    <div class="q">${esc(item.q)}</div>
    <div class="a md">${md(item.answer)}</div>
    <div class="actions"><button class="kb-save" title="В личную базу — появится в «Заучивании»">☆ В базу</button></div>`;
  $(".kb-save", el).onclick = async (e) => {
    try {
      await api("POST", `/api/kb/${item.id}/save`);
      e.target.textContent = "★ В базе"; e.target.classList.add("on"); e.target.disabled = true;
      toast("★ Добавлено в базу — будет в «Заучивании»", true); updateDue();
    } catch (err) { toast(err.message); }
  };
  return el;
}

async function loadBase() {
  if ($("#baseBank").checked) return loadBank();
  const q = encodeURIComponent($("#baseSearch").value.trim());
  const saved = $("#baseSavedOnly").checked;
  const items = await api("GET", `/api/qa?saved=${saved}&q=${q}`);
  const list = $("#baseList");
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = `<p class="empty">${saved ? "В базе пока пусто.<br>Жми «☆ В базу» на полезных ответах (или ⌘⇧D) — либо задай вопрос в поле ввода, чтобы подготовить ответ заранее." : "Ничего не найдено."}</p>`;
  }
  items.forEach((qa) => list.appendChild(makeCard(qa)));
  fitSoon();
}
$("#baseSearch").oninput = () => { clearTimeout(baseTimer); baseTimer = setTimeout(loadBase, 250); };
$("#baseSavedOnly").onchange = loadBase;
$("#baseBank").onchange = () => {
  const bank = $("#baseBank").checked;
  $("#baseSavedOnly").closest("label").classList.toggle("hidden", bank);
  $("#bankCat").classList.toggle("hidden", !bank);
  loadBase();
};
$("#bankCat").onchange = loadBase;
const plural = (n, one, few, many) => {
  const a = n % 10, b = n % 100;
  return a === 1 && b !== 11 ? one : a >= 2 && a <= 4 && (b < 12 || b > 14) ? few : many;
};

async function loadBank() {
  const q = encodeURIComponent($("#baseSearch").value.trim());
  const sel = $("#bankCat");
  const data = await api("GET", `/api/kb?q=${q}&cat=${encodeURIComponent(sel.value || "")}`);
  if (!sel.options.length) {
    sel.innerHTML = `<option value="">Все темы</option>` +
      data.cats.map(([c, n]) => `<option value="${esc(c)}">${esc(c)} · ${n}</option>`).join("");
  }
  const list = $("#baseList");
  const n = data.found;
  const more = n > data.items.length ? ` — показаны первые ${data.items.length}, уточни поиском или темой` : "";
  list.innerHTML = data.total
    ? `<p class="empty">${q || sel.value ? `Найдено: ${n}` : `В банке ${n} ${plural(n, "ответ", "ответа", "ответов")}`}${more}</p>`
    : `<p class="empty">Банк ответов ещё не собран. Бесплатно, из открытых статей (≈280 ответов): <code>.venv/bin/python tools/build_kb_sources.py</code> в папке проекта — подробнее в README.</p>`;
  data.items.forEach((item) => list.appendChild(makeKbCard(item)));
  fitSoon();
}

/* ================= Заучивание (интервальные повторения) ================= */
let studyItems = [], studyIdx = 0, studyTotal = 0, revealed = false;
async function loadStudy() {
  const data = await api("GET", "/api/study");
  studyItems = data.due; studyTotal = data.total; studyIdx = 0;
  renderStudy();
  setDue(data.due.length);
}
function setDue(n) {
  $("#dueBadge").textContent = n;
  $("#dueBadge").classList.toggle("hidden", !n);
  $("#dueDot").classList.toggle("hidden", !n);
}
async function updateDue() {
  try { setDue((await api("GET", "/api/study")).due.length); } catch { /* сервер ещё стартует */ }
}
function renderStudy() {
  const box = $("#studyCard");
  revealed = false;
  if (!studyTotal) {
    box.innerHTML = `<p class="empty">Карточек пока нет.<br>Отмечай ответы «☆ В базу» — они появятся здесь для повторения.</p>`;
    return fitSoon();
  }
  const item = studyItems[studyIdx];
  if (!item) {
    box.innerHTML = `<p class="empty">🎉 На сегодня всё повторено (${studyTotal} в базе).<br>Следующие карточки появятся по расписанию.</p>
      <div class="center"><button class="pill ghost" id="studyAll">Повторить всё заново</button></div>`;
    $("#studyAll").onclick = async () => {
      studyItems = (await api("GET", "/api/qa?saved=true")).filter((x) => x.answer);
      studyIdx = 0; renderStudy();
    };
    return fitSoon();
  }
  box.innerHTML = `
    <div class="progress">Карточка ${studyIdx + 1} из ${studyItems.length} · уровень ${item.box} · повторений ${item.reviews}</div>
    <div class="question">${esc(item.question)}</div>
    <p class="hint" id="studyHint">Ответь вслух своими словами, затем открой ответ · <kbd>Пробел</kbd></p>
    <div class="reveal hidden">
      <div class="card"><div class="a md">${md(item.answer)}</div>
        ${item.notes ? `<div class="notes-view">${esc(item.notes)}</div>` : ""}</div>
    </div>
    <div class="grades hidden">
      <button class="pill g-again" data-g="again">Не помню <kbd>1</kbd></button>
      <button class="pill g-hard" data-g="hard">С трудом <kbd>2</kbd></button>
      <button class="pill g-good" data-g="good">Помню <kbd>3</kbd></button>
    </div>
    <div class="center" id="revealRow"><button class="pill primary" id="studyReveal">Показать ответ</button></div>`;
  $("#studyReveal").onclick = reveal;
  $$(".grades .pill", box).forEach((b) => (b.onclick = () => gradeCard(b.dataset.g)));
  fitSoon();
}
function reveal() {
  revealed = true;
  $(".reveal").classList.remove("hidden");
  $(".grades").classList.remove("hidden");
  $("#revealRow")?.remove();
  $("#studyHint")?.remove();
  fitSoon();
}
async function gradeCard(g) {
  const item = studyItems[studyIdx];
  await api("POST", `/api/study/${item.id}`, { result: g });
  studyIdx++;
  renderStudy();
  updateDue();
}
document.addEventListener("keydown", (e) => {
  if (view !== "study" || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || !studyItems[studyIdx]) return;
  if (e.code === "Space") { e.preventDefault(); if (!revealed) reveal(); }
  if (revealed && ["1", "2", "3"].includes(e.key)) gradeCard({ 1: "again", 2: "hard", 3: "good" }[e.key]);
});

/* ================= Разборы ================= */
let currentSession = null, reviewBuf = "";
async function loadSessions() {
  currentSession = null;
  $("#sessionsTitle").textContent = "Разборы интервью";
  $("#sessionView").classList.add("hidden");
  $("#sessionReview").classList.add("hidden");
  const list = $("#sessionList");
  list.classList.remove("hidden");
  const items = await api("GET", "/api/sessions");
  list.innerHTML = items.length ? "" : `<p class="empty">Здесь появятся прошедшие собеседования.<br>После каждого — ИИ-разбор: какие были вопросы, где ответы слабые и что повторить.</p>`;
  items.forEach((s) => {
    const row = document.createElement("div");
    row.className = "session-row";
    row.innerHTML = `<span>${esc(s.title)}</span><span class="meta">${s.qa_count} вопр.${s.has_review ? " · 🧠 разбор" : ""}${s.ended_at ? "" : " · идёт"}</span>`;
    row.onclick = () => openSession(s.id);
    list.appendChild(row);
  });
  fitSoon();
}
async function openSession(id) {
  currentSession = id;
  const s = await api("GET", `/api/sessions/${id}`);
  $("#sessionList").classList.add("hidden");
  $("#sessionView").classList.remove("hidden");
  $("#sessionsTitle").textContent = s.title;
  $("#sessionReviewText").innerHTML = md(s.review);
  const btn = $("#sessionReview");
  btn.classList.remove("hidden");
  btn.disabled = false;
  btn.textContent = s.review ? "🧠 Переделать разбор" : "🧠 Сделать разбор";
  $("#sessionTranscript").textContent = s.transcript || "(расшифровка сохраняется после «Стоп»)";
  const qa = await api("GET", `/api/qa?session_id=${id}`);
  const box = $("#sessionQa");
  box.innerHTML = qa.length ? "" : '<p class="empty">Нет вопросов.</p>';
  qa.forEach((q) => box.appendChild(makeCard(q)));
  fitSoon();
}
$("#sessionReview").onclick = () => {
  $("#sessionReview").disabled = true;
  send({ action: "review", session_id: currentSession });
};

/* ================= Горячие клавиши ================= */
async function loadKeys() {
  const lines = await api("GET", "/api/hotkeys");
  $("#keysList").innerHTML = lines.map((l) => {
    const [combo, ...rest] = l.split(/\s{2,}/);
    const keys = [...combo].map((ch) => `<kbd>${esc(ch)}</kbd>`).join("");
    return `<div class="key-row"><span class="combo">${keys}</span><span>${esc(rest.join(" "))}</span></div>`;
  }).join("");
  fitSoon();
}

/* ================= Настройки ================= */
async function loadSettings() {
  loadAiSettings();
  const [s, devices] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/devices")]);
  const f = $("#settingsForm");
  const SPECIAL = {
    interviewer_device: [["system", "🎧 Звук системы — всё, что слышно в наушниках (рекомендуется)"]],
    me_device: [["", "— не слушать —"], ["default", "🎙 Микрофон по умолчанию (тот же, что в Zoom)"]],
  };
  for (const name of ["interviewer_device", "me_device"]) {
    const sel = f.elements[name];
    const opts = [...SPECIAL[name], ...devices.map((d) => [d.name, d.name])];
    if (s[name] && !opts.some(([v]) => v === s[name])) opts.push([s[name], `${s[name]} (не подключено)`]);
    sel.innerHTML = opts.map(([v, l]) =>
      `<option value="${esc(v)}" ${v === s[name] ? "selected" : ""}>${esc(l)}</option>`).join("");
  }
  for (const [k, v] of Object.entries(s)) {
    const el = f.elements[k];
    if (!el || k.endsWith("_device")) continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v;
  }
  f.elements.deepgram_key.value = (s.api_keys || {}).deepgram || "";
  fitSoon();
}
$("#settingsForm").onsubmit = async (e) => {
  e.preventDefault();
  const data = {};
  for (const el of e.target.elements) {
    if (!el.name) continue;
    data[el.name] = el.type === "checkbox" ? el.checked : el.value;
  }
  try {
    await api("PUT", "/api/settings", data);
    toast(state.listening ? "Сохранено. Устройства и язык применятся после перезапуска прослушивания." : "Сохранено", true);
  } catch (err) { toast(err.message); }
};

$("#onTop").onchange = (e) => { callNative("set_on_top", e.target.checked); store("onTop", e.target.checked ? "1" : "0"); };
$("#opacity").oninput = (e) => { document.documentElement.style.setProperty("--glass-a", e.target.value); store("opacity", e.target.value); };
$("#fontSize").oninput = (e) => { document.documentElement.style.setProperty("--fs", `${e.target.value}px`); store("fontSize", e.target.value); fitSoon(); };

function restorePrefs() {
  const fs = store("fontSize");
  if (fs) { $("#fontSize").value = fs; document.documentElement.style.setProperty("--fs", `${fs}px`); }
  const op = store("opacity");
  if (op) { $("#opacity").value = op; document.documentElement.style.setProperty("--glass-a", op); }
  if (store("onTop") === "0") { $("#onTop").checked = false; callNative("set_on_top", false); }
}
window.addEventListener("pywebviewready", () => { restorePrefs(); fitSoon(); });

/* ================= Перетаскивание окна ================= */
// Окно тянется за фон, как в macOS (isMovableByWindowBackground), а текст и кнопки работают как обычно:
//  • кнопки, ссылки, поля, переключатели, всё с role/tabindex/cursor:pointer — клики (список — как в Tauri drag.js);
//  • под курсором буквы — выделение текста (курсор-«палочка»): можно выделить и скопировать ⌘C;
//  • остальное — поля и края карточек, промежутки, пустое место в строках, верхняя панель — тянет окно.
// Окно двигается, только когда мышь ушла на 4px — обычный клик его не сдвигает.
// С зажатой ⌥ Option выделение начинается откуда угодно (например, с пустого места перед абзацем).
(function windowDrag() {
  if (!OVERLAY) return;
  const CLICKABLE = "a, button, input, select, textarea, label, summary, option, " +
    "[contenteditable]:not([contenteditable='false']), [tabindex]:not([tabindex='-1']), " +
    "[role=button], [role=link], [role=menuitem], [role=tab], [role=checkbox], [role=radio], [role=switch], " +
    "[role=option], .no-drag, .card.editing";
  let start = null, moving = false;

  const clickable = (el) => !!el.closest(CLICKABLE) || getComputedStyle(el).cursor === "pointer";

  function onScrollbar(e) {  // полоса прокрутки (и вертикальная, и у широких таблиц/кода) — прокрутка
    for (let el = e.target; el && el !== document.documentElement; el = el.parentElement) {
      const cs = getComputedStyle(el), r = el.getBoundingClientRect();
      if (el.scrollHeight > el.clientHeight + 1 && /(auto|scroll)/.test(cs.overflowY) && e.clientX > r.right - 14) return true;
      if (el.scrollWidth > el.clientWidth + 1 && /(auto|scroll)/.test(cs.overflowX) && e.clientY > r.bottom - 14) return true;
    }
    return false;
  }

  // Под курсором именно буква (а не пустое место справа от строки или между абзацами)?
  function overText(x, y) {
    let node = null, offset = 0;
    if (document.caretRangeFromPoint) {
      const r = document.caretRangeFromPoint(x, y);
      if (r) { node = r.startContainer; offset = r.startOffset; }
    } else if (document.caretPositionFromPoint) {
      const p = document.caretPositionFromPoint(x, y);
      if (p) { node = p.offsetNode; offset = p.offset; }
    }
    if (!node || node.nodeType !== Node.TEXT_NODE || !node.data.trim()) return false;
    const cs = getComputedStyle(node.parentElement);
    if ((cs.userSelect || cs.webkitUserSelect) === "none") return false;  // невыделяемые подписи тянут окно
    const range = document.createRange();
    for (const [a, b] of [[offset - 1, offset], [offset, offset + 1]]) {  // символ слева и справа от каретки
      if (a < 0 || b > node.length) continue;
      range.setStart(node, a);
      range.setEnd(node, b);
      for (const q of range.getClientRects()) {
        if (x >= q.left - 1 && x <= q.right + 1 && y >= q.top - 1 && y <= q.bottom + 1) return true;
      }
    }
    return false;
  }

  function moveWindow(x, y) {
    const pv = window.pywebview;
    if (pv && typeof pv._jsApiCallback === "function") pv._jsApiCallback("pywebviewMoveWindow", [x, y], "move");
  }

  document.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || e.altKey || clickable(e.target) || onScrollbar(e) || overText(e.clientX, e.clientY)) return;
    start = { sx: e.screenX, sy: e.screenY, cx: e.clientX, cy: e.clientY };
    moving = false;
    e.preventDefault();  // фон: выделение текста не начинаем
  }, true);

  window.addEventListener("mousemove", (e) => {
    if (!start) return;
    if (!(e.buttons & 1)) { start = null; return; }
    if (!moving && Math.hypot(e.screenX - start.sx, e.screenY - start.sy) < 4) return;
    if (!moving) { moving = true; document.body.classList.add("dragging"); }
    moveWindow(e.screenX - start.cx, e.screenY - start.cy);
  });

  window.addEventListener("mouseup", () => {
    if (start && !moving) window.getSelection()?.removeAllRanges();  // простой клик по фону снимает выделение
    start = null;
    if (moving) { moving = false; document.body.classList.remove("dragging"); }
  });
})();

/* ================= Логотип: встроенный SVG ================= */
// <img> со SVG и CSS-фильтрами WebKit растеризует и размывает, поэтому вставляем вектор прямо в страницу
$$("[data-svg]").forEach(async (el) => {
  try { el.innerHTML = await (await fetch(el.dataset.svg)).text(); } catch { /* без логотипа тоже работает */ }
});

/* ================= Старт ================= */
restorePrefs();
connect();
updateDue();
showView("live");
