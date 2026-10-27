r"""Дымовой тест панели: копия панели во временном каталоге, свой порт и state_dir, без браузера; живую панель,
её config.local.json и задачи владельца не трогает.  python tests\smoke.py   (код 0 — всё прошло)
Сценарий: new → add/ask → batch → set (по id) → сервер: state.json, POST draft/submit → hook видит ответ → ack."""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.request

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp(prefix='tm-smoke-')
TM = os.path.join(tmp, 'panel')   # копия: config.local.json владельца не копируется и не перезаписывается
shutil.copytree(SRC, TM, ignore=shutil.ignore_patterns('config.local.json', '__pycache__', 'tests'))
port = 8799
settings = os.path.join(tmp, 'settings.json')
json.dump({'port': port, 'state_dir': os.path.join(tmp, 'state'),
           'tabs': [{'id': 't', 'title': 'T', 'root': os.path.join(tmp, 'tasks')}],
           'sources': {'usage': {'enabled': False}, 'sessions': {'enabled': False}, 'keepwarm': {'enabled': False},
                       'board': {'dir': os.path.join(tmp, 'board')}}}, open(settings, 'w'))
env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'TASK_SESSION': 'smoke-sid'}
# личный файл — из TASK_MONITOR_SETTINGS (в копии нет config.local.json)
env['TASK_MONITOR_SETTINGS'] = settings
fails = []


def tm(*args, stdin=None):
    r = subprocess.run([sys.executable, os.path.join(TM, 'task.py'), *args], input=stdin, capture_output=True,
                       text=True, encoding='utf-8', env=env)
    return r.returncode, r.stdout.strip()


def check(name, cond, got=''):
    print(('ok   ' if cond else 'FAIL ') + name + ('' if cond else f' — {got}'))
    if not cond:
        fails.append(name)


t0 = time.time()
d = os.path.join(tmp, 'tasks', 'a1')
check('new', tm('new', d, '--title', 'Тест', '--plan', 'один', 'два') == (0, 'ok a1'))
check('add', tm('add', 'a1', 'три', 'note=x')[1] == 'ok пункт 3')
code, out = tm('ask', 'a1', 'Делать ли так?', '--opt', 'да', '--opt', 'нет')
check('ask', out == 'ok пункт 4', out)
code, out = tm('batch', 'a1', stdin='# Этап 2\n+ пять | after=3\n1 status=done\n')
check('batch', out == 'ok пункт 5', out)
code, out = tm('batch', 'a1', stdin='+ шесть\n99 status=done\n')
check('batch атомарен', code == 1 and len(json.load(open(os.path.join(d, 'task.json'), encoding='utf-8'))['plan']) == 5, out)
check('set неверный статус → ошибка', tm('set', 'a1', '2', 'status=xx')[0] == 1)
check('show', '2 todo' in tm('show', 'a1')[1] or 'два' in tm('show', 'a1')[1])

srv = subprocess.Popen([sys.executable, os.path.join(TM, 'server.py')], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
try:
    base = f'http://127.0.0.1:{port}'
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # мимо системного прокси
    for _ in range(50):
        try:
            st = json.load(op.open(base + '/state.json')); break
        except OSError:
            time.sleep(0.2)
    items = st['sources'].get('tabs', {}).get('items', [])
    check('state.json: задача видна', [i['id'] for i in items] == ['a1'], items)
    check('модули без ошибок', not [n for n, m in st['sources']['core']['modules'].items() if m['error']], st['sources']['core'])

    def post(src, act, args):
        req = urllib.request.Request(f'{base}/api/{src}/{act}', json.dumps(args).encode(), {'Content-Type': 'application/json', 'Origin': base})
        try:
            return json.load(op.open(req))
        except urllib.error.HTTPError as e:
            print('POST', act, e.code, e.read().decode('utf-8')); raise
    post('tabs', 'draft', {'dir': d, 'n': 4, 'opt': 'да'})
    check('submit', post('tabs', 'submit', {'dir': d}).get('sent') == 1)
    try:
        op.open(urllib.request.Request(f'http://localhost:{port}/api/tabs/submit', b'{}', {'Origin': 'http://evil'}))
        check('чужой Origin → 403', False)
    except urllib.error.HTTPError as e:
        check('чужой Origin → 403', e.code == 403, e.code)
    try:
        op.open(urllib.request.Request(f'{base}/api/tabs/submit', json.dumps({'dir': d}).encode()))
        check('без Origin → 403', False)
    except urllib.error.HTTPError as e:
        check('без Origin → 403', e.code == 403, e.code)
finally:
    srv.terminate()

hook_in = json.dumps({'session_id': 'smoke-sid', 'cwd': d})
code, out = tm('hook', stdin=hook_in)
check('hook показывает ответ', 'a1#4' in out and 'да' in out, out)
check('hook второй раз молчит', tm('hook', stdin=hook_in)[1] == '')
check('ack', tm('ack', 'a1', '4', 'done')[1] == 'ok')
check('unseen пуст', tm('unseen')[1] == 'новых ответов нет')
print(f'{len(fails)} ошибок, {time.time() - t0:.1f} с')
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if fails else 0)
