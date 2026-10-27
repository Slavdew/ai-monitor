"""Лимиты подписки — тот же запрос, что панель /usage (недокументированный, может измениться).

curl, а не urllib: через системный прокси urllib рвёт TLS. Токен читается из файла и никуда не
пишется. Опрос не чаще every_sec — его держит сервер; вручную — POST /api/usage/refresh.
Сбой не стирает последние удачные цифры: они остаются с отметкой ok_at, ошибка — рядом.
"""
import json
import os
import subprocess
import time

from monitor.core import expand, now_ms, read_json

URL = 'https://api.anthropic.com/api/oauth/usage'
_last = {}   # последний удачный ответ: limits, ok_at


def fetch(src):
    cred = read_json(expand(src['credentials']), {}) or {}
    token = cred.get('claudeAiOauth', {}).get('accessToken')
    if not token:
        return None, 'нет OAuth-токена в ' + src['credentials']
    try:
        # токен — через stdin (curl -K -), не аргументом: командная строка процесса видна в журналах
        # аудита (событие 4688, Sysmon, EDR)
        p = subprocess.run(['curl', '-s', '--max-time', '20', '-w', '\n%{http_code} %{errormsg}', URL,
                            '-H', 'anthropic-beta: oauth-2025-04-20', '-K', '-'],
                           input=f'header = "Authorization: Bearer {token}"\n'.encode(), capture_output=True, timeout=30,
                           # под pythonw (скрытый запуск) без флага каждый curl открывает окно консоли
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired) as e:
        return None, f'curl: {type(e).__name__}'
    body, _, tail = p.stdout.decode('utf-8', 'replace').rpartition('\n')
    code, _, msg = tail.partition(' ')
    if code == '000':
        return None, f'нет ответа (сеть/прокси): {msg or "curl " + str(p.returncode)}'
    try:
        data = json.loads(body or '{}')
    except ValueError:
        return None, f'HTTP {code}: не JSON: {body[:120]}'
    if not data.get('limits'):
        err = data.get('error')
        detail = err.get('message') or err.get('type') if isinstance(err, dict) else (err or body[:160])
        return None, f'HTTP {code}: {detail}'
    return data, ''


def log(cfg, text):
    """state_dir/usage.log — сбои и чем кончилась серия повторов: по нему видно, когда и как часто рвётся (≤ 256 КБ)."""
    try:
        path = os.path.join(cfg['state_dir'], 'usage.log')
        if os.path.exists(path) and os.path.getsize(path) > 1 << 18:
            os.replace(path, path + '.1')
        with open(path, 'a', encoding='utf-8') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S ') + text + '\n')
    except OSError:
        pass


def collect(cfg, src, ctx):
    data, error = fetch(src)
    tries = 1
    for wait in (5, 20, 60):   # TLS через прокси рвётся на рукопожатии сериями — повторы с паузой
        if not error.startswith('нет ответа'):
            break
        log(cfg, f'попытка {tries}: {error}; повтор через {wait} с')
        time.sleep(wait)
        data, error = fetch(src)
        tries += 1
    if error or tries > 1:
        log(cfg, f'итог после {tries} попыток: {error or "ok"}')
    if data:
        limits = []
        for lim in data['limits']:
            model = ((lim.get('scope') or {}).get('model') or {}).get('display_name')
            label = {'session': '5 часов', 'weekly_all': 'Неделя'}.get(lim.get('kind'), model or lim.get('kind'))
            limits.append({'kind': lim.get('kind'), 'label': label, 'percent': lim.get('percent'),
                           'resets_at': lim.get('resets_at'), 'severity': lim.get('severity')})
        _last.update(limits=limits, ok_at=now_ms())
    out = dict(_last)
    if error:
        out.update(error=error, error_at=now_ms())
    return out
