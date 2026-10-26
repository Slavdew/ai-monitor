"""Общее для сервера и CLI: конфиг, пути, атомарная запись, шапка Markdown.

Источник данных — модуль monitor.sources.<имя> с функцией collect(cfg, src, ctx) -> dict
и необязательным словарём ACTIONS {имя: fn(cfg, src, args) -> dict}. Порядок и включение —
cfg["sources"]; ctx — результаты источников, собранных раньше в этом же проходе.
"""
import contextlib
import importlib
import json
import os
import re
import tempfile
import time

HOME = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def expand(path, base=HOME):
    path = os.path.expanduser(os.path.expandvars(path.replace('%LOCALAPPDATA%', os.environ.get('LOCALAPPDATA', ''))))
    return os.path.normpath(path if os.path.isabs(path) else os.path.join(base, path))


def merge(base, over):
    """Словари сливаются вглубь, всё остальное (в том числе списки) заменяется."""
    out = dict(base)
    for k, v in over.items():
        out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def read_json(path, default=None):
    try:
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def locked(path, tries=40, wait=0.05, stale=10):
    """Замок на чтение-правку-запись файла: <path>.lock через O_EXCL. Два агента (или агент и страница)
    правят одну задачу — без замка вторая запись затирает первую. Замок старше stale с — брошенный, снимается."""
    lock = path + '.lock'
    os.makedirs(os.path.dirname(lock) or '.', exist_ok=True)
    for _ in range(tries):
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > stale:
                    os.remove(lock)
                    continue
            except OSError:
                pass
            time.sleep(wait)
    else:
        raise TimeoutError(f'файл занят другим писателем: {path}')
    try:
        os.close(fd)
        yield
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(tmp, path)


def load_config():
    """config.default.json, поверх — личный файл по ссылке из config.local.json (вне git)."""
    cfg = read_json(os.path.join(HOME, 'config.default.json'), {})
    local = read_json(os.path.join(HOME, 'config.local.json'), {})
    tabs_base = HOME   # относительные корни — от файла, где вкладки объявлены
    if local.get('settings'):
        path = expand(local['settings'])
        personal = read_json(path, {})
        cfg = merge(cfg, personal)
        if 'tabs' in personal:
            tabs_base = os.path.dirname(path)
    for tab in cfg.get('tabs', []):
        tab['root'] = expand(tab['root'], tabs_base)
    cfg['state_dir'] = expand(cfg.get('state_dir', '%LOCALAPPDATA%/claude-task-monitor'))
    return cfg


def sources(cfg):
    """[(имя, настройки, модуль)] включённых источников в порядке конфига."""
    out = []
    for name, src in cfg.get('sources', {}).items():
        if src.get('enabled', True):
            out.append((name, src, importlib.import_module(f'monitor.sources.{name}')))
    return out


def model_on(cfg, feature):
    """Функция, тратящая токены модели, включена: общий рубильник model.enabled и её собственный флаг."""
    m = cfg.get('model', {})
    return bool(m.get('enabled', True) and m.get(feature, True))


def front_matter(path):
    """YAML-шапка Markdown, только плоские `ключ: значение` — больше HANDOFF.md не нужно."""
    try:
        with open(path, encoding='utf-8-sig') as f:
            text = f.read(16384)
    except OSError:
        return {}, ''
    meta = {}
    m = re.match(r'---\r?\n(.*?)\r?\n---\r?\n?', text, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(':')
            if sep and not line.startswith((' ', '#')):
                meta[k.strip()] = v.split(' #')[0].strip().strip('"\'')
        text = text[m.end():]
    return meta, text


def session_id():
    """Сессия агента: TASK_SESSION (любой агент, в task.py ещё --sid), иначе CLAUDE_CODE_SESSION_ID."""
    return os.environ.get('TASK_SESSION') or os.environ.get('CLAUDE_CODE_SESSION_ID', '')


def log_call(cfg, via, action, args, ok, ms, out_chars=0, in_chars=0, err=''):
    """Журнал вызовов панели: state_dir/calls.jsonl, строка на вызов — для статистики по функциям.

    via — cli / page; who — agent, если у вызова есть сессия (session_id), иначе human.
    in/out_chars — размер команды (со stdin) и вывода: ≈ токены, которые вызов стоил модели (÷ ~3); err — текст ошибки.
    """
    sid = session_id() if via == 'cli' else ''   # сервер мог стартовать из сессии
    rec = {'ts': now_ms(), 'via': via, 'who': 'agent' if sid else 'human', 'sid': sid,
           'action': action, 'args': args, 'ok': ok, 'ms': ms, 'in_chars': in_chars, 'out_chars': out_chars}
    if err:
        rec['err'] = err[:300]
    try:
        os.makedirs(cfg['state_dir'], exist_ok=True)
        with open(os.path.join(cfg['state_dir'], 'calls.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass   # журнал не мешает работе


def now_ms():
    return int(time.time() * 1000)
