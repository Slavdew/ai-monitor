#!/usr/bin/env node
// Хуки сжатия контекста для скилла handoff.
//   node handoff-hook.js save    — PostCompact: compact_summary в <каталог HANDOFF>/.handoff/compact-<время>.md
//   node handoff-hook.js remind  — SessionStart (matcher compact): строка в контекст — где HANDOFF и что сверить
// HANDOFF задачи ищется так: последний существующий путь …HANDOFF.md в хвосте журнала сессии
// (команда «Продолжаем работу по …», запись файла) → строка «handoff: <путь>» в CLAUDE.md проекта →
// docs/HANDOFF.md корня репозитория. Не нашёл — save пишет в %LOCALAPPDATA%\handoff\compact\.
// Ошибки гасит, код возврата всегда 0: хук не должен мешать сжатию.
const fs = require('fs');
const path = require('path');

const TAIL = 4 << 20;   // хвост журнала, байт
const FALLBACK = path.join(process.env.LOCALAPPDATA || '', 'handoff', 'compact');

const pad = (n) => String(n).padStart(2, '0');
const fileStamp = (d) =>
  `${d.getFullYear()}${pad(d.getMonth() + 1)}${pad(d.getDate())}-${pad(d.getHours())}${pad(d.getMinutes())}${pad(d.getSeconds())}`;
const hhmm = (d) => `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;

function tail(file) {
  const fd = fs.openSync(file, 'r');
  try {
    const size = fs.fstatSync(fd).size;
    const len = Math.min(size, TAIL);
    const buf = Buffer.alloc(len);
    fs.readSync(fd, buf, 0, len, size - len);
    return buf.toString('utf8');
  } finally {
    fs.closeSync(fd);
  }
}

// Только ввод пользователя и Write/Edit модели: в выводах инструментов пути бывают чьи угодно.
function candidates(e) {
  const c = e.message && e.message.content;
  if (e.type === 'user' && e.turnOrigin === 'human') {
    const text = typeof c === 'string' ? c : (Array.isArray(c) ? c.filter((b) => b.type === 'text').map((b) => b.text).join('\n') : '');
    return text.match(/[A-Za-z]:[\\/][^"\n<>|*?]*?HANDOFF\.md/g) || [];
  }
  if (e.type === 'assistant' && Array.isArray(c)) {
    return c.filter((b) => b.type === 'tool_use' && /^(Write|Edit)$/.test(b.name) && b.input &&
      /HANDOFF\.md$/.test(String(b.input.file_path || ''))).map((b) => b.input.file_path);
  }
  return [];
}

function fromTranscript(file) {
  if (!file || !fs.existsSync(file)) return '';
  const lines = tail(file).split('\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    if (!lines[i].includes('HANDOFF.md')) continue;
    let e;
    try { e = JSON.parse(lines[i]); } catch (err) { continue; }   // первая строка хвоста обрезана
    if (e.isSidechain) continue;
    const found = candidates(e);
    for (let j = found.length - 1; j >= 0; j--) {
      const p = path.normalize(found[j]);
      if (fs.existsSync(p)) return p;
    }
  }
  return '';
}

function gitRoot(dir) {
  for (let d = path.resolve(dir); ; d = path.dirname(d)) {
    if (fs.existsSync(path.join(d, '.git'))) return d;
    if (path.dirname(d) === d) return '';
  }
}

function fromProject(cwd) {
  const root = gitRoot(cwd) || cwd;
  try {
    const m = /^handoff:\s*(.+?)\s*$/m.exec(fs.readFileSync(path.join(root, 'CLAUDE.md'), 'utf8'));
    if (m) {
      const p = path.resolve(root, m[1].replace(/^["'`]|["'`]$/g, ''));
      return fs.existsSync(p) && fs.statSync(p).isDirectory() ? path.join(p, 'HANDOFF.md') : p;
    }
  } catch (err) { /* нет CLAUDE.md */ }
  const p = path.join(root, 'docs', 'HANDOFF.md');
  return fs.existsSync(p) ? p : '';
}

const findHandoff = (data) => fromTranscript(data.transcript_path) || fromProject(data.cwd || process.cwd());

function save(data) {
  const summary = String(data.compact_summary || '');
  if (!summary) return;
  const handoff = findHandoff(data);
  const dir = handoff ? path.join(path.dirname(handoff), '.handoff') : FALLBACK;
  fs.mkdirSync(dir, { recursive: true });
  const head = `<!-- session: ${data.session_id || ''}; trigger: ${data.trigger || ''}; handoff: ${handoff || 'нет'} -->\n\n`;
  fs.writeFileSync(path.join(dir, `compact-${fileStamp(new Date())}.md`), head + summary + '\n');
}

function remind(data) {
  const handoff = findHandoff(data);
  const where = handoff
    ? `HANDOFF задачи — ${handoff} (изменён ${hhmm(fs.statSync(handoff).mtime)}).`
    : 'HANDOFF задачи не найден.';
  process.stdout.write(`Контекст сжат. ${where} Перед следующим действием сверить git status, ` +
    'CronList, ListAgents; слова владельца — только из сводки и HANDOFF, не додумывать.\n');
}

let input = '';
process.stdin.on('data', (c) => { input += c; });
process.stdin.on('end', () => {
  try {
    const data = JSON.parse(input || '{}');
    if (process.argv[2] === 'save') save(data);
    else if (process.argv[2] === 'remind') remind(data);
  } catch (err) { /* не мешаем сжатию */ }
});
