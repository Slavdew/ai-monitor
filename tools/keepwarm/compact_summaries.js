// Структура сводок автосжатия: заголовки и размер (без содержимого).
const fs = require('fs');
const path = require('path');
const root = path.join(process.env.USERPROFILE, '.claude', 'projects');
const since = Date.now() - 40 * 86400000;
for (const dir of fs.readdirSync(root)) {
  const d = path.join(root, dir);
  if (!fs.statSync(d).isDirectory()) continue;
  for (const name of fs.readdirSync(d)) {
    const f = path.join(d, name);
    if (!name.endsWith('.jsonl') || fs.statSync(f).mtimeMs < since) continue;
    for (const line of fs.readFileSync(f, 'utf8').split('\n')) {
      if (!line.includes('"isCompactSummary":true')) continue;
      let e; try { e = JSON.parse(line); } catch (err) { continue; }
      const c = e.message.content;
      const text = typeof c === 'string' ? c : c.map((b) => b.text || '').join('\n');
      const heads = text.split('\n').filter((l) => /^(#{1,4} |\d+\. \*\*|\*\*[^*]+\*\*:?$)/.test(l.trim())).map((l) => l.trim().slice(0, 70));
      console.log(`== ${e.timestamp} ${name.slice(0, 8)}: ${text.length} симв., ~${Math.round(text.length / 3.5 / 1000)}k ток.`);
      console.log('   ' + text.split('\n')[0].slice(0, 160));
      console.log(heads.slice(0, 14).map((h) => '   ' + h).join('\n'));
    }
  }
}
