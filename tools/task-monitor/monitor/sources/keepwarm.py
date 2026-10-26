"""Keepwarm по сессии: флаг <off_dir>/<sid>.off читает tools/keepwarm/keepwarm-gate.js и гасит пинги.

ALL.off — keepwarm выключен для всех: его ставит рубильник model (enabled / keepwarm = false).
"""
import os
import re

from monitor.core import expand, model_on


def collect(cfg, src, ctx):
    d = expand(src['off_dir'])
    all_off = not model_on(cfg, 'keepwarm')
    flag = os.path.join(d, 'ALL.off')
    if all_off != os.path.exists(flag):
        os.makedirs(d, exist_ok=True)
        open(flag, 'w').close() if all_off else os.remove(flag)
    off = [f[:-4] for f in os.listdir(d) if f.endswith('.off') and f != 'ALL.off'] if os.path.isdir(d) else []
    return {'off': sorted(off), 'all_off': all_off}


def set_keepwarm(cfg, src, args):
    sid = args['sid']
    if not re.fullmatch(r'[\w-]+', sid):
        raise ValueError('sid')
    d = expand(src['off_dir'])
    os.makedirs(d, exist_ok=True)
    flag = os.path.join(d, f'{sid}.off')
    if args['on']:
        if os.path.exists(flag):
            os.remove(flag)
    else:
        open(flag, 'w').close()
    return {'ok': True}


ACTIONS = {'set': set_keepwarm}
