"""Доска объявлений сессий (навык session-board): читает <доска>.board в %LOCALAPPDATA%/1c-ib-queue.

Формат и срез последних — как Get-IbNotes в skills/session-board/scripts/_queue.ps1 (и хук
session-board.js): строка TSV «когда · до · чей · текст», время — [datetime]::ToBinary(), ключ —
« #key» в колонке «чей»; новое с тем же тегом и ключом замещает прежнее, close снимает всё по тегу.
Пишет панель только через Say-Board.ps1 навыка — формат не дублируется.
"""
import glob
import os
import re
import subprocess
import time

from monitor.core import expand

BLOCKING = ('ib', 'stop', 'branch')
CLOSE = 'close'


def _ms(binary):
    """[datetime]::ToBinary() → мс эпохи: UTC-тики с флагом вида в старших битах."""
    ticks = int(binary) & 0x3FFFFFFFFFFFFFFF
    return (ticks - 621355968000000000) // 10000


def _tag_id(tag):
    m = re.search(r'(?<!\d)(\d{6})(?!\d)', tag)
    return m.group(1) if m and m.group(1) != '000000' else tag.lower()


def read_board(path, now):
    notes = []
    try:
        with open(path, encoding='utf-8-sig') as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    for line in lines:
        p = line.split('\t', 3)
        if len(p) < 4 or not re.fullmatch(r'-?\d+', p[0]) or not re.fullmatch(r'-?\d+', p[1]):
            continue
        m = re.match(r'^(.*\S)\s+#(\S+)$', p[2])
        tag = m.group(1) if m else p[2]
        notes.append({'raw': p[0], 'at': _ms(p[0]), 'until': _ms(p[1]), 'owner': p[2], 'tag': tag, 'tag_id': _tag_id(tag),
                      'key': m.group(2).lower() if m else '', 'text': p[3]})
    closed, latest = {}, {}
    for n in notes:
        if n['key'] == CLOSE:
            closed[n['tag_id']] = max(closed.get(n['tag_id'], 0), n['at'])
        if n['key']:
            k = n['tag_id'] + '#' + n['key']
            latest[k] = max(latest.get(k, 0), n['at'])
    # каждой строке — её судьба: действует, замещена, снята закрытием, истекла (для истории)
    for n in notes:
        n['blocking'] = n['key'] in BLOCKING
        if n['key'] and n['at'] < latest[n['tag_id'] + '#' + n['key']]:
            n['state'] = 'replaced'
        elif n['key'] != CLOSE and n['at'] < closed.get(n['tag_id'], 0):
            n['state'] = 'closed'
        elif n['until'] < now:
            n['state'] = 'expired'
        else:
            n['state'] = 'active'
    return sorted(notes, key=lambda n: -n['at'])


def collect(cfg, src, ctx):
    d = expand(src.get('dir', '%LOCALAPPDATA%/1c-ib-queue'))
    now = int(time.time() * 1000)
    boards = []
    default = re.sub(r'\W', '_', src.get('default') or os.environ.get('SESSION_BOARD', ''))
    hist = int(src.get('history', 100))
    for path in sorted(glob.glob(os.path.join(d, '*.board'))):
        name = os.path.basename(path)[:-6]
        notes = read_board(path, now)
        boards.append({'name': name, 'machine': name == default,
                       'notes': [n for n in notes if n['state'] == 'active'], 'history': notes[:hist]})
    if default and not any(b['name'] == default for b in boards):
        boards.insert(0, {'name': default, 'machine': True, 'notes': [], 'history': []})   # доска машины, ещё пустая
    boards.sort(key=lambda b: not b['machine'])
    return {'boards': boards, 'owner': src.get('owner', 'владелец'), 'key': src.get('key', 'user')}


def say(cfg, src, args):
    """Действие владельца с доской — через Say-Board.ps1 навыка; доска — по имени файла.

    Ничего не стирается: правка и снятие — новые строки, замещающие прежнюю (тот же тег и ключ),
    закрытие — строка close по тегу; кто и что сделал — пометкой в тексте, история — в файле доски.
    mode: new (тег — владелец) | edit | delete (тег и ключ — у объявления) | close (тег).
    """
    owner = src.get('owner', 'владелец')
    mode = args.get('mode', 'new')
    text = str(args.get('text') or '').strip()
    tag = owner if mode == 'new' else str(args.get('tag') or '')
    if mode in ('edit', 'delete') and not args.get('key'):
        raise ValueError('у объявления нет ключа: формат доски не умеет заменить одну такую строку — закройте тег')
    if mode == 'edit':
        text = f'[изменено: {owner}] {text}'
    elif mode == 'delete':
        text, args['hours'] = f'[снято: {owner}] {text}', 0
    elif mode == 'close':
        text = f'[закрыто: {owner}] {text or "объявления тега сняты"}'
    if not text or not tag:
        raise ValueError('пустое объявление или тег')
    script = os.path.join(expand(src.get('skill', '../../skills/session-board')), 'scripts', 'Say-Board.ps1')
    cmd = ['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script, '-Owner', tag, '-Message', text]
    if mode == 'close':
        cmd += ['-Close']
    elif args.get('key'):
        cmd += ['-Key', str(args['key']).strip()]
    if mode != 'close' and args.get('hours') not in (None, ''):
        cmd += ['-Hours', str(int(args['hours']))]
    env = dict(os.environ, SESSION_BOARD=args['board'])
    env.pop('CLAUDE_CODE_SESSION_ID', None)   # объявление владельца — не своё у сессии, запустившей сервер
    r = subprocess.run(cmd, env=env, cwd=expand(src.get('dir', '%LOCALAPPDATA%/1c-ib-queue')),
                       capture_output=True, timeout=30, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    out = (r.stdout or b'').decode('cp866', 'replace').strip()
    if r.returncode:
        raise ValueError((r.stderr or b'').decode('cp866', 'replace').strip()[:300] or out)
    return {'ok': True, 'out': out}


ACTIONS = {'say': say}
