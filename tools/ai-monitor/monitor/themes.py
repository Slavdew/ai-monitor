"""Темы оформления: web/themes/<имя>.json и каталоги cfg["theme_dirs"] (свои темы; то же имя — перекрывает).

Структура темы — как у web/themes/default.json: title, fonts {ui, mono, size}, light {цвет: значение}, dark {…}.
Чего в теме нет, берётся из default.json: своя тема может задать только отличия.
"""
import json
import os
import re

from monitor.core import HOME, expand, merge

DEFAULT = 'default'
NAME = re.compile(r'^[\w-]{1,40}$')
VALUE = re.compile(r'^[^;{}<>\\]{1,200}$')   # значение CSS без выхода из объявления


def dirs(cfg):
    return [os.path.join(HOME, 'web', 'themes')] + [expand(d) for d in cfg.get('theme_dirs', [])]


def files(cfg):
    found = {}
    for d in dirs(cfg):
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.endswith('.json') and NAME.match(f[:-5]):
                    found[f[:-5]] = os.path.join(d, f)   # позже — свой каталог, перекрывает поставку
    return found


def read(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load(cfg, name):
    fs = files(cfg)
    base = read(fs.get(DEFAULT, ''))
    return merge(base, read(fs[name])) if name in fs and name != DEFAULT else base


def listing(cfg):
    return [{'name': n, 'title': read(p).get('title') or n} for n, p in files(cfg).items()]


def css(cfg, name=None):
    name = name if name and NAME.match(name) else (cfg.get('ui') or {}).get('look') or DEFAULT
    t = load(cfg, name)

    def decl(d, prefix='--'):
        return '; '.join(f'{prefix}{k}:{v}' for k, v in (d or {}).items()
                         if NAME.match(str(k)) and isinstance(v, str) and VALUE.match(v))
    fonts = decl(t.get('fonts'), '--font-')
    light, dark = decl(t.get('light')), decl(t.get('dark') or t.get('light'))
    return (f'/* тема {name}: {t.get("title", "")} */\n'
            f':root {{ {fonts}; {light} }}\n'
            f'@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ {dark} }} }}\n'
            f':root[data-theme="dark"] {{ {dark} }}\n')
