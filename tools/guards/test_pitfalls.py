r"""Журнал граблей на настоящих ошибках из транскриптов: python test_pitfalls.py → «N ошибок»."""
import json
import os
import subprocess
import sys
import tempfile

sys.stdout.reconfigure(encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'pitfalls.py')
os.environ['PITFALLS_LOG'] = os.path.join(tempfile.mkdtemp(), 'log.jsonl')   # до импорта: LOG читается при загрузке
sys.path.insert(0, HERE)
import pitfalls  # noqa: E402

CASES = [
    # (имя, инструмент, ввод, вывод, ждём id)
    ('усечённый heredoc', 'Bash', "python - <<'EOF'\n…", "Exit code 2\n/usr/bin/bash: -c: line 237: unexpected EOF "
     "while looking for matching `''", 'bash.heredoc-truncated'),
    ('worktree-сторож', 'Bash', 'git status; echo $?', 'This agent is isolated in the worktree C:\\x but this command '
     'is too complex to verify', 'wt.isolation-refused'),
    ('съеденный \\r в Edit', 'Edit', 'a.cs\nline.TrimEnd(\'\r\')', '<tool_use_error>String to replace not found in '
     'file.', 'edit.escape-lost'),
    ('обычный not found', 'Edit', 'a.cs\nfoo()', '<tool_use_error>String to replace not found in file.',
     'edit.not-found'),
    ('charmap', 'Bash', 'py x.py', "Traceback (most recent call last):\n  File …\nUnicodeEncodeError: 'charmap' codec "
     "can't encode character '\\u2192'", 'py.charmap'),
    ('тип исключения из конца длинной трассировки', 'Bash', 'py x.py', 'Traceback (most recent call last):\n'
     + '  File "x.py", line 1, in f\n' * 60 + 'KeyError: \'a\'', 'py.traceback.KeyError'),
    ('BOM в конвейере', 'PowerShell', '"true" | ssh r sh', 'sh: line 1: \ufefftrue: not found', 'ps.pipe-bom'),
    ('NativeCommandError', 'PowerShell', 'git log 2>&1', 'git : warning\n    + CategoryInfo : NotSpecified: '
     '(warning:String) [], RemoteException\n    + FullyQualifiedErrorId : NativeCommandError', 'ps.native-stderr'),
    ('отказ сторожа из транскрипта', 'Bash', 'cat > a <<EOF', 'PreToolUse:Bash hook error: Сторож (heredoc_backslash): '
     'В теле heredoc', 'guard.heredoc_backslash'),
    ('голый код выхода', 'Bash', 'false', 'Exit code 1', 'exit.nonzero'),
    ('ls нет файла', 'Bash', 'ls x', "Exit code 2\nls: cannot access 'x': No such file or directory", 'fs.missing'),
    ('FileNotFoundError — не fs.missing', 'Bash', 'py x', "Traceback (most recent call last):\nFileNotFoundError: "
     "[Errno 2] No such file or directory: 'a'", 'py.traceback.FileNotFoundError'),
]

fails = 0


def check(name, ok, extra=''):
    global fails
    print(('ok   ' if ok else 'FAIL ') + name + ('' if ok else ' — ' + extra))
    fails += not ok


for name, tool, inp, out, want in CASES:
    got = pitfalls.record(tool, inp, out, 'test')['id']
    check(name, got == want, f'получили {got}')

a = pitfalls.record('Bash', 'x', 'Exit code 3\nfoo: bar 17 at C:\\Dev\\a.txt', 'test')
b = pitfalls.record('Bash', 'y', 'Exit code 9\nfoo: bar 42 at D:\\Work\\b.txt', 'test')
check('неизвестная ошибка: одинаковый fp при других числах и путях', a['fp'] == b['fp'] and a['id'] == 'unclassified',
      f"{a['fp']} {b['fp']}")
r = pitfalls.record('Bash', 'curl -H "Authorization: Bearer abc123"', 'token=sk-ant-xyz', 'test')
check('секреты вырезаны', 'abc123' not in r['in'] and 'sk-ant' not in r['out'], json.dumps(r, ensure_ascii=False))


def hook(ev):
    return subprocess.run([sys.executable, SCRIPT, 'hook'], input=ev if isinstance(ev, bytes) else
                          json.dumps(ev).encode(), capture_output=True).returncode


check('успешный вызов не пишется', hook({'hook_event_name': 'PostToolUse', 'tool_name': 'Bash',
                                         'tool_input': {'command': 'ls'}, 'tool_response': {'stdout': 'a'}}) == 0
      and not os.path.exists(os.environ['PITFALLS_LOG']))
check('ошибка пишется', hook({'hook_event_name': 'PostToolUseFailure', 'tool_name': 'Read', 'session_id': 's1',
                              'tool_input': {'file_path': 'C:\\nope'}, 'error': 'File does not exist.'}) == 0
      and pitfalls.read_log()[-1]['id'] == 'read.missing')
check('испорченный ввод → 0', hook(b'not json') == 0)
before = [r['id'] for r in pitfalls.read_log()]
subprocess.run([sys.executable, SCRIPT, 'reclassify'], capture_output=True)
check('reclassify не меняет id', [r['id'] for r in pitfalls.read_log()] == before)
print(f'{fails} ошибок')
