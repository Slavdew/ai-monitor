// Проверка панели глазами: безголовый Edge/Chrome по CDP выполняет шаги и делает снимки.
// Для агента: одна команда вместо своего скрипта; вывод — по строке на шаг (минимум токенов).
//
//   node ui.mjs [--url http://127.0.0.1:8770/] [--out каталог] [--size 1400x1000] шаг ...
//
// Шаги (выполняются по порядку, после каждого — пауза на перерисовку):
//   view:<имя>          вкладка панели (Задачи, Сессии, Доска… — как на кнопке или data-view)
//   card:<id задачи>    открыть карточку задачи
//   click:<css>         щёлкнуть первый подходящий элемент
//   type:<css>=<текст>  ввести текст в поле (с событием input — черновики сохраняются как от человека)
//   scroll:<css>        прокрутить к элементу
//   text:<css>          напечатать текст элементов (проверка без картинки — дешевле снимка)
//   count:<css>         число элементов
//   eval:<js>           выполнить выражение, напечатать результат
//   wait:<мс>           подождать
//   shot:<имя>          снимок экрана в <out>/<имя>.png (путь печатается)
//
// Действия настоящие: щелчок по «Зафиксировать ответы» запишет ответы. Данные без браузера —
// `task.py api <источник> <действие> ...`.
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const argv = process.argv.slice(2);
const opt = (k, d) => { const i = argv.indexOf(k); return i < 0 ? d : argv.splice(i, 2)[1]; };
const url = opt('--url', 'http://127.0.0.1:8770/');
const out = opt('--out', path.join(os.tmpdir(), 'task-monitor-ui'));
const [w, h] = opt('--size', '1400x1000').split('x');
const steps = argv;
if (!steps.length) { console.log(fs.readFileSync(new URL(import.meta.url), 'utf8').split('\nimport')[0]); process.exit(0); }
fs.mkdirSync(out, { recursive: true });

const browser = ['C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', 'C:/Program Files/Microsoft/Edge/Application/msedge.exe',
  'C:/Program Files/Google/Chrome/Application/chrome.exe'].find((p) => fs.existsSync(p));
if (!browser) { console.log('ошибка: не найден Edge или Chrome'); process.exit(1); }
const port = 9300 + Math.floor(Math.random() * 500);
const profile = path.join(out, 'profile-' + port);   // свой на запуск: недобитый прошлый браузер держит профиль
const proc = spawn(browser, ['--headless=new', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`,
  `--window-size=${w},${h}`, '--no-first-run', 'about:blank'], { stdio: 'ignore' });
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let code = 0;
try {
  let list;
  for (let i = 0; i < 40 && !list; i++) { await sleep(250); try { list = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); } catch {} }
  if (!list) throw new Error('браузер не ответил');
  const ws = new WebSocket(list.find((t) => t.type === 'page').webSocketDebuggerUrl);
  await new Promise((r, j) => { ws.addEventListener('open', r); ws.addEventListener('error', j); });
  let id = 0; const wait = {};
  ws.addEventListener('message', (e) => { const m = JSON.parse(e.data); if (wait[m.id]) { wait[m.id](m); delete wait[m.id]; } });
  const cdp = (method, params = {}) => new Promise((r) => { wait[++id] = r; ws.send(JSON.stringify({ id, method, params })); });
  const js = async (expr) => {
    const m = await cdp('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
    if (m.result?.exceptionDetails) throw new Error(m.result.exceptionDetails.exception?.description?.split('\n')[0] || 'ошибка JS');
    return m.result?.result?.value;
  };
  const q = (css) => JSON.stringify(css);
  const need = (css) => `(() => { const e = document.querySelector(${q(css)}); if (!e) throw new Error('нет элемента: ' + ${q(css)}); return e; })()`;

  await cdp('Page.navigate', { url });
  for (let i = 0; i < 40 && !(await js(`!!document.querySelector('[data-view]')`)); i++) await sleep(250);

  for (const s of steps) {
    const k = s.indexOf(':'); const cmd = s.slice(0, k); const arg = s.slice(k + 1);
    let res;
    if (cmd === 'view') res = await js(`(() => { const b = [...document.querySelectorAll('[data-view]')].find(x => x.dataset.view === ${q(arg)} || x.textContent.trim().startsWith(${q(arg)})); if (!b) throw new Error('нет вкладки'); b.click(); return b.dataset.view; })()`);
    else if (cmd === 'card') res = await js(`(() => { const b = [...document.querySelectorAll('[data-act=card]')].find(x => x.dataset.ref.split('|')[1] === ${q(arg)}); if (!b) throw new Error('нет задачи'); b.click(); return 'ok'; })()`);
    else if (cmd === 'click') res = await js(`(${need(arg)}.click(), 'ok')`);
    else if (cmd === 'type') { const [css, ...t] = arg.split('='); res = await js(`(() => { const e = ${need(css)}; e.focus(); e.value = ${q(t.join('='))}; e.dispatchEvent(new Event('input', { bubbles: true })); e.blur(); return 'ok'; })()`); }
    else if (cmd === 'scroll') res = await js(`(${need(arg)}.scrollIntoView({ block: 'start' }), 'ok')`);
    else if (cmd === 'text') res = await js(`[...document.querySelectorAll(${q(arg)})].map(e => e.innerText.trim().replace(/\\s+/g, ' ')).join(' ¦ ')`);
    else if (cmd === 'count') res = await js(`document.querySelectorAll(${q(arg)}).length`);
    else if (cmd === 'eval') res = await js(arg);
    else if (cmd === 'wait') { await sleep(+arg); res = 'ok'; }
    else if (cmd === 'shot') {
      const file = path.join(out, (arg || 'shot') + '.png');
      fs.writeFileSync(file, Buffer.from((await cdp('Page.captureScreenshot')).result.data, 'base64'));
      res = file;
    } else throw new Error(`неизвестный шаг: ${s}`);
    console.log(`${s.length > 60 ? s.slice(0, 57) + '…' : s} → ${typeof res === 'string' ? res : JSON.stringify(res)}`);
    if (!['text', 'count', 'eval', 'wait', 'shot'].includes(cmd)) await sleep(700);
  }
  ws.close();
  // штатно закрыть браузер: PID запущенного процесса — лишь пусковой, главный Edge живёт отдельно
  const ver = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  const bw = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise((r) => bw.addEventListener('open', r));
  bw.send(JSON.stringify({ id: 1, method: 'Browser.close' }));
  await sleep(500);
} catch (e) {
  console.log('ошибка: ' + e.message);
  code = 1;
} finally {
  // добить всё, что запущено с нашим профилем (после ошибки Browser.close не доходит)
  if (process.platform === 'win32') {
    spawnSync('powershell', ['-NoProfile', '-Command', `Get-CimInstance Win32_Process -Filter "name like '%.exe'" | ` +
      `Where-Object { $_.CommandLine -like '*${profile.replace(/'/g, "''")}*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }`], { stdio: 'ignore' });
  } else proc.kill();
  await sleep(300);
  // профили убираем по возможности: Windows отпускает файлы с задержкой — хвосты подчистит следующий запуск
  for (const d of fs.readdirSync(out)) if (d.startsWith('profile')) try { fs.rmSync(path.join(out, d), { recursive: true, force: true }); } catch {}
}
process.exit(code);
