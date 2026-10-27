"""Панель задач: python server.py → http://127.0.0.1:<port>.

Цикл раз в tick_sec собирает источники (monitor.sources.*) в одно состояние; меняется содержимое —
растёт rev, и страница по SSE (/events) забирает /state.json. Источник с every_sec обновляется в
своём потоке, цикл берёт последний результат. Запись со страницы — POST /api/<источник>/<действие>.
"""
import hashlib
import json
import mimetypes
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from monitor.core import HOME, allowed_file, in_tab_root, load_config, log_call, module_on, modules, now_ms, sources  # noqa: E402

FAILS, PAUSE_SEC = 5, 600   # ошибок модуля подряд до паузы, длина паузы
OWNER_ONLY = {('tabs', 'draft'), ('tabs', 'submit'), ('tabs', 'request')}

WEB = os.path.join(HOME, 'web')
TYPES = {'.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css'}
TEXT_EXT = {'.md', '.markdown', '.txt', '.log', '.json', '.py', '.ps1', '.cmd', '.js', '.mjs', '.css', '.sh', '.cs', '.yml', '.yaml', '.xml', '.bsl'}


def local_file(cfg, path):
    """/f/C/Dev/x.md → <dev>\\x.md, если файл есть и лежит в корне вкладки или в file_roots."""
    from urllib.parse import unquote
    m = re.match(r'^/f/([A-Za-z])/(.+)$', path)
    if not m:
        return None
    return allowed_file(cfg, f'{m.group(1)}:\\' + unquote(m.group(2)).replace('/', '\\'))


class Hub:
    def __init__(self):
        self.cfg = load_config()
        self.data, self.slow, self.rev, self.body = {}, {}, 0, b'{}'
        self.wake = {}   # медленный источник -> событие: POST /api/<источник>/refresh будит его сразу
        self.digest = ''
        self.changed = threading.Condition()
        self.kick = threading.Event()
        self.fails, self.last_err = {}, {}   # модуль -> (ошибок подряд, пауза до); последняя ошибка

    def _slow(self, name, src, mod):
        wake = self.wake.setdefault(name, threading.Event())
        while True:
            self.slow[name] = self._guarded(name, lambda: mod.collect(self.cfg, src, {}))
            self.kick.set()
            wake.wait(src['every_sec'])
            wake.clear()

    def start(self):
        for name, src, mod in sources(self.cfg):
            if src.get('every_sec'):
                threading.Thread(target=self._slow, args=(name, src, mod), daemon=True).start()
        threading.Thread(target=self._loop, daemon=True).start()

    def _guarded(self, name, fn):
        """Вызов модуля (A4): ошибка — {'error'}; FAILS подряд — пауза PAUSE_SEC, модуль не дёргается зря."""
        n, until = self.fails.get(name, (0, 0))
        if until > time.time():
            return {'error': self.last_err.get(name, ''), 'paused_until': int(until * 1000)}
        try:
            out = fn()
            self.fails.pop(name, None)
            return out
        except Exception as e:  # модуль не валит сервер
            self.last_err[name] = f'{type(e).__name__}: {e}'
            n += 1
            self.fails[name] = (0, time.time() + PAUSE_SEC) if n >= FAILS else (n, 0)
            return {'error': self.last_err[name]}

    def collect(self):
        ctx = {}
        for name, src, mod in sources(self.cfg):
            if src.get('every_sec'):
                ctx[name] = self.slow.get(name, {'pending': True})
                continue
            ctx[name] = self._guarded(name, lambda: mod.collect(self.cfg, src, ctx))
        mods = {n: {'title': r['manifest'].get('title', ''), 'version': r['manifest'].get('version', ''), 'core': r['core'],
                    'on': module_on(self.cfg, n), 'error': r['error'], 'dir': r['dir']}
                for n, r in modules(self.cfg).items()}
        ctx_all = {**ctx, 'core': {'modules': mods}}
        raw = json.dumps(ctx_all, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha1(raw.encode()).hexdigest()
        if digest != self.digest:
            with self.changed:
                self.digest, self.rev = digest, self.rev + 1
                self.body = json.dumps({'rev': self.rev, 'at': now_ms(), 'sources': ctx_all,
                                        'model': self.cfg.get('model', {}), 'ui': self.cfg.get('ui', {})},
                                       ensure_ascii=False).encode()
                self.changed.notify_all()

    def _loop(self):
        while True:
            self.collect()
            self.kick.wait(self.cfg.get('tick_sec', 2))
            self.kick.clear()

    def act(self, source, action, args):
        if source == 'core' and action == 'reload':   # пересобрать реестр модулей без перезапуска
            modules(self.cfg, reload=True)
            log_call(self.cfg, 'page', 'core/reload', args, True, 0)
            self.kick.set()
            return {'ok': True, 'note': 'новые медленные модули (every_sec) — после перезапуска сервера'}
        if action == 'refresh' and source in self.wake:
            self.wake[source].set()
            log_call(self.cfg, 'page', f'{source}/refresh', args, True, 0)
            return {'ok': True}
        if isinstance(args.get('dir'), str) and not in_tab_root(self.cfg, args['dir']):
            raise PermissionError('каталог вне корней вкладок')   # страница пишет только в задачи панели
        for name, src, mod in sources(self.cfg):
            if name == source and action in getattr(mod, 'ACTIONS', {}):
                t0, ok = time.time(), False
                try:
                    res = mod.ACTIONS[action](self.cfg, src, args)
                    ok = True
                finally:
                    log_call(self.cfg, 'page', f'{source}/{action}', args, ok, int((time.time() - t0) * 1000))
                self.kick.set()
                return res
        raise KeyError(f'{source}/{action}')


hub = Hub()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype='application/json', sandbox=False):
        self.send_response(code)
        self.send_header('Content-Type', ctype if ';' in ctype or not ctype.startswith(('text/', 'application/json')) else f'{ctype}; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        # чужие HTML-файлы — в песочнице: свой пустой origin, до /api не достанут
        if sandbox: self.send_header('Content-Security-Policy', 'sandbox allow-scripts allow-popups allow-forms')
        self.end_headers()
        self.wfile.write(body)

    def _guard(self, post):
        """Только свой адрес: чужой сайт в браузере (подмена DNS, форма на POST) получает 403."""
        port = hub.cfg.get('port', 8770)
        hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}
        if self.headers.get('Host') not in hosts or (post and self.headers.get('Origin') not in {None, *(f'http://{h}' for h in hosts)}):
            self._send(403, b'{"error": "host"}')
            return False
        return True

    def do_GET(self):
        if not self._guard(False):
            return
        path = self.path.split('?')[0]
        if path.startswith('/f/'):
            p = local_file(hub.cfg, path)
            if not p:
                return self._send(404, json.dumps({'error': 'файла нет или он вне корней вкладок и file_roots'}, ensure_ascii=False).encode())
            ext = os.path.splitext(p)[1].lower()
            ctype = 'text/plain' if ext in TEXT_EXT else (mimetypes.guess_type(p)[0] or 'application/octet-stream')
            with open(p, 'rb') as f:
                return self._send(200, f.read(), ctype, sandbox=True)
        if path == '/state.json':
            return self._send(200, hub.body)
        if path == '/events':
            return self._events()
        name = 'index.html' if path == '/' else os.path.basename(path)
        file = os.path.join(WEB, name)
        if os.path.isfile(file):
            with open(file, 'rb') as f:
                return self._send(200, f.read(), TYPES.get(os.path.splitext(name)[1], 'text/plain'))
        self._send(404, b'{}')

    def _events(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        seen = -1
        try:
            while True:
                with hub.changed:
                    if hub.rev == seen:
                        hub.changed.wait(15)
                    rev = hub.rev
                self.wfile.write(f'data: {rev}\n\n'.encode() if rev != seen else b': ping\n\n')
                self.wfile.flush()
                seen = rev
        except OSError:
            pass

    def do_POST(self):
        if not self._guard(True):
            return
        parts = self.path.strip('/').split('/')
        if len(parts) != 3 or parts[0] != 'api':
            return self._send(404, b'{}')
        # слова владельца (ответы, черновики, просьбы) — только со страницы: браузер ставит Origin на POST,
        # curl и task.py api — нет. Иначе агент мог бы «ответить» себе от имени владельца (аудит 27.10)
        if (parts[1], parts[2]) in OWNER_ONLY and self.headers.get('Origin') is None:
            return self._send(403, json.dumps({'error': 'действие владельца — только со страницы панели'}, ensure_ascii=False).encode())
        size = int(self.headers.get('Content-Length', 0))
        if size > 1 << 20:
            return self._send(413, b'{"error": "body"}')
        try:
            args = json.loads(self.rfile.read(size) or b'{}')
            res = hub.act(parts[1], parts[2], args)
            self._send(200, json.dumps(res, ensure_ascii=False).encode())
        except Exception as e:
            self._send(400, json.dumps({'error': f'{type(e).__name__}: {e}'}, ensure_ascii=False).encode())


if __name__ == '__main__':
    hub.start()
    port = hub.cfg.get('port', 8765)
    print(f'task-monitor: http://127.0.0.1:{port}', flush=True)
    ThreadingHTTPServer.allow_reuse_address = False   # на Windows иначе молча делит порт с чужим сервером
    ThreadingHTTPServer(('127.0.0.1', port), Handler).serve_forever()
