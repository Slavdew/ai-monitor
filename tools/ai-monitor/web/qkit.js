// qkit — выбор ответов прямо в документе разбора (HTML в каталоге задачи). В панели на весь документ — один
// вопрос «ответы в файле» (task.py ask T "…" --doc файл); сюда владелец щёлкает варианты, а сводка уходит ответом
// на тот вопрос: «Перенести в панель» (документ открыт в просмотре панели) или «Скопировать» и вставить.
// Разметка: <body data-task="id задачи" data-item="N вопроса в панели">; вопрос — <div data-q="1" data-title="кратко">,
// варианты — <button data-opt="текст">, свой ответ — <textarea data-text>. Подключение: <script src="/qkit.js"></script>.
(() => {
  const qs = [...document.querySelectorAll('[data-q]')];
  if (!qs.length) return;
  const css = document.createElement('style');
  css.textContent = `[data-q] [data-opt]{cursor:pointer}[data-q] [data-opt].qk-on{outline:2px solid #6c9cf0;outline-offset:1px;font-weight:600}
  .qk-bar{position:fixed;right:16px;bottom:16px;z-index:99;display:flex;gap:8px;align-items:center;padding:8px 12px;border-radius:10px;
  background:#21211f;color:#e8e8e3;border:1px solid #6c9cf0;box-shadow:0 6px 18px rgb(0 0 0/.35);font:13px system-ui,"Segoe UI",sans-serif}
  .qk-bar button{font:inherit;border:1px solid #6c9cf0;border-radius:6px;padding:4px 10px;cursor:pointer;background:#6c9cf0;color:#171716;font-weight:600}
  .qk-bar button:disabled{opacity:.5;cursor:default}.qk-bar button.qk-2{background:none;color:#6c9cf0;font-weight:400}.qk-bar .qk-msg{color:#9a9a93}`;
  document.head.appendChild(css);
  const pick = {};   // номер вопроса в файле → вариант
  for (const q of qs) {
    q.addEventListener('click', (e) => {
      const b = e.target.closest('[data-opt]');
      if (!b) return;
      const n = q.dataset.q, same = pick[n] === b.dataset.opt;
      q.querySelectorAll('[data-opt]').forEach((x) => x.classList.toggle('qk-on', !same && x === b));
      if (same) delete pick[n]; else pick[n] = b.dataset.opt;
      update();
    });
    q.querySelector('textarea[data-text]')?.addEventListener('input', update);
  }
  const title = (q) => (q.dataset.title || q.querySelector('h1,h2,h3,h4,p,b')?.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80);
  const answers = () => qs.map((q) => ({ n: q.dataset.q, t: title(q), opt: pick[q.dataset.q] || '', text: (q.querySelector('textarea[data-text]')?.value || '').trim() }))
    .filter((a) => a.opt || a.text);
  const summary = () => answers().map((a) => `${a.n}. ${a.t}: ${[a.opt, a.text].filter(Boolean).join(' — ')}`).join('\n');
  const inPanel = window.parent !== window;
  const bar = document.createElement('div');
  bar.className = 'qk-bar';
  bar.innerHTML = '<span class="qk-n"></span><button class="qk-go">Перенести в панель</button><button class="qk-2 qk-copy">Скопировать</button><span class="qk-msg"></span>';
  document.body.appendChild(bar);
  const msg = (t) => { bar.querySelector('.qk-msg').textContent = t; };
  function update() {
    const n = answers().length;
    bar.querySelector('.qk-n').textContent = `Выбрано: ${n} из ${qs.length}`;
    bar.querySelector('.qk-go').disabled = !n || !inPanel;
    bar.querySelector('.qk-go').title = inPanel ? 'В ответ на вопрос панели черновиком — потом «Зафиксировать ответы»'
      : 'Перенос работает, когда документ открыт в просмотре панели; иначе — «Скопировать» и вставить в ответ';
  }
  bar.querySelector('.qk-go').addEventListener('click', () => {
    window.parent.postMessage({ type: 'ai-monitor/answer', task: document.body.dataset.task || '', item: +document.body.dataset.item || 0, text: summary() }, '*');
    msg('отправлено в панель…');
  });
  bar.querySelector('.qk-copy').addEventListener('click', async () => {
    try { await navigator.clipboard.writeText(summary()); msg('скопировано — вставьте в ответ на вопрос панели'); } catch { prompt('Скопируйте ответы:', summary()); }
  });
  window.addEventListener('message', (e) => { if (e.data?.type === 'ai-monitor/answer-ok') msg(e.data.text); });
  update();
})();
