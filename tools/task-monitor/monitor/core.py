"""Общее для сервера и CLI: конфиг, пути, атомарная запись, шапка Markdown, реестр модулей.

Источник данных — функция collect(cfg, src, ctx) -> dict и необязательный словарь
ACTIONS {имя: fn(cfg, src, args) -> dict}: у ядра — monitor/sources/tabs.py, у модулей —
modules/<имя>/source.py рядом с module.json. ctx — результаты источников, собранных раньше в
этом же проходе (порядок — по after манифеста).
"""
import contextlib
import hashlib
import importlib
import importlib.util
import json
import os
import re
import sys
import tempfile
import threading
import time
import types

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


def read_json_strict(path):
    """Как read_json, но «файла нет» ({}) и «файл испорчен» (ошибка) различаются: битый файл не перезаписывать
    пустым — план пропал бы молча (аудит надёжности 27.10)."""
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
    except (UnicodeDecodeError, ValueError) as e:
        raise ValueError(f'{path} испорчен ({type(e).__name__}) — правка не записана, исправьте файл или верните из git') from None


def write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write('\n')
        # на Windows replace падает, пока файл открыт читателем (сервер, антивирус, OneDrive) — повтор до ~1 с
        for k in range(20):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if k == 19:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


PERSONAL = '%LOCALAPPDATA%/claude-task-monitor/settings.json'


def load_config():
    """config.default.json, поверх — личный файл (A7): ссылка из config.local.json (вне git), иначе
    TASK_MONITOR_SETTINGS, иначе %LOCALAPPDATA%/claude-task-monitor/settings.json, если он есть."""
    cfg = read_json(os.path.join(HOME, 'config.default.json'), {})
    local = read_json(os.path.join(HOME, 'config.local.json'), {})
    tabs_base = HOME   # относительные корни — от файла, где вкладки объявлены
    path = local.get('settings') or os.environ.get('TASK_MONITOR_SETTINGS') or ''
    if not path and os.path.exists(expand(PERSONAL)):
        path = PERSONAL
    if path:
        path = expand(path)
        personal = read_json(path, {})
        cfg = merge(cfg, personal)
        if 'tabs' in personal:
            tabs_base = os.path.dirname(path)
    for tab in cfg.get('tabs', []):
        tab['root'] = expand(tab['root'], tabs_base)
    cfg['state_dir'] = expand(cfg.get('state_dir', '%LOCALAPPDATA%/claude-task-monitor'))
    return cfg


def in_tab_root(cfg, path):
    """Путь внутри корня какой-нибудь вкладки (каталог задачи панели) — после realpath: без выхода через .. и junction."""
    n = os.path.normcase(os.path.realpath(path))
    for t in cfg.get('tabs', []):
        r = os.path.normcase(os.path.realpath(t['root'])).rstrip('\\/')
        if n == r or n.startswith(r + os.sep):
            return True
    return False


def allowed_file(cfg, path):
    """Полный путь файла, если он есть и лежит в корне вкладки или в file_roots, иначе None."""
    if not os.path.isfile(path):   # дешёвая проверка первой: realpath на Windows дорог
        return None
    p = os.path.realpath(path)
    n = os.path.normcase(p)
    for r in _real_roots(cfg):
        if n == r or n.startswith(r + '\\'):
            return p
    return None


_roots_cache = {}


def _real_roots(cfg):
    """realpath корней вкладок и file_roots — один раз на набор корней, не на каждый путь (аудит 27.10)."""
    key = tuple(t['root'] for t in cfg.get('tabs', [])) + tuple(cfg.get('file_roots', []))
    if key not in _roots_cache:
        _roots_cache[key] = [os.path.normcase(os.path.realpath(r)).rstrip('\\') for r in key]
    return _roots_cache[key]


# пути к файлам в тексте задачи: C:\x\y.md, docs/a.md, `путь с пробелами`, [имя](путь), голое имя.md
_ABS = r'[A-Za-z]:[\\/][^\s`\'"<>|*?«»()\[\]]+'
_REL = r'(?<![\w./\\:-])(?:[\w.-]+[\\/])+[\w.-]+\.[A-Za-z0-9]{1,6}'
_BARE = r'(?<![\w./\\:-])[\w-][\w.-]*\.(?:md|markdown|html?|txt|json|log|pdf|png|jpe?g|svg|py|ps1|js|mjs|cmd)\b'
PATH_RE = re.compile(rf'`([^`\n]+)`|\]\(([^)\s]+)\)|({_ABS})|({_REL})|({_BARE})')


def text_paths(cfg, texts, base):
    """{как написано: полный путь} для путей из текстов, которые ведут на существующий разрешённый файл.
    Относительный путь ищется от каталога задачи и его предков, затем от корней вкладок."""
    bases, up = [], base
    while up and up not in bases:
        bases.append(up)
        up = os.path.dirname(up)
    bases += [t['root'] for t in cfg.get('tabs', [])]
    out, tried, now = {}, set(), time.time()
    for text in texts:
        for m in PATH_RE.finditer(text):
            raw = next(g for g in m.groups() if g).rstrip('.,;:')
            if raw in tried or not re.search(r'\.[A-Za-z0-9]{1,6}$', raw):
                continue
            tried.add(raw)   # и ненайденный путь второй раз не ищем
            hit = _path_cache.get((base, raw))
            if hit and now - hit[1] < PATH_TTL:   # тик сервера — раз в 2 с, файлы меняются реже
                p = hit[0]
            else:
                rel, p = raw.replace('/', '\\'), None
                for b in [''] if re.match(r'[A-Za-z]:\\', rel) else bases:
                    p = allowed_file(cfg, os.path.join(b, rel) if b else rel)
                    if p:
                        break
                _path_cache[(base, raw)] = (p, now)
            if p:
                out[raw] = p
    return out


_path_cache, PATH_TTL = {}, 30


# --- реестр модулей (decisions.md A1–A4) ---
# Ядро: источник tabs (monitor/sources). Модуль — каталог с module.json и source.py (collect, ACTIONS)
# в modules/ панели или в каталогах cfg["module_dirs"]. Настройки модуля: settings манифеста, поверх —
# cfg["sources"][имя]. Сломанный модуль не валит панель: ошибка — в modules()[имя]["error"].
CORE_API = 1
CORE_SOURCES = ('tabs',)
_registry = {}


def _tree_hash(d):
    h = hashlib.sha1()
    for f in sorted(os.listdir(d)):
        p = os.path.join(d, f)
        if os.path.isfile(p):
            h.update(f.encode())
            with open(p, 'rb') as fh:
                h.update(fh.read())
    return h.hexdigest()[:10]


def modules(cfg, reload=False):
    """{имя: {name, dir, manifest, mod, error, hash, core}} — собирается один раз на процесс."""
    if _registry and not reload:
        return _registry
    _registry.clear()
    pkg = sys.modules.setdefault('tm_modules', types.ModuleType('tm_modules'))   # модули — tm_modules.<имя>
    pkg.__path__ = []
    for name in CORE_SOURCES:
        _registry[name] = {'name': name, 'core': True, 'dir': os.path.join(HOME, 'monitor', 'sources'), 'manifest': {'api': CORE_API},
                           'mod': importlib.import_module(f'monitor.sources.{name}'), 'error': '', 'hash': ''}
    dirs = [os.path.join(HOME, 'modules')] + [expand(d) for d in cfg.get('module_dirs', [])]
    for base in dirs:
        for sub in sorted(os.listdir(base)) if os.path.isdir(base) else []:
            d = os.path.join(base, sub)
            man = read_json(os.path.join(d, 'module.json'))
            if not isinstance(man, dict):
                continue
            name = man.get('name') or sub
            rec = {'name': name, 'core': False, 'dir': d, 'manifest': man, 'mod': None, 'error': '', 'hash': _tree_hash(d)}
            prev = _registry.get(name)
            if prev and man.get('replaces') != name:
                rec['error'] = f'имя {name} уже занято модулем из {prev["dir"]} (нужно "replaces": "{name}")'
                _registry[f'{name}@{d}'] = rec
                continue
            if man.get('api') != CORE_API:
                rec['error'] = f'модуль для api {man.get("api")}, ядро — {CORE_API}'
            else:
                try:
                    spec = importlib.util.spec_from_file_location(f'tm_modules.{name}', os.path.join(d, 'source.py'))
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = mod
                    spec.loader.exec_module(mod)
                    rec['mod'] = mod
                except Exception as e:   # сломанный модуль — запись с ошибкой, не падение
                    rec['error'] = f'{type(e).__name__}: {e}'
            _registry[name] = rec
    return _registry


def module_settings(cfg, name):
    rec = modules(cfg).get(name) or {}
    return merge(rec.get('manifest', {}).get('settings', {}), cfg.get('sources', {}).get(name, {}))


def module_on(cfg, name):
    rec = modules(cfg).get(name)
    return bool(rec and rec['mod'] and module_settings(cfg, name).get('enabled', rec['manifest'].get('enabled', True)))


def sources(cfg):
    """[(имя, настройки, модуль)] включённых и загрузившихся источников: ядро, затем модули по after
    (кто читает ctx другого — после него), при равенстве — по имени."""
    recs = {n: r for n, r in modules(cfg).items() if '@' not in n and module_on(cfg, n)}
    order, seen = [], set()

    def visit(n, path=()):
        if n in seen or n not in recs or n in path:
            return
        for dep in recs[n]['manifest'].get('after', []):
            visit(dep, path + (n,))
        seen.add(n)
        order.append(n)
    for n in [*CORE_SOURCES, *sorted(recs)]:
        visit(n)
    out = []
    for n in order:
        src = module_settings(cfg, n)
        if recs[n]['manifest'].get('every_sec') and 'every_sec' not in src:
            src['every_sec'] = recs[n]['manifest']['every_sec']
        out.append((n, src, recs[n]['mod']))
    return out


HOOK_BUDGET, HOOK_TOTAL, HOOK_LINES = 0.3, 1.0, 12   # с на модуль, с на всех, строк от модуля


def module_hooks(cfg, data):
    """Вклад модулей в хук UserPromptSubmit (A6, A15): hook.py модулей с "hooks": true в манифесте,
    hook(cfg, src, data) -> str. Все — в своих потоках сразу; кто не успел к бюджету — молчит в этот раз,
    ошибка модуля — тоже молчание: хук не должен мешать вводу."""
    jobs = []
    for name, src, _ in sources(cfg):
        rec = modules(cfg)[name]
        path = os.path.join(rec['dir'], 'hook.py')
        if not rec['manifest'].get('hooks') or not os.path.isfile(path):
            continue
        out = {}

        def run(name=name, src=src, path=path, out=out):
            try:
                spec = importlib.util.spec_from_file_location(f'tm_modules.{name}_hook', path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                out['text'] = mod.hook(cfg, src, data) or ''
            except Exception:
                pass
        t = threading.Thread(target=run, daemon=True)
        t.start()
        jobs.append((t, out))
    end, lines = time.time() + HOOK_TOTAL, []
    for t, out in jobs:
        t.join(max(0, min(HOOK_BUDGET, end - time.time())))
        text = out.get('text', '').rstrip('\n')
        if text:
            ls = text.split('\n')
            lines += ls[:HOOK_LINES] + ([f'… ещё строк: {len(ls) - HOOK_LINES}'] if len(ls) > HOOK_LINES else [])
    return lines


def require(cfg, name):
    """Модуль, без которого команда не работает (claims для next/take): выключен или сломан — понятная ошибка."""
    rec = modules(cfg).get(name)
    if not rec or not module_on(cfg, name):
        why = 'не установлен' if not rec else rec['error'] or f'выключен в настройках (sources.{name}.enabled)'
        raise ValueError(f'модуль {name}: {why}')
    return rec['mod']


def pid_alive(pid, start=None):
    """Процесс с этим pid жив (на Windows через OpenProcess: os.kill(pid, 0) там шлёт Ctrl+C).

    start — время создания процесса (FILETIME, procStart реестра сессий): после перезагрузки или закрытия
    сессии pid достаётся чужому процессу, и без сверки времени мёртвая сессия считалась бы живой."""
    if sys.platform != 'win32':
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    k = ctypes.windll.kernel32
    h = k.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k.GetExitCodeProcess(h, ctypes.byref(code)) and code.value == 259   # STILL_ACTIVE
    if ok and start:
        t = [ctypes.c_ulonglong() for _ in range(4)]   # создание, выход, ядро, пользователь
        if k.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
            ok = abs(t[0].value - int(start)) < 10_000_000   # в пределах секунды: реестр пишет то же значение
    k.CloseHandle(h)
    return bool(ok)


def git_root(path):
    """Корень git-репозитория, внутри которого лежит path, или None. Задачи и вложения ведутся вне
    репозиториев (владелец 27.10): иначе файлы задач уйдут в коммиты проекта."""
    p = os.path.abspath(path)
    while True:
        if os.path.exists(os.path.join(p, '.git')):
            return p
        up = os.path.dirname(p)
        if up == p:
            return None
        p = up



_agents = {'at': 0, 'list': [], 'error': ''}


def claude_sessions(max_age=3.0):
    """Живые сессии Claude Code из документированного `claude agents --json` (code.claude.com/docs/en/agent-view):
    [{sessionId, pid, name, status, cwd, kind, startedAt}]. Внутренний реестр ~/.claude/sessions не читаем — формат
    не документирован (решение владельца 28.10). Кэш на max_age секунд: команда идёт 0,2 с. Ошибка — [] и текст в
    _agents['error'], панель её покажет."""
    import shutil
    import subprocess
    now = time.time()
    if now - _agents['at'] < max_age:
        return _agents['list']
    exe = shutil.which('claude') or os.path.expanduser('~/.local/bin/claude.exe')
    try:
        p = subprocess.run([exe, 'agents', '--json'], capture_output=True, timeout=15,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        data = json.loads(p.stdout.decode('utf-8', 'replace') or '[]')
        _agents.update(at=now, list=[a for a in data if isinstance(a, dict)], error='' if p.returncode == 0 else f'claude agents: код {p.returncode}')
    except (OSError, ValueError, subprocess.TimeoutExpired) as e:
        _agents.update(at=now, list=[], error=f'claude agents --json: {type(e).__name__}')
    return _agents['list']


def boot_ms():
    """Время загрузки системы (мс эпохи): захват, записанный раньше, остался от прошлого запуска Windows."""
    if sys.platform == 'win32':
        import ctypes
        f = ctypes.windll.kernel32.GetTickCount64
        f.restype = ctypes.c_ulonglong
        return now_ms() - f()
    try:
        with open('/proc/uptime') as fh:
            return now_ms() - int(float(fh.read().split()[0]) * 1000)
    except OSError:
        return 0


def model_on(cfg, feature):
    """Функция, тратящая токены модели, включена: общий рубильник model.enabled и её собственный флаг."""
    m = cfg.get('model', {})
    return bool(m.get('enabled', True) and m.get(feature, True))


def front_matter(path):
    """YAML-шапка Markdown, только плоские `ключ: значение` — больше HANDOFF.md не нужно."""
    try:
        with open(path, encoding='utf-8-sig', errors='replace') as f:   # README в cp1251 — норма в 1С, не повод ронять вкладку
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
           'action': action, 'args': _log_args(args), 'ok': ok, 'ms': ms, 'in_chars': in_chars, 'out_chars': out_chars}
    if err:
        rec['err'] = err[:300]
    try:
        os.makedirs(cfg['state_dir'], exist_ok=True)
        path = os.path.join(cfg['state_dir'], 'calls.jsonl')
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX:   # одно поколение, как журнал доски
            os.replace(path, path + '.1')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
    except OSError:
        pass   # журнал не мешает работе


LOG_MAX = 1 << 20


def _log_args(args):
    """В журнал — форма вызова, не содержимое: тексты просьб, ответов, черновиков и объявлений —
    только длиной (удалённое из задачи не должно жить в журнале). Короткие значения — как есть."""
    short = lambda v: v if not isinstance(v, str) or len(v) <= 40 else f'<{len(v)} симв.>'
    if isinstance(args, dict):   # имена полей и пути — как есть, любой другой текст — длиной
        keep = ('dir', 'board', 'sid', 'field', 'mode', 'status', 'source')
        return {k: (v if k in keep else f'<{len(v)} симв.>') if isinstance(v, str) else short(v) for k, v in args.items()}
    if isinstance(args, list):
        return [short(v) for v in args]
    return args


def now_ms():
    return int(time.time() * 1000)
