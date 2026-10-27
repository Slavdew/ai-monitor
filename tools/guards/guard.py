r"""Сторож вызовов инструментов Claude Code: частые грабли, которые проще не допустить, чем чинить.

Хук PreToolUse (ставит tools/task-monitor/install.py): JSON вызова на stdin. Нарушение — код 2 и причина в
stderr: Claude Code отменяет этот один вызов и показывает причину агенту, тот переделывает. Ввод
владельца не блокируется. Любая собственная ошибка сторожа — код 0: сторож не должен мешать работе.

Правила — функции rule_*(tool, inp) → текст нарушения или None. Новое правило — ещё одна функция.
"""
import json
import os
import re
import sys

HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


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


def rule_heredoc_backslash(tool, inp):
    """Обратный слеш в теле heredoc приходит в Bash-инструменте искажённым: «\\n» → перевод строки,
    «<tasks_root>» → табуляции (память heredoc-backslash: больше десятка случаев с 19.09.2026)."""
    if tool != 'Bash':
        return None
    for body in heredoc_bodies(inp.get('command', '')):
        if '\\' in body:
            return ('В теле heredoc есть обратный слеш — в этом окружении он искажается (\\n, \\t в путях Windows). '
                    'Запишите скрипт файлом через Write (в scratchpad) и запустите его, а правку кода с «\\» '
                    'сделайте через Edit.')
    return None


def rule_sed_windows_path(tool, inp):
    """sed -i с путём Windows в замене съедает обратные слеши (23.10.2026, дважды за вечер)."""
    if tool != 'Bash':
        return None
    cmd = inp.get('command', '')
    if re.search(r'\bsed\b[^|;&]*\s(-i|--in-place)', cmd) and re.search(r'[A-Za-z]:\\', cmd):
        return 'sed -i с путём Windows: обратные слеши в замене теряются. Сделайте правку через Edit.'
    return None


RULES = [v for k, v in sorted(globals().items()) if k.startswith('rule_')]


def log_refusal(rule, tool, inp, call):
    """Отказ — в журнал граблей (pitfalls.py рядом); журнала нет или сломан — отказ всё равно в силе."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import pitfalls
        pitfalls.log_guard(rule, tool, inp, call.get('cwd', ''), call.get('session_id', ''))
    except Exception:
        pass


def main():
    try:
        call = json.loads(sys.stdin.buffer.read().decode('utf-8', 'replace') or '{}')
        tool, inp = call.get('tool_name', ''), call.get('tool_input') or {}
        for rule in RULES:
            why = rule(tool, inp)
            if why:
                sys.stderr.buffer.write(f'Сторож ({rule.__name__[5:]}): {why}\n'.encode('utf-8'))
                log_refusal(rule.__name__[5:], tool, inp, call)
                return 2
    except Exception:   # сломанный сторож не должен останавливать работу
        return 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
