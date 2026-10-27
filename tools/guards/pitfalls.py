r"""Журнал граблей: каждая ошибка инструмента и каждый отказ сторожа — строка JSONL с id из справочника.

Агент причину не формулирует: id выводится из (инструмент, ввод, вывод) правилами CATALOG, поэтому одна грабля —
всегда один id, сколько бы раз и какими словами ни случилась. Не подошло ни одно правило — id `unclassified` и
отпечаток fp (хэш нормализованной первой строки ошибки): по fp видно, какое правило дописать, а `reclassify`
пересчитает старые записи по новому справочнику.

  pitfalls.py hook                         — хук PostToolUse/PostToolUseFailure: событие на stdin, всегда код 0
  pitfalls.py report TOOL [файл|-]         — грабли без ошибки инструмента: сырой вывод (не пересказ) файлом
                                             или на stdin (из PowerShell — файлом: конвейер портит кириллицу)
  pitfalls.py stats [--since 2026-10-01] [--by id|tool|day|agent] [--top 30]
  pitfalls.py reclassify                   — пересчитать id всех записей по текущему CATALOG
  pitfalls.py backfill <каталог проектов>  — заполнить журнал из старых транскриптов (*.jsonl)
  pitfalls.py ids                          — справочник: id и что он значит

Журнал: %USERPROFILE%\.claude\pitfalls\log.jsonl (PITFALLS_LOG — другой путь). Счётчики считаются из журнала,
отдельного файла с числами нет: его нельзя рассинхронизировать.
"""
import collections
import datetime
import hashlib
import json
import os
import re
import sys

LOG = os.environ.get('PITFALLS_LOG') or os.path.join(os.path.expanduser('~'), '.claude', 'pitfalls', 'log.jsonl')
HEAD = 300   # сколько знаков ввода и вывода хранить: хватает на reclassify, не раздувает журнал

# (id, инструмент, регулярное выражение по выводу, по вводу или None, что значит). Первое совпадение выигрывает:
# частное выше общего. id не переименовывать — по ним статистика; устаревшее правило удалять вместе с id нельзя,
# только дописывать новое выше.
CATALOG = [
    ('guard.*', None, None, None, 'отказ сторожа guard.py; суффикс — имя правила'),
    ('wt.isolation-refused', 'Bash|PowerShell', r'This agent is isolated in the worktree', None,
     'сторож изоляции worktree отверг команду агента'),
    ('agent.worktree-no-git', 'Agent', r'Cannot create agent worktree', None, 'isolation=worktree вне git'),
    ('bash.heredoc-truncated', 'Bash', r"unexpected EOF while looking for matching", None,
     'длинная команда обрезана / незакрытая кавычка'),
    ('edit.escape-lost', 'Edit', r'String to replace not found', r'[\r\t]', 'в old_string \\r/\\t стали CR/TAB'),
    ('edit.not-found', 'Edit', r'String to replace not found', None, 'old_string нет в файле'),
    ('edit.many-matches', 'Edit', r'Found \d+ matches of the string', None, 'old_string не уникальна'),
    ('edit.not-read', 'Edit|Write', r'has not been read yet', None, 'правка без Read'),
    ('edit.stale', 'Edit|Write', r'modified since read', None, 'файл изменён после Read'),
    ('read.missing', 'Read', r'File does not exist', None, 'файла нет'),
    ('read.too-large', 'Read', r'exceeds maximum allowed tokens', None, 'файл больше лимита Read'),
    ('py.charmap', None, r"UnicodeEncodeError: 'charmap'", None, 'Python печатает не-cp1251 в консоль'),
    ('py.bom-json', None, r'Unexpected UTF-8 BOM', None, 'json без utf-8-sig'),
    ('ps.pipe-bom', None, r'line 1: ﻿\S*: not found', None, 'конвейер PowerShell в native начался с BOM'),
    ('ps.ps1-no-bom', 'PowerShell', r'missing the terminator|СЃ|Р°Р', r'\.ps1', '.ps1 без BOM прочитан как cp1251'),
    ('ps.native-stderr', 'PowerShell', r'NativeCommandError', None, '2>&1 у native-программы'),
    ('ps.parse', 'PowerShell', r'ParserError|Missing .* in hash literal|Missing closing|Unexpected token', None,
     'разбор PowerShell / Invoke-Expression'),
    ('ps.curl-alias', 'PowerShell', r'Invoke-WebRequest : A parameter cannot be found', None, 'curl = Invoke-WebRequest'),
    ('ps.mojibake', 'PowerShell', r'Р[ЎС°ЅѕёµР][Р°-яЃ-џ]', None, 'UTF-8 прочитан как cp1251 (Get-Content без -Encoding)'),
    ('ssh.denied', 'Bash|PowerShell', r'Permission denied \(publickey', None, 'ssh: ключ не дошёл (IdentityAgent, Bitwarden)'),
    ('grep.p-locale', 'Bash', r'-P supports only unibyte and UTF-8 locales', None, 'grep -P без LC_ALL=C.UTF-8'),
    ('cc.sleep-blocked', 'Bash|PowerShell', r'Blocked: sleep', None, 'sleep перед командой запрещён — Monitor'),
    ('cc.rm-blocked', 'Bash|PowerShell', r'safety check, not by the user', None, 'встроенная проверка удаления'),
    ('cc.classifier-denied', None, r'denied by the Claude Code auto mode classifier', None, 'отказ авто-режима'),
    ('cc.classifier-no-verdict', None, r'classifier gave no verdict', None, 'классификатор не ответил'),
    ('cc.user-rejected', None, r"user doesn't want to proceed", None, 'отказ пользователя (шум)'),
    ('cc.input-invalid', None, r'InputValidationError', None, 'неверные параметры инструмента'),
    ('cc.timeout', 'Bash|PowerShell', r'Command timed out', None, 'команда дольше лимита'),
    ('git.pathspec', None, r"pathspec '.*' did not match", None,
     'git принял текст за путь (сообщение коммита аргументом, here-string PowerShell) — -F <файл>'),
    ('cc.hook-error', None, r'hook error', None, 'сбой хука (наш или чужой)'),
    ('test.red', None, r'(?m)Failed! - Failed: \d+|^AssertionError|^[1-9]\d* ошибок', None, 'красный тест — рабочий цикл, шум'),
    ('out.mojibake', None, '�{3}', None, 'вывод в не той кодировке (cp1251/cp866 в UTF-8)'),
    ('fs.missing', 'Bash', r'(?m)^(ls|cat|cd|head|tail|cp|mv|rm|stat|wc|bash|sh)(: line \d+)?: .*No such file or directory', None, 'файла/каталога нет'),
    ('web.socket-closed', 'WebFetch', r'Socket is closed', None, 'WebFetch: обрыв'),
    ('web.domain-unverified', 'WebFetch', r'Unable to verify if domain', None, 'WebFetch: домен не проверен'),
    ('net.tls', None, r'schannel|SSL_ERROR|handshake', None, 'TLS через прокси'),
    ('py.traceback', None, r'Traceback \(most recent call last\)', None, 'исключение Python; суффикс — тип'),
]
_RX = [(i, re.compile(t) if t else None, re.compile(o) if o else None, re.compile(n) if n else None, d)
       for i, t, o, n, d in CATALOG]
_NORM = [(re.compile(r'[A-Za-z]:[\\/][^\s\'"]*|/[a-z]/[^\s\'"]*|/tmp/[^\s\'"]*'), '<path>'),
         (re.compile(r'\b[0-9a-f]{7,40}\b'), '<hex>'), (re.compile(r'\d+'), 'N'), (re.compile(r'\s+'), ' ')]
_EXC = re.compile(r'^(\w+(?:Error|Exception|Warning))\b', re.M)
_SECRET = re.compile(r'(sk-ant-[\w-]+|gh[pousr]_\w{20,}|Bearer\s+\S+|(?i:password|token|secret|api[_-]?key)\s*[=:]\s*\S+)')


def signature(out):
    """Нормализованная первая значимая строка вывода: числа, пути, хэши убраны — одинаковые ошибки сходятся."""
    lines = [l for l in out.splitlines() if l.strip() and not l.startswith('Exit code')]
    s = lines[0] if lines else ''
    for rx, rep in _NORM:
        s = rx.sub(rep, s)
    return s.strip()[:160]


def classify(tool, inp, out, guard_rule=None):
    """(id, fp) по вызову и выводу. inp — строка ввода (команда, путь, old_string)."""
    m = re.search(r'Сторож \((\w+)\)', out)   # отказ сторожа, как его видит транскрипт
    if guard_rule or m:
        return 'guard.' + (guard_rule or m.group(1)), ''
    for pid, t, o, n, _ in _RX:
        if pid == 'guard.*' or (t and not t.fullmatch(tool)) or (o and not o.search(out)) or (n and not n.search(inp)):
            continue
        if pid == 'py.traceback':
            m = _EXC.findall(out)
            return pid + ('.' + m[-1] if m else ''), ''
        return pid, ''
    if re.match(r'Exit code [1-9]', out) and not signature(out):
        return 'exit.nonzero', ''
    return 'unclassified', hashlib.sha1(f'{tool}|{signature(out)}'.encode()).hexdigest()[:10]


def input_text(tool, inp):
    if not isinstance(inp, dict):
        return str(inp)
    if tool in ('Bash', 'PowerShell'):
        return inp.get('command', '')
    if tool == 'Edit':
        return inp.get('file_path', '') + '\n' + inp.get('old_string', '')
    if tool in ('Read', 'Write', 'NotebookEdit'):
        return inp.get('file_path', '')
    return json.dumps(inp, ensure_ascii=False)


def text_of(x):
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return '\n'.join(text_of(c.get('text', c) if isinstance(c, dict) else c) for c in x)
    if isinstance(x, dict):
        for k in ('error', 'stderr', 'content', 'text', 'message'):
            if x.get(k):
                return text_of(x[k])
        return json.dumps(x, ensure_ascii=False)
    return '' if x is None else str(x)


def record(tool, inp, out, src, ts=None, session='', agent='', cwd='', guard_rule=None, tu=''):
    """tu — tool_use_id: по нему backfill не повторяет то, что уже записали хук и сторож."""
    inp, out = inp[:HEAD], clip(out)   # классифицировать то же, что хранится: reclassify даст тот же id
    pid, fp = classify(tool, inp, out, guard_rule)
    return {'v': 1, 'tu': tu,'ts': ts or datetime.datetime.now().astimezone().isoformat(timespec='seconds'), 'id': pid,
            'fp': fp, 'tool': tool, 'src': src, 'session': session[:36], 'agent': agent, 'cwd': cwd,
            'sig': signature(out) if pid == 'unclassified' else '',
            'in': _SECRET.sub('<secret>', inp), 'out': _SECRET.sub('<secret>', out)}


def clip(out):
    """Начало и конец вывода: у трассировки тип исключения — в последней строке, reclassify нужен он."""
    return out if len(out) <= HEAD + 400 else out[:HEAD] + '\n…\n' + out[-400:]


def append(recs):
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, 'a', encoding='utf-8', newline='\n') as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


def read_log():
    if not os.path.exists(LOG):
        return []
    with open(LOG, encoding='utf-8', errors='replace') as f:
        return [json.loads(l) for l in f if l.strip()]


def is_error(ev):
    """Событие хука — ошибка? PostToolUseFailure — всегда; PostToolUse — по признакам в ответе инструмента."""
    if ev.get('hook_event_name') == 'PostToolUseFailure' or ev.get('error'):
        return True
    resp = ev.get('tool_response')
    if isinstance(resp, dict):
        if resp.get('is_error') or resp.get('interrupted'):
            return True
        code = resp.get('exit_code', resp.get('exitCode', resp.get('returnCode')))
        if isinstance(code, int) and code != 0:
            return True
    return False


def cmd_hook():
    try:
        ev = json.loads(sys.stdin.buffer.read().decode('utf-8-sig', 'replace') or '{}')
        if not is_error(ev):
            return 0
        tool = ev.get('tool_name', '')
        out = text_of(ev.get('error')) or text_of(ev.get('tool_response'))
        if re.fullmatch(r'Exit code \d+', out.strip()):
            out = from_transcript(ev.get('transcript_path'), ev.get('tool_use_id')) or out
        append([record(tool, input_text(tool, ev.get('tool_input') or {}), out, 'hook', session=ev.get('session_id', ''),
                       agent=ev.get('agent_type') or ev.get('agent_id') or 'main', cwd=ev.get('cwd', ''),
                       tu=ev.get('tool_use_id', ''))])
    except Exception:   # журнал не должен мешать работе
        pass
    return 0


def from_transcript(path, tu_id, tail=2_000_000):
    """Полный вывод вызова из хвоста транскрипта: в PostToolUseFailure у Bash поле error — только «Exit code N»
    (зонд 28.10 на `false`; приходит ли туда stderr у настоящих ошибок — не проверено)."""
    if not path or not tu_id or not os.path.exists(path):
        return ''
    with open(path, 'rb') as f:
        f.seek(max(0, os.path.getsize(path) - tail))
        lines = f.read().decode('utf-8', 'replace').splitlines()
    for line in reversed(lines):
        if tu_id not in line:
            continue
        try:
            for c in json.loads(line).get('message', {}).get('content', []):
                if isinstance(c, dict) and c.get('tool_use_id') == tu_id:
                    return text_of(c.get('content'))
        except (ValueError, AttributeError):
            continue
    return ''


def log_guard(rule, tool, inp, cwd='', session='', tu=''):
    """Отказ сторожа — тоже грабля; вызывается из guard.py, ошибки глотает."""
    try:
        append([record(tool, input_text(tool, inp), '', 'guard', session=session, cwd=cwd, guard_rule=rule, tu=tu)])
    except Exception:
        pass


def cmd_report(tool, path):
    raw = sys.stdin.buffer.read() if path in (None, '-') else open(path, 'rb').read()
    out = raw.decode('utf-8-sig', 'replace')   # конвейер PowerShell отдаёт BOM; кириллицу надёжнее файлом
    r = record(tool, '', out, 'report', session=os.environ.get('CLAUDE_CODE_SESSION_ID', ''))
    append([r])
    print(r['id'] + (f" fp={r['fp']} «{r['sig']}»" if r['fp'] else ''))
    return 0


def cmd_stats(since=None, by='id', top=30):
    recs = [r for r in read_log() if not since or r['ts'] >= since]
    key = {'id': lambda r: r['id'] if r['id'] != 'unclassified' else f"unclassified {r['fp']} «{r['sig'][:70]}»",
           'tool': lambda r: r['tool'], 'day': lambda r: r['ts'][:10], 'agent': lambda r: r.get('agent') or '?'}[by]
    c = collections.Counter(key(r) for r in recs)
    sess = collections.defaultdict(set)
    for r in recs:
        sess[key(r)].add(r.get('session'))
    print(f'{len(recs)} записей, {len(c)} групп' + (f' с {since}' if since else ''))
    for k, n in c.most_common(top):
        print(f'{n:6} {len(sess[k]):4} сесс.  {k}')
    return 0


def cmd_reclassify():
    recs = read_log()
    moved = 0
    for r in recs:
        if r['id'].startswith('guard.'):
            continue
        pid, fp = classify(r['tool'], r.get('in', ''), r.get('out', ''))
        if (pid, fp) != (r['id'], r['fp']):
            moved += 1
            r['id'], r['fp'] = pid, fp
            r['sig'] = signature(r.get('out', '')) if pid == 'unclassified' else ''
    tmp = LOG + '.tmp'
    with open(tmp, 'w', encoding='utf-8', newline='\n') as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')
    os.replace(tmp, LOG)
    print(f'{moved} из {len(recs)} записей сменили id')
    return 0


def cmd_backfill(root):
    seen = {r.get('tu') for r in read_log() if r.get('tu')}   # повторный backfill не дублирует
    new = []
    for dp, _, files in os.walk(root):
        for fn in files:
            if fn.endswith('.jsonl'):
                new += _scan(os.path.join(dp, fn), seen)
    append(new)
    print(f'добавлено {len(new)}')
    return 0


def _scan(path, seen):
    calls, out = {}, []
    with open(path, encoding='utf-8', errors='replace') as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            content = (rec.get('message') or {}).get('content')
            if not isinstance(content, list):
                continue
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get('type') == 'tool_use':
                    calls[c.get('id')] = (c.get('name', ''), c.get('input') or {}, rec.get('timestamp', ''))
                elif c.get('type') == 'tool_result' and c.get('tool_use_id') not in seen:
                    body = text_of(c.get('content'))
                    m = re.search(r'Exit code (\d+)', body[:300])
                    if not (c.get('is_error') or 'Traceback (most recent' in body or (m and m.group(1) != '0')):
                        continue
                    tool, inp, ts = calls.get(c.get('tool_use_id'), ('?', {}, rec.get('timestamp', '')))
                    out.append(record(tool, input_text(tool, inp), body, 'backfill', ts=ts,
                                      session=rec.get('sessionId', ''), agent='sub' if rec.get('isSidechain') else 'main',
                                      cwd=rec.get('cwd', ''), tu=c.get('tool_use_id', '')))
    return out


def main(argv):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    cmd = argv[1] if len(argv) > 1 else 'stats'
    if cmd == 'hook':
        return cmd_hook()
    if cmd == 'report':
        return cmd_report(argv[2], argv[3] if len(argv) > 3 else None)
    if cmd == 'stats':
        opts = dict(zip(argv[2::2], argv[3::2]))
        return cmd_stats(opts.get('--since'), opts.get('--by', 'id'), int(opts.get('--top', 30)))
    if cmd == 'reclassify':
        return cmd_reclassify()
    if cmd == 'backfill':
        return cmd_backfill(argv[2])
    if cmd == 'ids':
        for pid, t, _, _, d in CATALOG:
            print(f'{pid:28} {t or "*":16} {d}')
        return 0
    print(__doc__)
    return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv))
