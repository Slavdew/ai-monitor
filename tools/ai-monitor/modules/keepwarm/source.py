"""Keepwarm по сессии: флаг <off_dir>/<sid>.off читает tools/keepwarm/keepwarm-gate.js и гасит пинги.

ALL.off — keepwarm выключен для всех: его ставит рубильник model (enabled / keepwarm = false).
Настройки, которые читает тот же шлюз (каталог state_dir, по умолчанию %LOCALAPPDATA%/keepwarm):
  config.json        — личный график поверх общего (src config): окно начала дня, часы, выходные,
                       autoStart — ставить таймер самому по обычным задачам;
  day-start          — сегодняшний день {start, end}: «сегодня работаем до»;
  session/<sid>.json — настройки сессии {pings, until, handoff}.
"""
import json
import os
import re
import time

from monitor.core import expand, model_on, read_json, write_json

GRAPH_KEYS = ('workdayHours', 'startWindow', 'offDays', 'autoStart', 'rulesOn', 'rules')


def _dirs(src):
    state = expand(src.get('state_dir', '%LOCALAPPDATA%/keepwarm'))
    return state, os.path.join(state, 'session')


def _today(state):
    day = read_json(os.path.join(state, 'day-start'), {}) or {}
    at = day.get('start') or day.get('end')
    return day if at and time.localtime(at / 1000)[:3] == time.localtime()[:3] else {}


def collect(cfg, src, ctx):
    d = expand(src['off_dir'])
    all_off = not model_on(cfg, 'keepwarm')
    flag = os.path.join(d, 'ALL.off')
    if all_off != os.path.exists(flag):
        os.makedirs(d, exist_ok=True)
        open(flag, 'w').close() if all_off else os.remove(flag)
    off = [f[:-4] for f in os.listdir(d) if f.endswith('.off') and f != 'ALL.off'] if os.path.isdir(d) else []
    state, sess = _dirs(src)
    base = read_json(expand(src['config']), {}) or {} if src.get('config') else {}
    mine = read_json(os.path.join(state, 'config.json'), {}) or {}
    graph = {'workdayHours': 9, 'startWindow': ['07:00', '11:00'], 'offDays': [6, 0], 'autoStart': True,
             **{k: base[k] for k in GRAPH_KEYS if k in base}, **{k: mine[k] for k in GRAPH_KEYS if k in mine}}
    sessions = {}
    if os.path.isdir(sess):
        for f in os.listdir(sess):
            if f.endswith('.json'):
                sessions[f[:-5]] = read_json(os.path.join(sess, f), {}) or {}
    return {'off': sorted(off), 'all_off': all_off, 'graph': graph, 'today': _today(state), 'sessions': sessions}


def _sid(args):
    sid = args['sid']
    if not re.fullmatch(r'[\w-]+', sid):
        raise ValueError('sid')
    return sid


def set_keepwarm(cfg, src, args):
    sid = _sid(args)
    d = expand(src['off_dir'])
    os.makedirs(d, exist_ok=True)
    flag = os.path.join(d, f'{sid}.off')
    if args['on']:
        if os.path.exists(flag):
            os.remove(flag)
    else:
        open(flag, 'w').close()
    return {'ok': True}


def set_session(cfg, src, args):
    """Настройки сессии: pings (0 — без счёта), until (мс, пусто — по таймеру), handoff (true/false/пусто)."""
    sid = _sid(args)
    _, sess = _dirs(src)
    out = {}
    if int(args.get('pings') or 0) > 0:
        out['pings'] = min(int(args['pings']), 48)
    if args.get('until'):
        until = int(args['until'])
        if not 0 < until - time.time() * 1000 <= 24 * 3600 * 1000:   # свой срок — в пределах суток: не «навсегда»
            raise ValueError('срок — в ближайшие 24 ч')
        out['until'] = until
    if args.get('handoff') in (True, False):
        out['handoff'] = args['handoff']
    path = os.path.join(sess, f'{sid}.json')
    if out:
        write_json(path, out)
    elif os.path.exists(path):
        os.remove(path)
    return {'ok': True}


def _graph_value(k, v):
    """Значение графика того типа, что ждёт keepwarm-gate.js: иначе шлюз упал бы и пропустил пинги (аудит 27.10)."""
    hm = lambda x: isinstance(x, str) and re.fullmatch(r'([01]?\d|2[0-3]):[0-5]\d', x)
    if k == 'workdayHours' and isinstance(v, (int, float)) and 1 <= v <= 16:
        return v
    if k == 'startWindow' and isinstance(v, list) and len(v) == 2 and all(hm(x) for x in v):
        return v
    if k == 'offDays' and isinstance(v, list) and all(isinstance(x, int) and 0 <= x <= 6 for x in v):
        return sorted(set(v))
    if k in ('autoStart', 'rulesOn') and isinstance(v, bool):
        return v
    if k == 'rules' and isinstance(v, list) and len(v) <= 12:   # расширенный график: правила по порядку
        out = []
        for r in v:
            days, kinds = r.get('days'), r.get('kinds')
            ok = (isinstance(r, dict) and hm(r.get('from')) and hm(r.get('to')) and r.get('mode') in ('until', 'pings')
                  and isinstance(days, list) and all(isinstance(x, int) and 0 <= x <= 6 for x in days)
                  and (kinds is None or isinstance(kinds, list) and all(isinstance(x, str) and len(x) <= 20 for x in kinds)))
            if not ok:
                raise ValueError('rules: недопустимое правило')
            out.append({'name': str(r.get('name') or '')[:40], 'days': sorted(set(days)), 'from': r['from'], 'to': r['to'],
                        'mode': r['mode'], 'pings': max(1, min(48, int(r.get('pings') or 6))),
                        **({'kinds': kinds} if kinds is not None else {})})
        return out
    raise ValueError(f'{k}: недопустимое значение')


def set_graph(cfg, src, args):
    """Личный график: только известные ключи; пустое значение — вернуть общий."""
    state, _ = _dirs(src)
    path = os.path.join(state, 'config.json')
    mine = read_json(path, {}) or {}
    for k in GRAPH_KEYS:
        if k in args:
            if args[k] is None:
                mine.pop(k, None)
            else:
                mine[k] = _graph_value(k, args[k])
    write_json(path, mine)
    return {'ok': True}


def set_day_end(cfg, src, args):
    """«Сегодня работаем до ЧЧ:ММ» — тот же файл, что пишет keepwarm-gate.js --day-end; пусто — снять."""
    state, _ = _dirs(src)
    path = os.path.join(state, 'day-start')
    day = _today(state)
    hm = args.get('end') or ''
    if not hm:
        day.pop('end', None)
    else:
        if not re.fullmatch(r'\d\d?:\d\d', hm):
            raise ValueError('ЧЧ:ММ')
        h, m = map(int, hm.split(':'))
        t = time.localtime()
        end = int(time.mktime((t.tm_year, t.tm_mon, t.tm_mday, h, m, 0, 0, 0, -1)) * 1000)
        if end <= time.time() * 1000:
            raise ValueError('время уже прошло')
        day = {'start': day.get('start') or int(time.time() * 1000), 'end': end}
    os.makedirs(state, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(day, f)
    return {'ok': True}


ACTIONS = {'set': set_keepwarm, 'session': set_session, 'graph': set_graph, 'dayend': set_day_end}
