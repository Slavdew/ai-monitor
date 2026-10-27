r"""Подключить панель задач и доску объявлений к Claude Code на этой машине (повторный запуск безопасен).

  py install.py [--board home] [--bin <папка>] [--name tm] [--clean <settings.json проекта> ...]
  py install.py --check [<проект> ...]   только отчёт о копиях навыков (дубли, устаревшие)

- копирует навыки task-monitor, session-board и handoff в ~/.claude/skills;
- в ~/.claude/settings.json: один хук UserPromptSubmit — task.py hook (ответы владельца, доска и другие модули;
  прежний session-board.js снимается),
  разрешение на task.py, переменная SESSION_BOARD — доска машины, когда в проекте нет .dev.env;
- обёртка `tm` (имя — --name): `tm.cmd` для PowerShell и cmd, `tm` без расширения для Git Bash, в папку --bin
  (по умолчанию %LOCALAPPDATA%	ask-monitorin), папка — в пользовательский PATH (без прав администратора;
  открытые VS Code и приложение Claude увидят её после перезапуска); разрешения Bash(tm *), PowerShell(tm *).
  Без обёртки навык зовёт task.py полным путём — работает и так;
- --clean: убрать из настроек проекта прежние хуки plan board и task.py (иначе сработают дважды).
"""
import argparse
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
CLAUDE = os.path.join(os.path.expanduser('~'), '.claude')
TASK = os.path.join(HERE, 'task.py')
PY = sys.executable   # тот Python, которым ставили: py и python есть не на каждой машине вместе
HOOKS = [f'"{PY}" "{TASK}" hook']   # один хук-диспетчер: ответы владельца и hook.py модулей (доска — modules/board)
REPLACED = ('session-board.js',)   # прежние хуки, которые диспетчер заменил: снимаются
ALLOW = [f'Bash(python {TASK.replace(os.sep, "/")} *)', f'PowerShell(python {TASK} *)']
OLD = ('plan-board\\plan.py', 'task-monitor\\task.py')


def load(path):
    try:
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def save(path, d):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
        f.write('\n')


SKILL_TASK = '<repo>/tools/task-monitor/task.py'   # путь в SKILL.md репозитория


def fix_paths(path, data=None):
    """Путь к task.py в навыке — этой машины, не владельца (копия навыка ставится сюда же)."""
    if data is not None:
        return data.replace(SKILL_TASK.encode(), TASK.replace(os.sep, '/').encode())
    if os.path.exists(path):
        with open(path, 'rb') as f:
            b = f.read()
        with open(path, 'wb') as f:
            f.write(fix_paths(path, b))


def wrapper(bin_dir, name):
    """tm → python task.py: два файла (Git Bash не находит .cmd без расширения), папка — в PATH пользователя."""
    os.makedirs(bin_dir, exist_ok=True)
    fwd = TASK.replace(os.sep, '/')
    with open(os.path.join(bin_dir, name + '.cmd'), 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(f'@"{PY}" "{TASK}" %*\n')
    with open(os.path.join(bin_dir, name), 'w', encoding='utf-8', newline='\n') as f:
        f.write(f'#!/bin/sh\nexec "{PY.replace(os.sep, "/")}" "{fwd}" "$@"\n')
    if sys.platform != 'win32':
        os.chmod(os.path.join(bin_dir, name), 0o755)
        return False
    import ctypes
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        try:
            cur, kind = winreg.QueryValueEx(k, 'Path')
        except FileNotFoundError:
            cur, kind = '', winreg.REG_EXPAND_SZ
        parts = [x for x in cur.split(';') if x]
        norm = lambda x: os.path.normcase(os.path.expandvars(x)).rstrip('\\/')
        if any(norm(x) == norm(bin_dir) for x in parts):
            return False
        winreg.SetValueEx(k, 'Path', 0, kind, ';'.join(parts + [bin_dir]))
    # сообщить запущенным программам (Проводник), что окружение изменилось
    ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, 'Environment', 2, 5000, None)
    return True


def tree_hash(d, fix=False):
    """Хеш содержимого каталога навыка (пути и байты, без __pycache__) — сравнить копию с репозиторием."""
    import hashlib
    h = hashlib.sha1()
    for root, dirs, files in os.walk(d):
        dirs[:] = sorted(x for x in dirs if x != '__pycache__')
        for f in sorted(files):
            p = os.path.join(root, f)
            h.update(os.path.relpath(p, d).replace(os.sep, '/').encode())
            with open(p, 'rb') as fh:
                b = fh.read()
            h.update(fix_paths(p, b) if fix and f == 'SKILL.md' else b)   # копия навыка — с путём этой машины
    return h.hexdigest()[:10]


def check(projects):
    """Где лежат копии навыков: личная (~/.claude/skills) перекрывает проектную (<проект>/.claude/skills) —
    две копии значат, что работает личная; копия, отличная от репозитория, — устарела или правлена на месте."""
    bad = 0
    for name in SKILLS:
        ref = tree_hash(os.path.join(REPO, 'skills', name), fix=True)
        places = [('личная', os.path.join(CLAUDE, 'skills', name))] + \
                 [(f'проект {p}', os.path.join(p, '.claude', 'skills', name)) for p in projects]
        found = [(k, p) for k, p in places if os.path.isdir(p)]
        for k, p in found:
            same = os.path.realpath(p) == os.path.realpath(os.path.join(REPO, 'skills', name)) or tree_hash(p) == ref
            bad += not same
            print(f'{name}: {k} — {"как в репозитории" if same else "ОТЛИЧАЕТСЯ от репозитория (устарела или правлена)"}')
        if len(found) > 1:
            bad += 1
            print(f'{name}: копий {len(found)} — работает личная, проектная не видна')
        if not found:
            print(f'{name}: не установлен')
    print('дублей и расхождений нет' if not bad else f'замечаний: {bad}')


SKILLS = ('task-monitor', 'session-board', 'handoff')


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--board', default='home', help='имя доски машины (SESSION_BOARD); пусто — не задавать')
    p.add_argument('--clean', nargs='*', default=[], help='settings.json проектов, откуда убрать старые хуки')
    p.add_argument('--bin', default=os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'task-monitor', 'bin'),
                   help='папка для обёртки (будет добавлена в PATH пользователя)')
    p.add_argument('--name', default='tm', help='имя обёртки; пусто — не ставить')
    p.add_argument('--check', nargs='*', metavar='ПРОЕКТ', help='только отчёт: где копии навыков и совпадают ли с репозиторием; ничего не меняет')
    a = p.parse_args()
    if a.check is not None:
        return check(a.check or [os.getcwd()])

    for name in SKILLS:
        dst = os.path.join(CLAUDE, 'skills', name)
        shutil.copytree(os.path.join(REPO, 'skills', name), dst, dirs_exist_ok=True)
        fix_paths(os.path.join(dst, 'SKILL.md'))
        print('навык:', dst)

    path = os.path.join(CLAUDE, 'settings.json')
    d = load(path)
    groups = d.setdefault('hooks', {}).setdefault('UserPromptSubmit', [])
    for g in groups:   # прежний хук доски на Node — его работу делает диспетчер
        g['hooks'] = [h for h in g.get('hooks', []) if not any(r in h.get('command', '') for r in REPLACED)
                      and not (TASK in h.get('command', '') and h.get('command') not in HOOKS)]   # прежний запуск того же хука
    groups[:] = [g for g in groups if g.get('hooks')]
    have = {h.get('command') for g in groups for h in g.get('hooks', [])}
    new = [{'type': 'command', 'command': c, 'timeout': 5} for c in HOOKS if c not in have]
    if new:
        groups.append({'hooks': new})
    rules = ALLOW + ([f'Bash({a.name} *)', f'PowerShell({a.name} *)'] if a.name else [])
    allow = d.setdefault('permissions', {}).setdefault('allow', [])
    allow += [r for r in rules if r not in allow]
    if a.board:
        d.setdefault('env', {})['SESSION_BOARD'] = a.board
    save(path, d)
    print('настройки:', path, '— хуков добавлено', len(new))

    for proj in a.clean:
        pd = load(proj)
        for g in pd.get('hooks', {}).get('UserPromptSubmit', []):
            g['hooks'] = [h for h in g.get('hooks', []) if not any(o in h.get('command', '') for o in OLD)]
        ups = [g for g in pd.get('hooks', {}).get('UserPromptSubmit', []) if g['hooks']]
        if 'hooks' in pd:
            if ups:
                pd['hooks']['UserPromptSubmit'] = ups
            else:
                pd['hooks'].pop('UserPromptSubmit', None)
            if not pd['hooks']:
                pd.pop('hooks')
        save(proj, pd)
        print('очищено:', proj)
    if a.name:
        added = wrapper(a.bin, a.name)
        print(f'обёртка: {os.path.join(a.bin, a.name)}(.cmd)' + (' — папка добавлена в PATH пользователя, перезапустите VS Code и приложение Claude' if added else ''))
    print('Готово. Новые сессии подхватят сами; в открытых — после перезапуска.')


if __name__ == '__main__':
    main()
