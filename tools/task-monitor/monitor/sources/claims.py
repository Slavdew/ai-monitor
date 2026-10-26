"""Захват задач сессиями: «возьми первую открытую задачу» без пересечений между сессиями.

<state_dir>/claims/<id>.json = {sid, at}. Создаётся атомарно (O_EXCL), поэтому две сессии не возьмут
одну задачу. Захват сессии, которой больше нет в живых, считается свободным и перезаписывается.
Живость — по реестру ~/.claude/sessions; сессия вне реестра (другой агент, TASK_SESSION) жива, пока
захват моложе sources.claims.ttl_sec — каждый вызов task.py этой сессии его продлевает (touch).
"""
import glob
import json
import os
import re

from monitor.core import expand, now_ms, read_json
from monitor.sources.sessions import alive

OPEN_SKIP = ('done', 'blocked', 'waiting-owner')   # статусы из шапки HANDOFF, которые не берём


def _dir(cfg):
    return os.path.join(cfg['state_dir'], 'claims')


def _registry(cfg):
    """{sid: жив ли процесс} по реестру сессий Claude Code."""
    reg = expand(cfg['sources'].get('sessions', {}).get('registry', '~/.claude/sessions'))
    out = {}
    for path in glob.glob(os.path.join(reg, '*.json')):
        r = read_json(path) or {}
        if r.get('sessionId'):
            out[r['sessionId']] = out.get(r['sessionId']) or alive(int(r.get('pid', 0)))
    return out


def _alive(cfg, c, reg):
    if c.get('sid') in reg:
        return reg[c['sid']]
    ttl = cfg['sources'].get('claims', {}).get('ttl_sec', 1800)
    return now_ms() - c.get('at', 0) < ttl * 1000


def _safe(task_id):
    return re.sub(r'[^\w.-]', '_', task_id)


def claims(cfg, reg=None):
    """{task_id: {sid, at}} только живых сессий; захваты мёртвых удаляются."""
    reg = _registry(cfg) if reg is None else reg
    out = {}
    for path in glob.glob(os.path.join(_dir(cfg), '*.json')):
        c = read_json(path) or {}
        if _alive(cfg, c, reg):
            out[c['task']] = c
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    return out


def touch(cfg, sid):
    """Продлить захваты сессии (вызов task.py — признак жизни для сессий вне реестра)."""
    for path in glob.glob(os.path.join(_dir(cfg), '*.json')):
        c = read_json(path) or {}
        if c.get('sid') == sid:
            c['at'] = now_ms()
            try:
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(json.dumps(c, ensure_ascii=False))
            except OSError:
                pass


def pick_tabs(cfg, tab=None):
    """Вкладки для next и предупреждения: --tab, sources.claims.tabs или все, кроме ideas."""
    have = [t['id'] for t in cfg.get('tabs', [])]
    if tab:
        if tab not in have:
            raise ValueError(f'нет вкладки {tab} (есть: {", ".join(have)})')
        return [tab], []
    want = cfg['sources'].get('claims', {}).get('tabs') or [t for t in have if t != 'ideas']
    return want, [t for t in want if t not in have]


def collect(cfg, src, ctx):
    return {'list': claims(cfg)}


def claim(cfg, task_id, sid, force=False):
    os.makedirs(_dir(cfg), exist_ok=True)
    path = os.path.join(_dir(cfg), _safe(task_id) + '.json')
    claims(cfg)   # убрать захваты мёртвых сессий, чтобы O_EXCL не споткнулся о них
    data = json.dumps({'task': task_id, 'sid': sid, 'at': now_ms()}, ensure_ascii=False)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        cur = read_json(path) or {}
        if not force and cur.get('sid') != sid:
            return False
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(data)
    return True


def release(cfg, task_id, sid=None):
    path = os.path.join(_dir(cfg), _safe(task_id) + '.json')
    cur = read_json(path) or {}
    if os.path.exists(path) and (sid is None or cur.get('sid') == sid):
        os.remove(path)
        return True
    return False


def pick(cfg, items, sessions, sid, tab=None, project=None, force=False):
    """Первая открытая задача в порядке списка панели, свободная от других живых сессий; захватывает её."""
    taken = {t: c['sid'] for t, c in claims(cfg).items() if c['sid'] != sid}
    busy = {s['task'] for s in sessions if s['alive'] and s['task'] and s['sid'] != sid}
    order = {t['id']: k for k, t in enumerate(cfg.get('tabs', []))}
    tabs = pick_tabs(cfg, tab)[0]   # идеи и т. п. — не брать
    cand = [i for i in items
            if i['tab'] in tabs and (not project or i['project'] == project)
            and i['status'] not in OPEN_SKIP and i['percent'] != 100 and not i.get('waits')]
    cand.sort(key=lambda i: (order.get(i['tab'], 99), i['project'] or '', i['id']))
    for i in cand:
        if not force and (i['id'] in taken or i['id'] in busy):
            continue
        if claim(cfg, i['id'], sid, force):
            return i
    return None


ACTIONS = {'release': lambda cfg, src, a: {'ok': release(cfg, a['task'])}}
