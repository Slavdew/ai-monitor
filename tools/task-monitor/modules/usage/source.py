"""Лимиты подписки — из файла, который пишет строка состояния Claude Code (statusline.py рядом).

Источник — документированное поле rate_limits входа строки состояния: 5 часов и неделя, только Pro и Max, после
первого ответа в сессии. Прежний способ (oauth/usage с токеном подписки) убран 28.10: он не документирован и не
разрешён условиями Anthropic. Лимита по отдельной модели (Fable и т. п.) в документированных данных нет.
Окно, у которого прошло время сброса (resets_at), показывается сброшенным (0 %), пока строка состояния не пришлёт
новое: иначе панель держала бы заполненный лимит, который уже восстановился (владелец 28.10).
"""
import os
from datetime import datetime

from monitor.core import read_json


def _past(iso):
    try:
        return datetime.fromisoformat(iso).timestamp() <= datetime.now().timestamp()
    except (TypeError, ValueError):
        return False


def collect(cfg, src, ctx):
    data = read_json(os.path.join(cfg['state_dir'], 'usage.json'), None)
    if not data or not data.get('limits'):
        seen = read_json(os.path.join(cfg['state_dir'], 'statusline-seen.json'), None)
        why = ('строка состояния вызывалась, но лимитов не передала (только Pro и Max, после первого ответа)' if seen
               else 'строка состояния ещё не вызывалась: она подхватывается при запуске сессии — перезапустите '
                    'сессию после install.py; окно VS Code может её не запускать, тогда лимиты придут от сессии в терминале')
        return {'empty': True, 'note': why}
    limits = [dict(x, percent=0, reset=True) if _past(x.get('resets_at')) else x for x in data['limits']]
    return {'limits': limits, 'ok_at': data.get('ok_at')}
