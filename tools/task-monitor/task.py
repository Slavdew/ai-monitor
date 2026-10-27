"""task.py — панель задач из командной строки (T — id или каталог задачи, N — номер пункта с 1)

  ls [--all] [--tab T]                  открытые задачи (--all — и сделанные)
  show T [--all]                        открытые пункты строкой «N статус текст» (--all — все)
  new <каталог> --title … --goal … --plan "п1" "п2"     [--append — дописать план существующей]
  add T "текст" [note=… section=… after=3] [--ask --opt A --opt Б | --req]   → ok пункт N
  ask T "вопрос" [--note совет] [--opt A --opt Б]        req T "просьба" [--note …]
  set T N status=done note="…" after=3,4 text="…" section=…      поля пункта
  set T priority=1 stage="…" health=ok next="…" related+=B docs+="Назв::путь"   поля задачи
  batch T < строки                      пачка правок плана одной записью
  act T "что" --run "команда" [--where cmd] | --how "шаги"   ожидаемое действие владельца («Ожидаемые действия»)
  ack T N… [done]                       ответ учтён (done — заодно закрыть вопрос); синоним — принят T N… [done]
  unseen [--mine]                       неподтверждённые ответы с готовыми командами
  next [--tab T] [--project P] | take T | release [T]  взять первую свободную / эту / отпустить
  hold <ресурс> "зачем" [--ttl 20m] | free [ресурс] | held     захват ИБ, стенда, ветки (help hold)
  qa T "вопрос владельца" "ответ в 1–3 строки" [--item N]   ответ на его вопрос — в «Ждут вас» до «Прочитано»
  api [источник действие ключ=…] | stats [--days 7] | hook | watch [--task T] | modules

Ответ: ok / ok пункт N; ошибка — одна строка «ошибка: …», код 1. Сессия — TASK_SESSION, --sid или
CLAUDE_CODE_SESSION_ID. Подробно: task.py help batch | set | links | questions | api | agent
"""
import argparse
import collections
import glob
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from monitor.core import claude_sessions, expand, in_tab_root, load_config, log_call, model_on, module_hooks, module_on, module_settings, modules, read_json, require, session_id, sources  # noqa: E402
from monitor import questions  # noqa: E402
from monitor.sources import tabs  # noqa: E402

TOPICS = {
    'set': """set — правка пункта или задачи одной грамматикой ключ=значение (значение с пробелами — в кавычках)

  set T N status=done note="где результат" after=3,4 text="…" section=Каркас who=…
      status: todo doing done skip later (wait — только у вопроса, новый вопрос — ask)
      note=null / note= — убрать; after=3,4 — ждёт пунктов 3 и 4, after= — можно сразу, after=null — по умолчанию
      text= у вопроса: прежний текст сохраняется (страница покажет «было → стало»), просьба «проще» снимается
  set T priority=1 stage="каркас" health=ok next="что дальше" blocked="ответ ИТ" desc="…"   несколько за раз
      priority: 1 срочно, 2 важно, 3 обычно, 4 потом; health: ok risk blocked или пусто (с risk — risk="в чём риск"); stage — если этап
      не совпадает с разделом пункта в работе; поле=null — убрать
  Связи и документы — task.py help links. Ответ: ok / ok пункт N.""",
    'batch': """batch T — пачка правок плана из stdin одной записью; ошибка в любой строке — не меняется ничего

  # Раздел                 раздел для следующих новых пунктов (разделы по порядку = этапы)
  + текст | note="…" after=3          новый пункт (todo); поля — после « | »
  3 status=done note=null             поля пункта 3 — как set T 3 …
Ключи: text status note section who after; null — убрать поле. Ответ: ok пункты 9–11 (новые) или ok.
  python task.py batch a <<'EOF'
  # Проверка
  + прогнать зонды | note="на копии"
  4 status=done
  EOF""",
    'links': """Связи и документы задачи — set T ключ=…, ключ+=… (добавить), ключ-=… (убрать)

  parent=X                      родитель (одна задача)
  related, blocks, blocked_by   списки id: related+=B, blocked_by=C;D (= заменяет список; разделитель «;»)
  docs+="Название::путь-или-url"        приложенные: на чём задача основана (ТЗ, исходные документы)
  artifacts+="Название::путь"           артефакты: промежуточные результаты (разборы, макеты)
  docs-="Название" или docs-="путь"     убрать документ
.md/.html каталога задачи без пометки панель показывает артефактами сама. Ответ: ok docs: 2 (было 1).""",
    'questions': """Вопросы и просьбы владельцу

  ask T "вопрос" --note "мой совет: A" --opt "A" --opt "Б"   → ok пункт N (статус wait)
      --opt — кнопки ответа, 2–4 коротких; «↺ проще» и «★ Как советуешь» страница добавит сама — рекомендацию
      вариантом не дублировать (со «★» ask отклонит): она в --note, варианты — альтернативы.
      Похожий открытый вопрос ask отклонит и назовёт номер — дополнить его set T N note=…; повторить — --force.
  Ответ приходит хуком («Панель задач: ответы владельца…») или unseen — строка с готовой командой:
      ack T N done          учёл и закрыл (ответ может менять и другие пункты — поправить план до ack)
      ПЕРЕФРАЗИРОВАТЬ → set T N text="проще", затем ack T N (статус не менять — вопрос ждёт дальше)
  Черновики владельца (draft в task.json) не читать — он ещё думает. Ответ в чате — set T N status=done note="ответ в чате: …".
  req T "просьба его словами" --note "где ведётся"   → ok пункт N; блок «Ваши просьбы», пока не done/skip.
  Владелец пишет просьбу сам (панель, «+ Просьба») — приходит как ответ: «НОВАЯ ПРОСЬБА ВЛАДЕЛЬЦА …»,
      в show — [новая]; take задачи печатает непрочитанное сразу. Предложить разобрать, затем ack T N.
  Хук: полный текст — один раз на сессию и только по задачам этой сессии (её вопрос, захват, session в
  HANDOFF, cwd в каталоге задачи); остальное — «ещё ответов в других задачах: N», напоминание — раз в 5 вводов.
  Долгая автономная работа — Monitor на task.py watch --task T (строка на каждую отправку владельца).""",
    'api': """api — действие страницы через запущенный сервер, как кнопкой (тот же журнал, страницы обновятся сразу)

  api                               список действий
  api tabs field dir=<каталог> field=stage value="…"     действие страницы; ответы, черновики и просьбы владельца — только со страницы (403)
Значения — строки; JSON только явный: null true false […] {…}. dir и пути — всегда строки.
Что видно в окне — node <панель>/ui.mjs card:<id> text:<css> count:<css> shot:<имя> (безголовый Edge,
строка на шаг; node ui.mjs — список шагов). Щелчки настоящие: «Зафиксировать ответы» запишет ответы.""",
    'hold': """Захват общего ресурса (ИБ 1С, стенд, ветка): пока держите — другие сессии получают отказ

  hold <ресурс> "зачем" [--ttl 20m] [--task T] [--force]   → ok держу: … | занято: <кто>, «зачем», до … — вы в очереди N-й
      ресурс — короткое имя, одно на всех: «ИБ dev», «стенд test-1», «ветка feature/123»
      --ttl — срок (30s, 20m, 2h); без него — пока жива сессия. Повторный hold своего — продление
      отказ ставит в очередь; повторите hold позже — место в очереди сохраняется
  free [ресурс]                     отпустить (без имени — все свои и место в очередях)
  held                              кто что держит и кто ждёт, строка на ресурс

Сброс сам: закрыли сессию или перезагрузили Windows — захват снят (процесс сверяется по времени создания).
Владелец видит захваты чипом у задачи, строкой в «Сессиях» и вкладкой «Занято»; повисший снимает кнопкой.""",
}
AGENT_EXTRA = """
## Агент без хуков

Хук UserPromptSubmit есть только в Claude Code. Другой агент: `task.py unseen --mine` перед каждым ответом
пользователю; долгая работа — `task.py watch --task T` в фоне. Сессия — `TASK_SESSION=<любой id>` или
`--sid <id>` (иначе вызовы пишутся как human, next/release не работают); захват такой сессии жив, пока
она вызывает task.py (sources.claims.ttl_sec, по умолчанию 30 минут)."""
LEGACY = ('plan', 'note', 'after', 'reword', 'link')   # прежние формы: работают, в help не показаны
PAIR = re.compile(r'([\w.]+)(\+=|-=|=)(.*)', re.S)
_stdin_chars = 0


class Parser(argparse.ArgumentParser):
    def error(self, message):   # одной строкой в stdout, код 1 — как остальные ошибки
        m = re.match(r"argument cmd: invalid choice: '([^']*)'", message)
        print(f'ошибка: {f"нет команды {m.group(1)}" if m else message} (task.py help)')
        sys.exit(1)


def resolve(cfg, ref):
    if os.path.isdir(ref):
        return os.path.abspath(ref)
    hit = [i for i in tabs.collect(cfg, {}, {})['items'] if i['id'] == ref]
    if not hit:
        raise ValueError(f'нет задачи {ref} (task.py ls --all)')
    if len(hit) > 1:
        raise ValueError(f'id {ref} — {len(hit)} задачи: {"; ".join(i["dir"] for i in hit)} — укажите каталог')
    return hit[0]['dir']


def help_text(topic):
    if not topic:
        return __doc__.strip()
    if topic == 'agent':
        for p in (os.path.join(HERE, '..', '..', 'skills', 'task-monitor', 'SKILL.md'),
                  os.path.join(os.path.expanduser('~'), '.claude', 'skills', 'task-monitor', 'SKILL.md')):
            if os.path.exists(p):
                with open(p, encoding='utf-8') as f:
                    text = re.sub(r'\A---\n.*?\n---\n', '', f.read(), flags=re.S)
                return text.strip() + '\n' + AGENT_EXTRA
        return __doc__.strip() + '\n' + AGENT_EXTRA
    if topic not in TOPICS:
        raise ValueError(f'нет темы {topic} (есть: {", ".join(TOPICS)}, agent)')
    return TOPICS[topic]


def parser():
    p = Parser(prog='task.py', add_help=False)
    sub = p.add_subparsers(dest='cmd', required=True, parser_class=Parser)
    sp = lambda name: sub.add_parser(name, add_help=False)
    hp = sp('help'); hp.add_argument('topic', nargs='?')
    ls = sp('ls'); ls.add_argument('--all', action='store_true'); ls.add_argument('--tab')
    sh = sp('show'); sh.add_argument('ref'); sh.add_argument('--all', action='store_true')
    n = sp('new'); n.add_argument('dir')
    for f in ('id', 'title', 'project', 'goal'):
        n.add_argument('--' + f)
    n.add_argument('--plan', nargs='*', default=[]); n.add_argument('--append', action='store_true')
    for name in ('add', 'ask', 'req', 'act'):   # ask / req / act — синонимы add --ask / --req / --act
        x = sp(name)
        x.add_argument('ref'); x.add_argument('text'); x.add_argument('pairs', nargs='*')
        x.add_argument('--ask', action='store_true'); x.add_argument('--req', action='store_true')
        x.add_argument('--note', default=''); x.add_argument('--section', default='')
        x.add_argument('--opt', action='append', default=[]); x.add_argument('--force', action='store_true')
        x.add_argument('--act', action='store_true'); x.add_argument('--run', default='')
        x.add_argument('--how', default=''); x.add_argument('--where', default='')
    st = sp('set'); st.add_argument('ref'); st.add_argument('rest', nargs='+')
    bt = sp('batch'); bt.add_argument('ref')
    ak = sp('ack'); ak.add_argument('ref'); ak.add_argument('n', nargs='+')
    un = sp('unseen'); un.add_argument('--mine', action='store_true')
    sp('hook')
    wt = sp('watch'); wt.add_argument('--task')
    ap = sp('api'); ap.add_argument('source', nargs='?'); ap.add_argument('action', nargs='?'); ap.add_argument('pairs', nargs='*')
    sa = sp('stats'); sa.add_argument('--days', type=float, default=7)
    sp('modules')
    nx = sp('next'); nx.add_argument('--tab'); nx.add_argument('--project'); nx.add_argument('--force', action='store_true')
    rl = sp('release'); rl.add_argument('ref', nargs='?')
    tk = sp('take'); tk.add_argument('ref'); tk.add_argument('--force', action='store_true')
    hd = sp('hold'); hd.add_argument('res'); hd.add_argument('why', nargs='?', default=''); hd.add_argument('--ttl')
    hd.add_argument('--task'); hd.add_argument('--force', action='store_true')
    fr = sp('free'); fr.add_argument('res', nargs='?')
    sp('held')
    qa = sp('qa'); qa.add_argument('ref'); qa.add_argument('q'); qa.add_argument('a'); qa.add_argument('--item', type=int)
    # прежние формы — синонимы set (ими пользуются живые сессии)
    pl = sp('plan'); pl.add_argument('ref'); pl.add_argument('n'); pl.add_argument('status')
    nt = sp('note'); nt.add_argument('ref'); nt.add_argument('n'); nt.add_argument('text')
    rw = sp('reword'); rw.add_argument('ref'); rw.add_argument('n'); rw.add_argument('text')
    af = sp('after'); af.add_argument('ref'); af.add_argument('n'); af.add_argument('after', nargs='*')
    ln = sp('link'); ln.add_argument('ref'); ln.add_argument('pairs', nargs='+')
    return p


def ttl_sec(text):
    """20m, 2h, 90s, 45 (минуты) → секунды."""
    m = re.fullmatch(r'(\d+(?:[.,]\d+)?)\s*([smhсмч]?)', (text or '').strip().lower())
    if not m:
        raise ValueError(f'срок «{text}»: нужно 30s, 20m или 2h')
    k = {'s': 1, 'с': 1, 'h': 3600, 'ч': 3600}.get(m.group(2), 60)
    return int(float(m.group(1).replace(',', '.')) * k)


def res_cmd(cfg, a):
    """hold / free / held — захваты ресурсов (modules/claims)."""
    claims = require(cfg, 'claims')
    names = {r['sessionId']: r.get('name') or r['sessionId'][:8] for r in claude_sessions() if r.get('sessionId')}
    who = lambda sid: names.get(sid, sid[:8])
    hhmm = lambda ms: time.strftime('%H:%M', time.localtime(ms / 1000))
    if a.cmd == 'held':
        hs = claims.holds(cfg)
        for r, c in sorted(hs.items()):
            print(f'{r}: {who(c["sid"])}{" (" + c["task"] + ")" if c.get("task") else ""}'
                  f'{", «" + c["why"] + "»" if c.get("why") else ""}, с {hhmm(c["since"])}'
                  f'{", до " + hhmm(c["until"]) if c.get("until") else ""}'
                  f'{"; ждут: " + ", ".join(who(x["sid"]) for x in c["queue"]) if c["queue"] else ""}')
        if not hs:
            print('ресурсы свободны')
        return
    sid = session_id()
    if not sid:
        raise ValueError('нужна сессия: TASK_SESSION=<id>, --sid <id> или CLAUDE_CODE_SESSION_ID')
    if a.cmd == 'free':
        gone = claims.free(cfg, a.res, sid)
        print(f'ok отпущено: {", ".join(gone)}' if gone else 'нечего отпускать')
        return
    task = tabs.read_item(resolve(cfg, a.task), {'id': ''}, cfg)['id'] if a.task else None
    ok, c = claims.hold(cfg, a.res, sid, a.why, ttl_sec(a.ttl) if a.ttl else None, task, a.force)
    if ok:
        print(f'ok держу: {a.res}{" до " + hhmm(c["until"]) if c.get("until") else " (пока жива сессия)"}')
        return
    q = [x['sid'] for x in c.get('queue', [])]
    place = q.index(sid) + 1 if sid in q else 1
    raise ValueError(f'занято: {who(c["sid"])}{" (" + c["task"] + ")" if c.get("task") else ""}'
                     f'{", «" + c["why"] + "»" if c.get("why") else ""}'
                     f'{", до " + hhmm(c["until"]) if c.get("until") else ""} — вы в очереди {place}-й (--force — забрать)')


def main():
    sys.stdout.reconfigure(encoding='utf-8')
    argv = sys.argv[1:]
    if '--sid' in argv:   # сессия агента без CLAUDE_CODE_SESSION_ID
        k = argv.index('--sid')
        os.environ['TASK_SESSION'] = argv[k + 1] if k + 1 < len(argv) else ''
        del argv[k:k + 2]
    if argv and argv[0] == 'принят':   # русский синоним ack (acknowledge — «подтвердить получение»)
        argv = ['ack', *argv[1:]]
    if not argv or argv[0] in ('-h', '--help'):
        print(help_text(None))
        return
    if '-h' in argv or '--help' in argv:   # task.py batch -h — тема команды
        argv = ['help', argv[0] if argv[0] in TOPICS else ('questions' if argv[0] in ('ask', 'req', 'ack', 'unseen') else 'hold' if argv[0] in ('free', 'held') else '')]
    cfg = load_config()
    cfg['_no_paths'] = True   # пути-ссылки нужны только странице
    a = parser().parse_args(argv)
    if a.cmd == 'help':
        try:
            print(help_text(a.topic))
        except ValueError as e:
            print(f'ошибка: {e}')
            sys.exit(1)
        return
    if a.cmd == 'stats':
        return stats(cfg, a.days)
    if a.cmd == 'modules':
        return list_modules(cfg)
    if a.cmd in ('hook', 'watch'):   # без журнала вызовов: хук срабатывает на каждый ввод
        return hook(cfg) if a.cmd == 'hook' else watch(cfg, a.task)
    sid = session_id()
    if sid and not model_on(cfg, 'cli'):
        log_call(cfg, 'cli', a.cmd, argv[1:], False, 0, 0, len(' '.join(sys.argv)))
        print('панель: вызовы агента выключены (model.cli)')   # одна строка — минимум токенов
        return
    if sid and module_on(cfg, 'claims'):
        require(cfg, 'claims').touch(cfg, sid)   # захват сессии вне реестра жив, пока она зовёт task.py
    out, real, t0, ok, exit_code, err = io.StringIO(), sys.stdout, time.time(), False, 0, ''
    sys.stdout = out
    try:
        run(cfg, a)
        ok = True
    except Exception as e:   # одной строкой: traceback — лишние токены агенту
        err = e.args[0] if isinstance(e, (ValueError, KeyError, OSError)) and e.args and isinstance(e.args[0], str) \
            else f'{type(e).__name__}: {e}'
        print(f'ошибка: {err}')
        exit_code = 1
    finally:
        sys.stdout = real
        real.write(out.getvalue())
        log_call(cfg, 'cli', a.cmd, argv[1:], ok, int((time.time() - t0) * 1000),
                 len(out.getvalue()), len(' '.join(sys.argv)) + _stdin_chars, err)
    sys.exit(exit_code)


def stats(cfg, days):
    since = (time.time() - days * 86400) * 1000
    agg = collections.defaultdict(lambda: [0, 0, 0, 0])   # вызовов, ошибок, символов ввода, вывода
    errs = []
    try:
        with open(os.path.join(cfg['state_dir'], 'calls.jsonl'), encoding='utf-8') as f:
            for line in f:
                r = json.loads(line)
                if r['ts'] >= since:
                    v = agg[(r['who'], r['via'], r['action'])]
                    v[0] += 1; v[1] += not r['ok']; v[2] += r.get('in_chars', 0); v[3] += r.get('out_chars', 0)
                    if r.get('err'):
                        errs.append(f'{r["action"]}: {r["err"]}')
    except OSError:
        pass
    print(f'{"кто":6} {"через":5} {"действие":14} {"вызовов":>7} {"ошибок":>6} {"≈токенов":>9}')
    for (who, via, action), (n, err, ci, co) in sorted(agg.items(), key=lambda kv: -kv[1][0]):
        print(f'{who:6} {via:5} {action:14} {n:7} {err:6} {round((ci + co) / 3) if via == "cli" else 0:9}')
    if errs:
        print('последние ошибки:\n  ' + '\n  '.join(errs[-5:]))


def list_modules(cfg):
    """Модули строкой: имя, вкл/выкл/ошибка, версия, хеш содержимого (A14 — видно, что за код исполняется), путь."""
    for n, r in modules(cfg).items():
        st = 'ошибка: ' + r['error'] if r['error'] else ('ядро' if r['core'] else 'вкл' if module_on(cfg, n) else 'выкл')
        print(f"{n} {st} v{r['manifest'].get('version', '-')} {r['hash'] or '-'} {r['dir']}")


def hook(cfg):
    """UserPromptSubmit — один хук на всё (A6): stdin читается один раз, вклад ядра (ответы и просьбы
    владельца по задачам этой сессии) и hook.py модулей (доска…). Тишина, если сказать нечего."""
    d = {}
    if not sys.stdin.isatty():
        try:
            d = json.loads(sys.stdin.read().lstrip('﻿') or '{}')
        except ValueError:
            pass
    sid, cwd = d.get('session_id') or session_id(), d.get('cwd') or ''
    d['session_id'] = sid
    lines = []
    if model_on(cfg, 'questions'):
        try:
            lines = questions.hook(cfg, sid or 'unknown', cwd)
        except (OSError, ValueError):
            pass
    if lines and '→' in ''.join(lines):   # есть ответы с командами — путь к task.py для них
        print(f'Панель задач: ответы владельца — учесть и подтвердить (python {os.path.abspath(__file__).replace(os.sep, "/")} <команда>):')
        print('\n'.join(lines))
    elif lines:
        print('Панель задач: ' + '; '.join(lines))
    try:
        extra = module_hooks(cfg, d)
    except Exception:   # хук не мешает вводу
        extra = []
    if extra:
        print('\n'.join(extra))
    sys.stdout.flush()
    os._exit(0)   # не ждать модулей, не успевших к бюджету (A15)


def watch(cfg, task=None):
    """Для Monitor: строка на каждую отправку ответов владельцем (--task — только эта задача)."""
    path = os.path.join(cfg['state_dir'], 'events.jsonl')
    pos = os.path.getsize(path) if os.path.exists(path) else 0
    while True:
        if os.path.exists(path) and os.path.getsize(path) > pos:
            with open(path, encoding='utf-8') as f:
                f.seek(pos)
                for line in f:
                    if line.strip():
                        e = json.loads(line)
                        if not task or e.get('task') == task:
                            print(f'панель: владелец ответил в задаче {e.get("task")} (пункты {e.get("items")}) — task.py unseen', flush=True)
                pos = f.tell()
        time.sleep(1)


def _api_value(k, v):
    """Значение api: строка; JSON — только явный (null true false […] {…}); dir и пути — всегда строки."""
    if k in ('dir', 'path', 'url', 'task', 'text', 'note', 'rephrase', 'opt', 'value'):
        return None if v == 'null' else v
    if v in ('null', 'true', 'false') or v[:1] in ('[', '{'):
        try:
            return json.loads(v)
        except ValueError:
            pass
    return v


def api(cfg, a):
    """Действие страницы через запущенный сервер: та же запись, журнал и мгновенное обновление открытых страниц."""
    acts = {name: list(getattr(mod, 'ACTIONS', None) or []) for name, _, mod in sources(cfg)}
    if not a.source or not a.action:
        for name, names in acts.items():
            if names and (not a.source or name == a.source):
                print(f'{name}: {", ".join(names)}')
        return
    if a.action not in acts.get(a.source, []):
        have = '; '.join(f'{n}: {", ".join(v)}' for n, v in acts.items() if v)
        raise ValueError(f'нет действия {a.source} {a.action} (есть — {have})')
    args = {}
    for pair in a.pairs:
        k, eq, v = pair.partition('=')
        if not eq:
            raise ValueError(f'«{pair}»: нужно ключ=значение')
        if k == 'n':   # номер пункта с 1 → index страницы с 0
            k, v = 'index', str(int(v) - 1)
            args[k] = int(v)
            continue
        args[k] = _api_value(k, v)
    req = urllib.request.Request(f'http://127.0.0.1:{cfg.get("port", 8770)}/api/{a.source}/{a.action}',
                                 json.dumps(args, ensure_ascii=False).encode(), method='POST')
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            res = json.loads(r.read().decode() or '{}')
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            body = json.loads(body).get('error', body)
        except ValueError:
            pass
        raise ValueError(re.sub(r'^\w+Error: ', '', body)) from None
    except urllib.error.URLError as e:
        raise OSError(f'панель не отвечает ({e.reason}) — start.cmd') from None
    print('ok' if res == {'ok': True} else json.dumps(res, ensure_ascii=False))


def _num(x):
    try:
        return int(x)
    except ValueError:
        raise ValueError(f'номер пункта «{x}» — нужно число с 1') from None


def _item_fields(pairs):
    """«ключ=значение» пункта: null — убрать; пустые note/section/who — убрать."""
    out = {}
    for x in pairs:
        m = PAIR.fullmatch(x)
        if not m or m.group(2) != '=':
            raise ValueError(f'«{x}»: нужно ключ=значение, ключи {", ".join(tabs.ITEM_KEYS)}')
        k, v = m.group(1), m.group(3)
        if k not in tabs.ITEM_KEYS:
            raise ValueError(f'ключ {k}: у пункта допустимы {", ".join(tabs.ITEM_KEYS)} (поля задачи — set T ключ=… без N)')
        out[k] = None if v == 'null' or (v == '' and k in ('note', 'section', 'who')) else v
    return out


def set_item(cfg, d, n, fields):
    if not fields:
        raise ValueError('нечего менять: set T N status=… note=… after=… text=…')
    tabs.update_task(cfg, d, lambda t: tabs.apply_item(cfg, t.setdefault('plan', []), n, fields))
    return f'ok пункт {n}'


def set_task(cfg, d, pairs):
    """Поля задачи: ключ=значение, списки связей — += добавить, -= убрать, = заменить (элементы через «;»)."""
    ops = []
    for x in pairs:
        m = PAIR.fullmatch(x)
        if not m:
            raise ValueError(f'«{x}»: нужно ключ=значение (или ключ+=, ключ-= для связей и документов)')
        field = tabs.field_name(m.group(1))
        if m.group(2) != '=' and field[6:] not in tabs.LINK_LISTS:
            raise ValueError(f'{m.group(1)}{m.group(2)}: добавлять и убирать можно только в списках {", ".join(tabs.LINK_LISTS)}')
        ops.append((field, m.group(2), m.group(3)))
    report = {}   # список: (было до первой правки, стало после последней)

    def fn(t):
        for field, op, v in ops:
            box, key = (t.setdefault('links', {}), field[6:]) if field.startswith('links.') else (t, field)
            if v == 'null' and op == '=':
                box.pop(key, None)
                continue
            if op == '=':
                new = tabs.check_field(field, v)
            else:
                cur = list(box.get(key) or [])
                vals = tabs.split_list(v)
                if op == '+=':
                    new = cur + [x for x in vals if x not in cur]
                else:   # документ можно убрать по названию или пути
                    new = [x for x in cur if not any(x == y or str(x).partition('::')[0].strip() == y
                                                     or str(x).rpartition('::')[2].strip() == y for y in vals)]
                    if len(new) == len(cur):
                        raise ValueError(f'{key}: нет «{v}» (есть: {"; ".join(map(str, cur)) or "пусто"})')
                report[key] = (report.get(key, (len(cur),))[0], len(new))
            box[key] = new
    tabs.update_task(cfg, d, fn)
    return 'ok' + ''.join(f' {k}: {n} (было {was});' for k, (was, n) in report.items()).rstrip(';')


def show(cfg, d, all_):
    t = read_json(os.path.join(d, cfg['item']['task_file']), {}) or {}
    plan = t.get('plan', [])
    open_ = [k for k, p in enumerate(plan) if p.get('status', 'todo') in ('todo', 'doing', 'wait')]
    print(f'{t.get("id") or os.path.basename(d)} — {t.get("title", "")} | открыто {len(open_)} из {len(plan)}')
    sec = None
    for k, p in enumerate(plan):
        if not all_ and k not in open_:
            continue
        cur = p.get('section') or ''
        if cur != (sec or '') and (cur or sec):
            print(f'# {cur or "(без раздела)"}')
            sec = cur
        mark = ('вопрос: ' if p.get('q') else '') + ('просьба: ' if p.get('req') else '') \
            + (('[новая] ' if p.get('req') else '[ответ] ') if p.get('seen') is False else '')
        text = ' '.join(str(p.get('text', '')).split())
        text = text if len(text) <= 70 else text[:69] + '…'
        print(f'{k + 1} {p.get("status", "todo")} {mark}{text}')
    hidden = collections.Counter(p.get('status', 'todo') for k, p in enumerate(plan) if k not in open_)
    if hidden and not all_:
        print('скрыто: ' + ', '.join(f'{s} {c}' for s, c in hidden.items()) + ' (--all)')


def add(cfg, a):
    d = resolve(cfg, a.ref)
    fields = _item_fields(a.pairs)
    if a.cmd == 'act' or a.act:   # ожидаемое действие владельца — вопрос вида act: те же ответ, «проще», отправка
        # владелец должен понять, что и где сделать, не спрашивая (28.10): команда или шаги — обязательно
        if not (a.run or a.how):
            raise ValueError('действие без команды и шагов: --run "команда" [--where PowerShell|cmd|Git Bash] или '
                             '--how "что сделать по шагам"; заводить, только когда его можно выполнить сейчас')
        extra = {'act': True, **({'cmd': a.run, 'where': a.where or 'PowerShell'} if a.run else {}),
                 **({'how': a.how} if a.how else {})}
        n = questions.ask(cfg, d, a.text, a.note or fields.pop('note', '') or '', a.force, a.opt or ['Сделал', 'Не буду'], extra)
        return f'ok пункт {n}'
    if a.cmd == 'ask' or a.ask:
        n = questions.ask(cfg, d, a.text, a.note or fields.pop('note', '') or '', a.force, a.opt)
        rest = {k: v for k, v in fields.items() if k in ('section', 'after', 'who')}
        if rest:
            set_item(cfg, d, n, rest)
        return f'ok пункт {n}'
    item = {'text': a.text, 'status': 'todo'}
    if a.cmd == 'req' or a.req:   # просьба владельца: req=true — блок «Ваши просьбы», пока не сделана
        item.update({'req': True, 'at': time.strftime('%Y-%m-%d')})
    item.update({k: v for k, v in (('note', a.note), ('section', a.section)) if v})
    if 'status' in fields and fields['status'] == 'wait':
        raise ValueError('wait — только у вопроса: task.py ask T "вопрос"')
    k = []

    def fn(t):
        plan = t.setdefault('plan', [])
        plan.append(item)
        k.append(len(plan))
        tabs.apply_item(cfg, plan, len(plan), fields)
    tabs.update_task(cfg, d, fn)
    return f'ok пункт {k[0]}'


def run(cfg, a):
    global _stdin_chars
    if a.cmd == 'api':
        return api(cfg, a)
    if a.cmd in ('add', 'ask', 'req', 'act'):
        print(add(cfg, a))
    elif a.cmd == 'show':
        show(cfg, resolve(cfg, a.ref), a.all)
    elif a.cmd == 'set':
        d, r = resolve(cfg, a.ref), a.rest
        if re.fullmatch(r'-?\d+', r[0]):
            print(set_item(cfg, d, int(r[0]), _item_fields(r[1:])))
        elif len(r) == 2 and not PAIR.fullmatch(r[0]):   # прежняя форма: set T поле значение
            tabs.set_field(cfg, {'dir': d, 'field': r[0], 'value': r[1]})
            print('ok')
        else:
            print(set_task(cfg, d, r))
    elif a.cmd == 'plan':
        print(set_item(cfg, resolve(cfg, a.ref), _num(a.n), {'status': a.status}))
    elif a.cmd == 'note':
        print(set_item(cfg, resolve(cfg, a.ref), _num(a.n), {'note': a.text}))
    elif a.cmd == 'reword':
        print(set_item(cfg, resolve(cfg, a.ref), _num(a.n), {'text': a.text}))
    elif a.cmd == 'after':
        print(set_item(cfg, resolve(cfg, a.ref), _num(a.n), {'after': [_num(x) for x in a.after]}))
    elif a.cmd == 'link':   # прежняя форма: элементы через запятую (или «;»), список заменяется
        d = resolve(cfg, a.ref)
        for pair in a.pairs:
            k, _, v = pair.partition('=')
            tabs.set_field(cfg, {'dir': d, 'field': 'links.' + k,
                                 'value': v if k == 'parent' else tabs.split_list(v, ';' if ';' in v else ',')})
        print('ok')
    elif a.cmd == 'batch':
        sys.stdin.reconfigure(encoding='utf-8')
        body = sys.stdin.read().lstrip('﻿')
        _stdin_chars = len(body)
        r = tabs.batch(cfg, resolve(cfg, a.ref), body.splitlines())
        add_ = r['added']
        print('ok' + (f' пункт {add_[0]}' if len(add_) == 1 else f' пункты {add_[0]}–{add_[-1]}' if add_ else ''))
    elif a.cmd == 'ack':
        done = a.n[-1] == 'done'
        nums = [_num(x) for x in (a.n[:-1] if done else a.n)]
        if not nums:
            raise ValueError('ack T N… [done] — нужен номер пункта')
        questions.ack(cfg, resolve(cfg, a.ref), nums, done)
        print('ok')
    elif a.cmd == 'unseen':
        sid = session_id()
        if a.mine and not sid:
            raise ValueError('--mine: нужна сессия — TASK_SESSION, --sid или CLAUDE_CODE_SESSION_ID')
        print('\n'.join(questions.unseen(cfg, sid, os.getcwd(), a.mine)) or 'новых ответов нет')
    elif a.cmd == 'ls':
        items = [i for i in tabs.collect(cfg, {}, {})['items'] if (not a.tab or i['tab'] == a.tab)
                 and (a.all or (i['status'] != 'done' and i['percent'] != 100))]
        if a.tab and a.tab not in [t['id'] for t in cfg.get('tabs', [])]:
            raise ValueError(f'нет вкладки {a.tab} (есть: {", ".join(t["id"] for t in cfg.get("tabs", []))})')
        for i in items:
            pct = '' if i['percent'] is None else f' {i["percent"]}%'
            print(f'{i["tab"]:8} {i["id"]:20} {i["status"] or "-":8}{pct}  {i["title"]}')
        if not items:
            print('нет задач' + ('' if a.all else ' (открытых; --all — все)'))
    elif a.cmd == 'new':
        d = os.path.abspath(a.dir)
        if os.path.exists(os.path.join(d, cfg['item']['task_file'])) and not a.append:
            raise ValueError(f'задача уже есть: {d} — дописать план: new … --append или add')
        os.makedirs(d, exist_ok=True)

        def fn(t):
            for f in ('id', 'title', 'project', 'goal'):
                if getattr(a, f):
                    t[f] = getattr(a, f)
            t.setdefault('id', os.path.basename(d))
            t['plan'] = t.get('plan', []) + [{'text': x, 'status': 'todo'} for x in a.plan]
        t = tabs.update_task(cfg, d, fn)
        print(f'ok {t["id"]}')
        if not in_tab_root(cfg, d):   # задача есть, но панель и команды по id её не увидят
            roots = '; '.join(x['root'] for x in cfg.get('tabs', []))
            print(f'предупреждение: {d} вне корней вкладок — в панели её не будет; заводить внутри: {roots}')
    elif a.cmd == 'qa':   # ответ на вопрос владельца — в журнал «вопрос — ответ» задачи (раздел «Ждут вас»)
        n = tabs.qa_add(cfg, resolve(cfg, a.ref), a.q, a.a, a.item, session_id() or '')
        print(f'ok ответ {n}' if n else 'журнал «вопрос — ответ» выключен (qa.enabled) — не записано')
    elif a.cmd in ('hold', 'free', 'held'):
        res_cmd(cfg, a)
    elif a.cmd in ('next', 'release', 'take'):
        sid = session_id()
        if not sid:
            raise ValueError('нужна сессия: TASK_SESSION=<id>, --sid <id> или CLAUDE_CODE_SESSION_ID')
        claims = require(cfg, 'claims')
        if a.cmd == 'take':   # взять конкретную задачу: ответы владельца по ней хук покажет этой сессии целиком
            d = resolve(cfg, a.ref)
            i = tabs.read_item(d, {'id': ''}, cfg)
            if not claims.claim(cfg, i['id'], sid, a.force):
                raise ValueError(f'задача {i["id"]} занята другой сессией (--force — забрать)')
            print(f'ok взята: {i["id"]} — {i["title"]}')
            # непрочитанное по задаче — сразу: новые просьбы и ответы владельца, пока сессии не было
            for line in questions.unseen(cfg):
                if line.startswith(i['id'] + '#'):
                    print(line)
            return
        if a.cmd == 'release':
            mine = [a.ref] if a.ref else [t for t, c in claims.claims(cfg).items() if c['sid'] == sid]
            gone = [t for t in mine if claims.release(cfg, t, sid)]
            print(f'ok отпущено: {", ".join(gone)}' if gone else 'нечего отпускать')
            return
        want, missing = claims.pick_tabs(cfg, a.tab)
        if missing:
            print(f'предупреждение: нет вкладок {", ".join(missing)} (sources.claims.tabs)')
        items = tabs.collect(cfg, {}, {})['items']
        empty = [t for t in want if t not in missing and not any(i['tab'] == t for i in items)]
        if empty:
            print(f'предупреждение: во вкладках {", ".join(empty)} нет задач')
        ss = require(cfg, 'sessions').collect(cfg, module_settings(cfg, 'sessions'), {'tabs': {'items': items},
                                                               'claims': {'list': claims.claims(cfg)}})['list']
        i = claims.pick(cfg, items, ss, sid, a.tab, a.project, a.force)
        if not i:
            print(f'свободных открытых задач нет (вкладки: {", ".join(want)})')
            return
        # то, что нужно для начала работы: id, название, каталог, как продолжить
        print(f'взята: {i["id"]} — {i["title"]}\nкаталог: {i["dir"]}' + (f'\n{i["resume"]}' if i['resume'] else ''))


if __name__ == '__main__':
    main()
