"""Оценка: сколько стоят остывания кэша в основных сессиях и сколько стоили бы пинги."""
import csv, glob, sys
from collections import defaultdict
from datetime import datetime, timedelta

W_IN, W_CW, W_CR, W_OUT = 1.0, 2.0, 0.1, 5.0   # веса к цене входного токена (кэш 1 ч)
TTL = timedelta(minutes=60)
DAY_START, DAY_END = 9, 19                      # рабочий день, будни

rows = defaultdict(list)
total = 0.0
for f in glob.glob(sys.argv[1] + r"\tokens-*.csv"):
    with open(f, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            i, cw, cr, o = (int(r[k] or 0) for k in ("input", "cache_write", "cache_read", "output"))
            total += i * W_IN + cw * W_CW + cr * W_CR + o * W_OUT
            if r["agent_type"] != "main":
                continue
            rows[r["session"]].append((datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S"), i, cw, cr))

def workday(a, b):
    return a.date() == b.date() and a.weekday() < 5 and a.hour >= DAY_START and b.hour < DAY_END

cold_in = cold_out = 0.0          # переплата за остывание: в течение дня / через ночь
n_in = n_out = 0
smart = naive = 0.0               # цена пингов: только в простоях > 1 ч / каждые 30 мин весь день
for s, rs in rows.items():
    rs.sort()
    for (t0, i0, cw0, cr0), (t1, i1, cw1, cr1) in zip(rs, rs[1:]):
        gap, ctx = t1 - t0, i0 + cw0 + cr0
        wd = workday(t0, t1)
        if wd:
            naive += int(gap / timedelta(minutes=30)) * ctx * W_CR
        if gap > TTL and cw1 > 0.5 * ctx and ctx > 10000:
            extra = cw1 * (W_CW - W_CR)
            if wd:
                cold_in += extra; n_in += 1
                smart += int(gap / timedelta(minutes=55)) * ctx * W_CR
            else:
                cold_out += extra; n_out += 1

M = 1e6
print(f"Всего за период: {total/M:.1f} M ед. (ед. = входной токен)")
print(f"Остывания в течение дня: {n_in} шт., переплата {cold_in/M:.1f} M ({cold_in/total:.1%})")
print(f"Остывания через ночь/выходные: {n_out} шт., переплата {cold_out/M:.1f} M ({cold_out/total:.1%})")
print(f"Пинги только в простое >1 ч (каждые 55 мин): {smart/M:.1f} M -> выгода {(cold_in-smart)/M:.1f} M")
print(f"Пинги каждые 30 мин весь день (нижняя оценка): {naive/M:.1f} M -> выгода {(cold_in-naive)/M:.1f} M")
