r"""Подключить панель ai-monitor к Claude Code на этой машине (повторный запуск безопасен).

  py install.py [--tasks <каталог задач>] [--board home] [--name am]
  py install.py --use <версия>        откат: ссылка current — на уже установленную версию, без копирования
  py install.py --check [<проект> ...]   только отчёт о копиях навыков (дубли, устаревшие)

- копирует репозиторий в %LOCALAPPDATA%\ai-monitor\versions\<версия> и ставит на неё ссылку current: хук, навыки,
  строка состояния, сервер и ярлык работают из current, рабочая копия репозитория на них не влияет (аудит 27.10);
- навыки ai-monitor, session-board, handoff — в ~/.claude/skills; прежний навык task-monitor снимается;
- ~/.claude/settings.json: UserPromptSubmit — task.py hook (ответы владельца, доска, модули); PreToolUse — сторож
  tools/guards/guard.py; PostToolUseFailure — журнал граблей tools/guards/pitfalls.py; строка состояния — лимиты
  подписки для панели; env PYTHONUTF8, LC_ALL; запрет чтения .credentials.json; прежние хуки task-monitor снимаются;
- обёртка `am` (--name): am.cmd и am в %LOCALAPPDATA%\ai-monitor\bin, папка — в PATH пользователя; прежняя tm снимается;
- ярлык ai-monitor.lnk в автозагрузке (трей и сервер при входе), прежний task-monitor.lnk убирается; сервер
  перезапускается из новой версии;
- первая установка: спрашивает каталог задач (--tasks; на работе — выгрузка из CRM, новый не создаётся, если дан),
  keepwarm выключен, пока не включите в «Сессиях».
Все команды хуков — в обёртке: нет файла (ветка без панели) — код 0, ввод и вызовы не блокируются.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CLAUDE = os.path.join(os.path.expanduser('~'), '.claude')
LOCAL = os.environ.get('LOCALAPPDATA', os.path.expanduser('~'))
HOME_DIR = os.path.join(LOCAL, 'ai-monitor')
VERSIONS = os.path.join(HOME_DIR, 'versions')
CURRENT = os.path.join(HOME_DIR, 'current')
RUN = os.path.join(CURRENT, 'tools', 'ai-monitor')
TASK = os.path.join(RUN, 'task.py')
GUARD = os.path.join(CURRENT, 'tools', 'guards', 'guard.py')
PITFALLS = os.path.join(CURRENT, 'tools', 'guards', 'pitfalls.py')
STATUS = os.path.join(RUN, 'modules', 'usage', 'statusline.py')
PY = sys.executable   # полный путь: py.exe — лишний процесс (аудит граблей: 35 мс против 80–117)


def wrapped(path, *args, swallow2=False):
    """Команда хука: файла нет — код 0 (иначе код 2 у UserPromptSubmit блокировал бы ввод — проверено 27.10)."""
    argv = ''.join(f",'{a}'" for a in args)
    fix = 'x=sys.exit;sys.exit=lambda c=0:x(0 if c==2 else c);' if swallow2 else ''
    return (f'"{PY}" -c "import os,runpy,sys;p=r\'{path}\';sys.argv=[p{argv}];{fix}'
            f'runpy.run_path(p,run_name=\'__main__\') if os.path.isfile(p) else None"')


HOOK = wrapped(TASK, 'hook', swallow2=True)
GUARD_HOOK = wrapped(GUARD)
GUARD_MATCHER = 'Bash|PowerShell|Edit|Write|Agent|Read|Grep'   # сторож отвечает JSON в stdout с кодом 0
PITFALLS_HOOK = wrapped(PITFALLS, 'hook')
STATUS_CMD = wrapped(STATUS)
ENV = {'PYTHONUTF8': '1', 'LC_ALL': 'C.UTF-8'}
DENY = ['Read(**/.credentials.json)']
OLD_MARKS = ('task-monitor\\task.py', 'task-monitor/task.py', 'session-board.js', 'plan-board\\plan.py')
SKILLS = ('ai-monitor', 'session-board', 'handoff')
OLD_SKILLS = ('task-monitor',)
OLD_BIN = os.path.join(LOCAL, 'task-monitor', 'bin')
SKILL_TASK = '<repo>/tools/ai-monitor/task.py'   # путь в SKILL.md репозитория — заменяется на путь этой машины


def load(path):
    try:
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(path, d):
    """Через временный файл и с копией прежнего (<файл>.bak): settings.json Claude Code не должен остаться обрезанным."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        shutil.copy2(path, path + '.bak')
    with open(path + '.tmp', 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write('\n')
    os.replace(path + '.tmp', path)


def fix_paths(path, data=None):
    if data is not None:
        return data.replace(SKILL_TASK.encode(), TASK.replace(os.sep, '/').encode())
    if os.path.exists(path):
        with open(path, 'rb') as f:
            b = f.read()
        with open(path, 'wb') as f:
            f.write(fix_paths(path, b))


# --- версии ---

def version_name():
    try:
        h = subprocess.run(['git', '-C', REPO, 'rev-parse', '--short', 'HEAD'], capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(['git', '-C', REPO, 'status', '--porcelain'], capture_output=True, text=True).stdout.strip()
    except OSError:
        h, dirty = '', ''
    return (h or 'local') + (time.strftime('-%Y%m%d-%H%M%S') if dirty or not h else '')


def point_current(target):
    """Ссылка current → target (junction: без прав администратора). Удаление junction не трогает цель."""
    if os.path.lexists(CURRENT):
        os.rmdir(CURRENT) if os.path.isdir(CURRENT) else os.remove(CURRENT)
    if sys.platform == 'win32':
        # mklink понимает только обратные слеши
        r = subprocess.run(['cmd', '/c', 'mklink', '/J', os.path.normpath(CURRENT), os.path.normpath(target)],
                           capture_output=True, text=True)
        if r.returncode:
            sys.exit(f'ссылка current не создана: {(r.stdout + r.stderr).strip()}')
    else:
        os.symlink(target, CURRENT)


def deploy(use=None, keep=3):
    os.makedirs(VERSIONS, exist_ok=True)
    if use:
        dst = os.path.join(VERSIONS, use)
        if not os.path.isdir(dst):
            sys.exit(f'нет версии {use}: есть {", ".join(sorted(os.listdir(VERSIONS)))}')
    else:
        dst = os.path.join(VERSIONS, version_name())
        if not os.path.isdir(dst):
            shutil.copytree(REPO, dst, ignore=shutil.ignore_patterns('.git', '__pycache__', 'config.local.json'))
    point_current(dst)
    old = sorted((d for d in os.listdir(VERSIONS) if os.path.join(VERSIONS, d) != dst),
                 key=lambda d: os.path.getmtime(os.path.join(VERSIONS, d)))
    for d in old[:-keep] if len(old) > keep else []:   # последние keep прежних — для отката
        shutil.rmtree(os.path.join(VERSIONS, d), ignore_errors=True)
    return os.path.basename(dst)


# --- настройки Claude Code ---

def set_hook(d, event, command, matcher=None, mark=None):
    """Одна запись хука: прежние с тем же mark (имя файла) — заменить, старые task-monitor — снять."""
    groups = d.setdefault('hooks', {}).setdefault(event, [])
    for g in groups:
        g['hooks'] = [h for h in g.get('hooks', []) if not any(m in h.get('command', '') for m in OLD_MARKS)
                      and not (mark and mark in h.get('command', ''))]
    groups[:] = [g for g in groups if g.get('hooks')]
    entry = {'hooks': [{'type': 'command', 'command': command, 'timeout': 5}]}
    if matcher is not None:
        entry['matcher'] = matcher
    groups.append(entry)


def settings(a):
    path = os.path.join(CLAUDE, 'settings.json')
    d = load(path)
    before = json.dumps(d, sort_keys=True)
    set_hook(d, 'UserPromptSubmit', HOOK, mark='task.py')
    set_hook(d, 'PreToolUse', GUARD_HOOK, GUARD_MATCHER, mark='guard.py')
    set_hook(d, 'PostToolUseFailure', PITFALLS_HOOK, '', mark='pitfalls.py')
    sl = d.get('statusLine') or {}
    if not sl or 'statusline.py' in sl.get('command', ''):   # своя строка состояния владельца — не трогаем
        d['statusLine'] = {'type': 'command', 'command': STATUS_CMD}
    else:
        print('строка состояния уже своя — лимиты в панель не попадут: добавьте в неё вызов', STATUS)
    d.setdefault('env', {}).update(ENV)
    if a.board:
        d['env']['SESSION_BOARD'] = a.board
    perms = d.setdefault('permissions', {})
    perms['allow'] = [r for r in perms.get('allow', []) if 'task-monitor' not in r]
    for r in [f'Bash(python {TASK.replace(os.sep, "/")} *)', f'PowerShell(python {TASK} *)'] + \
             ([f'Bash({a.name} *)', f'PowerShell({a.name} *)'] if a.name else []):
        if r not in perms['allow']:
            perms['allow'].append(r)
    perms['deny'] = perms.get('deny', []) + [r for r in DENY if r not in perms.get('deny', [])]
    save(path, d)
    print('настройки:', path, '— изменены' if json.dumps(d, sort_keys=True) != before else '— без изменений')


# --- обёртка, PATH, автозагрузка, сервер ---

def user_path(add=None, remove=None):
    if sys.platform != 'win32':
        return False
    import ctypes
    import winreg
    norm = lambda x: os.path.normcase(os.path.expandvars(x)).rstrip('\\/')
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            cur, kind = winreg.QueryValueEx(k, 'Path')
        except FileNotFoundError:
            cur, kind = '', winreg.REG_EXPAND_SZ
        parts = [x for x in cur.split(';') if x and not (remove and norm(x) == norm(remove))]
        if add and not any(norm(x) == norm(add) for x in parts):
            parts.append(add)
        if ';'.join(parts) == cur:
            return False
        winreg.SetValueEx(k, 'Path', 0, kind, ';'.join(parts))
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, 'Environment', 2, 5000, None)
    return True


def wrapper(bin_dir, name):
    """am → python task.py: два файла (Git Bash не находит .cmd без расширения)."""
    os.makedirs(bin_dir, exist_ok=True)
    with open(os.path.join(bin_dir, name + '.cmd'), 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(f'@"{PY}" "{TASK}" %*\n')
    with open(os.path.join(bin_dir, name), 'w', encoding='utf-8', newline='\n') as f:
        f.write(f'#!/bin/sh\nexec "{PY.replace(os.sep, "/")}" "{TASK.replace(os.sep, "/")}" "$@"\n')
    for f in ('tm', 'tm.cmd'):   # прежняя обёртка task-monitor
        try:
            os.remove(os.path.join(OLD_BIN, f))
        except OSError:
            pass
    user_path(remove=OLD_BIN)
    return user_path(add=bin_dir)


def ps(cmd):
    return subprocess.run(['powershell', '-NoProfile', '-Command', cmd], capture_output=True, text=True)


def autostart():
    start = os.path.join(RUN, 'start.cmd')
    r = ps("$s=[Environment]::GetFolderPath('Startup');"
           "Remove-Item -LiteralPath (Join-Path $s 'task-monitor.lnk') -ErrorAction SilentlyContinue;"
           "$l=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $s 'ai-monitor.lnk'));"
           f"$l.TargetPath='{start}';$l.Arguments='/silent';$l.WorkingDirectory='{RUN}';$l.WindowStyle=7;$l.Save()")
    print('автозагрузка: ai-monitor.lnk' if r.returncode == 0 else f'автозагрузка не создана: {r.stderr.strip()[:200]}')


def restart_server():
    """Сервер и трей — из новой версии: прежние (task-monitor или другая версия) останавливаются."""
    # $PID — сама эта команда: в её строке тоже есть «server.py|tray.ps1», без исключения она останавливала себя
    # раньше старого трея (28.10 старый трей task-monitor пережил установку)
    ps("Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'server\\.py|tray\\.ps1' -and "
       "$_.CommandLine -notmatch 'install\\.py' -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process "
       "-Id $_.ProcessId -Force -ErrorAction SilentlyContinue }")
    time.sleep(1)
    subprocess.Popen(['cmd', '/c', os.path.join(RUN, 'start.cmd'), '/silent'], cwd=RUN,
                     creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    # проверка: на порту — сервер из current (сравнение по папке, которую сервер сам сообщает в state.json)
    import gzip
    import urllib.request
    want, home = os.path.normcase(os.path.realpath(RUN)), ''
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for _ in range(20):
        time.sleep(0.5)
        try:
            raw = opener.open('http://127.0.0.1:8770/state.json', timeout=2).read()
            home = json.loads(gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw)['sources']['core'].get('home', '')
            break
        except Exception:
            continue
    if os.path.normcase(home) == want:
        print('сервер: работает из', CURRENT)
    else:
        print(f'ВНИМАНИЕ: на 8770 не новый сервер ({home or "не отвечает"}) — закройте прежний (трей → Закрыть) и '
              f'запустите {os.path.join(RUN, "start.cmd")}')


def first_run(a):
    """Первая установка: переносит состояние task-monitor (ядро делает это само), спрашивает каталог задач."""
    subprocess.run([PY, '-c', f'import sys;sys.path.insert(0,r"{RUN}");from monitor.core import load_config;load_config()'],
                   check=False)
    personal = os.path.join(HOME_DIR, 'settings.json')
    if os.path.exists(personal):
        return
    tasks = a.tasks or input('Каталог задач (на работе — выгрузка из CRM; Enter — создать %USERPROFILE%\\tasks): ').strip()
    tasks = os.path.expandvars(tasks) if tasks else os.path.join(os.path.expanduser('~'), 'tasks')
    os.makedirs(tasks, exist_ok=True)
    save(personal, {'tabs': [{'id': 'tasks', 'title': 'Задачи', 'group': 'Задачи', 'root': tasks}]})
    off = os.path.join(LOCAL, 'keepwarm', 'off')
    os.makedirs(off, exist_ok=True)
    open(os.path.join(off, 'ALL.off'), 'a').close()   # keepwarm у новых установок выключен (аудит 27.10)
    print('каталог задач:', tasks, '· keepwarm выключен — включается в «Сессиях»')


def tree_hash(d, fix=False):
    import hashlib
    h = hashlib.sha1()
    for root, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x != '__pycache__')
        for f in sorted(files):
            p = os.path.join(root, f)
            h.update(os.path.relpath(p, d).replace(os.sep, '/').encode())
            with open(p, 'rb') as fh:
                b = fh.read()
            h.update(fix_paths(p, b) if fix and f == 'SKILL.md' else b)
    return h.hexdigest()[:10]


def check(projects):
    bad = 0
    for name in SKILLS:
        ref = tree_hash(os.path.join(REPO, 'skills', name), fix=True)
        places = [('личная', os.path.join(CLAUDE, 'skills', name))] + \
                 [(f'проект {p}', os.path.join(p, '.claude', 'skills', name)) for p in projects]
        found = [(k, p) for k, p in places if os.path.isdir(p)]
        for k, p in found:
            same = tree_hash(p) == ref
            bad += not same
            print(f'{name}: {k} — {"как в репозитории" if same else "ОТЛИЧАЕТСЯ от репозитория (устарела или правлена)"}')
        if len(found) > 1:
            bad += 1
            print(f'{name}: копий {len(found)} — работает личная, проектная не видна')
        if not found:
            print(f'{name}: не установлен')
    print('дублей и расхождений нет' if not bad else f'замечаний: {bad}')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--tasks', help='каталог задач для первой установки (иначе спросит)')
    p.add_argument('--board', default='home', help='имя доски машины (SESSION_BOARD); пусто — не задавать')
    p.add_argument('--name', default='am', help='имя обёртки; пусто — не ставить')
    p.add_argument('--use', help='откат: переключить current на установленную версию')
    p.add_argument('--check', nargs='*', metavar='ПРОЕКТ', help='только отчёт о копиях навыков; ничего не меняет')
    p.add_argument('--no-system', action='store_true', help='не трогать PATH, автозагрузку и сервер (проверка)')
    a = p.parse_args()
    if a.check is not None:
        return check(a.check or [os.getcwd()])
    print('версия:', deploy(a.use), '→', CURRENT)
    first_run(a)
    for name in SKILLS:
        dst = os.path.join(CLAUDE, 'skills', name)
        shutil.copytree(os.path.join(CURRENT, 'skills', name), dst, dirs_exist_ok=True)
        fix_paths(os.path.join(dst, 'SKILL.md'))
    for name in OLD_SKILLS:
        shutil.rmtree(os.path.join(CLAUDE, 'skills', name), ignore_errors=True)
    print('навыки:', ', '.join(SKILLS), '(прежний task-monitor снят)')
    settings(a)
    if a.name and not a.no_system:
        added = wrapper(os.path.join(HOME_DIR, 'bin'), a.name)
        print(f'обёртка: {a.name}' + (' — папка в PATH, перезапустите VS Code' if added else ''))
    if not a.no_system:
        autostart()
        restart_server()
    print('Готово. Открытые сессии Claude подхватят хуки сами; навыки — после перезапуска сессии.')


if __name__ == '__main__':
    main()
