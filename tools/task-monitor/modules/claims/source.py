"""Захваты сессий: задач («возьми первую открытую задачу» без пересечений) и общих ресурсов (ИБ, стенд, ветка).

<state_dir>/claims/<id>.json = {sid, at}. Создаётся атомарно (O_EXCL), поэтому две сессии не возьмут
одну задачу. Захват сессии, которой больше нет в живых, считается свободным и перезаписывается.
Живость — по реестру ~/.claude/sessions; сессия вне реестра (другой агент, TASK_SESSION) жива, пока
захват моложе sources.claims.ttl_sec — каждый вызов task.py этой сессии его продлевает (touch).
Перезагрузка и закрытие сессии снимают захваты сами: процесс сверяется по времени создания (procStart
реестра), а захват вне реестра, записанный до загрузки Windows, мёртв сразу, не дожидаясь ttl.

Ресурсы: <state_dir>/claims/res/<имя>.json = {res, sid, why, at, since, until, task}; until — срок (--ttl),
без него — пока жива сессия. Отказ ставит сессию в очередь: claims/wait/<имя>__<sid>.json.
"""
import glob
import json
import os
import re

from monitor.core import boot_ms, expand, module_settings, now_ms, read_json, write_json
from monitor.core import pid_alive as alive

OPEN_SKIP = ('done', 'blocked', 'waiting-owner')   # статусы из шапки HANDOFF, которые не берём


def _dir(cfg):
    return os.path.join(cfg['state_dir'], 'claims')


def _registry(cfg):
    """{sid: жив ли процесс} по реестру сессий Claude Code."""
    reg = expand(module_settings(cfg, 'sessions').get('registry', '~/.claude/sessions'))
    out = {}
    for path in glob.glob(os.path.join(reg, '*.json')):
        r = read_json(path) or {}
        if r.get('sessionId'):
            out[r['sessionId']] = out.get(r['sessionId']) or alive(int(r.get('pid', 0)), r.get('procStart'))
    return out


def _alive(cfg, c, reg):
    if c.get('sid') in reg:
        return reg[c['sid']]
    if c.get('reg'):   # сессия была в реестре и пропала из него — закрыта (реестр хранит только живые)
        return False
    ttl = module_settings(cfg, 'claims').get('ttl_sec', 1800)
    return c.get('at', 0) > boot_ms() and now_ms() - c.get('at', 0) < ttl * 1000


def _safe(task_id):
    return re.sub(r'[^\w.-]', '_', task_id)


def claims(cfg, reg=None):
    """{task_id: {sid, at}} только живых сессий; захваты мёртвых удаляются."""
    reg = _registry(cfg) if reg is None else reg
    out = {}
    for path in glob.glob(os.path.join(_dir(cfg), '*.json')):
        c = read_json(path)
        if c is None:   # пишется прямо сейчас (или испорчен) — не наш повод удалять чужой захват
            continue
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
    for path in [p for d in ('', 'res', 'wait') for p in glob.glob(os.path.join(_dir(cfg), d, '*.json'))]:
        c = read_json(path) or {}
        if c.get('sid') == sid:
            c['at'] = now_ms()
            try:   # временный файл и os.replace: читающий claims() не увидит пустой файл (аудит 27.10)
                tmp = f'{path}.{os.getpid()}.tmp'
                with open(tmp, 'w', encoding='utf-8') as f:
                    f.write(json.dumps(c, ensure_ascii=False))
                os.replace(tmp, path)
            except OSError:
                pass


def pick_tabs(cfg, tab=None):
    """Вкладки для next и предупреждения: --tab, sources.claims.tabs или все, кроме ideas."""
    have = [t['id'] for t in cfg.get('tabs', [])]
    if tab:
        if tab not in have:
            raise ValueError(f'нет вкладки {tab} (есть: {", ".join(have)})')
        return [tab], []
    want = module_settings(cfg, 'claims').get('tabs') or [t for t in have if t != 'ideas']
    return want, [t for t in want if t not in have]


def collect(cfg, src, ctx):
    reg = _registry(cfg)
    return {'list': claims(cfg, reg), 'res': holds(cfg, reg)}


def claim(cfg, task_id, sid, force=False):
    os.makedirs(_dir(cfg), exist_ok=True)
    path = os.path.join(_dir(cfg), _safe(task_id) + '.json')
    reg = _registry(cfg)
    claims(cfg, reg)   # убрать захваты мёртвых сессий, чтобы O_EXCL не споткнулся о них
    data = json.dumps({'task': task_id, 'sid': sid, 'at': now_ms(), 'reg': sid in reg}, ensure_ascii=False)
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


# --- ресурсы ---

def _scan(cfg, sub, reg):
    """[(путь, захват)] живых; мёртвые и истёкшие (until) удаляются."""
    out, now = [], now_ms()
    for path in glob.glob(os.path.join(_dir(cfg), sub, '*.json')):
        c = read_json(path)
        if c is None:
            continue
        if _alive(cfg, c, reg) and not (c.get('until') and c['until'] < now):
            out.append((path, c))
        else:
            try:
                os.remove(path)
            except OSError:
                pass
    return out


def _unwait(cfg, res, sid):
    try:
        os.remove(os.path.join(_dir(cfg), 'wait', f'{_safe(res)}__{_safe(sid)}.json'))
    except OSError:
        pass


def holds(cfg, reg=None):
    """{ресурс: захват} с очередью ожидающих (queue — [{sid, task}] в порядке прихода)."""
    reg = _registry(cfg) if reg is None else reg
    out = {c['res']: dict(c, queue=[]) for _, c in _scan(cfg, 'res', reg)}
    for _, w in sorted(_scan(cfg, 'wait', reg), key=lambda x: x[1].get('since', 0)):
        if w['res'] in out:
            out[w['res']]['queue'].append({'sid': w['sid'], 'task': w.get('task')})
        else:   # ресурс освободился — ждать больше нечего
            _unwait(cfg, w['res'], w['sid'])
    return out


def hold(cfg, res, sid, why='', ttl_sec=None, task=None, force=False):
    """(True, захват) или (False, чужой захват с очередью) — тогда сессия встаёт в очередь."""
    d = os.path.join(_dir(cfg), 'res')
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, _safe(res) + '.json')
    reg = _registry(cfg)
    cur = holds(cfg, reg)   # убрать мёртвые, чтобы O_EXCL не споткнулся о них
    if task is None:   # задача сессии — по её захвату задачи
        task = next((t for t, c in claims(cfg).items() if c['sid'] == sid), None)
    now = now_ms()
    c = {'res': res, 'sid': sid, 'why': why, 'at': now, 'since': now,
         'until': now + ttl_sec * 1000 if ttl_sec else None, 'task': task, 'reg': sid in reg}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
    except FileExistsError:
        old = cur.get(res) or read_json(path) or {}
        if not force and old.get('sid') != sid:
            os.makedirs(os.path.join(_dir(cfg), 'wait'), exist_ok=True)
            wp = os.path.join(_dir(cfg), 'wait', f'{_safe(res)}__{_safe(sid)}.json')
            since = (read_json(wp) or {}).get('since', now)   # повторная попытка не теряет место в очереди
            write_json(wp, {'res': res, 'sid': sid, 'task': task, 'at': now, 'since': since, 'reg': sid in reg})
            return False, holds(cfg).get(res, old)
        if old.get('sid') == sid:
            c['since'] = old.get('since', now)   # продление своего захвата: «держит с» не сбрасывается
        fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(json.dumps(c, ensure_ascii=False))
    _unwait(cfg, res, sid)
    return True, c


def free(cfg, res=None, sid=None):
    """Снять захват ресурса (sid — только свой; без res — все ресурсы сессии) и ожидания сессии → [ресурсы]."""
    gone = []
    for r, c in holds(cfg).items():
        if (res is None or r == res) and (sid is None or c['sid'] == sid):
            try:
                os.remove(os.path.join(_dir(cfg), 'res', _safe(r) + '.json'))
                gone.append(r)
            except OSError:
                pass
    if sid:
        for path in glob.glob(os.path.join(_dir(cfg), 'wait', f'*__{_safe(sid)}.json')):
            if res is None or (read_json(path) or {}).get('res') == res:
                try:
                    os.remove(path)
                except OSError:
                    pass
    return gone


ACTIONS = {'release': lambda cfg, src, a: {'ok': release(cfg, a['task'])},
           # владелец снимает повисший захват ресурса со страницы (OWNER_ONLY в server.py)
           'free': lambda cfg, src, a: {'ok': bool(free(cfg, a['res']))}}
