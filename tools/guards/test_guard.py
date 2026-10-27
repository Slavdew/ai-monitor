r"""Проверка сторожа на настоящих командах из транскриптов и аудитов: python test_guard.py → «N ошибок»."""
import json
import os
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')   # консоль cp1251 не печатает «→»
GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'guard.py')
BS = '\\'
TMP = tempfile.mkdtemp()
NOGIT = tempfile.mkdtemp()                                              # каталог вне git для worktree-правила
ZAP = 'C:\\Work\\shop'                                                  # условные репозитории из настроек ниже
HOST = '10.0.0.1'
CFG = {'secret_files': [r'router-password\.txt', r'-signing\.pem(?!\.pub)'],
       'release': {r'\\shop(\\|$)': r'^(master|[\w./-]*-v\d[\w.-]*)$', r'\\site(\\|$)': r'^(main|release/[\w.-]+)$'},
       'shared_index': [r'\\shop(\\|$)'], 'ssh_hosts': {HOST: 'См. заметку о входе.'}, 'source_roots': ['C:\\Work']}
with open(os.path.join(TMP, 'guards.json'), 'w', encoding='utf-8') as f:
    json.dump(CFG, f)
ENV = dict(os.environ, PITFALLS_LOG=os.path.join(TMP, 'log.jsonl'),   # срабатывания — не в живой журнал
           GUARD_CONFIG=os.path.join(TMP, 'guards.json'))


def run(tool, inp, cwd=''):
    call = {'tool_name': tool, 'tool_input': inp, 'cwd': cwd, 'scratchpad_dir': TMP, 'tool_use_id': 'toolu_t'}
    r = subprocess.run([sys.executable, GUARD], input=json.dumps(call).encode(), capture_output=True, env=ENV)
    out = json.loads(r.stdout) if r.stdout.strip() else {}
    h = out.get('hookSpecificOutput', {})
    if 'permissionDecision' in h:
        return h['permissionDecision'], h['permissionDecisionReason']
    if 'updatedInput' in h:
        return 'fix', h['updatedInput']
    return 'pass', r.returncode


LONG = "python - <<'EOF'\n" + "print('x')\n" * 700 + 'EOF'
CASES = [
    # (имя, инструмент, ввод, cwd, ждём: pass | deny | ask | fix, проверка исправления)
    ('heredoc с путём Windows', 'Bash', {'command': f"python - <<'PYEOF'\nsub('README.md', 'x', 'C:{BS}Work{BS}docs')\nPYEOF"}, '', 'deny', None),
    ('heredoc с \\n в строке', 'Bash', {'command': f"python - <<'EOF'\nprint('a{BS}nb')\nEOF\necho done"}, '', 'deny', None),
    ('heredoc без слеша', 'Bash', {'command': "python - <<'EOF'\nprint('ok')\nEOF"}, '', 'pass', None),
    ('слеш вне heredoc', 'Bash', {'command': f"python - <<'EOF'\nprint(1)\nEOF\nls C:{BS}Work"}, '', 'pass', None),
    ('heredoc коммита без слеша', 'Bash', {'command': 'git commit -m "$(cat <<\'EOF\'\nтекст\nEOF\n)"'}, '', 'pass', None),
    ('sed -i с путём Windows', 'Bash', {'command': f"sed -i 's|x|C:{BS}Work{BS}docs|' a.md"}, '', 'deny', None),
    ('sed без -i', 'Bash', {'command': f"sed -n 1p C:{BS}a.txt"}, '', 'pass', None),
    ('обычная команда', 'Bash', {'command': 'git status --short'}, '', 'pass', None),
    # длинный heredoc → в файл
    ('длинный heredoc → bash <файл>', 'Bash', {'command': LONG}, '', 'fix',
     lambda i: i['command'].startswith("bash '") and open(i['command'][6:-1], encoding='utf-8').read().strip() == LONG),
    ('короткий heredoc не трогаем', 'Bash', {'command': "cat > a <<'EOF'\nx\nEOF"}, '', 'pass', None),
    # секреты
    ('Read .credentials.json', 'Read', {'file_path': 'C:\\Users\\me\\.claude\\.credentials.json'}, '', 'deny', None),
    ('cat пароля роутера', 'Bash', {'command': 'cat C:/Work/router/router-password.txt'}, '', 'deny', None),
    ('проверка «задан» — можно', 'Bash', {'command': '[ -s router/router-password.txt ] && echo задан'}, '', 'pass', None),
    ('публичный ключ — можно', 'Bash', {'command': 'cat keys/release-signing.pub.pem'}, '', 'pass', None),
    ('env без фильтра', 'Bash', {'command': 'env'}, '', 'deny', None),
    ('env с маской — можно', 'Bash', {'command': "env | sed 's/=.*/=…/'"}, '', 'pass', None),
    # прокси
    ('netsh winhttp set', 'PowerShell', {'command': 'netsh winhttp set proxy 127.0.0.1:8080'}, '', 'deny', None),
    ('реестр ProxyEnable', 'PowerShell', {'command': "Set-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings' ProxyEnable 0"}, '', 'deny', None),
    ('чтение прокси — можно', 'Bash', {'command': 'reg query "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings" | grep -i proxy'}, '', 'pass', None),
    ('прокси в тексте памяти — можно', 'Bash', {'command': "echo 'не трогать netsh winhttp set proxy' >> notes.md"}, '', 'pass', None),
    # push = выпуск
    ('push master выпускного репозитория', 'Bash', {'command': f'cd {ZAP} && git push origin master'}, '', 'ask', None),
    ('push тега выпуска', 'Bash', {'command': 'git push origin router-v0.2.3'}, ZAP, 'ask', None),
    ('push dev — можно', 'Bash', {'command': 'git push origin dev'}, 'C:\\Work\\site', 'pass', None),
    ('push HEAD:main', 'PowerShell', {'command': 'git -C C:\\Work\\site push origin HEAD:main'}, '', 'ask', None),
    ('push в другом репозитории — можно', 'Bash', {'command': 'git push origin main'}, 'C:\\Work\\lib', 'pass', None),
    ('текст про push — можно', 'Bash', {'command': f"cd {ZAP} && echo 'git push origin master — после да' >> notes.md"}, '', 'pass', None),
    # worktree вне git
    ('Agent worktree вне git', 'Agent', {'prompt': 'x', 'isolation': 'worktree'}, NOGIT, 'deny', None),
    ('Agent worktree в git — можно', 'Agent', {'prompt': 'x', 'isolation': 'worktree'}, os.path.dirname(GUARD), 'pass', None),
    # PowerShell
    ('.ps1 с кириллицей → BOM', 'Write', {'file_path': 'a.ps1', 'content': 'Write-Host "привет"'}, '', 'fix',
     lambda i: i['content'] == '\ufeffWrite-Host "привет"'),
    ('.ps1 ASCII не трогаем', 'Write', {'file_path': 'a.ps1', 'content': 'Write-Host hi'}, '', 'pass', None),
    ('curl → curl.exe', 'PowerShell', {'command': 'curl -s https://example.com'}, '', 'fix',
     lambda i: i['command'] == 'curl.exe -s https://example.com'),
    ('curl внутри here-string не трогаем', 'PowerShell', {'command': "$s = @'\ncurl -s x\n'@\n$s | Out-Null"}, '', 'pass', None),
    ('конвейер в ssh → $OutputEncoding', 'PowerShell', {'command': '"uname -a" | ssh -o IdentityAgent=SSH_AUTH_SOCK root@10.0.0.1 sh'}, '', 'fix',
     lambda i: i['command'].startswith('$OutputEncoding = New-Object System.Text.UTF8Encoding $false; ')),
    ('python -I → -X utf8', 'Bash', {'command': 'PYTHONIOENCODING=utf-8 py -I x.py'}, '', 'fix',
     lambda i: i['command'] == 'PYTHONIOENCODING=utf-8 py -I -X utf8 x.py'),
    ('python -I уже с -X utf8', 'Bash', {'command': 'py -I -X utf8 x.py'}, '', 'pass', None),
    ('Set-Content в исходник', 'PowerShell', {'command': "Set-Content C:\\Work\\shop\\src\\Router.cs $t"}, '', 'deny', None),
    ('Set-Content -Encoding utf8 в .md — можно', 'PowerShell', {'command': "Set-Content -Encoding utf8 C:\\Work\\notes\\README.md 'x'"}, '', 'pass', None),
    ('Set-Content во временный — можно', 'PowerShell', {'command': "Set-Content $env:TEMP\\a.json '{}'"}, '', 'pass', None),
    ('ssh к роутеру без агента', 'Bash', {'command': 'ssh root@10.0.0.1 uptime'}, '', 'deny', None),
    ('адрес роутера в heredoc — можно', 'Bash', {'command': "python - <<'EOF'\nprint('ssh root@10.0.0.1')\nEOF"}, '', 'pass', None),
    ('git add -A в общем индексе', 'Bash', {'command': f'cd {ZAP} && git add -A && git commit -m x'}, '', 'deny', None),
    ('git add -A в worktree агента — можно', 'Bash', {'command': 'git add -A'}, ZAP + '\\.claude\\worktrees\\agent-a1', 'pass', None),
    ('git add путей — можно', 'Bash', {'command': 'git add -- router/x.uc'}, ZAP, 'pass', None),
]

fails = 0
for name, tool, inp, cwd, want, check in CASES:
    kind, val = run(tool, inp, cwd)
    ok = kind == want and (check is None or check(val)) and (kind not in ('deny', 'ask') or 'Сторож' in val)
    print(('ok   ' if ok else 'FAIL ') + name + ('' if ok else f' — {kind}: {val!r}'[:300]))
    fails += not ok
r = subprocess.run([sys.executable, GUARD], input=b'not json', capture_output=True, env=ENV)
ok = r.returncode == 0 and not r.stdout
print(('ok   ' if ok else 'FAIL ') + 'испорченный ввод → пустой ответ, код 0 (сторож не мешает)')
fails += not ok
logged = [json.loads(l)['id'] for l in open(ENV['PITFALLS_LOG'], encoding='utf-8')]
want_ids = {'guard.heredoc_backslash', 'guard.release_push', 'guard.ps1_bom', 'guard.long_heredoc', 'guard.secrets'}
ok = want_ids <= set(logged)
print(('ok   ' if ok else 'FAIL ') + f'срабатывания записаны в журнал граблей ({len(logged)})' + ('' if ok else f': нет {want_ids - set(logged)}'))
fails += not ok
print(f'{fails} ошибок')
