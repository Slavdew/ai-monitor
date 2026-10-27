"""Вопросы владельцу: пункт плана задачи со статусом wait (флаг q остаётся и после ответа).

Владелец пишет ответ черновиком (draft — агенты его не видят) и отправляет пачкой по задаче:
ответ дописывается в answers[], пункт помечается seen=false, запись уходит в state_dir/answers.json
(ожидают прочтения) и строкой в state_dir/events.jsonl (для watch). Агент узнаёт об ответе хуком
UserPromptSubmit (полный текст — один раз на сессию, отметки в state_dir/seen/<sid>.json) и
подтверждает ack — запись уходит из answers.json. Логика перенесена из plan board (<dev>\\plan-board).
"""
import json
import os
import re
import time

from monitor.core import front_matter, locked, now_ms, read_json, session_id, write_json
from monitor.sources import tabs


def _pending_path(cfg):
    return os.path.join(cfg['state_dir'], 'answers.json')


def _seen_path(cfg, sid):
    return os.path.join(cfg['state_dir'], 'seen', re.sub(r'[^\w-]', '_', sid) + '.json')


def _task(cfg, dir_):
    return read_json(os.path.join(dir_, cfg['item']['task_file']), {}) or {}


def _words(s):
    return {w for w in re.findall(r'\w+', (s or '').lower()) if len(w) > 2}


def ask(cfg, dir_, text, note='', force=False, opts=()):
    """Новый вопрос в план задачи. Похожий открытый вопрос (≥ 0,6 общих слов) — отказ: дополнить его.
    opts — 2–4 коротких варианта ответа (кнопки на странице); «↺ проще» и «Как советуешь» страница добавляет сама."""
    opts = [o.strip() for o in opts if o.strip()]
    if opts and not 2 <= len(opts) <= 4:
        raise ValueError(f'--opt: 2–4 коротких варианта, дано {len(opts)}; «↺ проще» и «Как советуешь» страница добавит сама')
    w = _words(text)
    for k, p in enumerate(_task(cfg, dir_).get('plan', [])):
        o = _words(p.get('text'))
        if not force and p.get('status') == 'wait' and w and o and len(w & o) / len(w | o) >= 0.6:
            raise ValueError(f'похожий вопрос уже ждёт ответа: пункт {k + 1} «{p["text"]}» — '
                             f'дополнить: set <задача> {k + 1} note="…", или повторить с --force')
    item = {'text': text, 'status': 'wait', 'q': True, 'asked': now_ms()}
    if session_id():
        item['by'] = session_id()   # ответ хук покажет этой сессии
    if note:
        item['note'] = note
    if opts:
        item['opts'] = opts
    task = tabs.update_task(cfg, dir_, lambda t: t.setdefault('plan', []).append(item))
    return len(task['plan'])


def draft(cfg, args):
    """Черновик ответа владельца: пустой текст — убрать. rephrase: строка (можно пустую) — «перефразировать проще»
    с комментарием, None — снять просьбу."""
    def fn(t):
        p = tabs.item(t.get('plan', []), tabs._n(args))
        if 'text' in args:
            text = str(args.get('text') or '').strip()
            if text:
                p['draft'] = text
            else:
                p.pop('draft', None)
        if 'rephrase' in args:
            if args['rephrase'] is None:
                p.pop('rephrase_draft', None)
            else:
                p['rephrase_draft'] = str(args['rephrase']).strip()
        if 'opt' in args:   # выбранный вариант ответа: текст варианта, @advice — как советует агент, @reword — проще
            if args['opt']:
                p['draft_opt'] = str(args['opt'])
            else:
                p.pop('draft_opt', None)
    tabs.update_task(cfg, args['dir'], fn)
    return {'ok': True}


def submit(cfg, args):
    """Все черновики задачи разом: ответы дописываются, агентам — answers.json и строка в events.jsonl."""
    sent = []

    def fn(t):
        for k, p in enumerate(t.get('plan', [])):
            text, rephrase, opt = p.pop('draft', None), p.pop('rephrase_draft', None), p.pop('draft_opt', None)
            # вариант + текст: текст — дополнение к варианту; без варианта текст — свой ответ; «проще» — просьба переписать
            if opt == '@reword':
                rephrase, text = text or '', None
            elif opt:
                label = 'Как советуешь' + (f' ({p["note"]})' if p.get('note') else '') if opt == '@advice' else opt
                text = label + (f' — {text}' if text else '')
            if text:
                p.setdefault('answers', []).append({'text': text, 'at': now_ms()})
            if rephrase is not None:
                p['rephrase'] = {'text': rephrase or 'проще', 'at': now_ms()}
            if text or rephrase is not None:
                p['seen'] = False
                sent.append((k, p))
        if not sent:
            return
        # очередь агентам — до записи задачи (внутри её замка): не записалась — задача не тронута, черновики
        # остались и отправка повторится; иначе ответ ушёл бы в task.json, а агент о нём не узнал (аудит 27.10)
        tid = t.get('id') or os.path.basename(args['dir'])
        with locked(_pending_path(cfg)):
            pending = read_json(_pending_path(cfg), {}) or {}
            for k, p in sent:
                stamps = [a['at'] for a in p.get('answers', [])] + ([p['rephrase']['at']] if p.get('rephrase') else [])
                pending[f'{tid}#{k + 1}'] = {'dir': args['dir'], 'n': k + 1, 'q': p['text'],
                                            'answers': [a['text'] for a in p.get('answers', [])],
                                            'rephrase': (p.get('rephrase') or {}).get('text'), 'at': max(stamps),
                                            **({'by': p['by']} if p.get('by') else {})}
            write_json(_pending_path(cfg), pending)
    task = tabs.update_task(cfg, args['dir'], fn)
    if not sent:
        return {'ok': True, 'sent': 0}
    tid = task.get('id') or os.path.basename(args['dir'])
    with open(os.path.join(cfg['state_dir'], 'events.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps({'kind': 'submit', 'task': tid, 'items': [k + 1 for k, _ in sent], 'at': now_ms()},
                           ensure_ascii=False) + '\n')
    return {'ok': True, 'sent': len(sent)}


def request(cfg, args):
    """Просьба владельца со страницы: пункт req (блок «Ваши просьбы») с seen=false и запись в answers.json —
    агент увидит её хуком, в unseen и при take как непрочитанную и предложит разобрать; ack — прочитано."""
    text = ' '.join(str(args.get('text', '')).split())
    if not text:
        raise ValueError('пустая просьба')
    at = now_ms()
    out = {}

    def fn(t):
        plan = t.setdefault('plan', [])
        plan.append({'text': text, 'req': True, 'at': time.strftime('%Y-%m-%d'), 'seen': False, 'owner_at': at})
        out['n'] = len(plan)
        out['tid'] = t.get('id') or os.path.basename(args['dir'])
        with locked(_pending_path(cfg)):   # до записи задачи, как в submit: не записалось — просьбы нет нигде
            pending = read_json(_pending_path(cfg), {}) or {}
            pending[f'{out["tid"]}#{out["n"]}'] = {'dir': args['dir'], 'n': out['n'], 'q': text, 'answers': [], 'kind': 'req', 'at': at}
            write_json(_pending_path(cfg), pending)
    tabs.update_task(cfg, args['dir'], fn)
    tid = out['tid']
    with open(os.path.join(cfg['state_dir'], 'events.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps({'kind': 'req', 'task': tid, 'items': [out['n']], 'at': at}, ensure_ascii=False) + '\n')
    return {'ok': True, 'n': out['n']}





def act_done(cfg, args):
    """Владелец отметил ожидаемое действие (пункт act) сделанным или «не буду» — со страницы. Пункт закрывается,
    агент видит отметку хуком и в unseen, как ответ: проверяет результат и подтверждает ack."""
    n, skip = int(args['index']) + 1, bool(args.get('skip'))
    note = ' '.join(str(args.get('note') or '').split())
    at, out = now_ms(), {}

    def fn(t):
        p = tabs.item(t.get('plan', []), n)
        if not p.get('act'):
            raise ValueError(f'пункт {n} — не ожидаемое действие')
        p.update({'status': 'skip' if skip else 'done', 'seen': False, 'owner_at': at})
        if note:
            p['owner_note'] = note
        out['tid'] = t.get('id') or os.path.basename(args['dir'])
        with locked(_pending_path(cfg)):
            pending = read_json(_pending_path(cfg), {}) or {}
            pending[f'{out["tid"]}#{n}'] = {'dir': args['dir'], 'n': n, 'q': p['text'], 'answers': [note] if note else [],
                                            'kind': 'act', 'skip': skip, 'at': at}
            write_json(_pending_path(cfg), pending)
    tabs.update_task(cfg, args['dir'], fn)
    with open(os.path.join(cfg['state_dir'], 'events.jsonl'), 'a', encoding='utf-8') as f:
        f.write(json.dumps({'kind': 'act', 'task': out['tid'], 'items': [n], 'skip': skip, 'at': at}, ensure_ascii=False) + '\n')
    return {'ok': True}


def reword(cfg, dir_, n, text):
    """Агент переписал вопрос (по просьбе «перефразировать»): прежний текст — в was, просьба снята."""
    tabs.update_task(cfg, dir_, lambda t: tabs.apply_item(cfg, t.get('plan', []), n, {'text': text}))


def ack(cfg, dir_, nums, done=False):
    """Агент учёл ответы: пункт seen=true, запись уходит из answers.json; done — заодно закрыть вопрос.
    Пункт без неподтверждённого ответа — ошибка, не меняется ничего."""
    def fn(t):
        plan = t.get('plan', [])
        empty = [n for n in nums if tabs.item(plan, n).get('seen') is not False]
        if empty:
            raise ValueError(f'пункт {", ".join(map(str, empty))} — ответа нет' +
                             (' (закрыть: set <задача> N status=done)' if done else ''))
        for n in nums:
            plan[n - 1]['seen'] = True
            if done:
                plan[n - 1]['status'] = 'done'
    task = tabs.update_task(cfg, dir_, fn)
    tid = task.get('id') or os.path.basename(dir_)
    with locked(_pending_path(cfg)):
        pending = read_json(_pending_path(cfg), {}) or {}
        for n in nums:
            pending.pop(f'{tid}#{n}', None)
        write_json(_pending_path(cfg), pending)


def _line(key, r):
    """Ответ строкой с готовой командой: ack a 5 done / set a 7 text="…" и ack."""
    tid, _, n = key.rpartition('#')
    if r.get('kind') == 'act':
        what = 'НЕ БУДЕТ ДЕЛАТЬ' if r.get('skip') else 'ВЫПОЛНИЛ'
        return (f'{key}: ВЛАДЕЛЕЦ {what} ОЖИДАЕМОЕ ДЕЙСТВИЕ: {r["q"]}' + (f' | комментарий: {r["answers"][0]}' if r['answers'] else '')
                + f' → проверь результат (или учти отказ), затем ack {tid} {n}')
    if r.get('kind') == 'req':
        return (f'{key}: НОВАЯ ПРОСЬБА ВЛАДЕЛЬЦА: {r["q"]} → предложи владельцу разобрать (взять в работу, '
                f'уточнить или отложить), затем ack {tid} {n}')
    line = f'{key}: {r["q"]}'
    if r['answers']:
        line += f' | ответ: {" / ".join(r["answers"])}'
    if r.get('rephrase'):
        line += f' | ПЕРЕФРАЗИРОВАТЬ ({r["rephrase"]}) → set {tid} {n} text="…", затем ack {tid} {n}'
    else:
        line += f' → ack {tid} {n} done'
    return line


def _mine(cfg, sid, cwd):
    """Признак «задача этой сессии» для записи answers.json: вопрос задан ею, её захват, session в шапке
    HANDOFF или cwd хука внутри каталога задачи."""
    claimed = set()
    for f in os.listdir(os.path.join(cfg['state_dir'], 'claims')) if os.path.isdir(os.path.join(cfg['state_dir'], 'claims')) else []:
        c = read_json(os.path.join(cfg['state_dir'], 'claims', f)) or {}
        if c.get('sid') == sid:
            claimed.add(c.get('task'))
    cwd = os.path.normcase(os.path.normpath(cwd)) + os.sep if cwd else ''
    hand = {}

    def test(key, r):
        if r.get('by') == sid or key.rpartition('#')[0] in claimed:
            return True
        d = os.path.normcase(os.path.normpath(r['dir'])) + os.sep
        if cwd and cwd.startswith(d):
            return True
        if d not in hand:
            hand[d] = front_matter(os.path.join(r['dir'], cfg['item']['handoff_file']))[0].get('session', '')
        return hand[d] == sid
    return test


def unseen(cfg, sid=None, cwd='', mine=False):
    """Неподтверждённые ответы строками с готовыми командами. mine — только задачи сессии sid,
    остальные — одной строкой."""
    pending = read_json(_pending_path(cfg), {}) or {}
    test = _mine(cfg, sid, cwd) if mine and sid else None
    out, other = [], 0
    for key, r in sorted(pending.items()):
        if test and not test(key, r):
            other += 1
        else:
            out.append(_line(key, r))
    if other:
        out.append(f'ещё ответов в других задачах: {other} (task.py unseen)')
    return out


REMIND_EVERY, REMIND_MS = 5, 600000   # напоминание о неподтверждённых: раз в 5 вводов или 10 минут


def hook(cfg, sid, cwd=''):
    """Строки для хука UserPromptSubmit: полный текст ответа задач этой сессии — один раз; напоминание
    о неподтверждённых и счёт ответов чужих задач — не чаще REMIND_EVERY вводов / REMIND_MS."""
    pending = read_json(_pending_path(cfg), {}) or {}
    if not pending:
        return []
    path = _seen_path(cfg, sid)
    shown = read_json(path, {}) or {}
    mark = shown.get('_hook') or {'n': 0, 'at': 0}
    test = _mine(cfg, sid, cwd)
    new, already, other = [], [], 0
    for key, r in sorted(pending.items()):
        if not test(key, r):
            other += 1
        elif shown.get(key) == r['at']:
            already.append(key)
        else:
            new.append(_line(key, r))
            shown[key] = r['at']
    mark['n'] += 1
    remind = mark['n'] >= REMIND_EVERY or now_ms() - mark['at'] >= REMIND_MS
    out = []
    if new or remind:
        out = new + ([f'ещё не подтверждены (текст был выше): {", ".join(already)}'] if already else []) \
            + ([f'ещё ответов в других задачах: {other} (task.py unseen)'] if other else [])
        if out:
            mark = {'n': 0, 'at': now_ms()}
    shown['_hook'] = mark
    write_json(path, shown)
    return out
