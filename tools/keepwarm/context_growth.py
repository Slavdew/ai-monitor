"""Рост контекста основных сессий по token-log и грубая оценка выгоды compact.

python context_growth.py <AI_ROOT>\\tools\\token-log\\data
Ед. = цена входного токена: запись кэша 2x (1 ч), чтение 0,1x, вывод 5x.
"""
import csv, glob, sys
from collections import defaultdict
from statistics import median

W_IN, W_CW, W_CR, W_OUT = 1.0, 2.0, 0.1, 5.0
KEEP = 105_000         # контекст после compact: база сессии ~88k + сводка ~15k
SUMMARY_OUT = 15_000   # вывод модели на сводку

rows = defaultdict(list)
total = 0.0
for f in glob.glob(sys.argv[1] + r"\tokens-*.csv"):
    with open(f, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh, delimiter=";"):
            i, cw, cr, o = (int(r[k] or 0) for k in ("input", "cache_write", "cache_read", "output"))
            cost = i * W_IN + cw * W_CW + cr * W_CR + o * W_OUT
            total += cost
            if r["agent_type"] == "main":
                rows[r["session"]].append((r["ts"], i + cw + cr, cost))

main_cost = sum(c for rs in rows.values() for _, _, c in rs)
ctx_all = [x for rs in rows.values() for _, x, _ in rs]
peaks = sorted(max(x for _, x, _ in rs) for rs in rows.values())
first = [sorted(rs)[0][1] for rs in rows.values()]
M = 1e6
print(f"Сессий: {len(rows)}, ответов: {len(ctx_all)}; всего {total/M:.0f} M ед., основные сессии {main_cost/M:.0f} M ({main_cost/total:.0%})")
print(f"Контекст первого ответа (база): медиана {median(first)/1e3:.0f}k")
print("Пик контекста сессий: медиана {:.0f}k, 75% {:.0f}k, 90% {:.0f}k, макс {:.0f}k".format(
    median(peaks)/1e3, peaks[int(len(peaks)*.75)]/1e3, peaks[int(len(peaks)*.9)]/1e3, peaks[-1]/1e3))
for t in (200_000, 300_000, 500_000, 700_000):
    big = [(x, c) for rs in rows.values() for _, x, c in rs if x > t]
    print(f"  ответов при контексте > {t//1000}k: {len(big)} ({len(big)/len(ctx_all):.0%}), их цена {sum(c for _, c in big)/M:.0f} M ({sum(c for _, c in big)/main_cost:.0%} основных)")

# уже случившиеся compact: контекст упал больше чем вдвое с > 150k
drops = 0
for rs in rows.values():
    rs.sort()
    drops += sum(1 for (_, a, _), (_, b, _) in zip(rs, rs[1:]) if a > 150_000 and b < a / 2)
print(f"Похоже на compact (падение контекста > 2 раз с > 150k): {drops}")

# Модель: при достижении порога T — compact до KEEP, дальше контекст растёт так же
for t in (300_000, 400_000, 500_000):
    saved = cost_c = 0.0
    n = 0
    for rs in rows.values():
        cut = 0
        for _, x, _ in rs:
            if x - cut > t:
                cost_c += t * W_CR + SUMMARY_OUT * W_OUT + (KEEP - 45_000) * W_CW   # системная часть остаётся в кэше, правила и сводка пишутся заново
                cut = x - KEEP
                n += 1
            saved += cut * W_CR
    print(f"compact на {t//1000}k: {n} раз, цена {cost_c/M:.1f} M, экономия чтения {saved/M:.1f} M -> чисто {(saved-cost_c)/M:.1f} M ({(saved-cost_c)/total:.1%} всего)")
