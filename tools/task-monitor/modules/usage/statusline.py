"""Строка состояния Claude Code: лимиты подписки из документированного поля rate_limits — в файл для панели.

Claude Code передаёт команде строки состояния JSON на stdin (code.claude.com/docs/en/statusline): rate_limits
есть только у Pro и Max и только после первого ответа в сессии, окна five_hour и seven_day могут отсутствовать.
Токен подписки и недокументированный oauth/usage не используются (условия Anthropic, решение владельца 28.10).
Печатает короткую строку состояния; файл <state_dir>/usage.json пишется атомарно. Ставит install.py.
"""
import json
import os
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(HERE)))
from monitor.core import load_config, write_json  # noqa: E402

LABELS = (('five_hour', 'session', '5 часов'), ('seven_day', 'weekly_all', 'Неделя'))


def main():
    try:
        data = json.loads(sys.stdin.buffer.read().decode('utf-8', 'replace') or '{}')
    except ValueError:
        data = {}
    rl = data.get('rate_limits') or {}
    limits = []
    for key, kind, label in LABELS:
        w = rl.get(key)
        if isinstance(w, dict) and w.get('used_percentage') is not None:
            at = w.get('resets_at')
            limits.append({'kind': kind, 'label': label, 'percent': round(float(w['used_percentage'])),
                           # ISO со смещением «+03:00»: «+0300» браузер разбирает не везде
                           'resets_at': datetime.fromtimestamp(at).astimezone().isoformat() if at else None})
    if limits:
        try:
            write_json(os.path.join(load_config()['state_dir'], 'usage.json'),
                       {'limits': limits, 'ok_at': int(time.time() * 1000)})
        except Exception:   # строка состояния не должна падать из-за панели
            pass
    model = (data.get('model') or {}).get('display_name') or ''
    print(' · '.join([model] + [f'{x["label"]} {x["percent"]}%' for x in limits]).strip(' ·'))


if __name__ == '__main__':
    main()
