"""Лимиты подписки — из файла, который пишет строка состояния Claude Code (statusline.py рядом).

Источник — документированное поле rate_limits входа строки состояния: 5 часов и неделя, только Pro и Max, после
первого ответа в сессии. Прежний способ (oauth/usage с токеном подписки) убран 28.10: он не документирован и не
разрешён условиями Anthropic. Лимита по отдельной модели (Fable и т. п.) в документированных данных нет.
"""
import os

from monitor.core import read_json


def collect(cfg, src, ctx):
    data = read_json(os.path.join(cfg['state_dir'], 'usage.json'), None)
    if not data or not data.get('limits'):
        return {'error': 'нет данных: строка состояния Claude Code ещё не передавала лимиты (только Pro и Max, '
                         'после первого ответа в сессии; ставит install.py)'}
    return {'limits': data['limits'], 'ok_at': data.get('ok_at')}
