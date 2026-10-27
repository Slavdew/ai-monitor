// Самопроверка keepwarm-gate.js: node keepwarm-gate.test.js
// Свои настройки (KEEPWARM_CONFIG) и LOCALAPPDATA во временном каталоге — не зависит от времени и
// дня недели запуска.
const assert = require('assert');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawnSync } = require('child_process');

const GATE = path.join(__dirname, 'keepwarm-gate.js');
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'keepwarm-'));
const queue = path.join(tmp, '1c-ib-queue');
const dayStart = path.join(tmp, 'keepwarm', 'day-start');
const cfgFile = path.join(tmp, 'keepwarm.config.json');
fs.mkdirSync(queue);
const env = { ...process.env, LOCALAPPDATA: tmp, KEEPWARM_CONFIG: cfgFile };
// по умолчанию: окно начала дня — весь день, выходных нет
const setCfg = (c = {}) => fs.writeFileSync(cfgFile, JSON.stringify({ workdayHours: 9, startWindow: ['00:00', '23:59'], offDays: [], ...c }));
setCfg();

const pad = (n) => String(n).padStart(2, '0');
const ago = (min) => new Date(Date.now() - min * 60000).toISOString();
const at = (h, m) => new Date().setHours(h, m, 0, 0);   // сегодня в ЧЧ:ММ, мс
const setDay = (day) => fs.writeFileSync(dayStart, JSON.stringify(day));
// срок в формате запроса таймера, min минут от текущего момента (меньше нуля — в прошлом)
const due = (min) => {
  const d = new Date(Date.now() + min * 60000);
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
};
const timerPrompt = (min, tail = 'handoff') => `keepwarm: таймер кэша до ${due(min)}, ${tail}`;
const single = 'keepwarm: таймер кэша, один прогрев, handoff';
const human = (min) => ({ type: 'user', turnOrigin: 'human', timestamp: ago(min), message: { content: [{ type: 'text', text: 'задача' }] } });
const timer = (min) => ({ type: 'user', turnOrigin: 'scheduled', timestamp: ago(min), message: { content: 'keepwarm: таймер' } });
const tool = (min) => ({ type: 'user', timestamp: ago(min), message: { content: [{ type: 'tool_result' }] } });
const answer = (min) => ({ type: 'assistant', timestamp: ago(min) });

function gate(entries, prompt) {
  const file = path.join(tmp, 'session.jsonl');
  fs.writeFileSync(file, 'обрезанная строка"}\n' + entries.map((e) => JSON.stringify(e)).join('\n') + '\n');
  const input = JSON.stringify({ prompt, transcript_path: file, session_id: 's1' });
  return spawnSync(process.execPath, [GATE], { input, encoding: 'utf8', env }).stdout;
}
const run = (...args) => spawnSync(process.execPath, [GATE, ...args], { encoding: 'utf8', env });
const cli = (...args) => run('--prompt', ...args).stdout;
const today = due(0).slice(0, 10);

// чужой запрос не трогает, но отмечает первый ввод за день — повторный не перетирает
assert.strictEqual(gate([human(90), answer(89)], 'обычный ввод'), '');
const first = JSON.parse(fs.readFileSync(dayStart, 'utf8')).start;
assert.ok(Math.abs(first - Date.now()) < 5000);
gate([human(90), answer(89)], 'обычный ввод');
assert.strictEqual(JSON.parse(fs.readFileSync(dayStart, 'utf8')).start, first);
// первый ввод вне окна — весь день внеурочно
setCfg({ startWindow: ['00:00', '00:00'] });
assert.strictEqual(cli(), `keepwarm: таймер кэша, один прогрев на ${today}, handoff\n`);
setCfg();
setDay({ start: at(15, 0) });   // начало 15:00 + 9 ч — полночь, срок всегда впереди

// сессия в работе — гасит и возвращает курсор доски к последнему обработанному запросу
const cursor = path.join(queue, 'ib.cursor.s1');
fs.writeFileSync(cursor, String(Date.now()));
const last = human(20);
assert.match(gate([last, tool(15), answer(10)], timerPrompt(120)), /"decision":"block".*активна/);
assert.strictEqual(Number(fs.readFileSync(cursor, 'utf8')), Date.parse(last.timestamp));
// до срока молчит 55 мин — пинг, и повторно после прошлого пинга
assert.match(gate([human(60), answer(55)], timerPrompt(120)), /Обновление кэша — поддержание до \d\d:\d\d/);
assert.match(gate([human(120), answer(115), timer(60), answer(55)], timerPrompt(120)), /Обновление кэша/);
// срок истёк, молчит — handoff с готовым именем файла; пинги до срока не в счёт
const handoff = gate([human(120), answer(115), timer(60), answer(55)], timerPrompt(-10));
assert.match(handoff, /CronDelete.*скилл handoff.*tasks\\<номер> <тема>.*work\\handoffs.*по окончании поддержания кэша.*около \d\d:\d\d\./s);
assert.doesNotMatch(handoff, /\{[A-Z]+\}/);
assert.match(gate([human(60), answer(55)], `${timerPrompt(-10).replace(', handoff', ' (конец дня), handoff')}`), /по окончании рабочего дня/);
// срок истёк, но сессия в работе — гасит, handoff ждёт, пока сессия замолчит
assert.match(gate([human(20), answer(10)], timerPrompt(-10)), /активна/);
// срок истёк, «без handoff» — только снять таймер
const stop = gate([human(60), answer(55)], timerPrompt(-10, 'без handoff'));
assert.match(stop, /CronDelete.*Поддержание кэша завершено/s);
assert.doesNotMatch(stop, /скилл handoff/);
// handoff после срока уже был, таймер не снят — гасит, повторного нет
assert.match(gate([human(200), answer(195), timer(30), answer(29)], timerPrompt(-60)), /поддержание завершено/);
// погашенные запросы таймера (без ответа) не в счёт
assert.match(gate([human(60), answer(55), timer(30), timer(25)], timerPrompt(-10)), /скилл handoff/);
// новый ввод пользователя обнуляет счёт
assert.match(gate([human(240), timer(180), answer(175), human(60), answer(55)], timerPrompt(-200)), /скилл handoff/);

// «один прогрев»: первый простой — пинг, второй — handoff из-за простоя, дальше гасит
assert.match(gate([human(60), answer(55)], single), /последний прогрев/);
assert.match(gate([human(120), answer(115), timer(60), answer(55)], single), /скилл handoff.*из-за долгого простоя/s);
assert.match(gate([human(180), answer(175), timer(120), answer(115), timer(60), answer(55)], single), /поддержание завершено/);
assert.match(gate([human(120), answer(115), timer(60), answer(55)], single.replace('handoff', 'без handoff')), /Поддержание кэша завершено/);
assert.match(gate([human(20), answer(10)], single), /активна/);
// сводка сжатия записана как ввод пользователя — не обнуляет счёт
assert.match(gate([human(120), answer(115), timer(60), answer(55), { ...human(50), isCompactSummary: true }], single), /скилл handoff/);
// ход длиннее хвоста в 4 МБ — читает дальше и находит прошлый пинг
assert.match(gate([human(120), answer(115), timer(60), { ...tool(59), pad: 'x'.repeat(5 << 20) }, answer(55)], single), /скилл handoff/);

// старый запрос без срока: рабочий день — до его конца, внеурочно — «один прогрев»
assert.match(gate([human(60), answer(55)], 'keepwarm: таймер кэша'), /поддержание до 00:00/);
fs.rmSync(dayStart);
assert.match(gate([human(60), answer(55)], 'keepwarm: таймер кэша'), /последний прогрев/);

// текст запроса таймера
setDay({ start: at(15, 0) });
assert.match(cli('2', 'false'), /^keepwarm: таймер кэша до \d\d\.\d\d\.\d{4} \d\d:\d\d, без handoff\n$/);
assert.match(cli(), /^keepwarm: таймер кэша до .+ \(конец дня\), handoff\n$/);
// выходной — внеурочно, даже если день начался в окне; явные часы — срок
setCfg({ offDays: [0, 1, 2, 3, 4, 5, 6] });
assert.strictEqual(cli(), `keepwarm: таймер кэша, один прогрев на ${today}, handoff\n`);
assert.match(cli('2'), /^keepwarm: таймер кэша до .+, handoff\n$/);
assert.match(gate([human(60), answer(55)], 'keepwarm: таймер кэша'), /последний прогрев/);
// «сегодня работаем до 23:59» — рабочий день даже в выходной; сегодняшние таймеры по умолчанию
// следуют за ним, вчерашний и с явными часами — нет
if (new Date().getHours() < 23) {
  assert.strictEqual(run('--day-end', '23:59').stdout, 'keepwarm: сегодня рабочий день до 23:59\n');
  assert.match(cli(), /^keepwarm: таймер кэша до .+ 23:59 \(конец дня\), handoff\n$/);
  assert.match(gate([human(60), answer(55)], `keepwarm: таймер кэша, один прогрев на ${today}, handoff`), /поддержание до 23:59/);
  assert.match(gate([human(60), answer(55)], timerPrompt(-10).replace(', handoff', ' (конец дня), handoff')), /поддержание до 23:59/);
  assert.match(gate([human(60), answer(55)], 'keepwarm: таймер кэша, один прогрев на 01.01.2000, handoff'), /последний прогрев/);
  assert.match(gate([human(60), answer(55)], timerPrompt(-10)), /скилл handoff/);
}
assert.strictEqual(run('--day-end', '00:00').status, 1);   // время уже прошло
assert.strictEqual(run('--day-end', 'завтра').status, 1);
// заданный конец дня прошёл: новый «один прогрев» греет один раз, проживший срок — сразу handoff
const oneToday = `keepwarm: таймер кэша, один прогрев на ${today}, handoff`;
if (new Date(Date.now() - 100 * 60000).toDateString() === new Date().toDateString()) {
  setDay({ start: Date.now() - 100 * 60000, end: Date.now() - 90 * 60000 });
  assert.strictEqual(cli(), `${oneToday}\n`);
  assert.match(gate([human(80), answer(79)], oneToday), /последний прогрев/);
  assert.match(gate([human(80), answer(79), timer(60), answer(55)], oneToday), /скилл handoff.*по окончании рабочего дня/s);
  assert.match(gate([human(80), answer(79), timer(60), answer(55), timer(30), answer(29)], oneToday), /поддержание завершено/);
  assert.match(gate([human(200), answer(195), timer(150), answer(145), timer(120), answer(115)], oneToday), /скилл handoff/);
}
setCfg();
// ввода сегодня не было — внеурочно
fs.rmSync(dayStart);
assert.strictEqual(cli('false'), `keepwarm: таймер кэша, один прогрев на ${today}, без handoff\n`);
// рабочий день уже кончился — внеурочно
setDay({ start: Date.now() - 10 * 3600000 });
if (new Date(Date.now() - 10 * 3600000).toDateString() === new Date().toDateString()) {
  assert.strictEqual(cli(), `keepwarm: таймер кэша, один прогрев на ${today}, handoff\n`);
}

// настройки сессии из панели (session/<sid>.json) важнее запроса таймера
setDay({ start: at(15, 0) });
const own = path.join(tmp, 'keepwarm', 'session', 's1.json');
fs.mkdirSync(path.dirname(own), { recursive: true });
const setOwn = (o) => fs.writeFileSync(own, JSON.stringify(o));
setOwn({ pings: 2 });   // два пробуждения, третье — передачка, хотя срок впереди
assert.match(gate([human(120), answer(115), timer(60), answer(55)], timerPrompt(120)), /Обновление кэша/);
assert.match(gate([human(180), answer(175), timer(120), answer(115), timer(60), answer(55)], timerPrompt(120)), /скилл handoff.*по окончании поддержания кэша/s);
assert.match(gate([human(240), answer(235), timer(180), answer(175), timer(120), answer(115), timer(60), answer(55)], timerPrompt(120)), /поддержание завершено/);
setOwn({ until: Date.now() - 60000 });   // свой срок прошёл — передачка, хотя таймер до вечера
assert.match(gate([human(60), answer(55)], timerPrompt(120)), /скилл handoff/);
setOwn({ until: Date.now() - 60000, handoff: false });
assert.match(gate([human(60), answer(55)], timerPrompt(120)), /Поддержание кэша завершено/);
setOwn({ until: Date.now() - 60000, handoff: true });   // передачка, хотя таймер «без handoff»
assert.match(gate([human(60), answer(55)], timerPrompt(120, 'без handoff')), /скилл handoff/);
fs.rmSync(own);
// личный график поверх общего: автозапуск выключен — auto не ставит таймер, просьба ставит
const local = path.join(tmp, 'keepwarm', 'config.json');
fs.writeFileSync(local, JSON.stringify({ autoStart: false }));
assert.strictEqual(run('--prompt', 'auto').status, 2);
assert.match(cli(), /^keepwarm: таймер кэша до /);
fs.rmSync(local);
assert.match(cli('auto'), /^keepwarm: таймер кэша до /);

fs.rmSync(tmp, { recursive: true, force: true });
console.log('keepwarm-gate: ok');
