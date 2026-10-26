"""Подключить панель задач и доску объявлений к Claude Code на этой машине (повторный запуск безопасен).

  py install.py [--board home] [--bin <папка>] [--name tm] [--clean <settings.json проекта> ...]

- копирует навыки task-monitor, session-board и handoff в ~/.claude/skills;
- в ~/.claude/settings.json: хуки UserPromptSubmit (ответы владельца из панели, объявления доски),
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
HOOKS = [f'py "{TASK}" hook',
         f'node "{os.path.join(CLAUDE, "skills", "session-board", "hooks", "session-board.js")}"']
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


def wrapper(bin_dir, name):
    """tm → python task.py: два файла (Git Bash не находит .cmd без расширения), папка — в PATH пользователя."""
    os.makedirs(bin_dir, exist_ok=True)
    fwd = TASK.replace(os.sep, '/')
    with open(os.path.join(bin_dir, name + '.cmd'), 'w', encoding='utf-8', newline='\r\n') as f:
        f.write(f'@python "{TASK}" %*\n')
    with open(os.path.join(bin_dir, name), 'w', encoding='utf-8', newline='\n') as f:
        f.write(f'#!/bin/sh\nexec python "{fwd}" "$@"\n')
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


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--board', default='home', help='имя доски машины (SESSION_BOARD); пусто — не задавать')
    p.add_argument('--clean', nargs='*', default=[], help='settings.json проектов, откуда убрать старые хуки')
    p.add_argument('--bin', default=os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'task-monitor', 'bin'),
                   help='папка для обёртки (будет добавлена в PATH пользователя)')
    p.add_argument('--name', default='tm', help='имя обёртки; пусто — не ставить')
    a = p.parse_args()

    for name in ('task-monitor', 'session-board', 'handoff'):
        dst = os.path.join(CLAUDE, 'skills', name)
        shutil.copytree(os.path.join(REPO, 'skills', name), dst, dirs_exist_ok=True)
        print('навык:', dst)

    path = os.path.join(CLAUDE, 'settings.json')
    d = load(path)
    groups = d.setdefault('hooks', {}).setdefault('UserPromptSubmit', [])
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
