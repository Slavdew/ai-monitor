r"""Дымовой тест панели: копия панели во временном каталоге, свой порт и state_dir, без браузера; живую панель,
её config.local.json и задачи владельца не трогает.  python tests\smoke.py   (код 0 — всё прошло)
Сценарий: new → add/ask → batch → set (по id) → сервер: state.json, POST draft/submit → hook видит ответ → ack."""
import json, os, shutil, subprocess, sys, tempfile, time, urllib.parse, urllib.request
sys.stdout.reconfigure(encoding='utf-8')   # консоль cp1251 не печатает «→»

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


def tm(*args, stdin=None, sid=None):
    r = subprocess.run([sys.executable, os.path.join(TM, 'task.py'), *args], input=stdin, capture_output=True,
                       text=True, encoding='utf-8', env={**env, 'TASK_SESSION': sid} if sid else env)
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
code, out = tm('act', 'a1', 'Запустить установщик', '--run', 'py install.py', '--note', 'чтобы включился хук')
check('act: ожидаемое действие', out == 'ok пункт 6', out)

# захваты ресурсов: отказ с очередью, продление, снятие, сброс после перезагрузки
check('hold', tm('hold', 'ИБ dev', 'обновление', '--ttl', '20m', sid='A')[1].startswith('ok держу: ИБ dev до '))
code, out = tm('hold', 'ИБ dev', 'проверка', sid='B')
check('hold занято → очередь', code == 1 and '«обновление»' in out and 'очереди 1-й' in out, out)
check('held: держит и ждут', 'ИБ dev: A' in tm('held')[1] and 'ждут: B' in tm('held')[1], tm('held')[1])
check('hold --ttl с ошибкой', tm('hold', 'x', '--ttl', '5q', sid='A')[0] == 1)
check('free', tm('free', sid='A')[1] == 'ok отпущено: ИБ dev')
check('hold после free', tm('hold', 'ИБ dev', 'проверка', sid='B')[0] == 0)
rp = os.path.join(tmp, 'state', 'claims', 'res', 'ИБ_dev.json')
c = json.load(open(rp, encoding='utf-8')); c['at'] = 1000   # записан до загрузки Windows, сессия вне реестра
json.dump(c, open(rp, 'w', encoding='utf-8'))
check('захват до загрузки снят', tm('held')[1] == 'ресурсы свободны', tm('held')[1])
tm('hold', 'ИБ dev', 'проверка', sid='B')
c = json.load(open(rp, encoding='utf-8')); c['reg'] = True   # сессия была в реестре и закрылась — записи нет
json.dump(c, open(rp, 'w', encoding='utf-8'))
check('захват закрытой сессии снят', tm('held')[1] == 'ресурсы свободны', tm('held')[1])
tm('hold', 'стенд', 'тесты', sid='B')

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
    check('state.json: захват ресурса', 'стенд' in json.load(op.open(base + '/state.json'))['sources']['claims'].get('res', {}))
    try:
        op.open(urllib.request.Request(f'{base}/api/claims/free', json.dumps({'res': 'стенд'}).encode()))
        check('free без Origin → 403', False)
    except urllib.error.HTTPError as e:
        check('free без Origin → 403', e.code == 403, e.code)
    check('владелец снимает захват', post('claims', 'free', {'res': 'стенд'}).get('ok') is True)
    check('actdone со страницы', post('tabs', 'actdone', {'dir': d, 'index': 5, 'note': 'готово'}).get('ok') is True)
    try:
        op.open(urllib.request.Request(f'{base}/api/tabs/actdone', json.dumps({'dir': d, 'index': 5}).encode()))
        check('actdone без Origin → 403', False)
    except urllib.error.HTTPError as e:
        check('actdone без Origin → 403', e.code == 403, e.code)
    # форма «+ Задача»: каталог вне git — задача и вложения; внутри git — отказ (задачи ведутся вне репозиториев)
    sys.path.insert(0, SRC)
    from monitor.core import git_root
    req = urllib.request.Request(f'{base}/api/tabs/create', json.dumps({'tab': 't', 'kind': 'idea', 'project': 'П', 'title': 'Новая идея'}).encode(),
                                 {'Origin': base})
    if git_root(os.path.join(tmp, 'tasks')):
        try:
            op.open(req)
            check('create внутри git → отказ', False)
        except urllib.error.HTTPError as e:
            check('create внутри git → отказ', e.code == 400 and 'репозитори' in e.read().decode('utf-8'), e.code)
    else:
        r = json.load(op.open(req))
        t = json.load(open(os.path.join(r['dir'], 'task.json'), encoding='utf-8'))
        check('create: идея с проектом', t.get('kind') == 'idea' and t.get('project') == 'П' and r['id'] == 'novaya-ideya', t)
        up = urllib.request.Request(f'{base}/api/tabs/attach?dir={urllib.parse.quote(r["dir"])}&name=..%5Ca%3F.txt', b'abc', {'Origin': base})
        j = json.load(op.open(up))
        check('attach: в files, имя без пути', j.get('name') == 'a_.txt' and open(os.path.join(r['dir'], 'files', 'a_.txt'), 'rb').read() == b'abc', j)
    try:
        op.open(urllib.request.Request(f'{base}/api/tabs/create', b'{}'))
        check('create без Origin → 403', False)
    except urllib.error.HTTPError as e:
        check('create без Origin → 403', e.code == 403, e.code)
finally:
    srv.terminate()

hook_in = json.dumps({'session_id': 'smoke-sid', 'cwd': d})
code, out = tm('hook', stdin=hook_in)
check('hook показывает ответ', 'a1#4' in out and 'да' in out, out)
check('hook второй раз молчит', tm('hook', stdin=hook_in)[1] == '')
out = tm('unseen')[1]
check('unseen: действие выполнено', 'a1#6: ВЛАДЕЛЕЦ ВЫПОЛНИЛ ОЖИДАЕМОЕ ДЕЙСТВИЕ' in out and 'готово' in out, out)
check('ack действия', tm('ack', 'a1', '6')[1] == 'ok')
check('ack (синоним «принят»)', tm('принят', 'a1', '4', 'done')[1] == 'ok')
check('unseen пуст', tm('unseen')[1] == 'новых ответов нет')
print(f'{len(fails)} ошибок, {time.time() - t0:.1f} с')
shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if fails else 0)
