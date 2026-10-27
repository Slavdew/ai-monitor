// Панель задач. Состояние — /state.json, сигнал об изменении — SSE /events.
// Всё рисуется через patch(): строки с ключом, перерисовывается только строка с изменившимся HTML,
// порядок задаёт сортировка по стабильным полям — ничего не прыгает.
'use strict';

const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const store = {
  get(k, d) { try { return JSON.parse(localStorage.getItem('tm.' + k)) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem('tm.' + k, JSON.stringify(v)); } catch { /* без памяти */ } },
};

let state = { rev: 0, sources: {} };
let view = decodeURIComponent(location.hash.slice(1)) || store.get('view', null);   // #sessions, #tab:tasks
const sel = store.get('sel', {});        // вид -> выбранная задача «вкладка|id» (предпросмотр)
let cards = store.get('cards', []);      // открытые карточки: «вкладка|id»
if (view && view.startsWith('card:') && !cards.includes(view.slice(5))) cards.push(view.slice(5));   // #card:tasks|id
const urlSel = new URLSearchParams(location.search).getAll('sel');   // ?sel=tasks|id — для ссылок и снимков
const fold = new Set(store.get('fold', []));   // задачи со свёрнутыми подзадачами
// решённое скрыто по умолчанию; здесь — что владелец раскрыл: plan:вкладка|id (сделанные пункты), closed:вкладка (закрытые задачи)
const shown = new Set(store.get('shown', []));
// чипы-фильтры плана по статусу (мультивыбор, общий для всех задач); пусто — обычный вид со свёрнутым решённым
const pfilter = new Set(store.get('pfilter', []));
// вопрос ждёт владельца, пока нет ответа (и просьбы перефразировать); отвеченный ждёт агента — в счётчик не входит
const awaiting = (p) => p.status === 'wait' && !(p.answers || []).length && !p.rephrase;
const nWait = (i) => (i.plan || []).filter((p) => awaiting(p) && !p.act).length;   // действия — своим счётом, nAct
// просьба владельца (пункт req) открыта, пока не сделана и не снята
// отложенная просьба — не открытая: в плане под «отложено», в счёт «Просьбы» не входит
const openReq = (p) => p.req && p.status !== 'wait' && !['done', 'skip', 'later'].includes(p.state);
const nReq = (i) => (i.plan || []).filter(openReq).length;
// ожидаемое действие владельца (пункт act: «запустить install.py»): открыто, пока он не отметит «сделано» / «не буду»
const openAct = (p) => p.act && awaiting(p);
const nAct = (i) => (i.plan || []).filter(openAct).length;
// подписи статусов плана: коды остаются в данных и CLI, на странице — слова (ui.labels в конфиге)
const label = (st) => (state.ui?.labels || {})[st] || st;

// вид страницы: умолчания — ui в конфиге, выбор владельца в ⚙ — поверх, помнит браузер
const prefs = store.get('ui', {});
const pref = (k) => prefs[k] ?? (state.ui || {})[k];
const zoom = () => +pref('scale') || 1;
let narrow = false;
function applyPrefs() {
  const z = zoom();
  document.body.style.zoom = z === 1 ? '' : z;
  document.body.classList.toggle('full', pref('width') === 'full');
  const r = document.documentElement.style;
  r.setProperty('--wpage', (+pref('max_width') || 1400) + 'px');
  r.setProperty('--side', Math.round((+pref('split') || 0.38) * 100) + '%');
  if (prefs.vw) r.setProperty('--vw', prefs.vw + 'px');
  const th = pref('theme');
  if (th === 'light' || th === 'dark') document.documentElement.dataset.theme = th; else delete document.documentElement.dataset.theme;
  const was = narrow;
  narrow = innerWidth / z < 820;   // порог в пикселях страницы: с масштабом узкий режим наступает раньше
  document.body.classList.toggle('narrow', narrow);
  document.body.classList.toggle('wide', innerWidth / z >= 2200);
  return was !== narrow;
}
const setPref = (k, v) => { if (v === null) delete prefs[k]; else prefs[k] = v; store.set('ui', prefs); applyPrefs(); render(); };

const isClosed = (i) => i.status === 'done' || i.percent === 100;
// пиктограмма сворачивания: шеврон, повёрнут вниз у раскрытого
const chev = (open) => `<svg class="chev${open ? ' open' : ''}" viewBox="0 0 16 16" aria-hidden="true"><path d="M6 3.5 10.5 8 6 12.5"/></svg>`;
const moreRow = (key, n, what) => `<span class="more" tabindex="0" role="button" aria-expanded="${shown.has(key)}" data-act="show" data-key="${esc(key)}">${chev(shown.has(key))}${what}: ${n}</span>`;

// подвкладки — типы задач (вкладки конфига) второй строкой внутри вида; выбор помнит браузер по виду
const sub = store.get('sub', {});
function subtabs(tabs, count, warn) {
  if (tabs.length < 2) return null;
  const cur = tabs.some((t) => t.id === sub[view]) ? sub[view] : '';
  const total = tabs.reduce((s, t) => s + count(t), 0);
  const btn = (id, title, n) => `<button class="subtab${id === cur ? ' on' : ''}" data-act="sub" data-v="${esc(id)}" aria-pressed="${id === cur}">${esc(title)}${n ? ` <b>${n}</b>` : ''}</button>`;
  return { cur, row: { key: 'sub:' + view, cls: 'subtabs' + (warn ? ' warn' : ''), html: btn('', 'Все', total) + tabs.map((t) => btn(t.id, t.title, count(t))).join('') } };
}

// --- общий механизм ---

function patch(box, rows) {
  const old = new Map([...box.children].map((n) => [n.dataset.key, n]));
  let prev = null;
  for (const r of rows) {
    let n = old.get(r.key);
    if (n) old.delete(r.key);
    else { n = document.createElement('div'); n.dataset.key = r.key; }
    if (n._html !== r.html) { n.innerHTML = r.html; n._html = r.html; }
    if (n.className !== (r.cls || '')) n.className = r.cls || '';
    const want = prev ? prev.nextSibling : box.firstChild;
    if (want !== n) box.insertBefore(n, want);
    prev = n;
  }
  old.forEach((n) => n.remove());
}

function ago(ms) {
  const m = Math.round((Date.now() - ms) / 60000);
  if (m < 1) return 'сейчас';
  if (m < 60) return `${m} мин`;
  if (m < 1440) return `${Math.floor(m / 60)} ч ${m % 60} мин`;
  return new Date(ms).toLocaleDateString('ru-RU');
}
const plural = (n, one, few, many) => (n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 10 || n % 100 >= 20) ? few : many);
const agoSpan = (ms) => (ms ? `<span class="ago" data-ts="${ms}"></span>` : '—');
function tickAgo() { document.querySelectorAll('.ago').forEach((e) => { e.textContent = ago(+e.dataset.ts); }); }

async function post(source, action, args) {
  const r = await fetch(`/api/${source}/${action}`, { method: 'POST', body: JSON.stringify(args) });
  const j = await r.json();
  if (!r.ok) toast(j.error || 'Ошибка', 6000);
  return j;
}
function toast(text, ms = 1600) {
  const t = $('#toast');
  t.textContent = text; t.classList.add('on');
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('on'), ms);
}

// --- виды: вкладки из конфига + служебные; новый вид — запись в VIEWS ---

const src = (name) => state.sources[name] || {};
// идея — вид задачи: вкладка «Идеи» собирает идеи всех проектов, вкладки проектов — только задачи
// «@ideas» — подвкладка вида, не каталог
const inTab = (i, t) => (t.id === '@ideas' ? i.kind === 'idea' : i.tab === t.id && i.kind !== 'idea');

function liveSessions(id) {
  const list = (src('sessions').list || []).filter((s) => s.alive && s.task === id);
  return { n: list.length, last: Math.max(0, ...list.map((s) => s.last)) };
}

// задача взята сессией (task.py next): кем — по названию сессии
function claimTag(i, release) {
  const c = (src('claims').list || {})[i.id];
  if (!c) return '';
  const s = (src('sessions').list || []).find((x) => x.sid === c.sid);
  const who = s ? (s.title || s.sid.slice(0, 8)) : c.sid.slice(0, 8);
  return `<span class="tag claim" title="Взята сессией ${esc(c.sid)}">Взята: ${esc(who)}</span>`
    + (release ? ` <button class="ibtn" data-act="release" data-task="${esc(i.id)}" title="Отпустить захват">×</button>` : '');
}

// захваты ресурсов (task.py hold): ИБ, стенд, ветка — кто держит, до какого времени, кто ждёт
const holds = () => Object.values(src('claims').res || {});
const sessName = (sid) => { const s = (src('sessions').list || []).find((x) => x.sid === sid); return s ? (s.title || sid.slice(0, 8)) : sid.slice(0, 8); };
const hhmm = (ms) => new Date(ms).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
const holdTip = (h) => `${sessName(h.sid)}${h.why ? ' — ' + h.why : ''}, с ${hhmm(h.since)}${h.until ? ', до ' + hhmm(h.until) : ''}`;
function holdTags(i) {
  const mine = holds().filter((h) => h.task === i.id).map((h) =>
    `<span class="tag hold" title="${esc(holdTip(h))}">Держит: ${esc(h.res)}${h.until ? ' · до ' + hhmm(h.until) : ''}</span>`);
  const waits = holds().filter((h) => (h.queue || []).some((q) => q.task === i.id)).map((h) =>
    `<span class="tag wait" title="Занято: ${esc(holdTip(h))}">Ждёт: ${esc(h.res)} (${esc(sessName(h.sid))})</span>`);
  return [...mine, ...waits].join(' ');
}

const cardBtn = (i) => `<button class="ibtn go" data-act="card" data-ref="${esc(i.tab + '|' + i.id)}" title="Открыть карточку задачи" aria-label="Открыть карточку задачи"><svg viewBox="0 0 16 16" width="13" height="13" style="display:block;margin:auto" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="6.6" cy="6.6" r="4.2"/><path d="m9.8 9.8 3.8 3.8"/></svg></button>`;

// --- вопросы владельцу: пункт плана q; ответ черновиком (сохраняется сам), «Отправить Claude» — пачкой по задаче ---

const drafts = {};      // «каталог|индекс» -> текст, ещё не сохранённый сервером
const draftTimers = {};
const fmtTime = (ms) => new Date(ms).toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });

// вопрос (вариант B): совет агента блоком, варианты ответа кнопками, поле — свой ответ или дополнение к варианту.
// Варианты: «↺ проще» всегда первым, «★ Как советуешь» — если есть совет, затем opts агента. Без варианта
// текст — свой ответ; с вариантом — дополнение к нему; «проще» + текст — что именно непонятно
function qaHtml(i, p, k, edit) {
  const key = i.dir + '|' + k;
  const answers = (p.answers || []).map((a) => `<div class="ans">${linkPaths(esc(a.text), i.paths)} <span class="sess">· ${fmtTime(a.at)}</span></div>`).join('');
  const status = p.seen === false ? '<span class="sess unread">✓ Отправлено, ждёт агента</span>'
    : p.seen && (p.answers || []).length ? '<span class="sess ok">✓✓ Принято агентом</span>' : '';
  const asked = p.rephrase ? `<div class="ans">Просьба перефразировать${p.rephrase.text !== 'проще' ? ': ' + esc(p.rephrase.text) : ''} <span class="sess">· ${fmtTime(p.rephrase.at)}</span></div>` : '';
  const was = p.was ? `<div class="sess was">Было: <s>${esc(p.was)}</s></div>` : '';
  const advice = p.note ? `<div class="advice"><span class="lbl">${p.act ? 'Пояснение' : 'Совет агента'}</span> ${md(p.note, true, i)}</div>` : '';
  const at = `data-key="${esc(key)}" data-dir="${esc(i.dir)}" data-index="${k}"`;
  const draft = drafts[key] ?? p.draft ?? '';
  const opt = optOf(p, key);
  const optName = (v) => (v === '@reword' ? '↺ Проще' : v === '@advice' ? '★ Как советуешь' : v);
  if (!edit) {   // только смотреть: черновик серым, ответ — в карточке
    return `<div class="qa">${advice}${was}${answers}${asked}${status}
      ${draft || opt ? `<div class="sess">Черновик: ${opt ? `«${esc(optName(opt))}»` : ''}${opt && draft ? ' + ' : ''}${draft ? `«${esc(draft)}»` : ''}</div>` : ''}
      ${awaiting(p) ? `<span><button class="btn sm" data-act="answer" data-ref="${esc(i.tab + '|' + i.id)}" data-k="${k}">Ответить ↗</button></span>` : ''}</div>`;
  }
  const open = awaiting(p) || draft || opt || shown.has('add:' + key);
  if (!open) return `<div class="qa">${advice}${was}${answers}${asked}${status}${p.status === 'wait' ? `<span><button class="btn sm" data-act="show" data-key="add:${esc(key)}">Дополнить ответ</button></span>` : ''}</div>`;
  const opts = [['@reword', 'Вопрос непонятен — агент перепишет проще; в поле — что именно непонятно'],
    ...(p.note ? [['@advice', 'ответ — совет агента; в поле — дополнение']] : []), ...(p.opts || []).map((o) => [o, ''])];
  const bar = `<div class="qopts" role="radiogroup" aria-label="Варианты ответа">${opts.map(([v, tip]) =>
    `<button class="qopt${v === opt ? ' on' : ''}${v === '@reword' ? ' rw' : v === '@advice' ? ' adv' : ''}" role="radio" aria-checked="${v === opt}"
      data-act="qopt" ${at} data-v="${esc(v)}"${tip ? ` title="${esc(tip)}"` : ''}>${esc(optName(v))}</button>`).join('')}</div>`;
  const ph = opt === '@reword' ? 'Что непонятно (необязательно)'
    : opt ? `Дополнение к «${optName(opt)}» (необязательно)` : p.answers ? 'Дополнить ответ' : 'Свой ответ — или выберите вариант';
  return `<div class="qa">${advice}${was}${answers}${asked}${status}${bar}
    <textarea class="draft" rows="1" data-act="draft" ${at} placeholder="${esc(ph)}">${esc(draft)}</textarea></div>`;
}
// поле ответа — высотой по тексту (весь ответ виден и после ухода из поля)
function fitHeight(el) { if (!el.offsetParent) return; el.style.height = 'auto'; el.style.height = el.scrollHeight + 2 + 'px'; }
addEventListener('resize', () => document.querySelectorAll('textarea.draft').forEach(fitHeight));   // другая ширина — другой перенос
// ответ набран, но не отправлен: строка вопроса подсвечена «К отправке» (на вводе — сразу, без перерисовки)
const hasDraft = (i, p, k) => { const key = i.dir + '|' + k; return !!((drafts[key] ?? p.draft ?? '').trim() || optOf(p, key)); };
// выбранный вариант: несохранённое — drafts[«…|o»]; старая галочка «проще» (rephrase_draft) — как вариант «проще»
const optOf = (p, key) => (key + '|o' in drafts ? drafts[key + '|o'] : p.draft_opt ?? (p.rephrase_draft != null ? '@reword' : null)) || null;

// null — просьбы нет; строка (можно пустую) — есть. Несохранённое в drafts под ключом «…|r».
const rephraseOf = (p, key) => (key + '|r' in drafts ? drafts[key + '|r'] : p.rephrase_draft ?? null);

async function saveDraft(el) {
  clearTimeout(draftTimers[el.dataset.key]); delete draftTimers[el.dataset.key];
  const r = await post('tabs', 'draft', { dir: el.dataset.dir, index: +el.dataset.index, [el.dataset.field || 'text']: el.value });
  // не сохранилось — поле остаётся красным до удачной записи: ответ не теряется молча
  el.classList.toggle('unsaved', !r.ok);
  el.title = r.ok ? '' : `Не сохранено: ${r.error || 'ошибка'} — любая правка или уход из поля повторит`;
  if (!r.ok) draftTimers[el.dataset.key] = -1;   // отметка «не сохранено»: уход из поля и отправка повторят запись
}

// пункт плана; full — карточка: статус меняется меню (в предпросмотре легко задеть случайно);
// answer — поле ответа у вопроса (карточка и «Ждут вас»)
function planRow(i, p, k, full, answer = full) {
  const mark = p.state === 'waiting' ? `<span class="dep wait" title="Ждёт пунктов">⏳ После ${p.waits.join(', ')}</span>`
    : p.parallel && full && !p.req && p.state === 'ready' ? '<span class="dep"><span class="tag par" title="Можно вести одновременно с другими готовыми">Парал.</span></span>' : '<span class="dep"></span>';
  const st = p.status === 'wait' ? (awaiting(p) ? `<span class="tag st wait" title="Вопрос: ждёт вашего ответа">${esc(label('wait'))}</span>`
      : '<span class="tag st ok" title="Ответ отправлен, ждёт агента">Отвечено</span>')
    : full ? `<button class="tag st edit" data-act="plan" data-dir="${esc(i.dir)}" data-index="${k}" data-status="${esc(p.status || 'todo')}" title="Сменить статус" aria-haspopup="true">${esc(label(p.status || 'todo'))} ▾</button>`
      : `<span class="tag st">${esc(label(p.status || 'todo'))}</span>`;
  const ctx = p.status === 'wait' ? '' : ` data-ctx="plan" data-dir="${esc(i.dir)}" data-index="${k}" data-status="${esc(p.status || 'todo')}" data-text="${esc(p.text)}"`;
  return `<li class="${esc(p.state)}${p.q ? ' q' : ''}${p.q && p.status === 'wait' && hasDraft(i, p, k) ? ' drafted' : ''}"${ctx}><span class="n">${k + 1}</span>${st}${mark}<span class="t">${p.req ? '<span class="tag req" title="Ваша просьба">Просьба</span> ' : ''}${p.act ? '<span class="tag wait" title="Агент ждёт этого действия от вас">Действие</span> ' : ''}${md(p.text, true, i)}${p.q && awaiting(p) && p.asked ? ` <span class="sess qage">· ждёт <span class="ago" data-ts="${p.asked}"></span></span>` : ''}${p.req && p.note ? `<span class="sess"> — ${md(p.note, true, i)}</span>` : ''}${p.act && p.cmd ? `<span class="actcmd"><code class="copy" data-copy="${esc(p.cmd)}" tabindex="0" title="Скопировать">${esc(p.cmd)}</code></span>` : ''}</span>${p.q ? qaHtml(i, p, k, answer) : ''}</li>`;
}
const nDraftOf = (i) => (i.plan || []).filter((p, k) => (drafts[i.dir + '|' + k] ?? p.draft ?? '').trim() || rephraseOf(p, i.dir + '|' + k) !== null || optOf(p, i.dir + '|' + k)).length;
const submitInner = (i) => (nDraftOf(i) ? `<p><button class="btn primary" data-act="submit" data-dir="${esc(i.dir)}">Отправить Claude (${nDraftOf(i)})</button>
      <span class="sess">Черновики видны только вам, пока не отправлены</span></p>` : '');
// контейнер есть всегда: пока фокус в поле ответа, render отложен — кнопку обновляет refreshSubmit на месте
const submitHtml = (i) => `<div class="qsubmit" data-dir="${esc(i.dir)}">${submitInner(i)}</div>`;
function refreshSubmit(dir) {
  const i = items().find((x) => x.dir === dir);
  if (i) document.querySelectorAll(`.qsubmit[data-dir="${CSS.escape(dir)}"]`).forEach((el) => { el.innerHTML = submitInner(i); });
}
const copyBtn = (text, what) => `<button class="btn" data-copy="${esc(text)}" title="${esc(text)}">⧉ ${what}</button>`;

// Markdown в тексте задачи: полный — цель, описание, заметки; строчный — «Дальше», пункты, вопросы.
// `код` копируется щелчком. marked/DOMPurify грузятся с defer — до них текст просто экранируется.
// i — задача: пути к файлам из i.paths (их нашёл и проверил сервер) становятся ссылками.
function md(text, inline, i) {
  if (!text) return '';
  if (!window.marked || !window.DOMPurify) return esc(text);
  const html = DOMPurify.sanitize(inline ? marked.parseInline(String(text)) : marked.parse(String(text)))
    .replace(/<h[1-4]>/g, '<h5>').replace(/<\/h[1-4]>/g, '</h5>');
  // текст из DOMPurify экранирует & < >, но не кавычки — в атрибут без &quot; кавычка закрыла бы его (XSS, аудит 27.10)
  return linkPaths(html.replace(/<code>([^<]*)<\/code>/g, (m, t) => `<code class="copy" data-copy="${t.replace(/"/g, '&quot;')}" tabindex="0" title="Скопировать">${t}</code>`), i?.paths);
}
const fileLink = (a, abs) => {
  a.href = viewUrl(abs); a.target = '_blank'; a.rel = 'noopener'; a.title = abs; a.className = 'flink';
  a.dataset.ctx = 'doc'; a.dataset.path = abs;
  if (INPANEL.has(extOf(abs))) a.dataset.doc = abs;
};
function linkPaths(html, paths) {
  const keys = Object.keys(paths || {}).sort((a, b) => b.length - a.length);
  if (!keys.length) return html;
  const box = document.createElement('template');
  box.innerHTML = html;
  box.content.querySelectorAll('a[href]').forEach((a) => { const abs = paths[a.getAttribute('href')]; if (abs) fileLink(a, abs); });
  const alt = '(?<![\\w./\\\\-])(?:' + keys.map((k) => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|') + ')';   // не хвост чужого пути
  const re = new RegExp(alt, 'g'), has = new RegExp(alt);
  const walk = document.createTreeWalker(box.content, NodeFilter.SHOW_TEXT);
  const nodes = [];
  while (walk.nextNode()) if (!walk.currentNode.parentElement?.closest('a') && has.test(walk.currentNode.data)) nodes.push(walk.currentNode);
  for (const n of nodes) {
    const frag = document.createDocumentFragment();
    let at = 0;
    n.data.replace(re, (m, pos) => {
      frag.append(n.data.slice(at, pos));
      const a = document.createElement('a'); a.textContent = m; fileLink(a, paths[m]); frag.append(a);
      at = pos + m.length;
    });
    frag.append(n.data.slice(at));
    n.replaceWith(frag);
  }
  return box.innerHTML;
}

const PRIO = { 1: ['!1', 'bad', 'срочно'], 2: ['!2', 'wait', 'важно'], 3: null, 4: ['↓', '', 'потом'] };
const prioTag = (i) => { const p = PRIO[i.priority]; return p ? `<span class="tag prio ${p[1]}" title="Приоритет: ${p[2]}">${p[0]}</span>` : ''; };
// подсказка (title) — плоский текст: разметку Markdown снимаем, списки остаются строками
const plain = (s) => String(s || '').replace(/\*\*([^*]+)\*\*|__([^_]+)__/g, '$1$2').replace(/`([^`]+)`/g, '$1')
  .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1').replace(/^#+\s*/gm, '');
const stageTag = (i) => (i.stage ? `<span class="tag stage" title="Этап${i.current ? ' · в работе: ' + esc(plain(i.current)) : ''}">${md(i.stage, true, i)}${i.stage_n ? ' ' + i.stage_n : ''}</span>` : '');
// здоровье задачи (ставит агент): риск — оранжевым, стоит — красным с причиной; ok не рисуется.
// Щелчок — пояснение с причиной (поля risk / blocked) и кнопкой снять.
const HEALTH = {
  risk: ['⚠ Риск', 'wait', 'Риск: агент считает, что задача может не успеть к сроку или уйти не туда — стоит посмотреть, пока не поздно.'],
  blocked: ['⛔ Стоит', 'bad', 'Стоит: работа не двигается, пока не снимут внешнюю причину (ИТ, руководитель, другая задача).'],
};
const healthKind = (i) => (i.health === 'blocked' || i.blocked ? 'blocked' : i.health === 'risk' ? 'risk' : '');
const healthWhy = (i) => (healthKind(i) === 'blocked' ? i.blocked : i.risk) || '';
const healthTag = (i) => {
  if (i.error) return `<span class="tag bad" title="${esc('Файл задачи не прочитан: ' + i.error)}">⚠ Ошибка файла</span>`;
  const k = healthKind(i);
  if (!k) return '';
  const [txt, cls, about] = HEALTH[k], why = healthWhy(i);
  return `<span class="tag ${cls}" data-act="health" data-ref="${esc(i.tab + '|' + i.id)}" tabindex="0" role="button"
    title="${esc(about + '\nПричина: ' + (plain(why) || 'агент не указал') + '\nЩелчок — подробнее')}">${txt}${k === 'blocked' && why ? ': ' + md(why, true, i) : ''}</span>`;
};
function healthPop(t) {
  const i = findItem(t.dataset.ref), k = i && healthKind(i);
  if (!k) return;
  const why = healthWhy(i);
  menu(t, `<div class="mpop"><p>${esc(HEALTH[k][2])}</p><p><b>Причина:</b> ${why ? md(why, true, i) : '<span class="sess">Агент не указал — спросите в сессии задачи или в вопросе</span>'}</p></div><hr>`
    + mi('card', 'Открыть карточку') + mi('clear', k === 'risk' ? 'Снять риск' : 'Снять «Стоит»'), async (b) => {
    if (b.dataset.m === 'card') openCard(t.dataset.ref);
    else if (b.dataset.m === 'clear') {
      await post('tabs', 'field', { dir: i.dir, field: 'health', value: '' });
      if (k === 'blocked' && i.blocked) await post('tabs', 'field', { dir: i.dir, field: 'blocked', value: '' });
    }
    return true;
  });
}

// шкала этапов: разделы плана по порядку, у каждого сделано/всего; текущий — stage или первый незакрытый
function stages(i) {
  const work = (i.plan || []).filter((p) => !p.q && !p.req);
  const secs = [...new Set(work.map((p) => p.section).filter(Boolean))];
  const fin = (p) => p.state === 'done' || p.state === 'skip';
  const cur = secs.includes(i.stage) ? i.stage : secs.find((x) => work.some((p) => p.section === x && !fin(p)));
  return secs.map((x, n) => {
    const ps = work.filter((p) => p.section === x), d = ps.filter(fin).length;
    return { name: x, n: n + 1, of: secs.length, done: d, total: ps.length, st: d === ps.length ? 'done' : x === cur ? 'cur' : '' };
  });
}
const sfil = store.get('sfil', {});   // задача -> раздел плана, выбранный на шкале этапов
function stageBar(i, ref) {
  const ss = stages(i);
  if (ss.length < 2) return '';
  const work = (i.plan || []).filter((p) => !p.q && !p.req);
  const tip = (x) => `${x.name}: сделано ${x.done} из ${x.total}\n` + work.filter((p) => p.section === x.name)
    .map((p) => `${p.state === 'done' || p.state === 'skip' ? '✓' : p.status === 'doing' ? '●' : '·'} ${plain(p.text)}`).slice(0, 12).join('\n')
    + (x.total > 12 ? `\n… ещё ${x.total - 12}` : '');
  const cur = ss.find((x) => x.st === 'cur');
  return `<div class="sb-cap">Этапы — разделы плана; щелчок по этапу — его пункты${cur ? ` · <b>сейчас: этап ${cur.n} из ${cur.of} «${esc(cur.name)}»</b>, сделано ${cur.done} из ${cur.total}` : ''}</div>
  <ol class="stagebar" aria-label="Этапы">${ss.map((x) => `<li class="${x.st}${sfil[ref] === x.name ? ' on' : ''}" title="${esc(tip(x))}" data-act="stage" data-ref="${esc(ref)}" data-v="${esc(x.name)}" tabindex="0" role="button" aria-pressed="${sfil[ref] === x.name}">
    <i style="width:${Math.round(100 * x.done / x.total)}%"></i><span class="nm">${x.st === 'done' ? '✓ ' : x.st === 'cur' ? '● ' : ''}${esc(x.name)}</span>
    <b>${x.done}/${x.total}</b>${x.st === 'cur' ? `<span class="sn">Этап ${x.n}/${x.of}</span>` : ''}</li>`).join('')}</ol>`;
}

const remainTag = (i) => (i.remain == null ? '' : `<span class="sess remain" title="Осталось пунктов плана: к работе, в работе, вопросы">${i.remain ? 'Осталось ' + i.remain : 'Всё сделано'}</span>`);

// вкладки карточки: последняя выбранная для задачи; впервые — «Вопросы», если ждут ответа, иначе «План»
const ctab = store.get('ctab', {});

// задача целиком: шапка → сводка (вспомнить задачу) → вкладки «Вопросы / План / Файлы / Сведения».
// mode: card — правка на месте; preview — тот же состав, только смотреть (ответ — «Ответить ↗» в карточке)
function taskView(i, mode) {
  const card = mode === 'card';
  const ref = i.tab + '|' + i.id;
  const plan = i.plan || [];
  const row = (p, k) => planRow(i, p, k, card);
  const s = liveSessions(i.id);
  const names = (src('sessions').list || []).filter((x) => x.alive && x.task === i.id).map((x) => esc(x.title || x.sid.slice(0, 8)));

  const head = `<div class="t-head">${prioTag(i)}<span class="id">${esc(i.id)}</span><span class="title" title="${esc(i.title)}">${esc(i.title)}</span>
    <span class="t-chips">${healthTag(i)}${stageTag(i)}${remainTag(i)}${i.status ? `<span class="tag ${esc(i.status)}">${esc(label(i.status))}</span>` : ''}
    ${i.percent != null ? `<span class="bar" title="${i.percent}%"><i style="width:${i.percent}%"></i></span>` : ''}</span>
    <span class="t-act"><button class="btn" data-act="reqnew" data-ref="${esc(ref)}" title="Записать просьбу по задаче: агент увидит её непрочитанной и предложит разобрать">+ Просьба</button>${i.resume ? copyBtn(i.resume, 'Продолжить') : ''}${card ? '' : cardBtn(i)}</span></div>
    ${reqOpen.has(ref) ? `<div class="reqnew"><textarea data-act="reqtext" data-ref="${esc(ref)}" rows="2" placeholder="Просьба своими словами — Ctrl+Enter записать, Esc закрыть">${esc(reqDraft[ref] || '')}</textarea>
      <button class="btn primary" data-act="reqsave" data-ref="${esc(ref)}" data-dir="${esc(i.dir)}">Записать</button></div>` : ''}`;

  // паспорт: суть, где остановились, ход, сведения — всегда на виду
  const facts = [i.changed && `изм. <span class="ago" data-ts="${i.changed}"></span>`,
    i.updated && `handoff ${esc(String(i.updated).slice(0, 10))}${i.stale ? ' <span class="tag stale" title="Файлы новее handoff">Устарел</span>' : ''}`,
    i.project && esc(i.project), s.n && `<b class="live">●${s.n}</b> ${names.join(', ')}`, claimTag(i, card), holdTags(i),
    (i.waits || []).length && `<span class="tag blocked">⏳ Ждёт ${esc(i.waits.join(', '))}</span>`].filter(Boolean).join(' · ');
  const summary = `<section class="t-sum"><div class="t-sumbody">${i.goal ? `<div class="goal mdx">${md(i.goal, false, i)}</div>` : ''}${i.desc ? `<div class="desc mdx">${md(i.desc, false, i)}</div>` : ''}
      ${i.next ? `<div class="t-next"><span class="lbl">Дальше</span> ${md(i.next, true, i)}</div>` : ''}
      ${i.current ? `<div class="t-cur"><span class="lbl">В работе</span> ${md(i.current, true, i)}</div>` : ''}
      ${facts ? `<div class="t-facts">${facts}</div>` : ''}</div></section>`;

  // --- содержимое вкладок ---
  const waitQ = plan.map((p, k) => (awaiting(p) ? row(p, k) : '')).join('');
  const nAnswered = plan.filter((p) => p.status === 'wait' && !awaiting(p)).length;
  const ansKey = 'ans:' + ref;
  const answered = shown.has(ansKey) ? plan.map((p, k) => (p.status === 'wait' && !awaiting(p) ? row(p, k) : '')).join('') : '';
  const qPanel = `${waitQ ? `<ul class="plan">${waitQ}</ul>` : '<p class="sess">Вопросов, ждущих ответа, нет</p>'}
    ${card ? submitHtml(i) : ''}
    ${nAnswered ? `<p class="morep">${moreRow(ansKey, nAnswered, 'отвечено, ждёт агента')}</p>${answered ? `<ul class="plan">${answered}</ul>` : ''}` : ''}`;

  const doneKey = 'plan:' + ref, laterKey = 'later:' + ref;
  const work = plan.filter((p) => !p.q);
  const nDone = plan.filter((p) => p.state === 'done' || p.state === 'skip').length;
  const nLater = plan.filter((p) => p.state === 'later').length;
  const st = (p) => p.status || 'todo';
  const counts = {};
  for (const p of plan) if (p.status !== 'wait' && !openReq(p)) counts[st(p)] = (counts[st(p)] || 0) + 1;
  const order = src('tabs').plan_statuses || Object.keys(counts);
  const chips = Object.keys(counts).length > 1 ? `<p class="chips">${order.filter((x) => counts[x]).map((x) =>
    `<button class="chip${pfilter.has(x) ? ' on' : ''}" data-act="pf" data-k="${esc(x)}" aria-pressed="${pfilter.has(x)}">${esc(label(x))} <b>${counts[x]}</b></button>`).join('')}</p>` : '';
  const filtered = [...pfilter].some((x) => counts[x]);   // фильтр по статусам, которых в этой задаче нет, не прячет всё
  const visible = (p) => p.status !== 'wait' && !openReq(p) && (filtered ? pfilter.has(st(p))
    : !((p.state === 'done' || p.state === 'skip') && !shown.has(doneKey)) && !(p.state === 'later' && !shown.has(laterKey)));
  // разделы плана (section) сворачиваются; заголовки — в порядке первого появления
  const sf = sfil[ref] && plan.some((p) => p.section === sfil[ref]) ? sfil[ref] : '';
  const secs = sf ? [sf] : [...new Set(plan.map((p) => p.section || ''))];
  const inStage = (p) => !sf || (p.section === sf && !p.q && !p.req);
  const planList = secs.map((x) => {
    const sk = 'sec:' + ref + '|' + x, open = !fold.has(sk);
    const vis = (p) => (sf ? inStage(p) : visible(p));   // в фильтре этапа — все его пункты, и сделанные
    const rows = open || sf ? plan.map((p, k) => ((p.section || '') === x && vis(p) ? row(p, k) : '')).join('') : '';
    if (!x || sf) return rows;
    const n = plan.filter((p) => (p.section || '') === x && visible(p)).length;
    return n ? `<li class="psec" data-act="fold" data-id="${esc(sk)}" tabindex="0" role="button" aria-expanded="${open}">${chev(open)}${esc(x)} <span class="cnt">${n}</span></li>${rows}` : '';
  }).join('');
  const reqs = plan.map((p, k) => (openReq(p) ? row(p, k) : '')).join('');
  const pPanel = sf ? `<p class="sfil">Этап «${esc(sf)}» — все его пункты <button class="btn sm" data-act="stage" data-ref="${esc(ref)}" data-v="">× Все этапы</button></p>
    <ul class="plan">${planList}</ul>`
    : `${reqs ? `<h4 class="asks">Ваши просьбы · ${nReq(i)}</h4><ul class="plan">${reqs}</ul><h4 class="asks">План</h4>` : ''}
    ${chips}
    ${!filtered && (nDone || nLater) ? `<p class="morep">${nDone ? moreRow(doneKey, nDone, 'сделано') : ''}${nDone && nLater ? ' · ' : ''}${nLater ? moreRow(laterKey, nLater, 'отложено') : ''}</p>` : ''}
    ${planList ? `<ul class="plan">${planList}</ul>` : '<p class="sess">План пуст</p>'}`;

  // файлы: приложенные (на чём задача основана) и артефакты (промежуточные результаты)
  const docs = i.docs || [];
  const fItem = (d) => {
    const loc = isLocal(d.url), e = extOf(d.url);
    return `<li data-ctx="doc" data-path="${esc(d.url)}"><a href="${esc(loc ? viewUrl(d.url) : d.url)}" target="_blank" rel="noopener"${loc && INPANEL.has(e) ? ` data-doc="${esc(d.url)}"` : ''}><span class="k">${loc ? esc(e || 'файл') : '↗'}</span>${esc(docName(d))}</a>
      <code class="copy fpath" data-copy="${esc(d.url)}" tabindex="0" title="${esc(d.url)}">${esc(d.url)}</code></li>`;
  };
  const fGroup = (kind, title, hint) => {
    const g = docs.filter((d) => (d.kind || 'doc') === kind);
    return g.length ? `<h4 class="asks" title="${hint}">${title} · ${g.length}</h4><ul class="flist">${g.map(fItem).join('')}</ul>` : '';
  };
  const fPanel = docs.length ? fGroup('doc', 'Приложенные', 'исходные материалы, на которых основана задача')
    + fGroup('artifact', 'Артефакты', 'промежуточные результаты: разборы, макеты, handoff') : '<p class="sess">Файлов нет</p>';

  const links = Object.entries(i.links || {}).filter(([k, v]) => k !== 'docs' && k !== 'artifacts' && v && v.length).map(([k, v]) => `${k}: ${esc([].concat(v).join(', '))}`).join('; ');
  const sessRows = (src('sessions').list || []).filter((x) => x.alive && x.task === i.id)
    .map((x) => `<li><b>${esc(x.title || x.sid.slice(0, 8))}</b> <span class="tag ${x.status === 'busy' ? 'active' : ''}">${esc(label(x.status))}</span> ${agoSpan(x.last)}</li>`).join('');
  const kv = [
    ['Продолжить', i.resume && `<code class="copy" data-copy="${esc(i.resume)}" tabindex="0" title="Скопировать">${esc(i.resume)}</code>`],
    ['Каталог', `<code class="copy" data-copy="${esc(i.dir)}" tabindex="0" title="Скопировать">${esc(i.dir)}</code>`],
    ['Связи', links],
    ['Handoff', i.updated && esc(i.updated) + (i.stale ? ` · новее него файлов: ${i.stale}` : '')],
    ['Сессия', i.session && `<code class="copy" data-copy="${esc(i.session)}" tabindex="0">${esc(i.session)}</code>`],
    ['Сессии', sessRows && `<ul class="slist">${sessRows}</ul>`],
  ].filter(([, v]) => v).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');
  const info = `<section class="t-info"><h4 class="asks">Сведения</h4><dl class="kv">${kv}</dl></section>`;

  const tabs = [
    { id: 'q', title: 'Вопросы', n: nWait(i), tone: nWait(i) ? 'wait' : '', show: plan.some((p) => p.status === 'wait'), html: qPanel },
    { id: 'plan', title: 'План', n: work.length ? `${work.filter((p) => p.state === 'done').length}/${work.length}` : '', show: true, html: pPanel },
    { id: 'files', title: 'Файлы', n: docs.length, show: true, html: fPanel },
  ].filter((t) => t.show);
  const cur = tabs.find((t) => t.id === ctab[ref])?.id || (nWait(i) ? 'q' : 'plan');
  const bar = `<div class="t-tabs" role="tablist">${tabs.map((t) => `<button role="tab" class="t-tab${t.id === cur ? ' on' : ''}" aria-selected="${t.id === cur}"
    data-act="ctab" data-ref="${esc(ref)}" data-v="${t.id}">${t.title}${t.n ? ` <b class="${t.tone || ''}">${t.n}</b>` : ''}</button>`).join('')}</div>`;
  // вариант B: паспорт задачи слева (вспомнить), работа справа (шкала этапов, вкладки); на узком — друг под другом
  return `<article class="task ${mode}" data-ref="${esc(ref)}">${head}<div class="t-body"><div class="t-pass">${summary}${info}</div>
    <div class="t-work">${stageBar(i, ref)}${bar}<div class="t-panel" role="tabpanel">${tabs.find((t) => t.id === cur).html}</div></div></div></article>`;
}

function itemRow(i, child, kids, last) {
  const s = liveSessions(i.id);
  const pct = i.percent == null ? '' : `<span class="bar" title="${i.percent}%"><i style="width:${i.percent}%"></i></span>`;
  return {
    key: 'i:' + i.tab + ':' + i.id,
    cls: 'row' + (child ? ' child d' + Math.min(child, 3) + (last ? ' last' : '') : '') + (sel[view] === i.tab + '|' + i.id ? ' sel' : ''),
    html: `<div class="head" data-act="select" data-ctx="task" data-ref="${esc(i.tab + '|' + i.id)}" data-tab="${esc(i.tab)}" data-id="${esc(i.id)}" tabindex="0" role="button">
      ${prioTag(i)}<span class="id">${esc(i.id)}</span><span class="title" title="${esc(i.title)}">${esc(i.title)}</span>${stageTag(i)}${healthTag(i)}
      <span class="grow"></span>
      ${i.parent_kind === 'link' ? `<span class="tag link">→ ${esc(i.parent)}</span>` : ''}
      ${nWait(i) ? `<span class="tag wait" title="Вопросы ждут вашего ответа">Ждут вас: ${nWait(i)}</span>` : ''}${nAct(i) ? `<span class="tag wait" title="Агент ждёт от вас действий">Действия: ${nAct(i)}</span>` : ''}
      ${nReq(i) ? `<span class="tag req" title="Ваши просьбы, ещё не сделанные">Просьбы: ${nReq(i)}</span>` : ''}
      ${(i.waits || []).length ? `<span class="tag blocked" title="Заблокирована незавершёнными задачами">⏳ Ждёт ${esc(i.waits.join(', '))}</span>` : ''}
      ${claimTag(i)} ${holdTags(i)}
      ${i.status ? `<span class="tag ${esc(i.status)}">${esc(label(i.status))}</span>` : ''}
      ${i.stale ? '<span class="tag stale" title="Файлы новее handoff">Устарел</span>' : ''}
      ${remainTag(i)}${pct}
      <span class="sess">${s.n ? `<b>●${s.n}</b> ${agoSpan(s.last)}` : ''}</span>
      ${cardBtn(i)}
    </div>`,
  };
}

// --- документы задачи: .md/.html её каталога и links.docs; файлы — через /f/<диск>/<путь>, просмотр справа ---

const isLocal = (u) => /^[A-Za-z]:[\\/]/.test(u);
const fileUrl = (u) => '/f/' + u[0].toUpperCase() + '/' + u.slice(3).split(/[\\/]/).map(encodeURIComponent).join('/');
const extOf = (u) => (u.split(/[?#]/)[0].match(/\.([a-z0-9]+)$/i)?.[1] || '').toLowerCase();
const isMd = (u) => /^(md|markdown)$/.test(extOf(u));
// ссылка «открыть во вкладке»: Markdown — страницей панели с разметкой, остальное — как есть
const viewUrl = (u) => (isMd(u) ? '/?doc=' + encodeURIComponent(u) : fileUrl(u));
const INPANEL = new Set(['md', 'markdown', 'html', 'htm', 'txt', 'pdf', 'png', 'jpg', 'jpeg', 'svg', 'gif', 'webp', 'json', 'log',
  'py', 'ps1', 'js', 'mjs', 'cmd', 'sh', 'cs', 'css', 'yml', 'yaml', 'xml', 'bsl']);

function docName(d) {
  if (d.title) return d.title;
  if (isLocal(d.url)) return d.url.split(/[\\/]/).pop();
  try { const x = new URL(d.url); return x.hostname + (x.pathname.length > 1 ? x.pathname : ''); } catch { return d.url; }
}
function docsHtml(docs) {
  if (!docs || !docs.length) return '';
  return `<span class="docs">${docs.map((d) => {
    const loc = isLocal(d.url), e = extOf(d.url);
    return `<a href="${esc(loc ? viewUrl(d.url) : d.url)}" target="_blank" rel="noopener" title="${esc(d.url)}"${loc && INPANEL.has(e) ? ` data-doc="${esc(d.url)}"` : ''}><span class="k">${loc ? esc(e || 'файл') : '↗'}</span>${esc(docName(d))}</a>`;
  }).join('')}</span>`;
}
async function renderMd(path, box) {
  const r = await fetch(fileUrl(path), { cache: 'no-store' });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || 'HTTP ' + r.status);
  box.innerHTML = `<div class="md">${DOMPurify.sanitize(marked.parse(await r.text()))}</div>`;
  // относительные ссылки и картинки — от каталога файла; ссылка на другой .md открывается здесь же
  const base = location.origin + fileUrl(path);
  box.querySelectorAll('img[src]').forEach((img) => { img.src = new URL(img.getAttribute('src'), base).href; });
  box.querySelectorAll('a[href]').forEach((a) => {
    const h = a.getAttribute('href');
    if (h.startsWith('#')) return;
    const abs = new URL(h, base);
    a.href = abs.href; a.target = '_blank'; a.rel = 'noopener';
    if (abs.origin === location.origin && abs.pathname.startsWith('/f/') && /\.(md|markdown)$/i.test(abs.pathname)) {
      const parts = abs.pathname.slice(3).split('/').map(decodeURIComponent);
      a.dataset.doc = parts[0] + ':\\' + parts.slice(1).join('\\');
      a.href = viewUrl(a.dataset.doc) + abs.hash;
    }
  });
}
async function openDoc(path) {
  const v = $('#viewer'), body = $('#vbody');
  $('#vtitle').textContent = path.split(/[\\/]/).pop(); $('#vtitle').title = path;
  $('#vtab').href = viewUrl(path);
  v.hidden = false;
  if (isMd(path)) {
    body.innerHTML = '<div class="md"><p class="sess">Загружаю…</p></div>';
    try { await renderMd(path, body); body.scrollTop = 0; } catch (err) { body.innerHTML = `<div class="md"><p class="err">Не открылось: ${esc(err.message)}</p></div>`; }
  } else body.innerHTML = `<iframe src="${esc(fileUrl(path))}" title="${esc(path)}"></iframe>`;
}
function closeDoc() { $('#viewer').hidden = true; $('#vbody').innerHTML = ''; }

// --- вкладки: вкладки с одной group — одной кнопкой, каждая — сворачиваемым разделом ---

function renderGroup(groupTabs) {
  // вкладки группы — каталоги задач; «Идеи» — вид (kind в task.json), собирается из всех каталогов.
  // Прежний каталог ideas скрыт: его задачи — идеи по умолчанию (tabs.py), пока в них не записан kind
  const ideas = items().some((i) => i.kind === 'idea') ? [{ id: '@ideas', title: 'Идеи', root: '' }] : [];
  const tabs = [...groupTabs.filter((t) => t.id !== 'ideas'), ...ideas];
  const rows = [];
  let side = null;
  const st = subtabs(tabs, (t) => items().filter((i) => inTab(i, t) && !isClosed(i)).length);
  if (st) rows.push({ ...st.row, html: st.row.html + `<span class="grow"></span><button class="btn sm primary" data-act="newtask" aria-expanded="${!!nt}" title="Новая задача или идея">+ Задача</button>` });
  renderedGroup = groupTabs.map((t) => t.id);   // каталоги группы — выбор «Каталог» в форме
  if (nt) rows.push({ key: 'nt', cls: 'row ntform', html: nt.html });
  const shownTabs = st && st.cur ? tabs.filter((t) => t.id === st.cur) : tabs;
  for (const t of shownTabs) {
    const r = renderTab(t, shownTabs.length > 1);
    rows.push(...r.rows);
    side = side && side.some((x) => x.key.startsWith('pv:')) ? side : r.side;
  }
  return { rows, side };
}

function renderTab(tab, sectioned) {
  const all = (src('tabs').items || []).filter((i) => inTab(i, tab));
  const closedKey = 'closed:' + tab.id;
  const nClosed = all.filter(isClosed).length;
  // закрытые задачи скрыты вместе с поддеревом, пока раздел не раскрыт; выбранная — видна всегда
  const byIdAll = new Map(all.map((i) => [i.id, i]));
  const hiddenUp = (i) => { for (let x = i, n = 0; x && n < 10; x = byIdAll.get(x.parent), n++) if (isClosed(x)) return true; return false; };
  const items = shown.has(closedKey) ? all : all.filter((i) => !hiddenUp(i) || sel[view] === i.tab + '|' + i.id);
  const ids = new Set(items.map((i) => i.id));
  const kids = new Map();
  for (const i of items) if (i.parent && ids.has(i.parent)) (kids.get(i.parent) || kids.set(i.parent, []).get(i.parent)).push(i);
  const byId = (a, b) => a.id.localeCompare(b.id, 'ru', { numeric: true });
  const top = items.filter((i) => !(i.parent && ids.has(i.parent))).sort((a, b) => (a.project || '').localeCompare(b.project || '', 'ru') || (a.priority || 3) - (b.priority || 3) || nWait(b) - nWait(a) || byId(a, b));
  const rows = [];
  let project = null;
  const tabFolded = sectioned && fold.has('tab:' + tab.id);
  if (sectioned) {
    rows.push({ key: 'g:' + tab.id, cls: 'tabsec' + (tabFolded ? ' folded' : ''),
      html: `<h2 data-act="fold" data-id="tab:${esc(tab.id)}" tabindex="0" role="button" aria-expanded="${!tabFolded}" title="${tabFolded ? 'развернуть' : 'свернуть'}">${chev(!tabFolded)}${esc(tab.title)} <span class="cnt">${items.length}</span></h2>` });
  }
  const rootErr = (src('tabs').errors || []).find((e) => e.includes(tab.root));
  if (rootErr && !tabFolded) rows.push({ key: 'er:' + tab.id, cls: 'err', html: `${esc(rootErr.replace(tab.root, ''))}<code class="copy" data-copy="${esc(tab.root)}" tabindex="0">${esc(tab.root)}</code>` });
  const walk = (i, depth, last) => {
    const ch = (kids.get(i.id) || []).sort(byId);
    rows.push(itemRow(i, depth, ch.length, last));
    if (!ch.length) return;
    // полоса под задачей: сворачивает подзадачи, показывает выполнено/всего
    const done = ch.filter((c) => c.status === 'done' || c.percent === 100).length;
    const folded = fold.has(i.id);
    rows.push({
      key: 'k:' + i.tab + ':' + i.id, cls: 'strip' + (depth ? ' d' + Math.min(depth, 3) : '') + (folded ? ' folded' : ''),
      html: `<div class="strip-head" data-act="fold" data-id="${esc(i.id)}" tabindex="0" role="button" aria-expanded="${!folded}" title="${folded ? 'показать' : 'скрыть'} подзадачи">
        ${chev(!folded)}Подзадачи <b>${done}/${ch.length}</b></div>`,
    });
    if (!folded) ch.forEach((c, k) => walk(c, depth + 1, k === ch.length - 1));
  };
  for (const i of tabFolded ? [] : top) {
    if ((i.project || '') !== project) {
      project = i.project || '';
      if (project) rows.push({ key: 'p:' + tab.id + ':' + project, cls: 'sess', html: `<h3>${esc(project)}</h3>` });
    }
    walk(i, 0);
  }
  if (nClosed && !tabFolded) rows.push({ key: 'cl:' + tab.id, cls: 'morep', html: moreRow(closedKey, nClosed, 'закрытые задачи') });
  // ошибку корня уже показала строка выше — второй раз не повторять
  if (!all.length && !(src('tabs').errors || []).some((e) => e.includes(tab.root))) rows.push({ key: 'empty:' + tab.id, cls: 'empty', html: 'Пусто' });
  const picked = !narrow && items.find((i) => i.tab + '|' + i.id === sel[view]);
  // шапка — вровень с вкладками списка, чтобы предпросмотр начинался там же, где первая строка
  const head = { key: 'pv-head', cls: 'subtabs pvhead', html: '<span class="subtab">Предпросмотр</span>' };
  const side = picked ? [head, { key: 'pv:' + picked.id, cls: 'preview', html: taskView(picked, 'preview') }]
    : [head, { key: 'pv-empty', cls: 'sess pv-empty', html: 'Выберите задачу в списке' }];
  return { rows, side };
}

function findItem(ref) {
  const [tab, id] = ref.split('|');
  return (src('tabs').items || []).find((i) => i.tab === tab && i.id === id);
}

function renderCard(ref) {
  const i = findItem(ref);
  return [i ? { key: 'card:' + ref, cls: 'row card', html: taskView(i, 'card') }
    : { key: 'card-none', cls: 'empty', html: `Задача ${esc(ref)} больше не найдена` }];
}

// просьба владельца из карточки/предпросмотра: пункт req, агент получит её непрочитанной (хук, take, unseen)
const reqOpen = new Set(), reqDraft = {};
async function saveReq(ref, dir) {
  const text = (reqDraft[ref] || '').trim();
  if (!text) { toast('Пустая просьба'); return; }
  const r = await post('tabs', 'request', { dir, text });
  if (!r || r.error) return;   // ошибку уже показал post
  delete reqDraft[ref]; reqOpen.delete(ref); render(); toast('Просьба записана — агент увидит её непрочитанной');
}

// keepwarm: общий график (личный поверх общего, его читает keepwarm-gate.js) и настройки сессии под ⚙
const kwOpen = new Set();
const hm = (ms) => (ms ? new Date(ms).toTimeString().slice(0, 5) : '');
const DAYS = [[1, 'пн'], [2, 'вт'], [3, 'ср'], [4, 'чт'], [5, 'пт'], [6, 'сб'], [0, 'вс']];

// расширенный график keepwarm: правила «дни + интервал + как поддерживать + для каких видов ставить самим»;
// шлюз проверяет их сверху вниз, первое подошедшее — его значения (keepwarm-gate.js, ruleAt)
const KINDS = [['task', 'Задачи'], ['idea', 'Идеи'], ['ticket', 'Обращения']];
const RULES_SEED = [
  { name: 'Будни', days: [1, 2, 3, 4, 5], from: '07:00', to: '23:30', mode: 'until', pings: 6, kinds: ['task'] },
  { name: 'Выходные', days: [6, 0], from: '10:00', to: '23:00', mode: 'pings', pings: 6, kinds: ['task', 'idea'] },
];
const kwRules = () => (src('keepwarm').graph || {}).rules || [];
function kwRulesRow() {
  const rs = kwRules();
  // порядок и удаление — в начале строки: при переносе хвоста они не уезжают вниз
  const row = (r, n) => `<div class="kwrule" data-r="${n}"><span class="id">${n + 1}</span>
    <span class="kwmove"><button class="ibtn" data-act="kwrule" data-op="up" data-r="${n}" title="Выше: проверяется раньше"${n ? '' : ' disabled'}>↑</button><button class="ibtn" data-act="kwrule" data-op="down" data-r="${n}" title="Ниже"${n < rs.length - 1 ? '' : ' disabled'}>↓</button><button class="ibtn" data-act="kwrule" data-op="del" data-r="${n}" title="Удалить настройку">×</button></span>
    <input type="text" data-f="name" value="${esc(r.name || '')}" placeholder="Название" maxlength="40">
    <span class="kwdays">${DAYS.map(([d, t]) => `<label><input type="checkbox" data-f="day" value="${d}"${(r.days || []).includes(d) ? ' checked' : ''}>${t}</label>`).join('')}</span>
    <label>с <input type="time" data-f="from" value="${esc(r.from)}"></label><label>до <input type="time" data-f="to" value="${esc(r.to)}"></label>
    <select data-f="mode"><option value="until"${r.mode === 'until' ? ' selected' : ''}>до конца интервала</option><option value="pings"${r.mode === 'pings' ? ' selected' : ''}>по числу прогревов</option></select>
    <label${r.mode === 'pings' ? '' : ' hidden'}><input type="number" data-f="pings" min="1" max="48" value="${esc(r.pings || 6)}"> прогревов</label>
    <span class="kwkinds" title="Для каких видов задач сессия ставит таймер сама, без просьбы">Ставить самим: ${KINDS.map(([k, t]) => `<label><input type="checkbox" data-f="kind" value="${k}"${(r.kinds || ['task']).includes(k) ? ' checked' : ''}>${t}</label>`).join('')}</span></div>`;
  return { key: 'kw-rules', cls: 'row kwg kwrules', html: `<div data-kwr>
    <p class="sess">Настройки проверяются сверху вниз по текущему дню и времени — срабатывает первая подошедшая, остальные не смотрятся.
      Не подошла ни одна — один прогрев. Интервал через полночь (23:00–02:00) относится к дню начала. «Сегодня до» важнее настроек.</p>
    ${rs.map(row).join('')}
    <button class="btn sm" data-act="kwrule" data-op="add">+ Настройка</button></div>` };
}
function kwRulesFromDom() {
  return [...document.querySelectorAll('[data-kwr] .kwrule')].map((el) => {
    const v = (f) => el.querySelector(`[data-f="${f}"]`).value;
    const all = (f) => [...el.querySelectorAll(`[data-f="${f}"]:checked`)].map((x) => x.value);
    return { name: v('name'), days: all('day').map(Number), from: v('from') || '00:00', to: v('to') || '23:59', mode: v('mode'), pings: +v('pings') || 6, kinds: all('kind') };
  });
}
function kwGraphRow() {
  const k = src('keepwarm'), g = k.graph || {}, w = g.startWindow || ['07:00', '11:00'], off = new Set(g.offDays || []);
  const ext = g.rulesOn === true, hide = ext ? ' hidden' : '';
  return { key: 'kw-graph', cls: 'row kwg', html: `<div class="kwf" data-kwg>
    <b title="По нему keepwarm решает, до какого времени держать кэш сессий">График</b>
    <label${hide} title="Первый ввод за день в этом окне — день рабочий; вне окна — внеурочно, только один прогрев">Начало дня
      <input type="time" data-g="from" value="${esc(w[0])}"> – <input type="time" data-g="to" value="${esc(w[1])}"></label>
    <label${hide}>Длится <input type="number" min="1" max="16" step="0.5" data-g="workdayHours" value="${esc(g.workdayHours)}"> ч</label>
    <span class="kwdays"${hide} title="Выходные: весь день внеурочно">${DAYS.map(([n, d]) => `<label><input type="checkbox" data-g="off" value="${n}"${off.has(n) ? ' checked' : ''}>${d}</label>`).join('')}</span>
    <label title="Сегодня работаем до: день рабочий до этого времени, таймеры по умолчанию тянутся до него">Сегодня до
      <input type="time" data-g="dayend" value="${esc(hm(k.today?.end))}"></label>
    <label${hide} title="Сессия сама ставит keepwarm до конца рабочего дня; выключено — только по просьбе. По видам задач — в расширенной настройке">
      <input type="checkbox" data-g="autoStart"${g.autoStart === false ? '' : ' checked'}> Ставить самим</label>
    <label title="Несколько графиков: свои дни, интервал, способ поддержания и виды задач"><input type="checkbox" data-g="rulesOn"${ext ? ' checked' : ''}> Расширенная настройка</label>
  </div>` };
}
function kwSessionForm(s) {
  const o = (src('keepwarm').sessions || {})[s.sid] || {};
  const h = o.handoff === true ? 'yes' : o.handoff === false ? 'no' : '';
  return `<div class="kwf" data-kws="${esc(s.sid)}">
    <label title="Сколько раз будить молчащую сессию, потом передачка; пусто — пока не выйдет срок">Пробуждений до передачки
      <input type="number" min="0" max="48" data-k="pings" value="${o.pings || ''}" placeholder="По сроку"></label>
    <label title="Свой срок вместо срока таймера (конец дня или часы)">Держать до <input type="time" data-k="until" value="${esc(hm(o.until))}"></label>
    <label>В конце <select data-k="handoff"><option value=""${h ? '' : ' selected'}>Как в таймере</option>
      <option value="yes"${h === 'yes' ? ' selected' : ''}>Передачка</option><option value="no"${h === 'no' ? ' selected' : ''}>Только снять таймер</option></select></label>
  </div>`;
}
const kwSummary = (sid) => {
  const o = (src('keepwarm').sessions || {})[sid];
  if (!o) return '';
  return [o.pings && `${o.pings} проб.`, o.until && `до ${hm(o.until)}`, o.handoff === false && 'без передачки'].filter(Boolean).join(', ');
};
async function kwSave(t) {
  const f = t.closest('[data-kws],[data-kwg]');
  if (f.dataset.kws !== undefined) {
    const v = (k) => f.querySelector(`[data-k="${k}"]`).value;
    let until = null;
    if (v('until')) { const [hh, mm] = v('until').split(':').map(Number); until = new Date().setHours(hh, mm, 0, 0); if (until <= Date.now()) until += 86400000; }
    return post('keepwarm', 'session', { sid: f.dataset.kws, pings: +v('pings') || 0, until, handoff: { yes: true, no: false }[v('handoff')] ?? null });
  }
  const g = t.dataset.g, q = (k) => f.querySelector(`[data-g="${k}"]`);
  if (g === 'dayend') return post('keepwarm', 'dayend', { end: t.value });
  if (g === 'from' || g === 'to') return post('keepwarm', 'graph', { startWindow: [q('from').value, q('to').value] });
  if (g === 'workdayHours') return post('keepwarm', 'graph', { workdayHours: +t.value || null });
  if (g === 'off') return post('keepwarm', 'graph', { offDays: [...f.querySelectorAll('[data-g="off"]:checked')].map((x) => +x.value) });
  if (g === 'autoStart') return post('keepwarm', 'graph', { autoStart: t.checked });
  if (g === 'rulesOn') return post('keepwarm', 'graph', t.checked && !kwRules().length ? { rulesOn: true, rules: RULES_SEED } : { rulesOn: t.checked });
}

function renderSessions() {
  const off = new Set(src('keepwarm').off || []);
  const kw = 'keepwarm' in state.sources && !src('keepwarm').error;
  if (!(src('sessions').list || []).length) return [{ key: 's-none', cls: 'empty', html: 'Сессий нет' }];
  // настройки keepwarm — своим разделом с подписью и другим фоном: иначе сливаются с плашками сессий
  const head = (key, text) => ({ key, cls: 'tabsec static', html: `<h2>${text}</h2>` });
  const n = (src('sessions').list || []).length;
  return [...(kw ? [head('kw-h', 'Keepwarm — поддержание кэша сессий'), kwGraphRow(), ...(src('keepwarm').graph?.rulesOn ? [kwRulesRow()] : []), head('s-h', `Сессии <span class="cnt">${n}</span>`)] : []), ...(src('sessions').list || []).map((s) => [{
    key: 's:' + s.sid,
    cls: 'row' + (s.alive ? '' : ' dead'),
    html: `<div class="head">${s.title ? `<span class="title">${esc(s.title)}</span>` : ''}
      <code class="id copy" data-copy="${esc(s.sid)}" title="${esc(s.sid)} — скопировать">${esc(s.sid.slice(0, 8))}</code>
      ${s.address && s.address !== s.title ? `<code class="id copy" data-copy="${esc(s.address)}" title="Адрес для SendMessage — скопировать">@${esc(s.address)}</code>` : ''}
      <span class="tag ${s.status === 'busy' ? 'active' : ''}">${esc(label(s.status))}</span>
      <span class="id grow">${esc(s.cwd)}</span>
      <span class="sess">${s.task ? 'задача ' + esc(s.task) : 'без задачи'}</span>
      ${holds().filter((h) => h.sid === s.sid).map((h) => `<span class="tag hold" title="${esc(holdTip(h))}">Держит: ${esc(h.res)}</span><button class="ibtn" data-act="free" data-res="${esc(h.res)}" title="Снять захват «${esc(h.res)}»">×</button>`).join('')}
      ${holds().filter((h) => (h.queue || []).some((q) => q.sid === s.sid)).map((h) => `<span class="tag wait" title="Занято: ${esc(holdTip(h))}">Ждёт: ${esc(h.res)}</span>`).join('')}
      <span class="sess">Последнее ${agoSpan(s.last)}</span>
      ${kw && s.alive ? (src('keepwarm').all_off ? '<span class="sess" title="Выключен рубильником model в настройках">Keepwarm выкл.</span>'
        : `<label class="sess"><input type="checkbox" data-act="keepwarm" data-sid="${esc(s.sid)}" ${off.has(s.sid) ? '' : 'checked'}> Keepwarm</label>
          ${kwSummary(s.sid) ? `<span class="tag" title="Свои настройки keepwarm этой сессии">${esc(kwSummary(s.sid))}</span>` : ''}
          <button class="ibtn" data-act="kwset" data-sid="${esc(s.sid)}" title="Настройки keepwarm этой сессии" aria-expanded="${kwOpen.has(s.sid)}">⚙</button>`) : ''}
    </div>`,
  }, ...(kw && s.alive && kwOpen.has(s.sid) ? [{ key: 'kws:' + s.sid, cls: 'row kwrow', html: kwSessionForm(s) }] : [])]).flat()];   // форма — своей строкой: смена статуса сессии не стирает ввод
}

// «Занято»: захваты ресурсов списком от ресурса — свободна ли ИБ, кто держит, кто ждёт; повисший снимается
function renderBusy() {
  const hs = holds().sort((a, b) => a.res.localeCompare(b.res, 'ru'));
  if (!hs.length) return [{ key: 'busy-none', cls: 'empty', html: 'Все ресурсы свободны' }];
  return [{
    key: 'busy', cls: 'row busy', html: `<table class="busy"><tr><th>Ресурс</th><th>Кто держит</th><th>Зачем</th><th>Сколько</th><th>Очередь</th><th></th></tr>
      ${hs.map((h) => `<tr><td><b>${esc(h.res)}</b></td><td>${esc(sessName(h.sid))}${h.task ? ` · <span class="id">${esc(h.task)}</span>` : ''}</td>
        <td>${esc(h.why || '—')}</td><td>с ${hhmm(h.since)}${h.until ? ', до ' + hhmm(h.until) : ''}</td>
        <td>${(h.queue || []).map((q) => esc(sessName(q.sid))).join(', ') || '—'}</td>
        <td><button class="btn sm" data-act="free" data-res="${esc(h.res)}" title="Снять захват: держатель узнает при следующем hold или held">Снять</button></td></tr>`).join('')}</table>`,
  }];
}

// --- новая задача со страницы (просьба 106): форма, вложения перетаскиванием, вставкой (Ctrl+V) и выбором ---
// разметка формы не меняется, пока она открыта, — перерисовка панели не стирает ввод; файлы — прямо в DOM
let nt = null, renderedGroup = [];   // nt — { html, files: [File], tab, kind }
const isIdea = () => nt?.kind === 'idea';
function ntOpen(all, cur) {
  const tabs = all.filter((t) => t.id !== 'ideas');   // прежний каталог идей — не место для новых
  const ok = tabs.filter((t) => !t.repo);
  const tab = (ok.find((t) => t.id === cur) || ok[0] || {}).id, kind = cur === '@ideas' ? 'idea' : 'task';
  const btns = tabs.map((t) => `<button type="button" data-act="nttab" data-v="${esc(t.id)}" aria-pressed="${t.id === tab}"${t.repo ? ` disabled title="Каталог задач внутри репозитория ${esc(t.repo)}: задачи ведутся вне репозиториев — задайте вкладке new_root"` : ` title="${esc(t.new)}"`}>${esc(t.title)}</button>`).join('');
  // проект — группа в списке; подсказки — проекты существующих задач, по умолчанию — последний в выбранном каталоге
  const projs = [...new Set(items().map((i) => i.project).filter(Boolean))].sort((a, b) => a.localeCompare(b, 'ru'));
  const projOf = (id) => (items().filter((i) => i.tab === id && i.project).sort((a, b) => b.changed - a.changed)[0] || {}).project || '';
  const proj = projOf(tab);
  const kbtn = (k, t) => `<button type="button" data-act="ntkind" data-v="${k}" aria-pressed="${k === kind}">${t}</button>`;
  nt = { tab, kind, files: [], projOf, autoProj: proj, html: `<div class="ntf" id="ntf">
    <div class="ntrow"><span class="sess">Вид</span><span class="seg" id="ntkinds">${kbtn('task', 'Задача')}${kbtn('idea', 'Идея')}</span>
      ${tabs.length > 1 ? `<span class="sess">Каталог</span><span class="seg" id="nttabs">${btns}</span>` : ''}</div>
    ${ok.length ? '' : '<p class="sess">Все каталоги задач внутри репозиториев — новых задач некуда класть. Задайте каталогу new_root вне репозитория.</p>'}
    <div class="ntrow"><label class="sess grow">Название<input id="nttitle" type="text" placeholder="Например: Экспорт плана в CSV"></label>
      <label class="sess">Проект<input id="ntproj" type="text" list="ntprojs" value="${esc(proj)}"></label>
      <datalist id="ntprojs">${projs.map((x) => `<option value="${esc(x)}">`).join('')}</datalist></div>
    <label class="sess">Описание — зачем и что должно получиться<textarea id="ntgoal" rows="3"></textarea></label>
    <label class="sess" id="ntplanl"${kind === 'idea' ? ' hidden' : ''}>План — пункт на строку. Можно оставить пустым (агент заполнит сам в начале работы)<textarea id="ntplan" rows="2"></textarea></label>
    <div class="ntdrop" id="ntdrop">Перетащите файлы сюда, вставьте Ctrl+V или <button type="button" class="linkbtn" data-act="ntpick">выберите</button>
      <input type="file" id="ntinp" multiple hidden><div class="ntfiles" id="ntfiles"></div></div>
    <div class="ntrow"><button class="btn primary" data-act="ntsave"${ok.length ? '' : ' disabled'}>Создать</button>
      <label class="sess" id="ntaskl"${kind === 'idea' ? ' hidden' : ''}><input type="checkbox" id="ntask" checked> Просьба агенту «разобрать задачу»</label>
      <span class="grow"></span><span class="sess" id="ntstat"></span><button class="btn" data-act="ntcancel">Отмена</button></div></div>` };
  render();
  setTimeout(() => $('#nttitle')?.focus(), 0);
}
const fsize = (n) => (n < 1048576 ? Math.max(1, Math.round(n / 1024)) + ' КБ' : (n / 1048576).toFixed(1) + ' МБ');
function ntFiles(list) {
  if (!nt) return;
  for (const f of list) if (f.size || f.type) nt.files.push(f);
  const box = $('#ntfiles');
  if (box) box.innerHTML = nt.files.map((f, k) => `<span class="tag">${esc(f.name || 'вставка.png')} · ${fsize(f.size)} <button type="button" class="ibtn" data-act="ntdel" data-k="${k}" title="Убрать">×</button></span>`).join('');
}
async function ntSave() {
  const title = $('#nttitle').value.trim();
  if (!title) { toast('Нужно название'); $('#nttitle').focus(); return; }
  const stat = $('#ntstat'), btn = $('[data-act="ntsave"]');
  btn.disabled = true; stat.textContent = 'Создаю…';
  const r = await post('tabs', 'create', { tab: nt.tab, kind: nt.kind, project: $('#ntproj').value.trim(), title, goal: $('#ntgoal').value,
    plan: isIdea() ? [] : $('#ntplan').value.split('\n'), ask: !isIdea() && $('#ntask').checked });
  if (!r.ok) { btn.disabled = false; stat.textContent = ''; return; }
  const bad = [];
  for (const [k, f] of nt.files.entries()) {   // по одному: крупный файл не держит память страницы целиком
    stat.textContent = `Файлы ${k + 1} из ${nt.files.length}…`;
    const name = f.name || `вставка-${k + 1}.png`;
    const res = await fetch(`/api/tabs/attach?dir=${encodeURIComponent(r.dir)}&name=${encodeURIComponent(name)}`, { method: 'POST', body: f });
    if (!res.ok) bad.push(`${name}: ${(await res.json().catch(() => ({}))).error || res.status}`);
  }
  const ref = nt.tab + '|' + r.id;
  nt = null; render();
  if (bad.length) toast('Задача создана, файлы не легли: ' + bad.join('; '), 8000);
  else toast(`Создано: ${r.dir}`, 4000);
  sel[view] = ref; store.set('sel', sel);
}
document.addEventListener('dragover', (e) => { const f = e.target.closest?.('#ntf'); if (f) { e.preventDefault(); $('#ntdrop').classList.add('over'); } });
document.addEventListener('dragleave', (e) => { if (nt && !e.relatedTarget?.closest?.('#ntf')) $('#ntdrop')?.classList.remove('over'); });
document.addEventListener('drop', (e) => { if (e.target.closest?.('#ntf')) { e.preventDefault(); $('#ntdrop').classList.remove('over'); ntFiles(e.dataTransfer.files); } });
document.addEventListener('paste', (e) => { if (nt && $('#ntf') && e.clipboardData.files.length) { e.preventDefault(); ntFiles(e.clipboardData.files); } });
document.addEventListener('change', (e) => { if (e.target.id === 'ntinp') { ntFiles(e.target.files); e.target.value = ''; } });

// --- доска объявлений сессий (навык session-board): смотреть и писать от лица владельца ---

const boardForm = store.get('boardForm', { board: '', hours: 12 });
function renderBoard() {
  const b = src('board');
  const boards = b.boards || [];
  if (!boards.length) return [{ key: 'b-empty', cls: 'empty', html: 'Досок нет: задайте sources.board.default (например home) или работайте в 1С-проекте с .dev.env' }];
  const cur = boards.some((x) => x.name === boardForm.board) ? boardForm.board : boards[0].name;
  const label = (x) => x.machine ? `${x.name} — доска этой машины` : `${x.name} — доска ИБ 1С`;
  const rows = [{
    key: 'b-form', cls: 'row bform',
    html: `<div class="bf"><label class="sess" title="Доска — общий файл объявлений для сессий: у 1С-проекта своя на каждую ИБ (из .dev.env), без 1С — доска машины (SESSION_BOARD)">Доска
        <select data-act="bsel">${boards.map((x) => `<option value="${esc(x.name)}"${x.name === cur ? ' selected' : ''}>${esc(label(x))}</option>`).join('')}</select></label>
      <input id="btext" type="text" placeholder="Объявление сессиям — от лица «${esc(b.owner || 'владелец')}»">
      <label class="sess" title="Новое объявление с тем же ключом заменяет ваше прежнее (старое остаётся в истории). Пусто — не заменяет. ib, stop, branch — блокирующие, по умолчанию 2 ч">Ключ
        <input id="bkey" type="text" value="${esc(b.key || '')}"></label>
      <label class="sess" title="Через сколько часов объявление уйдёт само">Срок <input id="bhours" type="number" min="0" value="${esc(boardForm.hours)}"> ч</label>
      <button class="btn primary" data-act="bsay" data-mode="new">Объявить</button></div>`,
  }];
  const stLabel = { active: 'действует', replaced: 'заменено', closed: 'снято закрытием', expired: 'истекло' };
  const noteRow = (x, n, hist) => ({
    key: `${hist ? 'bh' : 'bn'}:${x.name}:${n.at}:${n.owner}`, cls: 'row' + (hist && n.state !== 'active' ? ' dead' : ''),
    html: `<div class="head" style="cursor:default"><span class="id">${esc(fmtTime(n.at))}</span><span class="title">${esc(n.tag)}</span>
      ${n.key ? `<span class="tag ${n.blocking ? 'wait' : ''}" title="${n.blocking ? 'блокирующий ключ' : 'ключ: новое с тем же тегом и ключом заменяет это'}">#${esc(n.key)}</span>` : ''}
      <span class="grow" title="${esc(n.text)}" style="white-space:normal">${esc(n.text)}</span>
      ${hist ? `<span class="tag">${stLabel[n.state] || ''}</span>` : `<span class="sess">До ${esc(fmtTime(n.until))}</span>
      ${n.key && n.key !== 'close' ? `<button class="ibtn" data-act="bsay" data-mode="edit" data-board="${esc(x.name)}" data-tag="${esc(n.tag)}" data-key="${esc(n.key)}" data-text="${esc(n.text)}" title="Изменить: новая строка заменит эту, прежняя останется в истории">✎</button>
        <button class="ibtn" data-act="bsay" data-mode="delete" data-board="${esc(x.name)}" data-tag="${esc(n.tag)}" data-key="${esc(n.key)}" data-text="${esc(n.text)}" title="Снять: заменить истёкшей строкой «снято», прежняя останется в истории">×</button>` : ''}
      <button class="ibtn" data-act="bsay" data-mode="close" data-board="${esc(x.name)}" data-tag="${esc(n.tag)}" title="Закрыть тег «${esc(n.tag)}»: снять все его объявления (close)">⊘</button>`}</div>`,
  });
  for (const x of boards) {
    rows.push({ key: 'bh:' + x.name, cls: 'tabsec', html: `<h2>${esc(label(x))} <span class="cnt">${x.notes.length}</span></h2>` });
    if (!x.notes.length) rows.push({ key: 'bn0:' + x.name, cls: 'empty', html: 'действующих объявлений нет' });
    for (const n of x.notes) rows.push(noteRow(x, n, false));
    const hk = 'bhist:' + x.name;
    if ((x.history || []).length) rows.push({ key: 'bhm:' + x.name, cls: 'morep', html: moreRow(hk, x.history.length, 'история (все строки, новые сверху)') });
    if (shown.has(hk)) for (const n of x.history) rows.push(noteRow(x, n, true));
  }
  return rows;
}

// «Ждут вас»: всё неразобранное владельцем — вопросы и его просьбы всех задач, ответ на месте
const items = () => src('tabs').items || [];
const pending = () => items().filter((i) => (i.plan || []).some((p) => p.status === 'wait') || nReq(i) || nAct(i));
function renderInbox() {
  const all = src('tabs').tabs || [];
  const isAsked = (i) => (i.plan || []).some((p) => p.status === 'wait' && !p.act);
  const isActing = (i) => (i.plan || []).some((p) => p.status === 'wait' && p.act);
  const st = subtabs(all.filter((t) => items().some((i) => inTab(i, t) && (isAsked(i) || isActing(i) || nReq(i)))),
    (t) => items().filter((i) => inTab(i, t)).reduce((s, i) => s + nWait(i) + nAct(i), 0), true);
  const inSub = (i) => !st || !st.cur || inTab(i, { id: st.cur });
  const asked = items().filter((i) => isAsked(i) && inSub(i));
  const withReq = items().filter((i) => nReq(i) && inSub(i));
  const rows = st ? [st.row] : [];
  // ожидаемые действия — первыми: агент стоит, пока их не сделать
  const acting = items().filter((i) => isActing(i) && inSub(i));
  if (acting.length) rows.push({ key: 'act-h', cls: 'tabsec static', html: `<h2>Ожидаемые действия <span class="cnt">${acting.reduce((s, i) => s + nAct(i), 0)}</span></h2>` });
  for (const i of acting) {   // как блок вопросов: варианты «Сделал» / «Не буду», свой ответ, «проще», отправка
    const fk = 'ina:' + i.tab + '|' + i.id, open = !fold.has(fk);
    const acts = open ? (i.plan || []).map((p, k) => (p.status === 'wait' && p.act ? planRow(i, p, k, false, true) : '')).join('') : '';
    rows.push({ key: 'act:' + i.tab + ':' + i.id, cls: 'row card inbox' + (open ? '' : ' folded'),
      html: `<div class="dhead fold" data-act="fold" data-id="${esc(fk)}" tabindex="0" role="button" aria-expanded="${open}">${chev(open)}<span class="id">${esc(i.id)}</span><span class="title grow" title="${esc(i.title)}">${esc(i.title)}</span>
        ${nAct(i) ? `<span class="tag wait">Действия: ${nAct(i)}</span>` : ''}${cardBtn(i)}</div>${open ? `<ul class="plan">${acts}</ul>${submitHtml(i)}` : ''}` });
  }
  if (acting.length) rows.push({ key: 'q-h', cls: 'tabsec static', html: '<h2>Вопросы</h2>' });
  const drafted = asked.filter(nDraftOf);
  if (drafted.length > 1) rows.push({ key: 'in-all', cls: 'morep', html: `<button class="btn primary" data-act="submitall">Отправить все (${drafted.reduce((s, i) => s + nDraftOf(i), 0)})</button> <span class="sess">Ответы ${drafted.length} задач одной кнопкой</span>` });
  if (!asked.length) rows.push({ key: 'in-none', cls: 'empty', html: 'Вопросов нет' });
  for (const i of asked) {
    const fk = 'in:' + i.tab + '|' + i.id, open = !fold.has(fk);
    const asks = open ? (i.plan || []).map((p, k) => (p.status === 'wait' && !p.act ? planRow(i, p, k, false, true) : '')).join('') : '';
    const n = nWait(i), done = (i.plan || []).filter((p) => p.status === 'wait' && !p.act).length - n;
    rows.push({ key: 'in:' + i.tab + ':' + i.id, cls: 'row card inbox' + (open ? '' : ' folded'),
      html: `<div class="dhead fold" data-act="fold" data-id="${esc(fk)}" tabindex="0" role="button" aria-expanded="${open}" title="${open ? 'свернуть' : 'развернуть'}">${chev(open)}<span class="id">${esc(i.id)}</span><span class="title grow" title="${esc(i.title)}">${esc(i.title)}</span>
        ${n ? `<span class="tag wait">Ждут вас: ${n}</span>` : ''}${done ? `<span class="tag ok">Отвечено: ${done}</span>` : ''}${cardBtn(i)}</div>
        ${open ? `<ul class="plan">${asks}</ul>${submitHtml(i)}` : ''}` });
  }
  // просьбы владельца ждут агента, не его — внизу свёрнутой строкой, чтобы видеть неразобранное
  const n = withReq.reduce((s, i) => s + nReq(i), 0);
  if (n) rows.push({ key: 'in-req', cls: 'morep', html: moreRow('inbox:req', n, 'ваши просьбы у агентов') });
  if (n && shown.has('inbox:req')) for (const i of withReq) {   // блок задачи сворачивается, как у вопросов
    const fk = 'inr:' + i.tab + '|' + i.id, open = !fold.has(fk);
    const reqs = open ? (i.plan || []).map((p, k) => (openReq(p) ? planRow(i, p, k, false) : '')).join('') : '';
    rows.push({ key: 'inr:' + i.tab + ':' + i.id, cls: 'row card inbox' + (open ? '' : ' folded'),
      html: `<div class="dhead fold" data-act="fold" data-id="${esc(fk)}" tabindex="0" role="button" aria-expanded="${open}" title="${open ? 'свернуть' : 'развернуть'}">${chev(open)}<span class="id">${esc(i.id)}</span><span class="title grow" title="${esc(i.title)}">${esc(i.title)}</span>
        <span class="tag req">Просьбы: ${nReq(i)}</span>${cardBtn(i)}</div>${open ? `<ul class="plan">${reqs}</ul>` : ''}` });
  }
  return rows;
}

// «Канбан»: открытые задачи по колонкам состояния (вычисляется, агенту ничего писать не нужно);
// карточка — главное о задаче, щелчок — её карточка. Типы задач — подвкладками
const KANBAN = [
  ['wait', 'Ждут вас', (i) => nWait(i) > 0 || nAct(i) > 0],
  ['blocked', 'Стоят', (i) => i.health === 'blocked' || !!i.blocked || (i.waits || []).length > 0],
  ['doing', 'В работе', (i) => liveSessions(i.id).n > 0 || !!(src('claims').list || {})[i.id] || (i.plan || []).some((p) => p.status === 'doing')],
  ['todo', 'К работе', () => true],
];
function kcard(i) {
  const ss = stages(i), cur = ss.find((x) => x.st === 'cur'), s = liveSessions(i.id);
  return `<div class="kcard" data-act="card" data-ref="${esc(i.tab + '|' + i.id)}" data-ctx="task" tabindex="0" role="button" title="Открыть карточку">
    <div class="kh">${prioTag(i)}<span class="id">${esc(i.id)}</span>${healthTag(i)}${s.n ? `<b class="live">●${s.n}</b>` : ''}</div>
    <div class="kt">${esc(i.title)}</div>
    ${cur ? `<div class="ks" title="${esc(cur.name)}: сделано ${cur.done} из ${cur.total}">● ${esc(cur.name)} <span class="sess">· этап ${cur.n} из ${cur.of}</span></div>` : i.stage ? `<div class="ks">${md(i.stage, true, i)}</div>` : ''}
    ${i.next ? `<div class="kn">${md(i.next, true, i)}</div>` : ''}
    <div class="kf">${nWait(i) ? `<span class="tag wait">Ждут вас: ${nWait(i)}</span>` : ''}${remainTag(i)}
      ${i.percent != null ? `<span class="bar" title="${i.percent}%"><i style="width:${i.percent}%"></i></span>` : ''}</div></div>`;
}
function renderKanban() {
  const tabs = src('tabs').tabs || [];
  const st = subtabs(tabs.filter((t) => items().some((i) => inTab(i, t))), (t) => items().filter((i) => inTab(i, t) && !isClosed(i)).length);
  const list = items().filter((i) => !isClosed(i) && (!st || !st.cur || inTab(i, { id: st.cur })))
    .sort((a, b) => (a.priority || 3) - (b.priority || 3) || nWait(b) - nWait(a) || (b.changed || 0) - (a.changed || 0));
  const cols = KANBAN.map(([id, title]) => ({ id, title, items: [] }));
  for (const i of list) cols[KANBAN.findIndex(([, , f]) => f(i))].items.push(i);
  const nDone = items().filter((i) => isClosed(i) && (!st || !st.cur || inTab(i, { id: st.cur }))).length;
  return [...(st ? [st.row] : []), { key: 'kb', cls: 'kanban', html: cols.map((c) => `<section class="kcol ${c.id}"><h3>${c.title} <span class="cnt">${c.items.length}</span></h3>
    ${c.items.map(kcard).join('') || '<p class="sess">—</p>'}</section>`).join('') + (nDone ? `<section class="kcol done"><h3>Сделано <span class="cnt">${nDone}</span></h3><p class="sess">Закрытые задачи — во вкладке задач, «› закрытые»</p></section>` : '') }];
}

function views() {
  const tabs = [];
  for (const t of src('tabs').tabs || []) {
    if (!t.group) { tabs.push({ id: 'tab:' + t.id, title: t.title, render: () => renderTab(t, false) }); continue; }
    if (tabs.some((v) => v.id === 'grp:' + t.group)) continue;
    const members = (src('tabs').tabs || []).filter((x) => x.group === t.group);
    tabs.push({ id: 'grp:' + t.group, title: t.group, render: () => renderGroup(members) });
  }
  const opened = cards.map((ref) => ({ id: 'card:' + ref, title: tabName(ref), card: ref, render: () => renderCard(ref) }));
  const board = 'board' in state.sources ? [{ id: 'board', title: 'Доска', render: renderBoard }] : [];
  const busy = holds().length ? [{ id: 'busy', title: 'Занято', render: renderBusy }] : [];   // появляется с первым захватом
  const inbox = pending().length ? [{ id: 'inbox', title: 'Ждут вас', render: renderInbox }] : [];
  return [...inbox, ...tabs, { id: 'kanban', title: 'Канбан', render: renderKanban }, { id: 'sessions', title: 'Сессии', render: renderSessions }, ...busy, ...board, ...opened];
}

function renderLimits() {
  const u = src('usage');
  const rows = (u.limits || []).map((l) => ({
    key: l.kind + (l.label || ''), cls: 'lim ' + (l.severity || ''),
    // полоска заполнения — видно, сколько осталось, не читая цифр; сброс — в подсказке
    html: `<span title="${l.resets_at ? 'Сброс ' + esc(new Date(l.resets_at).toLocaleString('ru-RU', { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' })) : ''}">${esc(l.label)} <b>${l.percent ?? '—'}%</b>
      <span class="lbar" aria-hidden="true"><i style="width:${Math.min(100, Math.max(0, +l.percent || 0))}%"></i></span></span>`,
  }));
  const hhmm = (ms) => new Date(ms).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  const info = [u.ok_at && `обновлено ${hhmm(u.ok_at)}`, u.error && `ошибка ${hhmm(u.error_at)}: ${u.error}`].filter(Boolean).join('\n');
  if (u.ok_at) rows.push({ key: 'at', html: `<span title="Последнее удачное обновление лимитов">${hhmm(u.ok_at)}</span>` });
  if (u.error) rows.push({ key: 'err', cls: u.limits ? 'stale' : 'err', html: u.limits ? 'устарело ⚠' : 'лимиты не загрузились' });
  if (info) rows.push({ key: 'info', html: `<button class="ibtn${u.error && !u.limits ? ' bad' : ''}" data-act="info" data-text="${esc(info)}" title="${esc(info)}">i</button>` });
  rows.push({ key: 'refresh', html: '<button class="ibtn" data-act="refresh" data-source="usage" title="Обновить лимиты сейчас">↻</button>' });
  const m = state.model || {};
  if (m.enabled === false) rows.unshift({ key: 'model', cls: 'lim warning', html: '<b title="model.enabled = false: функции, тратящие токены, выключены">Токены: выкл.</b>' });
  patch($('#limits'), rows);
}

let renderLater = false;
function render() {
  // не перерисовывать под пишущим владельцем: черновик потеряет фокус; дорисуем, когда уйдёт из поля
  const ae = document.activeElement;
  if (ae?.dataset?.act === 'draft' || (ae?.closest?.('.bf') && ae.tagName === 'INPUT')) { renderLater = true; return; }
  renderLater = false;
  const vs = views();
  const waitOf = (tabIds) => (src('tabs').items || []).filter((i) => tabIds.includes(i.tab)).reduce((s, i) => s + nWait(i) + nAct(i), 0);
  for (const v of vs) {
    if (v.id.startsWith('grp:')) v.wait = waitOf((src('tabs').tabs || []).filter((t) => 'grp:' + t.group === v.id).map((t) => t.id));
    else if (v.id.startsWith('tab:')) v.wait = waitOf([v.id.slice(4)]);
    else if (v.id === 'inbox') v.wait = waitOf((src('tabs').tabs || []).map((t) => t.id));
  }
  if (view && view.startsWith('tab:') && !vs.some((v) => v.id === view)) {   // старая ссылка #tab:X — вкладка теперь в группе
    const t = (src('tabs').tabs || []).find((x) => 'tab:' + x.id === view);
    if (t && t.group) view = 'grp:' + t.group;
  }
  if (!vs.some((v) => v.id === view)) view = vs.find((v) => v.id !== 'inbox')?.id;
  if (view && decodeURIComponent(location.hash.slice(1)) !== view) history.replaceState(null, '', '#' + view);
  if (urlSel.length && src('tabs').tabs) {
    const t = (src('tabs').tabs || []).find((x) => x.id === urlSel[0].split('|')[0]);
    if (t) view = t.group ? 'grp:' + t.group : 'tab:' + t.id;
    sel[view] = urlSel.shift();
  }
  patch($('#nav'), vs.map((v) => ({
    key: v.id,
    cls: v.card ? 'cardwrap' : '',   // × карточки — своя кнопка рядом, не внутри кнопки вкладки
    html: `<button class="${v.id === view ? 'on' : ''}${v.card ? ' cardtab' : ''}" data-view="${esc(v.id)}"${v.card ? ` title="${esc(cardTip(v.card))}"` : ''}${v.id === view ? ' aria-current="page"' : ''}>${esc(v.title)}${v.wait ? (v.id === 'inbox' ? ` <b class="wbadge" title="Вопросы ждут вашего ответа">${v.wait}</b>` : ' <b class="wdot" title="Здесь есть вопросы к вам">•</b>') : ''}</button>${v.card ? `<button class="x" data-act="close" data-ref="${esc(v.card)}" title="Закрыть карточку" aria-label="Закрыть карточку ${esc(v.title)}">×</button>` : ''}`,
  })));
  const v = vs.find((x) => x.id === view);
  const roots = (src('tabs').tabs || []).map((t) => t.root);   // ошибки корней показывает раздел вкладки
  const errs = (src('tabs').errors || []).filter((e) => !roots.some((r) => e.includes(r))).map((e, k) => ({ key: 'e' + k, cls: 'err', html: esc(e) }));
  const out = v ? v.render() : [];
  const { rows, side } = Array.isArray(out) ? { rows: out, side: null } : out;   // вид со списком и предпросмотром
  $('#main').classList.toggle('split', !!side);
  patch($('#list'), [...errs, ...rows]);
  patch($('#side'), side || []);
  document.querySelectorAll('textarea.draft').forEach(fitHeight);   // перерисованное поле — снова по тексту, не в одну строку
  renderLimits();
  tickAgo();
}

// --- события ---

document.addEventListener('click', async (e) => {
  const d = e.target.closest('a[data-doc]');
  if (d && !(e.ctrlKey || e.metaKey || e.shiftKey || e.button)) {
    e.preventDefault(); e.stopPropagation();
    if (document.body.classList.contains('docpage')) location.search = '?doc=' + encodeURIComponent(d.dataset.doc);
    else openDoc(d.dataset.doc);
    return;
  }
  if (e.target.closest('a.flink')) return;   // путь внутри `кода`: открыть, а не скопировать
  if (e.target.closest('#vclose')) { closeDoc(); return; }
  const t = e.target.closest('[data-view],[data-act],[data-copy]');
  if (!t) return;
  if (t.dataset.view) { go(t.dataset.view); return; }
  if (t.dataset.copy) { await navigator.clipboard.writeText(t.dataset.copy); toast('Скопировано'); return; }
  const a = t.dataset.act;
  if (a === 'fold') {
    e.stopPropagation();
    fold.has(t.dataset.id) ? fold.delete(t.dataset.id) : fold.add(t.dataset.id);
    store.set('fold', [...fold]); render();
  } else if (a === 'submit') {
    t.disabled = true;
    for (const el of document.querySelectorAll('[data-act="draft"]')) if (draftTimers[el.dataset.key]) await saveDraft(el);
    const r = await post('tabs', 'submit', { dir: t.dataset.dir });
    for (const k of Object.keys(drafts)) if (k.startsWith(t.dataset.dir + '|')) delete drafts[k];
    if (r.ok) toast(`отправлено: ${r.sent}`);
  } else if (a === 'qopt') {
    // вариант ответа: повторный щелчок снимает; старую галочку «проще» заменяет вариант
    const v = t.getAttribute('aria-checked') === 'true' ? '' : t.dataset.v;
    drafts[t.dataset.key + '|o'] = v || null; delete drafts[t.dataset.key + '|r'];
    // отметка — сразу на месте: пока фокус в поле ответа, render отложен до ухода из него
    t.closest('.qopts')?.querySelectorAll('.qopt').forEach((b) => { const on = b === t && !!v; b.classList.toggle('on', on); b.setAttribute('aria-checked', on); });
    const ta = document.querySelector(`textarea[data-key="${CSS.escape(t.dataset.key)}"]`);
    t.closest('li.q')?.classList.toggle('drafted', !!v || !!ta?.value.trim());
    refreshSubmit(t.dataset.dir);   // кнопка «Отправить Claude» — сразу, не дожидаясь ухода из поля
    await post('tabs', 'draft', { dir: t.dataset.dir, index: +t.dataset.index, opt: v, rephrase: null });
    render();
    ta?.focus();
  } else if (a === 'stage') {
    sfil[t.dataset.ref] = sfil[t.dataset.ref] === t.dataset.v ? '' : t.dataset.v; store.set('sfil', sfil);
    if (sfil[t.dataset.ref]) { ctab[t.dataset.ref] = 'plan'; store.set('ctab', ctab); }
    render();
  } else if (a === 'ctab') {
    ctab[t.dataset.ref] = t.dataset.v; store.set('ctab', ctab); render();
  } else if (a === 'reqnew') {
    e.stopPropagation();
    reqOpen.has(t.dataset.ref) ? reqOpen.delete(t.dataset.ref) : reqOpen.add(t.dataset.ref); render();
    document.querySelector(`textarea[data-act="reqtext"][data-ref="${CSS.escape(t.dataset.ref)}"]`)?.focus();
  } else if (a === 'reqsave') {
    e.stopPropagation(); await saveReq(t.dataset.ref, t.dataset.dir);
  } else if (a === 'kwset') {
    kwOpen.has(t.dataset.sid) ? kwOpen.delete(t.dataset.sid) : kwOpen.add(t.dataset.sid); render();
  } else if (a === 'health') {
    e.stopPropagation(); healthPop(t);
  } else if (a === 'answer') {
    ctab[t.dataset.ref] = 'q'; store.set('ctab', ctab); openCard(t.dataset.ref);
    document.querySelector(`.task.card textarea[data-index="${t.dataset.k}"]`)?.focus();
  } else if (a === 'sub') {
    sub[view] = t.dataset.v; store.set('sub', sub); render();
  } else if (a === 'submitall') {
    t.disabled = true;
    for (const el of document.querySelectorAll('[data-act="draft"]')) if (draftTimers[el.dataset.key]) await saveDraft(el);
    let sent = 0;
    for (const i of items().filter(nDraftOf)) {
      const r = await post('tabs', 'submit', { dir: i.dir });
      for (const k of Object.keys(drafts)) if (k.startsWith(i.dir + '|')) delete drafts[k];
      sent += r.sent || 0;
    }
    toast(`отправлено: ${sent}`);
  } else if (a === 'bsay') {
    const mode = t.dataset.mode;
    let args;
    if (mode === 'new') {
      const text = $('#btext').value.trim();
      if (!text) { toast('Пустое объявление'); return; }
      boardForm.hours = $('#bhours').value; store.set('boardForm', boardForm);
      args = { mode, board: $('.bf select').value, text, key: $('#bkey').value.trim(), hours: $('#bhours').value };
    } else {
      // правка и снятие — новой строкой поверх; прежняя остаётся в истории доски
      const old = (t.dataset.text || '').replace(/^\[(изменено|снято|закрыто): [^\]]*\]\s*/, '');
      const text = mode === 'edit' ? prompt('Новый текст объявления (прежний останется в истории):', old)
        : mode === 'close' ? (confirm(`Закрыть тег «${t.dataset.tag}» — снять все его объявления?`) ? '' : null)
          : (confirm('Снять объявление? Прежний текст останется в истории.') ? old : null);
      if (text === null || (mode === 'edit' && !text.trim())) return;
      args = { mode, board: t.dataset.board, tag: t.dataset.tag, key: t.dataset.key, text, hours: boardForm.hours };
    }
    t.disabled = true;
    const r = await post('board', 'say', args);
    t.disabled = false;
    if (r.ok) { if (mode === 'new') $('#btext').value = ''; toast({ new: 'объявлено', edit: 'изменено', delete: 'снято', close: 'тег закрыт' }[mode]); }
  } else if (a === 'show') {
    e.stopPropagation();
    shown.has(t.dataset.key) ? shown.delete(t.dataset.key) : shown.add(t.dataset.key);
    store.set('shown', [...shown]); render();
  } else if (a === 'pf') {
    e.stopPropagation();
    pfilter.has(t.dataset.k) ? pfilter.delete(t.dataset.k) : pfilter.add(t.dataset.k);
    store.set('pfilter', [...pfilter]); render();
  } else if (a === 'info') {
    toast(t.dataset.text, 6000);
  } else if (a === 'refresh') {
    await post(t.dataset.source, 'refresh', {}); toast('Обновляю…');
  } else if (a === 'release') {
    await post('claims', 'release', { task: t.dataset.task });
  } else if (a === 'newtask') {
    if (nt) { nt = null; render(); } else ntOpen((src('tabs').tabs || []).filter((t) => (renderedGroup || []).includes(t.id)), (sub[view] || ''));
  } else if (a === 'nttab') {
    nt.tab = t.dataset.v;
    const pi = $('#ntproj');   // проект, подставленный сам, — вслед за каталогом; вписанный вручную не трогаем
    if (pi.value === nt.autoProj) pi.value = nt.autoProj = nt.projOf(nt.tab);
    document.querySelectorAll('#nttabs button').forEach((b) => b.setAttribute('aria-pressed', b === t));
  } else if (a === 'ntkind') {
    nt.kind = t.dataset.v;
    document.querySelectorAll('#ntkinds button').forEach((b) => b.setAttribute('aria-pressed', b === t));
    $('#ntplanl').hidden = $('#ntaskl').hidden = isIdea();
  } else if (a === 'ntpick') {
    $('#ntinp').click();
  } else if (a === 'ntdel') {
    const f = nt.files; nt.files = []; f.splice(+t.dataset.k, 1); ntFiles(f);
  } else if (a === 'ntsave') {
    await ntSave();
  } else if (a === 'ntcancel') {
    nt = null; render();
  } else if (a === 'kwrule') {
    const rs = kwRulesFromDom(), n = +t.dataset.r, op = t.dataset.op;
    if (op === 'add') rs.push({ name: '', days: [], from: '00:00', to: '23:59', mode: 'until', pings: 6, kinds: ['task'] });
    else if (op === 'del') { if (!confirm(`Удалить настройку ${n + 1}?`)) return; rs.splice(n, 1); }
    else { const m = op === 'up' ? n - 1 : n + 1; [rs[n], rs[m]] = [rs[m], rs[n]]; }
    await post('keepwarm', 'graph', { rules: rs });
  } else if (a === 'free') {
    await post('claims', 'free', { res: t.dataset.res });
  } else if (a === 'select') {
    sel[view] = t.dataset.tab + '|' + t.dataset.id; store.set('sel', sel);
    if (narrow) openCard(sel[view]); else render();   // на узком предпросмотра нет — сразу карточка
  } else if (a === 'card') {
    e.stopPropagation();
    openCard(t.dataset.ref);
  } else if (a === 'close') {
    e.stopPropagation();
    cards = cards.filter((r) => r !== t.dataset.ref); store.set('cards', cards);
    if (view === 'card:' + t.dataset.ref) view = null;
    render();
  } else if (a === 'plan') {
    // меню статусов вместо перебора по щелчку: видно, что выбирается, случайный щелчок ничего не меняет
    const all = (src('tabs').plan_statuses || ['todo', 'doing', 'done']).filter((x) => x !== 'wait');
    menu(t, all.map((x) => `<button data-st="${esc(x)}"${x === t.dataset.status ? ' class="cur"' : ''}>${esc(label(x))}</button>`).join(''), async (b) => {
      if (b.dataset.st && b.dataset.st !== t.dataset.status) await post('tabs', 'plan', { dir: t.dataset.dir, index: +t.dataset.index, status: b.dataset.st });
      return true;
    });
  }
});
// черновик ответа: сохраняется сам через секунду после последней правки и при уходе из поля
document.addEventListener('input', (e) => {
  const el = e.target;
  if (el.dataset.act === 'reqtext') { reqDraft[el.dataset.ref] = el.value; return; }
  if (el.dataset.act !== 'draft') return;
  drafts[el.dataset.key] = el.value;
  const li = el.closest('li.q');
  if (li) li.classList.toggle('drafted', !!el.value.trim() || !!li.querySelector('.qopt.on'));
  if (el.dataset.dir) refreshSubmit(el.dataset.dir);
  if (el.tagName === 'TEXTAREA') fitHeight(el);
  clearTimeout(draftTimers[el.dataset.key]);
  draftTimers[el.dataset.key] = setTimeout(() => saveDraft(el), 1000);
});
// вариант ответа не забирает фокус у поля: иначе уход фокуса перерисует вопрос между нажатием и
// отпусканием, кнопка под мышью сменится — и повторный щелчок (снять вариант) потеряется
document.addEventListener('pointerdown', (e) => { if (e.button === 0 && e.target.closest('.qopt')) e.preventDefault(); });
document.addEventListener('focusout', async (e) => {
  const el = e.target;
  if (el.dataset?.act === 'draft' && draftTimers[el.dataset.key]) await saveDraft(el);
  setTimeout(() => { if (renderLater) render(); }, 0);   // фокус мог уйти в соседний черновик — render сам проверит
});
document.addEventListener('change', async (e) => {
  const t = e.target;
  if (t.closest('[data-kwr]')) {   // правило расширенного графика — все правила разом, порядок из окна
    if (t.dataset.f === 'mode') t.closest('.kwrule').querySelector('[data-f="pings"]').parentElement.hidden = t.value !== 'pings';
    try { await post('keepwarm', 'graph', { rules: kwRulesFromDom() }); toast('Сохранено'); } catch (err) { toast('Не сохранилось: ' + err.message); }
    return;
  }
  if (t.closest('[data-kws],[data-kwg]')) { try { await kwSave(t); toast('Сохранено'); } catch (err) { toast('Не сохранилось: ' + err.message); } return; }
  if (t.dataset.act === 'reph') {
    const rk = t.dataset.key + '|r';
    drafts[rk] = t.checked ? '' : null;
    clearTimeout(draftTimers[rk]); delete draftTimers[rk];
    await post('tabs', 'draft', { dir: t.dataset.dir, index: +t.dataset.index, rephrase: drafts[rk] });
    render();
    if (t.checked) document.querySelector(`[data-key="${CSS.escape(rk)}"]`)?.focus();
    return;
  }
  if (t.dataset.act === 'bsel') { boardForm.board = t.value; store.set('boardForm', boardForm); return; }
  if (t.dataset.act === 'keepwarm') await post('keepwarm', 'set', { sid: t.dataset.sid, on: t.checked });
});

async function pull() {
  const r = await fetch('/state.json');
  const s = await r.json();
  if (s.rev !== state.rev) { const first = !state.ui; state = s; if (first) applyPrefs(); render(); }
}

function go(v) { view = v; store.set('view', view); if (decodeURIComponent(location.hash.slice(1)) !== v) history.pushState(null, '', '#' + v); render(); scrollTo(0, 0); }
// имя вкладки карточки: своё короткое (поле short — задаёт владелец), иначе начало названия, иначе id
function tabName(ref) {
  const i = findItem(ref), id = ref.split('|')[1];
  if (!i) return id;
  if (i.short) return i.short;
  const t = String(i.title || id);
  return t.length > 24 ? t.slice(0, 23).trimEnd() + '…' : t;
}
const cardTip = (ref) => { const i = findItem(ref); return (i ? i.title + ' · ' : '') + ref.split('|')[1] + '\nДвойной щелчок — переименовать вкладку'; };
// переименование — поле прямо во вкладке: Enter сохранить, Esc отменить, пусто — вернуть название задачи
function renameTab(ref) {
  const btn = document.querySelector(`nav .cardtab[data-view="${CSS.escape('card:' + ref)}"]`), i = findItem(ref);
  if (!btn || !i) return;
  const inp = document.createElement('input');
  inp.className = 'tabname'; inp.value = i.short || ''; inp.placeholder = String(i.title || '').slice(0, 40);
  inp.title = 'Имя вкладки: Enter — сохранить, Esc — отменить, пусто — название задачи';
  inp.style.width = Math.max(8, (inp.value || inp.placeholder).length + 1) + 'ch';
  btn.replaceChildren(inp); inp.focus(); inp.select();
  let done = false;
  const finish = async (save) => {
    if (done) return; done = true;
    if (save && inp.value.trim() !== (i.short || '')) await post('tabs', 'field', { dir: i.dir, field: 'short', value: inp.value.trim() });
    render();
  };
  inp.addEventListener('keydown', (e) => { e.stopPropagation(); if (e.key === 'Enter') finish(true); else if (e.key === 'Escape') finish(false); });
  inp.addEventListener('click', (e) => e.stopPropagation());
  inp.addEventListener('blur', () => finish(true));
}
document.addEventListener('dblclick', (e) => { const b = e.target.closest('nav .cardtab'); if (b) renameTab(b.dataset.view.slice(5)); });
function openCard(ref) { if (!cards.includes(ref)) { cards.push(ref); store.set('cards', cards); } go('card:' + ref); }
// «Назад» и «Вперёд» браузера: вид — в адресе
addEventListener('popstate', () => {
  const v = decodeURIComponent(location.hash.slice(1));
  if (!v) return;
  if (v.startsWith('card:') && !cards.includes(v.slice(5))) { cards.push(v.slice(5)); store.set('cards', cards); }
  view = v; render();
});

// всплывающее меню у элемента или у точки ({x, y}); pick(кнопка) → true — закрыть.
// Клавиатура: ↑↓ Home End, Enter, Esc — фокус обратно туда, откуда открыли
let menuEl = null, menuBack = null;
function closeMenu(back) { menuEl?.remove(); menuEl = null; if (back && menuBack?.isConnected) menuBack.focus(); menuBack = null; }
function menu(anchor, html, pick) {
  closeMenu();
  menuBack = document.activeElement;
  const m = document.createElement('div'); m.className = 'menu'; m.setAttribute('role', 'menu'); m.innerHTML = html;
  m.querySelectorAll('button').forEach((b) => { b.setAttribute('role', 'menuitem'); b.tabIndex = -1; });
  document.body.append(m); menuEl = m;
  const z = zoom();
  const r = anchor.getBoundingClientRect ? anchor.getBoundingClientRect() : { left: anchor.x, right: anchor.x, top: anchor.y, bottom: anchor.y };
  m.style.left = Math.max(4, Math.min(r.left / z, innerWidth / z - m.offsetWidth - 8)) + 'px';
  m.style.top = Math.max(4, r.bottom / z + 4 + m.offsetHeight > innerHeight / z ? r.top / z - m.offsetHeight - 4 : r.bottom / z + 4) + 'px';
  m.addEventListener('click', async (e) => { const b = e.target.closest('button'); if (b && await pick(b)) closeMenu(true); });
  m.addEventListener('keydown', (e) => {
    const items = [...m.querySelectorAll('button')], k = items.indexOf(document.activeElement);
    const to = { ArrowDown: k + 1, ArrowUp: k - 1, Home: 0, End: items.length - 1 }[e.key];
    if (to !== undefined) { e.preventDefault(); items[(to + items.length) % items.length]?.focus(); }
  });
  (m.querySelector('.cur') || m.querySelector('button'))?.focus();
}
document.addEventListener('pointerdown', (e) => { if (menuEl && !menuEl.contains(e.target) && !e.target.closest('#prefs,[data-act="plan"]')) closeMenu(); });

// --- своё контекстное меню: правая кнопка, клавиша Menu, Shift+F10. Системное — Shift + правая кнопка,
// а также в полях ввода, на выделенном тексте и ссылках ---
const mi = (act, title, extra = '', key = '') => `<button data-m="${act}"${extra}>${title}${key ? `<span class="mk">${key}</span>` : ''}</button>`;
function ctxItems(t) {
  const task = t.closest('[data-ctx="task"],.task[data-ref]');
  const plan = t.closest('[data-ctx="plan"]');
  const doc = t.closest('[data-ctx="doc"],a[data-doc]');
  if (plan) {
    const all = (src('tabs').plan_statuses || []).filter((x) => x !== 'wait');
    const sts = plan.dataset.status ? all.map((x) => mi('st', esc(label(x)), ` data-v="${x}"${x === plan.dataset.status ? ' class="cur"' : ''}`)).join('') + '<hr>' : '';
    return { ctx: plan, html: sts + mi('copy', '⧉ Текст пункта', ` data-v="${esc(plan.dataset.text || '')}"`) };
  }
  if (doc) {
    const path = doc.dataset.path || doc.dataset.doc;
    return { ctx: doc, html: mi('doc', 'Открыть здесь', ` data-v="${esc(path)}"`, 'Enter') + mi('tab', 'Открыть во вкладке', ` data-v="${esc(path)}"`) + mi('copy', '⧉ Путь', ` data-v="${esc(path)}"`) };
  }
  if (task) {
    const i = findItem(task.dataset.ref);
    if (!i) return null;
    const pr = [1, 2, 3, 4].map((n) => mi('prio', ['срочно', 'важно', 'обычно', 'потом'][n - 1], ` data-v="${n}"${(i.priority || 3) === n ? ' class="cur"' : ''}`)).join('');
    return { ctx: task, html: mi('card', 'Открыть карточку', '', 'Enter') + mi('rename', 'Переименовать вкладку…') + (nWait(i) ? mi('answer', `Ответить на вопросы (${nWait(i)})`) : '')
      + (i.resume ? mi('copy', '⧉ Продолжить', ` data-v="${esc(i.resume)}"`) : '') + mi('copy', '⧉ id', ` data-v="${esc(i.id)}"`)
      + mi('copy', '⧉ Каталог', ` data-v="${esc(i.dir)}"`) + '<hr><div class="mh">Приоритет</div>' + pr
      + ((src('claims').list || {})[i.id] ? '<hr>' + mi('release', 'Отпустить захват') : ''), ref: task.dataset.ref, i };
  }
  return { ctx: null, html: mi('zoom', 'Крупнее', ' data-v="0.1"', 'Ctrl +') + mi('zoom', 'Мельче', ' data-v="-0.1"') + mi('zoom', 'Масштаб 100 %', ' data-v="0"')
    + '<hr>' + mi('fold', 'Свернуть всё') + mi('unfold', 'Развернуть всё') };
}
async function ctxPick(b, c) {
  const v = b.dataset.v, m = b.dataset.m;
  if (m === 'copy') { await navigator.clipboard.writeText(v); toast('Скопировано'); }
  else if (m === 'st') await post('tabs', 'plan', { dir: c.ctx.dataset.dir, index: +c.ctx.dataset.index, status: v });
  else if (m === 'doc') openDoc(v);
  else if (m === 'tab') open(viewUrl(v), '_blank', 'noopener');
  else if (m === 'card') openCard(c.ref);
  else if (m === 'rename') { openCard(c.ref); renameTab(c.ref); }
  else if (m === 'answer') { ctab[c.ref] = 'q'; store.set('ctab', ctab); openCard(c.ref); }
  else if (m === 'prio') await post('tabs', 'field', { dir: c.i.dir, field: 'priority', value: v });
  else if (m === 'release') await post('claims', 'release', { task: c.i.id });
  else if (m === 'zoom') setPref('scale', +v ? Math.min(2.5, Math.max(0.7, Math.round((zoom() + +v) * 10) / 10)) : null);
  else if (m === 'fold' || m === 'unfold') {
    for (const el of document.querySelectorAll('[data-act="fold"]')) { const id = el.dataset.id; if (m === 'fold') fold.add(id); else fold.delete(id); }
    store.set('fold', [...fold]); render();
  }
  return true;
}
function openCtx(target, at) {
  const c = ctxItems(target);
  if (!c) return false;
  menu(at, c.html, (b) => ctxPick(b, c));
  return true;
}
document.addEventListener('contextmenu', (e) => {
  const t = e.target;
  if (e.shiftKey || t.closest('input, textarea, select, #viewer') || String(getSelection()).trim()) return;   // системное меню
  if (t.closest('a[href]:not([data-doc]):not([data-ctx])')) return;
  if (openCtx(t, { x: e.clientX, y: e.clientY })) e.preventDefault();
});

// ⚙ — масштаб, ширина, тема, сброс размеров
function prefsHtml() {
  const seg = (k, opts) => `<span class="seg">${opts.map(([v, t]) => `<button data-k="${k}" data-v="${v}" class="${String(pref(k) ?? '') === v ? 'on' : ''}">${t}</button>`).join('')}</span>`;
  return `<div class="prefs"><div><div class="lbl">Масштаб</div><span class="seg"><button data-z="-0.1" aria-label="Меньше">A−</button><button data-z="0" title="Сбросить">${Math.round(zoom() * 100)}%</button><button data-z="0.1" aria-label="Больше">A+</button></span>
      <span class="seg">${[1, 1.25, 1.5, 2].map((v) => `<button data-zset="${v}" class="${zoom() === v ? 'on' : ''}">${v * 100}%</button>`).join('')}</span></div>
    ${prefs.scale === undefined && innerWidth > 2600 && devicePixelRatio === 1 ? `<div class="sess">Окно ${innerWidth} px — крупнее? <button class="btn" data-zset="1.5">150 %</button></div>` : ''}
    <div><div class="lbl">Ширина</div>${seg('width', [['center', 'по центру'], ['full', 'во всю']])}</div>
    <div><div class="lbl">Тема</div>${seg('theme', [['auto', 'авто'], ['light', 'светлая'], ['dark', 'тёмная']])}</div>
    <div><button class="btn" data-reset="1">Размеры по умолчанию</button></div></div>`;
}
$('#prefs').addEventListener('click', () => {
  if (menuEl) { closeMenu(); return; }
  menu($('#prefs'), prefsHtml(), (b) => {
    if (b.dataset.zset) setPref('scale', +b.dataset.zset);
    else if (b.dataset.z !== undefined) setPref('scale', +b.dataset.z ? Math.min(2.5, Math.max(0.7, Math.round((zoom() + +b.dataset.z) * 10) / 10)) : null);
    else if (b.dataset.k) setPref(b.dataset.k, b.dataset.v);
    else if (b.dataset.reset) { for (const k of ['scale', 'split', 'vw', 'width']) delete prefs[k]; document.documentElement.style.removeProperty('--vw'); setPref('max_width', null); }
    else return false;
    menuEl.innerHTML = prefsHtml();
    return false;
  });
});

// перетаскиваемые границы: список | предпросмотр (доля) и ширина просмотра документа (px); помнит браузер
function drag(el, onMove, onEnd) {
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault(); el.setPointerCapture(e.pointerId); el.classList.add('drag');
    const up = () => { el.classList.remove('drag'); el.removeEventListener('pointermove', onMove); el.removeEventListener('pointerup', up); onEnd(); };
    el.addEventListener('pointermove', onMove); el.addEventListener('pointerup', up);
  });
}
const setSplit = (f, save) => {
  prefs.split = Math.min(0.75, Math.max(0.2, Math.round(f * 100) / 100));
  document.documentElement.style.setProperty('--side', Math.round(prefs.split * 100) + '%');
  if (save) store.set('ui', prefs);
};
drag($('#split'), (e) => { const r = $('#main').getBoundingClientRect(); setSplit((r.right - e.clientX) / r.width); }, () => store.set('ui', prefs));
$('#split').addEventListener('dblclick', () => setPref('split', null));
$('#split').addEventListener('keydown', (e) => {
  const d = { ArrowLeft: 0.02, ArrowRight: -0.02 }[e.key];
  if (d) { e.preventDefault(); setSplit((+pref('split') || 0.38) + d, true); }
});
drag($('#vgrip'), (e) => {
  prefs.vw = Math.max(360, Math.round((innerWidth - e.clientX) / zoom()));
  document.documentElement.style.setProperty('--vw', prefs.vw + 'px');
}, () => store.set('ui', prefs));
addEventListener('resize', () => { if (applyPrefs()) render(); });
new ResizeObserver(() => document.documentElement.style.setProperty('--hh', $('header').offsetHeight + 'px')).observe($('header'));

document.addEventListener('keydown', (e) => {
  const rq = e.target.dataset?.act === 'reqtext' && e.target;
  if (rq && e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); rq.parentElement.querySelector('[data-act="reqsave"]').click(); return; }
  if (rq && e.key === 'Escape') { reqOpen.delete(rq.dataset.ref); render(); return; }
  if (nt && e.target.closest?.('#ntf')) {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); ntSave(); return; }
    if (e.key === 'Escape') { nt = null; render(); return; }
  }
  if (e.key === 'Escape') { if (menuEl) closeMenu(true); else if (!$('#viewer').hidden) closeDoc(); return; }
  if ((e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) && !e.target.closest('input, textarea, select')) {
    if (openCtx(e.target, e.target.getBoundingClientRect ? e.target : { x: 40, y: 80 })) e.preventDefault();
    return;
  }
  // не-кнопки с действием (строка задачи, сворачивание, «› сделано», копирование): Enter и пробел — как щелчок
  const t = e.target;
  if ((e.key === 'Enter' || e.key === ' ') && t.matches?.('[tabindex][data-act],[tabindex][data-copy]')
      && !/^(BUTTON|INPUT|TEXTAREA|SELECT)$/.test(t.tagName)) { e.preventDefault(); t.click(); }
});


// отдельная вкладка с одним .md: /?doc=C:\...
const DOC = new URLSearchParams(location.search).get('doc');
if (DOC) {
  document.body.classList.add('docpage');
  document.title = DOC.split(/[\\/]/).pop();
  addEventListener('DOMContentLoaded', () => renderMd(DOC, $('#list'))   // marked и DOMPurify — defer, готовы к этому событию
    .catch((err) => { $('#list').innerHTML = `<p class="err">Не открылось: ${esc(err.message)}</p>`; }));
} else if (!location.search.includes('nosse')) {   // ?nosse — для снимков headless-браузером
  const es = new EventSource('/events');
  let lost = null;   // связь пропала дольше 5 с — полоса со временем данных: на экране не свежее
  es.onopen = () => { $('#live').classList.add('on'); clearTimeout(lost); lost = null; $('#offline').hidden = true; };
  es.onerror = () => {
    $('#live').classList.remove('on');
    if (lost) return;
    lost = setTimeout(() => {
      const at = state.at ? new Date(state.at).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }) : '—';
      $('#offline').textContent = `Нет связи с сервером панели · данные на ${at} · поднять: start.cmd`; $('#offline').hidden = false;
    }, 5000);
  };
  es.onmessage = (m) => { if (+m.data !== state.rev) pull(); };
}
if (!DOC) { applyPrefs(); setInterval(tickAgo, 15000); pull(); addEventListener('DOMContentLoaded', () => { if (state.rev) { for (const n of $('#list').children) n._html = null; render(); } }); }
