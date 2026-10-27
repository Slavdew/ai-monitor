r"""Сторож вызовов инструментов Claude Code: частые грабли, которые проще не допустить, чем чинить.

Хук PreToolUse (ставит install.py панели): JSON вызова на stdin, ответ — JSON на stdout, код 0.
Правила двух видов:
  fix_*(tool, inp, ctx) → новый tool_input или None — вызов исправляется молча (updatedInput);
  rule_*(tool, inp, ctx) → текст (отказ, deny), ('ask', текст) (окно подтверждения владельцу) или None.
Сначала все fix_, потом rule_ по уже исправленному вызову. Каждое срабатывание — запись в журнал граблей
(pitfalls.py, id guard.<правило>). Любая собственная ошибка сторожа — пустой ответ: сторож не должен мешать работе.

Свои репозитории, хосты и файлы секретов — в настройках вне репозитория: GUARD_CONFIG или ~/.claude/guards.json
  {"secret_files": ["regex", …],                 — дополнительно к общим (.credentials.json, id_rsa, .env)
   "release": {"regex каталога": "regex ветки или тега"},  — push туда — окно подтверждения
   "shared_index": ["regex каталога"],            — git add -A / commit -a там — отказ
   "ssh_hosts": {"адрес": "как входить"},         — ssh на адрес без IdentityAgent=SSH_AUTH_SOCK — отказ с подсказкой
   "source_roots": ["C:\\Work"]}                  — Set-Content/Out-File в файлы кода под ними — отказ
Без настроек работают только общие правила.
"""
import json
import os
import re
import subprocess
import sys
import tempfile


def load_config():
    path = os.environ.get('GUARD_CONFIG') or os.path.join(os.path.expanduser('~'), '.claude', 'guards.json')
    try:
        with open(path, encoding='utf-8-sig') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


CFG = load_config()
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
SECRET_FILES = re.compile('|'.join([r'\.credentials\.json', r'(^|[\\/\s"\'=])id_(rsa|ed25519|ecdsa)(?![\w.])',
                                    r'(^|[\\/\s"\'=])\.env(?![\w.-])'] + CFG.get('secret_files', [])), re.I)
READERS = r'\b(cat|type|head|tail|less|more|Get-Content|gc|grep|Select-String|sls|strings|xxd|od|open)\b'
SEG = re.compile(r'&&|\|\||;|\n')
PY_I = re.compile(r'(?<![\w.-])((?:py|python3?|python)(?:\.exe)?)((?:\s+-[A-HJ-Za-z]\w*)*)\s+-I\b(?!\s+-X\s+utf8)')
PS_CURL = re.compile(r'(^|[\s;(|{])curl(?=\s)', re.M)
LONG_HEREDOC = 6500   # длиннее — Bash-инструмент обрезает команду (A1: обрывы с 6 840 знаков)


def heredoc_bodies(cmd):
    """Тела всех heredoc команды (до строки-ограничителя)."""
    lines = cmd.split('\n')
    out, i = [], 0
    while i < len(lines):
        m = HEREDOC.search(lines[i])
        i += 1
        if not m:
            continue
        body = []
        while i < len(lines) and lines[i].strip() != m.group(2):
            body.append(lines[i])
            i += 1
        out.append('\n'.join(body))
        i += 1
    return out


def code_only(cmd):
    """Команда без тел heredoc, here-string PowerShell и строковых литералов: текст правил и памяти — не команда."""
    for b in heredoc_bodies(cmd):
        if b:
            cmd = cmd.replace(b, '')
    cmd = re.sub(r"@['\"]\r?\n.*?\n['\"]@", "''", cmd, flags=re.S)
    cmd = re.sub(r"'[^'\n]*'", "''", cmd)
    return re.sub(r'"[^"\n]*"', '""', cmd)


def sub_in_code(rx, repl, cmd):
    """Замена rx только если все его вхождения — в коде, не в тексте; иначе None (не трогать)."""
    n = len(rx.findall(code_only(cmd)))
    return rx.sub(repl, cmd) if n and n == len(rx.findall(cmd)) else None


def norm(p):
    return (p or '').lower().replace('/', '\\')


# ------------------------------------------------------------------ исправления

def fix_ps1_bom(tool, inp, ctx):
    """.ps1 с кириллицей без BOM PowerShell 5.1 читает в cp1251 (A13, B1: 10 файлов после правила)."""
    p, c = inp.get('file_path', ''), inp.get('content', '')
    if tool == 'Write' and p.lower().endswith(('.ps1', '.psm1', '.psd1')) and not c.isascii() and c[:1] != '\ufeff':
        return dict(inp, content='\ufeff' + c)
    return None


def fix_ps_curl(tool, inp, ctx):
    """curl в PowerShell 5.1 — алиас Invoke-WebRequest (B6)."""
    if tool != 'PowerShell':
        return None
    cmd = sub_in_code(PS_CURL, r'\1curl.exe', inp.get('command', ''))
    return dict(inp, command=cmd) if cmd else None


def fix_ps_pipe_bom(tool, inp, ctx):
    """Конвейер PowerShell в ssh начинается с BOM; убирает только UTF8Encoding $false (A10, B4, C-G6)."""
    cmd = inp.get('command', '')
    if tool != 'PowerShell' or not re.search(r'\|\s*(&\s*)?["\']?[\w:\\./-]*\bssh(\.exe)?\b', code_only(cmd)):
        return None
    if re.search(r'UTF8Encoding\s*\(?\s*\$false', cmd):
        return None
    return dict(inp, command='$OutputEncoding = New-Object System.Text.UTF8Encoding $false; ' + cmd)


def fix_python_isolated_utf8(tool, inp, ctx):
    """python -I игнорирует PYTHON* (в т. ч. PYTHONUTF8 из env) → charmap; -X utf8 действует (A9, C-G9)."""
    if tool not in ('Bash', 'PowerShell'):
        return None
    cmd = sub_in_code(PY_I, r'\1\2 -I -X utf8', inp.get('command', ''))
    return dict(inp, command=cmd) if cmd else None


def fix_long_heredoc(tool, inp, ctx):
    """Длинная команда с heredoc обрезается → unexpected EOF (A1: 39×). Команда — в файл, запуск — bash <файл>."""
    cmd = inp.get('command', '')
    if tool != 'Bash' or '<<' not in cmd or len(cmd) <= LONG_HEREDOC:
        return None
    d = ctx.get('scratchpad_dir') or tempfile.gettempdir()
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"guard-{ctx.get('tool_use_id') or os.getpid()}.sh")
    with open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(cmd + '\n')
    return dict(inp, command=f"bash '{path.replace(os.sep, '/')}'")


# ------------------------------------------------------------------ отказы и подтверждения

def rule_heredoc_backslash(tool, inp, ctx):
    """Обратный слеш в теле heredoc приходит в Bash-инструменте искажённым: «\\n» → перевод строки,
    «D:\\Work\\tasks» → табуляции (память heredoc-backslash: больше двадцати случаев с 19.09.2026)."""
    if tool != 'Bash':
        return None
    for body in heredoc_bodies(inp.get('command', '')):
        if '\\' in body:
            return ('В теле heredoc есть обратный слеш — в этом окружении он искажается (\\n, \\t в путях Windows). '
                    'Запишите скрипт файлом через Write (в scratchpad) и запустите его, а правку кода с «\\» '
                    'сделайте через Edit.')
    return None


def rule_sed_windows_path(tool, inp, ctx):
    """sed -i с путём Windows в замене съедает обратные слеши (23.10.2026, дважды за вечер)."""
    if tool != 'Bash':
        return None
    cmd = inp.get('command', '')
    if re.search(r'\bsed\b[^|;&]*\s(-i|--in-place)', cmd) and re.search(r'[A-Za-z]:\\', cmd):
        return 'sed -i с путём Windows: обратные слеши в замене теряются. Сделайте правку через Edit.'
    return None


def rule_secrets(tool, inp, ctx):
    """Чтение файла с секретом или всех переменных окружения (B16; память secrets-never-shown)."""
    if tool in ('Read', 'Grep', 'Glob'):
        p = inp.get('file_path') or inp.get('path') or ''
        return f'Файл с секретом ({p}): не читать, только «задан / не задан».' if SECRET_FILES.search(p) else None
    if tool not in ('Bash', 'PowerShell'):
        return None
    raw = inp.get('command', '')
    for seg in SEG.split(raw):   # упоминание пути ([ -s f ] && echo задан, ls f) — не чтение
        seg = code_only(seg)
        if SECRET_FILES.search(seg) and re.search(READERS, seg):
            return 'Чтение файла с секретом: не читать, только «задан / не задан» (test -s, Test-Path).'
    cmd = code_only(raw)
    if tool == 'Bash' and re.search(r'(^|[;&(]\s*)(env|printenv)\s*(\|(?!\s*(grep|egrep|sed|awk|cut|findstr))|$|>)', cmd, re.M):
        return 'env/printenv без отбора выводит все переменные, в том числе ключи. Отфильтруйте или замаскируйте значения.'
    if tool == 'PowerShell' and re.search(r'(Get-ChildItem|gci|ls|dir)\s+env:\s*($|[;)]|\|(?!\s*(Where|\?|Select-String|sls|findstr)))',
                                          cmd, re.I | re.M):
        return 'Get-ChildItem env: без фильтра выводит все переменные, в том числе ключи.'
    return None


def rule_system_proxy(tool, inp, ctx):
    """Запись системного прокси обрывает связь всем сессиям, которые через него работают."""
    if tool not in ('Bash', 'PowerShell'):
        return None
    raw, cmd = inp.get('command', ''), code_only(inp.get('command', ''))
    writes = r'\b(reg(\.exe)?\s+(add|delete|import)|Set-ItemProperty|New-ItemProperty|Remove-ItemProperty)\b'
    if re.search(r'netsh(\.exe)?\s+winhttp\s+(set|reset|import)', cmd, re.I) or (
            re.search(r'Internet Settings|ProxyServer|ProxyEnable|AutoConfigURL', raw, re.I) and re.search(writes, cmd, re.I)):
        return 'Запись системного прокси — нельзя: через него работают все сессии и владелец.'
    return None


def git_dirs(cmd, cwd):
    return re.findall(r'(?:git\s+-C\s+|\b(?:cd|Set-Location|pushd)\s+(?:/d\s+)?)["\']?([^"\'\s;&|]+)', cmd) + [cwd or '']


def current_branch(d):
    try:
        r = subprocess.run(['git', '-C', d, 'symbolic-ref', '--short', 'HEAD'], capture_output=True, timeout=3)
        return r.stdout.decode().strip()
    except Exception:
        return ''


def rule_release_push(tool, inp, ctx):
    """Push в ветку, из которой собирается выпуск (настройка release), уходит пользователям сразу.
    Не отказ, а окно подтверждения владельцу: метку согласия агент поставил бы сам."""
    if tool not in ('Bash', 'PowerShell') or not CFG.get('release'):
        return None
    cmd = code_only(inp.get('command', ''))
    pushes = re.findall(r'\bgit\s+(?:-C\s+\S+\s+)?push\b([^;&|\n]*)', cmd)
    if not pushes:
        return None
    dirs = git_dirs(cmd, ctx.get('cwd', ''))
    for repo, pat in CFG['release'].items():
        hit = [d for d in dirs if re.search(repo, norm(d), re.I)]
        if not hit:
            continue
        for args in pushes:
            if '--tags' in args or '--all' in args or '--mirror' in args:
                return ('ask', f'push всех тегов/веток в {hit[0]} — это выпуск. Подтвердите.')
            refs = [r for r in args.split() if not r.startswith('-')][1:]   # первый — remote
            refs = [r.split(':')[-1].removeprefix('refs/heads/').removeprefix('refs/tags/') for r in refs]
            if not refs:
                refs = [current_branch(hit[0])]
            bad = [r for r in refs if re.match(pat, r)]
            if bad:
                return ('ask', f'push {", ".join(bad)} в {hit[0]} — это выпуск. Подтвердите, если владелец сказал «да».')
    return None


def rule_worktree_outside_git(tool, inp, ctx):
    """Agent с isolation=worktree из каталога не в git: отказ среды или worktree не от того репозитория."""
    if tool != 'Agent' or inp.get('isolation') != 'worktree':
        return None
    d = ctx.get('cwd', '')
    if not d or not os.path.isdir(d):
        return None
    try:
        r = subprocess.run(['git', '-C', d, 'rev-parse', '--is-inside-work-tree'], capture_output=True, timeout=3)
    except Exception:
        return None
    if r.returncode != 0:
        return f'Agent с isolation=worktree, а каталог сессии ({d}) не в git: сначала cd в репозиторий или запуск без изоляции.'
    return None


def rule_set_content_sources(tool, inp, ctx):
    """Set-Content в PowerShell 5.1 пишет cp1251, Out-File — UTF-16/BOM: порча исходников под source_roots."""
    roots = [norm(r).rstrip('\\') + '\\' for r in CFG.get('source_roots', [])]
    if tool != 'PowerShell' or not roots:
        return None
    cmd = inp.get('command', '')
    for m in re.finditer(r'\b(Set-Content|Out-File|Add-Content)\b([^|;\n]*)', cmd, re.I):
        args = m.group(2)
        paths = re.findall(r'((?:[A-Za-z]:\\)[^\s"\'`;|]*)', args)
        bom_ok = re.search(r'-Encoding\s+utf8\b', args, re.I)
        for p in paths:
            if not any(norm(p).startswith(r) for r in roots):
                continue
            ext = os.path.splitext(p)[1].lower()
            if bom_ok and ext in ('.cs', '.md', '.ps1', '.txt'):
                continue
            if ext in ('.cs', '.py', '.ps1', '.psm1', '.md', '.json', '.js', '.ts', '.uc', '.sh', '.xaml', '.csproj',
                       '.yml', '.yaml', '.toml', '.txt', '.html', '.css', '.cmd', '.bat', '.ini'):
                return f'{m.group(1)} в {p}: кодировка испортится (cp1251 или BOM). Правьте через Edit/Write.'
    return None


def rule_router_ssh_agent(tool, inp, ctx):
    """ssh на хост из ssh_hosts без IdentityAgent=SSH_AUTH_SOCK: ключ из агента Windows не доходит — Permission denied."""
    if tool not in ('Bash', 'PowerShell'):
        return None
    cmd = code_only(inp.get('command', ''))
    for host, hint in CFG.get('ssh_hosts', {}).items():
        if re.search(r'\b(ssh|scp)(\.exe)?\s[^\n|;&]*' + re.escape(host), cmd) and \
                'IdentityAgent=SSH_AUTH_SOCK' not in inp.get('command', ''):
            return f'ssh на {host} без -o IdentityAgent=SSH_AUTH_SOCK: ключ не дойдёт. {hint}'
    return None


def rule_shared_index_add_all(tool, inp, ctx):
    """git add -A / commit -a в репозитории, где индекс общий с соседней сессией (shared_index), забирает чужие файлы."""
    shared = CFG.get('shared_index', [])
    if tool not in ('Bash', 'PowerShell') or not shared:
        return None
    cur = ctx.get('cwd', '')
    for seg in re.split(r'&&|\|\||;|\n|\|', code_only(inp.get('command', ''))):   # каталог — на момент git add
        m = re.match(r'\s*(?:cd|Set-Location|sl|pushd)\s+(?:/d\s+)?["\']?([^"\'\s]+)', seg)
        if m:
            cur = m.group(1)
        m = re.search(r'\bgit\b(?:\s+-C\s+(\S+))?[^\n]*\b(add\s+(-A|--all|\.)(\s|$)|commit\s+(-\w*a\w*|--all)\b)', seg)
        if m:
            d = norm(m.group(1) or cur)
            if any(re.search(s, d, re.I) for s in shared) and '\\.claude\\worktrees\\' not in d:
                return f'git add -A / commit -a в общем индексе ({d}): перечислите пути (git add -- <пути>).'
    return None


FIXES = [fix_ps1_bom, fix_ps_curl, fix_ps_pipe_bom, fix_python_isolated_utf8, fix_long_heredoc]   # в файл — последним
RULES = [v for k, v in sorted(globals().items()) if k.startswith('rule_')]


def log_hit(rule, tool, inp, call):
    """Срабатывание — в журнал граблей (pitfalls.py рядом); журнала нет или сломан — сторож работает дальше."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import pitfalls
        pitfalls.log_guard(rule, tool, inp, call.get('cwd', ''), call.get('session_id', ''), call.get('tool_use_id', ''))
    except Exception:
        pass


def decide(call):
    """Ответ хука (dict) или None."""
    tool, inp = call.get('tool_name', ''), call.get('tool_input') or {}
    fixed = None
    for fix in FIXES:
        new = fix(tool, fixed or inp, call)
        if new is not None:
            fixed = new
            log_hit(fix.__name__[4:], tool, inp, call)
    cur = fixed or inp
    for rule in RULES:
        why = rule(tool, cur, call)
        if why:
            kind, text = why if isinstance(why, tuple) else ('deny', why)
            log_hit(rule.__name__[5:], tool, cur, call)
            return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': kind,
                                           'permissionDecisionReason': f'Сторож ({rule.__name__[5:]}): {text}'}}
    if fixed:
        return {'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'updatedInput': fixed}}
    return None


def main():
    try:
        call = json.loads(sys.stdin.buffer.read().decode('utf-8-sig', 'replace') or '{}')
        out = decide(call)
        if out:   # ASCII-JSON: кириллица через \u, консольная кодировка Windows её не испортит (C, п. 9)
            sys.stdout.buffer.write(json.dumps(out).encode('ascii'))
    except Exception:   # сломанный сторож не должен останавливать работу
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
