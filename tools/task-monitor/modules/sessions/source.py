"""Живые сессии Claude Code — из документированного `claude agents --json` (monitor.core.claude_sessions).

Живая — есть pid (команда сама проверяет процесс). Последнее сообщение — mtime транскрипта. Привязка к задаче —
по cfg sources.sessions.bind. Внутренний реестр ~/.claude/sessions не читаем с 28.10: формат не документирован.
"""
import glob
import json
import os

from monitor.core import claude_sessions, expand, now_ms, pid_alive, read_json, write_json

DAY_MS = 86400000


alive = pid_alive   # прежнее имя: им пользуются модули и task.py


def transcript(root, sid):
    hits = glob.glob(os.path.join(root, '*', f'{sid}.jsonl'))
    return max(hits, key=os.path.getmtime) if hits else ''


TITLE_KEYS = {'custom-title': 'customTitle', 'ai-title': 'aiTitle'}   # /rename важнее заголовка от ИИ
_titles = {}   # sid -> (mtime, title)


def _scan_titles(path, start):
    found = {}
    with open(path, 'rb') as f:
        f.seek(start)
        for line in f:
            if b'-title"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get('type') in TITLE_KEYS and d.get(TITLE_KEYS[d['type']]):
                found[d['type']] = d[TITLE_KEYS[d['type']]]
    return found


def title(path, sid, saved):
    """Заголовок вкладки: custom-title, иначе ai-title из транскрипта; читается хвост, весь файл — если в хвосте нет."""
    if not path:
        return saved.get(sid, '')
    mtime = os.path.getmtime(path)
    if sid in _titles and _titles[sid][0] == mtime:
        return _titles[sid][1]
    size = os.path.getsize(path)
    found = _scan_titles(path, max(0, size - (512 << 10)))
    if 'custom-title' not in found and size > (512 << 10):
        found = {**_scan_titles(path, 0), **found}
    t = found.get('custom-title') or found.get('ai-title') or saved.get(sid, '')
    _titles[sid] = (mtime, t)
    return t


def _bind_handoff_session(s, items):
    return next((i for i in items if i['session'] == s['sid']), None)


def _bind_cwd(s, items):
    cwd = os.path.normcase(os.path.normpath(s['cwd'])) + os.sep
    best = None
    for i in items:
        d = os.path.normcase(i['dir']) + os.sep
        if cwd.startswith(d) and (best is None or len(d) > len(best[0])):
            best = (d, i)
    return best and best[1]


def _bind_claim(s, items, claimed):
    task = next((t for t, c in claimed.items() if c['sid'] == s['sid']), None)
    return next((i for i in items if i['id'] == task), None)


BINDERS = {'claim': _bind_claim, 'handoff_session': lambda s, items, _: _bind_handoff_session(s, items),
           'cwd': lambda s, items, _: _bind_cwd(s, items)}


def collect(cfg, src, ctx):
    items = ctx.get('tabs', {}).get('items', [])
    troot = expand(src['transcripts'])
    saved_path = os.path.join(cfg['state_dir'], 'session-titles.json')
    saved = read_json(saved_path, {}) or {}
    out, now = [], now_ms()
    for r in claude_sessions():
        if not r.get('sessionId'):
            continue
        live = bool(r.get('pid'))
        t = transcript(troot, r['sessionId'])
        last = int(os.path.getmtime(t) * 1000) if t else r.get('updatedAt', 0)
        if not live and now - last > DAY_MS:
            continue
        name = ''   # name из agents — адрес («ai-ac»), не название: название — из транскрипта
        s = {'sid': r['sessionId'], 'pid': r['pid'], 'title': name or title(t, r['sessionId'], saved),
             'address': r.get('name', ''),   # по нему адресуют SendMessage, в том числе derived «ai-ac»
             'cwd': r.get('cwd', ''),
             'entry': r.get('kind', ''), 'status': r.get('status', '') if live else 'dead',
             'alive': live, 'started': r.get('startedAt', 0), 'last': last, 'task': ''}
        for b in src.get('bind', []):
            hit = BINDERS[b](s, items, ctx.get('claims', {}).get('list', {}))
            if hit:
                s['task'], s['bound_by'] = hit['id'], b
                break
        out.append(s)
    fresh = {s['sid']: s['title'] for s in out if s['title']}
    if any(saved.get(k) != v for k, v in fresh.items()):
        write_json(saved_path, {**saved, **fresh})   # название переживает закрытие сессии
    out.sort(key=lambda s: (not s['alive'], -s['started']))   # стабильный порядок: по старту, не по активности
    from monitor.core import _agents
    return {'list': out, **({'error': _agents['error']} if _agents['error'] else {})}
