#!/usr/bin/env node
// UserPromptSubmit: решает, что делать с запросом таймера keepwarm (префикс "keepwarm:").
// Сессия отвечала меньше WAKE_MIN назад — гасит запрос, к модели он не уходит. Сессия молчит:
// до срока из запроса таймера — пинг; после срока — handoff (или только снятие таймера, если в
// запросе «без handoff»), один раз до нового ввода пользователя. Внеурочно срока нет
// («один прогрев»): первый простой — пинг, второй — handoff. Считает по хвосту журнала
// сессии: ввод пользователя — turnOrigin "human", запрос таймера — "scheduled". Остальные
// запросы не трогает, только отмечает начало рабочего дня — первый ввод за день.
//
// Текст запроса таймера: node keepwarm-gate.js --prompt [часы] [true|false] — по умолчанию до
// конца рабочего дня (начало + workdayHours) с handoff; внеурочно (первый ввод вне окна
// startWindow, выходной из offDays, день кончился) — «один прогрев». «Сегодня работаем до …» —
// node keepwarm-gate.js --day-end ЧЧ:ММ. Настройки — keepwarm.config.json рядом или файл из
// KEEPWARM_CONFIG.
// Отладка: метка "#debug" в запросе таймера — порог 1 мин, handoff в DEBUG_DIR.
// Схема и оценка — <AI_ROOT>\ideas\Поддержание кэша сессий (keepwarm).md.
const fs = require('fs');
const path = require('path');

const WAKE_MIN = 25;        // таймер раз в 30 мин: будит на 25–55 мин молчания, кэш живёт 60
const TAIL = 4 << 20;       // хвост журнала, байт; ход длиннее — читает больше
const PREFIX = 'keepwarm:';
const STATE_DIR = path.join(process.env.LOCALAPPDATA || '', 'keepwarm');
const DAY_START = path.join(STATE_DIR, 'day-start');   // сегодняшний день: начало, конец от пользователя
const CONFIG = process.env.KEEPWARM_CONFIG || path.join(__dirname, 'keepwarm.config.json');
// имя файла handoff задаёт хук: скиллу не нужен отдельный вызов за временем
const TASK_DIR = 'файл "<AI_ROOT>\\tasks\\<номер> <тема>\\{FILE}"';
const DEBUG_DIR = 'файл "<AI_ROOT>\\tools\\keepwarm\\debug\\{FILE}" (отладка)';
const UNTIMER = 'сними таймер keepwarm этой сессии: CronDelete по ID из ответа CronCreate (ID не ' +
  'найти — CronList).';

const PING = 'keepwarm-gate: пинг поддержания кэша. Ответь ровно одной строкой ' +
  '«🔄 Обновление кэша — {TAIL}», инструменты не вызывай.\n';
const HANDOFF = 'keepwarm-gate: поддержание кэша закончено, пользователь молчит {N} мин. ' +
  `Сначала ${UNTIMER} Если работа по задаче в этой сессии не завершена — подготовь handoff на ` +
  'следующую сессию: скилл handoff, аргумент — {DIR} (путь в кавычках) и фокус — чем занималась ' +
  'сессия, одной фразой; задачи нет — файл "<AI_ROOT>\\work\\handoffs\\{FILE}". Вопросов не ' +
  'задавай. Документ и итоговое сообщение — по правилу C:\\Users\\v.pletnev\\.claude\\rules\\' +
  'handoff-message.md: причина — «{REASON}», кэш остынет около {COLD}. Работа завершена — handoff ' +
  'не нужен, ответь одной строкой.\n';
const STOP = `keepwarm-gate: поддержание кэша закончено. ${UNTIMER} Handoff не нужен. Ответь ` +
  'ровно одной строкой «⏹ Поддержание кэша завершено».\n';

const pad = (n) => String(n).padStart(2, '0');
const hhmm = (ms) => { const d = new Date(ms); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const stamp = (ms) => { const d = new Date(ms); return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()} ${hhmm(ms)}`; };
const fileStamp = (ms) => {
  const d = new Date(ms);
  return `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
};

function config() {
  try {
    return JSON.parse(fs.readFileSync(CONFIG, 'utf8'));
  } catch (err) {
    return {};
  }
}

const minutes = (hm) => { const [h, m] = String(hm).split(':').map(Number); return h * 60 + (m || 0); };

function inStartWindow(ms, cfg) {
  const [from, to] = (cfg.startWindow || ['07:00', '11:00']).map(minutes);
  const d = new Date(ms);
  const x = d.getHours() * 60 + d.getMinutes();
  return x >= from && x <= to;
}

// Сегодняшний день: { start — первый ввод, end — конец, заданный пользователем }; вчерашний — {}.
function readDay(now) {
  try {
    const day = JSON.parse(fs.readFileSync(DAY_START, 'utf8'));
    return new Date(day.start || day.end).toDateString() === new Date(now).toDateString() ? day : {};
  } catch (err) {
    return {};
  }
}

function writeDay(day) {
  fs.mkdirSync(STATE_DIR, { recursive: true });
  fs.writeFileSync(DAY_START, JSON.stringify(day));
}

function noteDayStart(now) {
  const day = readDay(now);
  if (!day.start) writeDay({ ...day, start: now });
}

// Рабочий день — от первого ввода за день, длится workdayHours: сдвиг графика сдвигает и конец.
// Первый ввод вне окна startWindow или выходной (offDays, 0 — воскресенье) — весь день внеурочно.
// Конец, заданный пользователем (--day-end), важнее всего.
function workday(now) {
  const day = readDay(now);
  if (day.end) return { off: false, end: day.end, set: true };
  const cfg = config();
  return {
    off: !day.start || !inStartWindow(day.start, cfg) || (cfg.offDays || [6, 0]).includes(new Date(now).getDay()),
    end: (day.start || 0) + (Number(cfg.workdayHours) || 9) * 3600000,
  };
}

// Как поддерживать: до срока или «один прогрев». Сегодняшний таймер по умолчанию («конец дня»,
// «один прогрев») следует за концом дня, заданным пользователем; таймер с явными часами — нет.
// «Один прогрев» после этого конца — снова один прогрев (warmOnce). В старых запросах срока нет —
// по рабочему дню.
function planOf(prompt, now) {
  const day = workday(now);
  const byDefault = prompt.includes('(конец дня)') || prompt.includes('один прогрев');
  if (byDefault && day.set && prompt.includes(stamp(now).slice(0, 10))) {
    return { deadline: day.end, endOfDay: true, warmOnce: prompt.includes('один прогрев') };
  }
  const m = /до (\d\d)\.(\d\d)\.(\d{4}) (\d\d):(\d\d)/.exec(prompt);
  if (m) {
    return { deadline: new Date(+m[3], m[2] - 1, +m[1], +m[4], +m[5]).getTime(), endOfDay: prompt.includes('(конец дня)') };
  }
  if (prompt.includes('один прогрев') || day.off) return { single: true };
  return { deadline: day.end, endOfDay: true };
}

// --prompt [часы] [true|false]: текст запроса для CronCreate. Без часов внеурочно — «один прогрев».
function timerPrompt(args, now) {
  const hours = args.find((a) => /^\d+([.,]\d+)?$/.test(a));
  const mode = args.includes('false') ? 'без handoff' : 'handoff';
  const day = workday(now);
  if (!hours && (day.off || day.end <= now)) {
    process.stdout.write(`keepwarm: таймер кэша, один прогрев на ${stamp(now).slice(0, 10)}, ${mode}\n`);
    return;
  }
  const end = hours ? now + Number(hours.replace(',', '.')) * 3600000 : day.end;
  process.stdout.write(`keepwarm: таймер кэша до ${stamp(end)}${hours ? '' : ' (конец дня)'}, ${mode}\n`);
}

// --day-end ЧЧ:ММ: «сегодня работаем до …» — день рабочий до этого времени, даже внеурочный.
function setDayEnd(hm, now) {
  const end = new Date(now).setHours(Math.floor(minutes(hm) / 60), minutes(hm) % 60, 0, 0);
  if (!/^\d\d?:\d\d$/.test(hm || '') || end <= now) {
    process.stderr.write(`keepwarm: нужно время позже текущего, ЧЧ:ММ — получено «${hm || ''}»\n`);
    process.exitCode = 1;
    return;
  }
  const day = readDay(now);
  writeDay({ start: day.start || now, end });
  process.stdout.write(`keepwarm: сегодня рабочий день до ${hhmm(end)}\n`);
}

function promptText(e) {
  const c = e.message && e.message.content;
  if (typeof c === 'string') return c;
  return Array.isArray(c) && c[0] && c[0].type === 'text' ? c[0].text : '';
}

// Время последнего ответа модели, время последнего запроса, на который модель ответила, время
// последнего пропущенного к модели запроса таймера после последнего ввода пользователя и их число.
// Читает хвост журнала; ввода пользователя в хвосте нет (ход модели длиннее) — вчетверо больше,
// до всего файла.
function scan(file) {
  const fd = fs.openSync(file, 'r');
  try {
    const size = fs.fstatSync(fd).size;
    for (let len = Math.min(size, TAIL); ; len = Math.min(size, len * 4)) {
      const buf = Buffer.alloc(len);
      fs.readSync(fd, buf, 0, len, size - len);
      const r = scanText(buf.toString('utf8'));
      if (r.human || len === size) return r;
    }
  } finally {
    fs.closeSync(fd);
  }
}

function scanText(text) {
  const r = { answerAt: 0, promptAt: 0, wakeAt: 0, wakes: 0, human: false };
  let answered = false;
  const lines = text.split('\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i];
    if (!line.includes('"type":"assistant"') && !line.includes('"turnOrigin"')) continue;
    let e;
    try { e = JSON.parse(line); } catch (err) { continue; }   // первая строка хвоста обрезана
    if (e.isSidechain || e.isCompactSummary) continue;        // сводка сжатия — тоже "human"
    if (e.type === 'assistant') {
      answered = true;
      if (!r.answerAt) r.answerAt = Date.parse(e.timestamp) || 0;
    } else if (e.type === 'user' && (e.turnOrigin === 'human' || e.turnOrigin === 'scheduled')) {
      const at = Date.parse(e.timestamp) || 0;
      if (answered && !r.promptAt) r.promptAt = at;
      if (e.turnOrigin === 'human') {
        r.human = true;
        break;
      }
      if (answered && promptText(e).startsWith(PREFIX)) {
        if (!r.wakeAt) r.wakeAt = at;
        r.wakes++;
      }
      answered = false;
    }
  }
  return r;
}

// Хук доски (session-board) на этот же запрос уже сдвинул курсор прочитанного, а вывод погашенного
// запроса до модели не дойдёт: возвращаем курсор ко времени последнего обработанного запроса,
// чтобы объявления показались на следующем вводе.
function restoreBoardCursor(sessionId, since, value) {
  const dir = path.join(process.env.LOCALAPPDATA || '', '1c-ib-queue');
  if (!value || !sessionId || !fs.existsSync(dir)) return;
  for (const name of fs.readdirSync(dir)) {
    const file = path.join(dir, name);
    if (name.endsWith(`.cursor.${sessionId}`) && fs.statSync(file).mtimeMs >= since) {
      fs.writeFileSync(file, String(value));
    }
  }
}

function gate(data) {
  const started = Date.now();
  const prompt = String(data.prompt || '');
  if (!prompt.startsWith(PREFIX)) {
    noteDayStart(started);
    return;
  }
  if (!data.transcript_path) return;
  const debug = prompt.includes('#debug');
  const wakeMin = debug ? 1 : WAKE_MIN;
  const plan = planOf(prompt, started);
  const s = scan(data.transcript_path);
  const idle = (started - s.answerAt) / 60000;   // ответа не нашёл — idle огромный, будим
  let reason = 'сессия активна';
  if (idle >= wakeMin) {
    const once = plan.single || (plan.warmOnce && started >= plan.deadline);
    if (once ? s.wakes < 1 : started < plan.deadline) {
      const tail = once ? 'последний прогрев' : `поддержание до ${hhmm(plan.deadline)}`;
      process.stdout.write(PING.replace('{TAIL}', tail));
      return;
    }
    // handoff один раз: последнее пробуждение было до срока; у «одного прогрева» — второе
    if ((once && s.wakes === 1) || s.wakeAt < plan.deadline) {
      let why = 'по окончании поддержания кэша';
      if (plan.single) why = 'из-за долгого простоя';
      else if (plan.endOfDay) why = 'по окончании рабочего дня';
      process.stdout.write(prompt.includes('без handoff') ? STOP : HANDOFF
        .replace('{N}', String(Math.floor(idle)))
        .replace('{DIR}', debug ? DEBUG_DIR : TASK_DIR)
        .replaceAll('{FILE}', `handoff-${fileStamp(started)}.md`)
        .replace('{REASON}', why)
        .replace('{COLD}', hhmm(started + 3600000)));   // кэш живёт час
      return;
    }
    reason = 'поддержание завершено, жду ввода';
  }
  // block — единственный способ без записи в контекст; continue:false пишет запрос в разговор
  process.stdout.write(JSON.stringify({ decision: 'block', reason: `keepwarm: ${reason}` }));
  const sessionId = String(data.session_id || '').replace(/[^\w-]/g, '');
  // хук доски работает параллельно: курсор правим после него
  setTimeout(() => {
    try { restoreBoardCursor(sessionId, started - 5000, s.promptAt); } catch (err) { /* не мешаем */ }
  }, 1500);
}

if (process.argv[2] === '--prompt') {
  timerPrompt(process.argv.slice(3), Date.now());
} else if (process.argv[2] === '--day-end') {
  setDayEnd(process.argv[3], Date.now());
} else {
  let input = '';
  process.stdin.on('data', (d) => { input += d; });
  process.stdin.on('end', () => {
    try {
      gate(JSON.parse(input || '{}'));
    } catch (err) {
      // ошибка хука не должна мешать вводу
    }
  });
}
