r"""Проверка сторожа на настоящих командах, которые ломали файлы: python test_guard.py → «N ошибок»."""
import json
import os
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')   # консоль cp1251 не печатает «→»
GUARD = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'guard.py')
BS = '\\'
ENV = dict(os.environ, PITFALLS_LOG=os.path.join(tempfile.mkdtemp(), 'log.jsonl'))   # отказы — не в живой журнал


def run(tool, **inp):
    r = subprocess.run([sys.executable, GUARD], input=json.dumps({'tool_name': tool, 'tool_input': inp}).encode(),
                       capture_output=True, env=ENV)
    return r.returncode, r.stderr.decode('utf-8')


CASES = [
    # (имя, инструмент, ввод, ждём код)
    ('heredoc с путём Windows', 'Bash', {'command': f"python - <<'PYEOF'\nsub('README.md', 'x', 'D:{BS}AI{BS}tasks')\nPYEOF"}, 2),
    ('heredoc с \\n в строке', 'Bash', {'command': f"python - <<'EOF'\nprint('a{BS}nb')\nEOF\necho done"}, 2),
    ('heredoc без слеша', 'Bash', {'command': "python - <<'EOF'\nprint('ok')\nEOF"}, 0),
    ('слеш вне heredoc', 'Bash', {'command': f"python - <<'EOF'\nprint(1)\nEOF\nls C:{BS}Dev"}, 0),
    ('heredoc коммита без слеша', 'Bash', {'command': 'git commit -m "$(cat <<\'EOF\'\nтекст\nEOF\n)"'}, 0),
    ('sed -i с путём Windows', 'Bash', {'command': f"sed -i 's|x|D:{BS}AI{BS}tasks|' a.md"}, 2),
    ('sed без -i', 'Bash', {'command': f"sed -n 1p C:{BS}a.txt"}, 0),
    ('обычная команда', 'Bash', {'command': 'git status --short'}, 0),
    ('не Bash', 'Edit', {'file_path': 'a', 'old_string': f'{BS}n', 'new_string': 'x'}, 0),
]

fails = 0
for name, tool, inp, want in CASES:
    code, err = run(tool, **inp)
    ok = code == want and (want == 0 or 'Сторож' in err)
    print(('ok   ' if ok else 'FAIL ') + name + ('' if ok else f' — код {code}: {err.strip()}'))
    fails += not ok
code, _ = subprocess.run([sys.executable, GUARD], input=b'not json', capture_output=True, env=ENV).returncode, ''
print(('ok   ' if code == 0 else 'FAIL ') + 'испорченный ввод → 0 (сторож не мешает)')
fails += code != 0
logged = [json.loads(l)['id'] for l in open(ENV['PITFALLS_LOG'], encoding='utf-8')]
ok = logged.count('guard.heredoc_backslash') == 2 and logged.count('guard.sed_windows_path') == 1
print(('ok   ' if ok else 'FAIL ') + f'отказы записаны в журнал граблей: {logged}')
fails += not ok
print(f'{fails} ошибок')
sys.exit(1 if fails else 0)
