"""Вкладки: каждый корень из cfg["tabs"] обходится, элемент — каталог с одним из маркеров.

Родитель элемента — links.parent из task.json (ссылка), иначе ближайший каталог-предок с
маркером. Статус, «дальше» и команда продолжения — из шапки HANDOFF.md.
"""
import os
import re
import shlex

from monitor.core import CORE_API, front_matter, locked, read_json, text_paths, write_json


def _title_goal(text):
    title, para = '', []
    for line in text.splitlines():
        s = line.strip()
        if not title and s.startswith('# '):
            title = s[2:].strip()
        elif title and s and not s.startswith(('#', '```', '|', 'Статус:')):
            para.append(s)
        elif para:
            break
    return title, ' '.join(para)[:500]


def _stale(dir_, since, it, limit=2000):
    """Сколько файлов каталога новее HANDOFF — та же мысль, что проверка свежести в скилле handoff."""
    skip = set(it['skip_dirs'])
    exclude = {it['handoff_file'], it['journal_file'], it['task_file']}
    n = seen = 0
    for root, dirs, files in os.walk(dir_):
        dirs[:] = [d for d in dirs if d not in skip]
        for f in files:
            if f in exclude or f.endswith('.log'):
                continue
            seen += 1
            try:
                n += os.path.getmtime(os.path.join(root, f)) > since
            except OSError:
                pass
            if seen >= limit:
                return n
    return n


def plan_states(plan, mode):
    """Состояние пункта: done / skip / doing / ready (можно начинать) / waiting (ждёт пунктов `after`).

    `after` — номера пунктов с 1. Не задан: в режиме sequential пункт ждёт предыдущий, в parallel —
    ничего. `parallel: true` у готового пункта — рядом есть ещё готовые, их можно вести одновременно.
    """
    out = [dict(p) for p in plan]
    finished = lambda k: out[k].get('status') in ('done', 'skip', 'later') or out[k].get('q')   # вопрос и отложенное не держат очередь работ
    for k, p in enumerate(out):
        after = p.get('after')
        if after is None:
            after = [k] if mode == 'sequential' and k else []
        p['waits'] = [n for n in after if 1 <= n <= len(out) and n != k + 1 and not finished(n - 1)]
        st = p.get('status') or 'todo'
        p['state'] = st if st in ('done', 'skip', 'doing', 'wait', 'later') else ('waiting' if p['waits'] else 'ready')
    active = [p for p in out if p['state'] in ('ready', 'doing')]
    for p in active:
        p['parallel'] = len(active) > 1
    return out


def _docs(dir_, task, it):
    """Документы задачи: links.docs — приложенные (на чём задача основана), links.artifacts — артефакты
    (промежуточные результаты), оба «Название::путь-или-url»; .md/.html каталога задачи, не названные явно, —
    артефакты (без модели). kind: doc | artifact."""
    out, seen = [], set()
    for kind, key in (('doc', 'docs'), ('artifact', 'artifacts')):
        for x in task.get('links', {}).get(key) or []:
            t, _, u = x.rpartition('::') if isinstance(x, str) else (x.get('title', ''), '', x.get('url', ''))
            out.append({'title': t.strip(), 'url': u.strip(), 'kind': kind})
            seen.add(os.path.normcase(u.strip()))
    try:
        names = sorted(f for f in os.listdir(dir_) if f.lower().endswith(tuple(it['doc_ext'])))
    except OSError:
        names = []
    for f in names[:it['max_docs']]:
        p = os.path.join(dir_, f)
        if os.path.normcase(p) not in seen:
            out.append({'title': '', 'url': p, 'kind': 'artifact'})
    return out


def read_item(dir_, tab, cfg):
    it = cfg['item']
    task = read_json(os.path.join(dir_, it['task_file']), {}) or {}
    hpath = os.path.join(dir_, it['handoff_file'])
    hmeta, _ = front_matter(hpath)
    title, goal = '', ''
    readme = os.path.join(dir_, 'README.md')
    if os.path.exists(readme):
        title, goal = _title_goal(front_matter(readme)[1])
    plan = plan_states(task.get('plan', []), task.get('plan_mode') or cfg.get('plan_mode', 'sequential'))
    work = [p for p in plan if not p.get('q')]   # вопросы владельцу — не работа, в процент не входят
    weights = [p.get('weight', 1) for p in work if p.get('status') not in ('skip', 'later')]
    done = sum(p.get('weight', 1) for p in work if p.get('status') == 'done')
    # три вопроса с одного взгляда: что важнее (priority), сколько осталось (из плана), на каком этапе
    # (stage или раздел пункта в работе: «раздел 3/9»)
    remain = sum(1 for p in plan if p.get('status', 'todo') in ('todo', 'doing') and not p.get('q'))         + sum(1 for p in plan if p.get('status') == 'wait')
    secs = list(dict.fromkeys(p.get('section') for p in work if p.get('section')))
    cur = next((p for p in work if p.get('status') == 'doing'), None)         or next((p for p in work if p.get('state') == 'ready'), None)
    stage = task.get('stage') or (cur or {}).get('section') or ''
    tpath = os.path.join(dir_, it['task_file'])
    item = {
        'id': task.get('id') or hmeta.get('task') or os.path.basename(dir_),
        'tab': tab['id'], 'dir': dir_,
        'title': task.get('title') or hmeta.get('title') or title or os.path.basename(dir_),
        'project': task.get('project') or hmeta.get('project') or '',
        'goal': task.get('goal') or goal,
        'desc': task.get('desc', ''),
        'health': task.get('health', ''),     # ok | risk | blocked — ставит агент
        'blocked': task.get('blocked', ''),   # почему стоит (внешнее: ИТ, руководитель)
        'risk': task.get('risk', ''),         # в чём риск: панель показывает по щелчку на «риск»
        'priority': task.get('priority') or 3,
        'stage': stage,
        'stage_n': f'{secs.index(stage) + 1}/{len(secs)}' if stage in secs and len(secs) > 1 else '',
        'current': (cur or {}).get('text', ''),
        'remain': remain if plan else None,
        'changed': int(os.path.getmtime(tpath) * 1000) if os.path.exists(tpath) else 0,
        'plan': plan,
        'percent': round(100 * done / sum(weights)) if weights else None,
        'links': task.get('links', {}),
        'docs': _docs(dir_, task, it),
        'status': hmeta.get('status') or task.get('status') or '',
        'next': hmeta.get('next') or task.get('next', ''),
        'updated': hmeta.get('updated', ''),
        'session': hmeta.get('session', ''),
        'resume': '',
        'stale': 0,
    }
    if os.path.exists(hpath):
        item['resume'] = hmeta.get('resume') or f'Продолжаем работу по {hpath}'
        item['stale'] = _stale(dir_, os.path.getmtime(hpath), it)
    # пути к файлам в тексте — страница делает их ссылками; только те, что ведут на существующий файл
    texts = [item[k] for k in ('goal', 'desc', 'next', 'current', 'blocked', 'risk', 'stage')]
    for p in plan:
        texts += [v for k, v in p.items() if isinstance(v, str) and k not in ('status', 'state', 'who', 'draft')]
        texts += [a.get('text', '') if isinstance(a, dict) else str(a) for a in p.get('answers') or []]
    item['paths'] = text_paths(cfg, [t for t in texts if t], dir_)
    return item


def scan_tab(tab, cfg):
    it = cfg['item']
    items, root = [], tab['root']
    if not os.path.isdir(root):
        return items, f'нет каталога {root}'
    base_depth = root.rstrip('\\/').count(os.sep)
    skip = set(it['skip_dirs'])
    skip_rel = {os.path.normcase(os.path.join(root, p)) for p in tab.get('skip', [])}   # пути внутри корня, которые ведёт другая вкладка
    for dir_, dirs, files in os.walk(root):
        depth = dir_.count(os.sep) - base_depth
        dirs[:] = sorted(d for d in dirs if d not in skip and not d.startswith('.')
                         and os.path.normcase(os.path.join(dir_, d)) not in skip_rel)
        if depth >= it['max_depth']:
            dirs[:] = []
        if depth and any(m in files for m in it['markers']):
            items.append(read_item(dir_, tab, cfg))
    return items, ''


def collect(cfg, src, ctx):
    items, errors = [], []
    for tab in cfg.get('tabs', []):
        found, err = scan_tab(tab, cfg)
        items += found
        if err:
            errors.append(err)
    by_dir = {os.path.normcase(i['dir']): i for i in items}
    ids = {i['id'] for i in items}
    for i in items:
        parent = i['links'].get('parent')
        if parent and parent in ids:
            i['parent'], i['parent_kind'] = parent, 'link'
            continue
        up = os.path.dirname(i['dir'])
        while up != os.path.dirname(up):
            if os.path.normcase(up) in by_dir:
                i['parent'], i['parent_kind'] = by_dir[os.path.normcase(up)]['id'], 'dir'
                break
            up = os.path.dirname(up)
    done = {i['id'] for i in items if i['status'] == 'done' or i['percent'] == 100}
    for i in items:   # блокирующие связи задач: ждёт, пока блокирующая не done
        i['waits'] = [b for b in i['links'].get('blocked_by') or [] if b not in done]
    tabs = [{'id': t['id'], 'title': t['title'], 'root': t['root'], 'group': t.get('group', '')} for t in cfg.get('tabs', [])]
    return {'tabs': tabs, 'items': items, 'errors': errors, 'plan_statuses': cfg['plan_statuses']}


# --- запись: task.json в каталоге задачи; панель и task.py — через эти функции ---

def _task_path(cfg, dir_):
    return os.path.join(dir_, cfg['item']['task_file'])


def update_task(cfg, dir_, fn):
    """Прочитать, поправить fn, записать — под замком. Исключение в fn — файл не тронут.
    v — версия формата (CORE_API): файл новее ядра (правило другое ядро — ветка, worktree, коллега) старое
    ядро не перезаписывает, чтобы не потерять незнакомые поля (A17)."""
    path = _task_path(cfg, dir_)
    with locked(path):
        task = read_json(path, {}) or {}
        if int(task.get('v') or 0) > CORE_API:
            raise ValueError(f'task.json формата v{task["v"]}, ядро панели — v{CORE_API}: обновите панель')
        fn(task)
        task['v'] = CORE_API
        write_json(path, task)
    return task


def item(plan, n):
    """Пункт плана по номеру с 1 — единственная проверка номера для CLI и страницы."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        raise ValueError(f'номер пункта «{n}» — нужно число с 1') from None
    if not 1 <= n <= len(plan):
        raise ValueError(f'нет пункта {n} (всего {len(plan)})')
    return plan[n - 1]


def _n(args):
    """Номер пункта из действия страницы: index с 0 (страница) или n с 1."""
    if 'n' in args:
        return int(args['n'])
    if 'index' not in args:
        raise ValueError('нужен index (с 0) или n (с 1)')
    return int(args['index']) + 1


ITEM_KEYS = ('text', 'status', 'note', 'section', 'who', 'after')


def apply_item(cfg, plan, n, fields):
    """Поля пункта N (с 1): None — убрать поле. text у вопроса — прежний в was, просьба «проще» снята;
    status=wait — только у вопроса; after — номера пунктов (список или «3,4»; пусто — можно сразу)."""
    p = item(plan, n)
    for k, v in fields.items():
        if k not in ITEM_KEYS:
            raise ValueError(f'ключ {k}: допустимы {", ".join(ITEM_KEYS)}')
        if v is None:
            p.pop(k, None)
            continue
        if k == 'status':
            if v not in cfg['plan_statuses']:
                raise ValueError(f'статус {v}: допустимы {", ".join(cfg["plan_statuses"])}')
            if v == 'wait' and not p.get('q'):
                raise ValueError(f'пункт {n} — не вопрос: wait только у вопроса, новый вопрос — task.py ask')
        elif k == 'after':
            v = after_list(v, len(plan), n)
        elif k == 'text':
            if not str(v).strip():
                raise ValueError('пустой текст')
            if p.get('q') and v != p.get('text'):
                p['was'] = p.get('text', '')   # страница покажет «было → стало»
                p.pop('rephrase', None)
        p[k] = v
    return p


def after_list(v, total, n=None):
    """«3,4» / [3, 4] → [3, 4] с проверкой диапазона; пусто — []."""
    if isinstance(v, str):
        v = [x for x in re.split(r'[,;\s]+', v) if x]
    out = []
    for x in v:
        try:
            m = int(x)
        except (TypeError, ValueError):
            raise ValueError(f'after: «{x}» — нужны номера пунктов через запятую') from None
        if not 1 <= m <= total:
            raise ValueError(f'after: нет пункта {m} (всего {total})')
        if m == n:
            raise ValueError(f'after: пункт {n} не может ждать сам себя')
        out.append(m)
    return out


def set_plan(cfg, args):
    update_task(cfg, args['dir'], lambda t: apply_item(cfg, t.setdefault('plan', []), _n(args), {'status': args['status']}))
    return {'ok': True}


BATCH_KEYS = ITEM_KEYS


def batch(cfg, dir_, lines):
    """Пачка правок плана одной записью (под замком, как update_task). Строки:
      # Раздел                      — раздел для следующих новых пунктов
      + текст [| ключ=значение …]   — новый пункт (todo)
      N ключ=значение …             — поля пункта N, как `set T N …`; значение null — убрать поле
    Ключи — ITEM_KEYS. Ошибка в любой строке — не меняется ничего. Возвращает номера новых пунктов."""
    ops, section = [], None
    for ln, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s:
            continue
        try:
            if s.startswith('#'):
                section = s.lstrip('#').strip() or None
                continue
            if s.startswith('+'):
                text, pairs = _split_add(s[1:])
                fields = {'text': text.strip(), 'status': 'todo', **({'section': section} if section else {})}
                fields.update(pairs)
                if not fields['text']:
                    raise ValueError('пустой текст')
                ops.append((ln, s, 'add', None, fields))
            else:
                n, _, rest = s.partition(' ')
                if not n.isdigit():
                    raise ValueError('строка — «# Раздел», «+ текст | ключ=…» или «N ключ=значение»')
                ops.append((ln, s, 'set', int(n), _pairs(rest)))
        except ValueError as e:
            raise ValueError(f'строка {ln} «{s}»: {e}') from None
    added = []

    def fn(t):
        plan = t.setdefault('plan', [])
        for ln, s, kind, n, fields in ops:
            try:
                if kind == 'add':
                    plan.append({'text': fields['text'], 'status': 'todo'})
                    n = len(plan)
                    added.append(n)
                    fields = {k: v for k, v in fields.items() if k != 'text' and v is not None}
                apply_item(cfg, plan, n, fields)
            except ValueError as e:
                raise ValueError(f'строка {ln} «{s}»: {e}') from None
    update_task(cfg, dir_, fn)   # ошибка в fn — до записи: файл не тронут
    return {'ok': True, 'added': added, 'changed': sum(o[2] == 'set' for o in ops)}


def _pairs(s):
    out = {}
    lex = shlex.shlex(s, posix=True)
    lex.whitespace_split, lex.escape = True, ''   # обратный слеш — буква: в значениях пути Windows
    last = None
    for tok in lex:
        k, eq, v = tok.partition('=')
        if eq and k in ITEM_KEYS:
            out[k], last = (None if v == 'null' else v), k
        elif last and out[last] is not None:   # note=два слова без кавычек: слово — продолжение значения
            out[last] += ' ' + tok
        else:
            raise ValueError(f'«{tok}»: нужно ключ=значение, ключи {", ".join(ITEM_KEYS)}; '
                             'пример: 3 status=done note="два слова"')
    return out


def _split_add(s):
    """«текст | ключ=…» → (текст, поля): поля — после первого «|», за которым идут ключ=значение;
    «|» внутри текста без ключей после него — часть текста."""
    err = None
    for m in re.finditer(r'\|', s):
        try:
            return s[:m.start()], _pairs(s[m.end():])
        except ValueError as e:
            if re.match(r'\s*(%s)=' % '|'.join(ITEM_KEYS), s[m.end():]):
                err = err or e   # похоже на поля, но не разбирается (кавычка не закрыта) — не прятать в текст
    if err:
        raise err
    return s, {}


TASK_FIELDS = ('title', 'project', 'goal', 'desc', 'next', 'stage', 'priority', 'health', 'blocked', 'risk', 'status', 'plan_mode')
LINK_LISTS = ('related', 'blocks', 'blocked_by', 'docs', 'artifacts')   # links.parent — строка


def field_name(field):
    """title → title, related / links.related → links.related; иначе ошибка со списком полей."""
    if field in TASK_FIELDS or field.startswith('links.') and field[6:] in ('parent', *LINK_LISTS):
        return field
    if field in ('parent', *LINK_LISTS):
        return 'links.' + field
    raise ValueError(f'поле {field}: допустимы {", ".join(TASK_FIELDS)}, parent, {", ".join(LINK_LISTS)}')


def split_list(v, sep=';'):
    return [x.strip() for x in v.split(sep) if x.strip()] if isinstance(v, str) else list(v or [])


def check_field(field, value):
    """Значение поля задачи после проверки: priority — число, списки links.* — список (строку режет по «;»)."""
    if field == 'health' and value not in ('', 'ok', 'risk', 'blocked'):
        raise ValueError('health: ok, risk, blocked или пусто')
    if field == 'priority':
        if str(value) not in ('1', '2', '3', '4'):
            raise ValueError('priority: 1 срочно, 2 важно, 3 обычно, 4 потом')
        return int(value)
    if field == 'plan_mode' and value not in ('sequential', 'parallel'):
        raise ValueError('plan_mode: sequential или parallel')
    if field[6:] in LINK_LISTS and field.startswith('links.'):
        return split_list(value)
    return value


def set_field(cfg, args):
    field = field_name(args['field'])
    value = check_field(field, args['value'])

    def fn(t):
        if field.startswith('links.'):
            t.setdefault('links', {})[field[6:]] = value
        else:
            t[field] = value
    update_task(cfg, args['dir'], fn)
    return {'ok': True}


def set_after(cfg, args):
    update_task(cfg, args['dir'], lambda t: apply_item(cfg, t.setdefault('plan', []), _n(args), {'after': args['after']}))
    return {'ok': True}


def _questions():
    from monitor import questions   # questions импортирует этот модуль
    return questions


ACTIONS = {'after': lambda cfg, src, a: set_after(cfg, a), 'plan': lambda cfg, src, a: set_plan(cfg, a), 'field': lambda cfg, src, a: set_field(cfg, a),
           'draft': lambda cfg, src, a: _questions().draft(cfg, a), 'submit': lambda cfg, src, a: _questions().submit(cfg, a),
           'request': lambda cfg, src, a: _questions().request(cfg, a)}
