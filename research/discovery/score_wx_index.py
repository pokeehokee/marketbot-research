"""Score idea #66: does the published Kalshi Weather Index beat the hourly temperature price?

Reads wx-<tag>-trades.csv.gz (one row per print, with the index KNOWN at the print).
The model never sees the outcome it prices:

  * Delta = settlement value - index known at the print, as a function of the horizon h
    (minutes from the known minute to the settlement minute). Its empirical distribution
    is fitted on one half of the DAYS and applied to the other half, both ways round, so
    every scored print is out of sample.
  * p_yes = P(known + Delta > strike) under that distribution.

Then, for each print, the taker's side is copied at the printed price only when the
model's probability for that side beats price + taker fee by a threshold: the print proves
the liquidity existed on that side at that price at that second, and nothing else is
assumed. Per-contract EV, losses against the implied count, and a day-clustered bootstrap.

Also reported: Brier of the market's price against Brier of the model on the same prints.
"""
import csv, gzip, math, random, sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
MODEL = "flat"   # "flat": random walk from the known index; "trend": + hour drift + last-15-min trend
H_EDGES = (0, 10, 15, 20, 30, 45, 60, 90, 200)


def hbucket(h):
    for lo, hi in zip(H_EDGES, H_EDGES[1:]):
        if lo <= h < hi:
            return lo
    return None


def fee_per_ct(p, n):
    n = max(1, int(round(n)))
    return math.ceil(0.07 * n * p * (1 - p) * 100 - 1e-9) / 100 / n


def p_above(deltas, need):
    """P(Delta > need) from an empirical sample, with a half-count either side of a tie."""
    if not deltas:
        return None
    gt = sum(1 for d in deltas if d > need)
    eq = sum(1 for d in deltas if d == need)
    return (gt + 0.5 * eq + 0.5) / (len(deltas) + 1)   # shrunk off 0 and 1


def p_yes(row, dist, coef=None):
    st, fl, cp, kv = row["strike_type"], row["floor"], row["cap"], row["known_v"]
    if coef is not None:
        mu = predict(row, coef)
        if mu is None:
            return None
        kv = kv + mu
    d = dist.get((row["series"], row["hb"])) or dist.get(("*", row["hb"]))
    if d is None:
        return None
    if st == "greater" and fl is not None:
        return p_above(d, fl - kv)
    if st == "less" and cp is not None:
        p = p_above(d, cp - kv - 1e-9)
        return None if p is None else 1 - p
    if st == "between" and fl is not None and cp is not None:
        a, b = p_above(d, fl - kv - 1e-9), p_above(d, cp - kv)
        return None if a is None else max(0.0, a - b)
    return None


def load(path):
    rows = []
    f = lambda x: float(x) if x not in ("", None, "None") else None
    with gzip.open(path, "rt") as fh:
        for r in csv.DictReader(fh):
            kv, kt, fv = f(r["known_v"]), f(r["known_t"]), f(r["final_v"])
            if kv is None or kt is None or fv is None or r["result"] not in ("yes", "no"):
                continue
            st = int(float(r["settle_ts"]))
            h = (st - kt) / 60.0
            hb = hbucket(h)
            if hb is None:
                continue
            rows.append(dict(series=r["series"], ticker=r["ticker"], settle=st,
                             day=datetime.fromtimestamp(st, ET).date().isoformat(),
                             strike_type=r["strike_type"], floor=f(r["floor"]), cap=f(r["cap"]),
                             result=r["result"], final_v=fv, known_v=kv, known_t=kt, h=h, hb=hb,
                             ts=float(r["trade_ts"]), side=r["taker_side"], paid=float(r["taker_paid"]),
                             n=float(r["count"])))
    return rows


def add_trend(rows, back_s=900):
    """trend15: index change over the 15 minutes before the KNOWN minute, read only from
    values that were themselves known (other prints of the same event carry them). No
    lookahead: every value used has known_t <= this print's known_t."""
    by_ev = defaultdict(dict)
    for r in rows:
        by_ev[(r["series"], r["settle"])][r["known_t"]] = r["known_v"]
    for r in rows:
        ev = by_ev[(r["series"], r["settle"])]
        target = r["known_t"] - back_s
        cands = [t for t in ev if target - 120 <= t <= target]
        r["trend"] = (r["known_v"] - ev[max(cands)]) if cands else None
        r["hour"] = datetime.fromtimestamp(r["settle"], ET).hour


def fit_drift(train):
    """Delta ~ a[series,hour]*h + b[series]*trend*(h/15), least squares on unique samples."""
    seen, by_s = set(), defaultdict(list)
    for r in train:
        k = (r["series"], r["settle"], r["known_t"])
        if k in seen or r.get("trend") is None:
            continue
        seen.add(k)
        by_s[r["series"]].append(r)
    coef = {}
    for s, rs in by_s.items():
        # b from within-hour demeaned regression, then a per hour from the remainder
        xs = [x["trend"] * x["h"] / 15 for x in rs]
        ys = [x["final_v"] - x["known_v"] for x in rs]
        hm = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
        for x, y, r in zip(xs, ys, rs):
            a = hm[r["hour"]]; a[0] += x; a[1] += y; a[2] += r["h"]; a[3] += 1
        num = den = 0.0
        for x, y, r in zip(xs, ys, rs):
            a = hm[r["hour"]]
            num += (x - a[0] / a[3]) * (y - a[1] / a[3]); den += (x - a[0] / a[3]) ** 2
        b = num / den if den else 0.0
        a_h = {}
        for hr, a in hm.items():
            rem = [(y - b * x, r["h"]) for x, y, r in zip(xs, ys, rs) if r["hour"] == hr]
            a_h[hr] = sum(v for v, _ in rem) / max(1e-9, sum(h for _, h in rem))
        coef[s] = (a_h, b)
    return coef


def predict(r, coef):
    c = coef.get(r["series"])
    if c is None or r.get("trend") is None:
        return None
    a_h, b = c
    return a_h.get(r["hour"], 0.0) * r["h"] + b * r["trend"] * r["h"] / 15


def fit(rows, coef=None):
    """Delta per (series, h-bucket), one sample per (event, known minute) -- not per print."""
    seen, dist = set(), defaultdict(list)
    for r in rows:
        k = (r["series"], r["settle"], r["known_t"])
        if k in seen:
            continue
        seen.add(k)
        mu = predict(r, coef) if coef is not None else 0.0
        if mu is None:
            continue
        d = round(r["final_v"] - r["known_v"] - mu, 2)
        dist[(r["series"], r["hb"])].append(d)
        dist[("*", r["hb"])].append(d)
    return {k: v for k, v in dist.items() if len(v) >= 30}


def boot(by_day, reps=2000, seed=7):
    days = list(by_day)
    if len(days) < 2:
        return None, None
    rng, out = random.Random(seed), []
    for _ in range(reps):
        pnl = ct = 0.0
        for _ in days:
            a, b = by_day[rng.choice(days)]
            pnl += a; ct += b
        if ct:
            out.append(pnl / ct)
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))]


def score(rows, thr, label, one_per_market=False):
    by_day = defaultdict(lambda: [0.0, 0.0])
    ct = pnl = lost = implied = 0.0
    lost_pos = n = 0
    taken = set()
    for r in sorted(rows, key=lambda r: r["ts"]):
        if r.get("p") is None:
            continue
        q = r["p"] if r["side"] == "yes" else 1 - r["p"]
        fee = fee_per_ct(r["paid"], r["n"])
        if q - r["paid"] - fee < thr:
            continue
        if one_per_market:
            if r["ticker"] in taken:
                continue
            taken.add(r["ticker"])
        won = 1.0 if r["result"] == r["side"] else 0.0
        c = r["n"]
        e = (won - r["paid"] - fee) * c
        ct += c; pnl += e; n += 1
        lost += (1 - won) * c; implied += (1 - r["paid"]) * c
        lost_pos += int(won == 0)
        by_day[r["day"]][0] += e; by_day[r["day"]][1] += c
    if not ct:
        print(f"  {label:<34} nothing taken")
        return
    lo, hi = boot(by_day)
    ci = f"[{lo*100:+.2f}, {hi*100:+.2f}]" if lo is not None else "[n/a]"
    print(f"  {label:<34} prints {n:6d} ct {ct:9.0f} days {len(by_day):3d} losing prints {lost_pos:5d} "
          f"ratio {lost/implied if implied else float('nan'):.2f}  EV {pnl/ct*100:+.2f}c {ci}")


def main(path):
    rows = load(path)
    days = sorted({r["day"] for r in rows})
    print(f"{len(rows)} prints with a known index, {len(days)} days, "
          f"{len({r['ticker'] for r in rows})} markets, series {sorted({r['series'] for r in rows})}")
    if len(days) < 4:
        print("too few days to split; stop")
        return
    add_trend(rows)
    half = set(days[: len(days) // 2])
    A = [r for r in rows if r["day"] in half]
    B = [r for r in rows if r["day"] not in half]
    for train, test in ((A, B), (B, A)):
        coef = fit_drift(train) if MODEL == "trend" else None
        dist = fit(train, coef)
        for r in test:
            r["p"] = p_yes(r, dist, coef)
    scored = [r for r in rows if r.get("p") is not None]
    print(f"{len(scored)} prints priced out of sample")

    # Calibration of the market vs the model on the same prints.
    print("\nBrier, market price vs index model, same prints (lower is better):")
    agg = defaultdict(lambda: [0.0, 0.0, 0.0])
    for r in scored:
        y = 1.0 if r["result"] == "yes" else 0.0
        m = r["paid"] if r["side"] == "yes" else 1 - r["paid"]
        for k in ("all", f"h {r['hb']:>3}+"):
            a = agg[k]; a[0] += (m - y) ** 2; a[1] += (r["p"] - y) ** 2; a[2] += 1
    for k in sorted(agg, key=lambda k: (k != "all", int(k.split()[1].rstrip('+')) if k != "all" else 0)):
        a = agg[k]
        print(f"  {k:<8} n {a[2]:7.0f}  market {a[0]/a[2]:.4f}  model {a[1]/a[2]:.4f}")

    print("\nCopy the print's side when model - price - taker fee >= threshold:")
    for thr in (0.0, 0.03, 0.05, 0.10):
        score(scored, thr, f"all, edge >= {thr*100:.0f}c")
    score(scored, 0.05, "edge >= 5c, first print per market", one_per_market=True)
    for name, sub in (("first half", [r for r in scored if r["day"] in half]),
                      ("second half", [r for r in scored if r["day"] not in half])):
        score(sub, 0.05, f"{name}, edge >= 5c")
    for s in sorted({r["series"] for r in scored}):
        score([r for r in scored if r["series"] == s], 0.05, f"{s}, edge >= 5c")
    for hb in H_EDGES[:-1]:
        score([r for r in scored if r["hb"] == hb], 0.05, f"h {hb}+ min, edge >= 5c")


if __name__ == "__main__":
    if len(sys.argv) > 2:
        MODEL = sys.argv[2]
    main(sys.argv[1])
