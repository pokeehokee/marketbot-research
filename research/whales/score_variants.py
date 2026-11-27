#!/usr/bin/env python3
"""Whale variants 3, 4 and 5 of PLAN-mutations.md, scored on whales-2's clean holdout.

Train = events before 2026-09-01. Holdout = 2026-09-01 to 2026-09-27, read once.
Every number is per position, CIs clustered by day, net of a copier's round-trip cost.

 3. Small, consistent wallets: >= 20 closed positions in train, realised win rate >= 60%,
    positive closedPnl in BOTH halves of train. Follow their entries in holdout.
 4. Fade the worst: train's bottom decile by summed closedPnl (>= 20 closed positions),
    their holdout entries taken in the OPPOSITE direction.
 5. Front-run the rest of a whale's order: enter at the event's FIRST fill + latency
    (5 s cloud box, 30 s phone), exit at its last fill + 60 s. Only multi-minute events.
    Built from 1-minute opens, so it is an UPPER bound on what a real follower gets.

Primary cell for 3 and 4 is the base test's pre-registered one: 60 s late, 1 hour hold.
Survival: holdout CI excludes zero after BH at q = 0.10 over the plan's 13 variants
(unscored ones count as p = 1), and both halves of the holdout are positive.
"""
import bisect
import csv
import gzip
import os
import sys
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from score_whales import boot, bh  # noqa: E402

TRAIN_END = "2026-09-01"
HOLD_END = "2026-09-27"
COSTS = (0.0004, 0.0010)
KEY = "r_d60_h3600"
ENTRY = ("open", "add", "flip")
CLOSING = ("close", "reduce", "flip")
MIN_CLOSED = 20
N_VARIANTS = 13
LATENCIES = (5, 30)


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(out):
    rows = []
    for s in (0, 1, 2):
        with gzip.open(os.path.join(out, f"whales-2-{s}.csv.gz"), "rt") as fo:
            for r in csv.DictReader(fo):
                for k in list(r):
                    if k.startswith("r_d") or k in ("notional", "closed_pnl", "px_first"):
                        r[k] = fnum(r[k])
                r["dir"] = int(r["dir"])
                r["t_first"] = int(r["t_first"]); r["t_last"] = int(r["t_last"])
                rows.append(r)
    return rows


def candles(out):
    ks, os_ = defaultdict(list), defaultdict(list)
    with gzip.open(os.path.join(out, "whales-2-0-candles.csv.gz"), "rt") as fo:
        for r in csv.DictReader(fo):
            ks[r["coin"]].append(int(r["k"])); os_[r["coin"]].append(float(r["o"]))
    return ks, os_


def open_at(ks, os_, coin, t):
    """Open of the first 1-minute bucket that STARTS at or after t (keys are bucket starts)."""
    k = ks[coin]
    i = bisect.bisect_left(k, t)
    if i == len(k) or k[i] - t >= 120:
        return None, None
    return k[i], os_[coin][i]


def stats(vals_by_day):
    """vals_by_day: {day: [net values]} -> dict with n, days, mean, CI, p, win, multiple."""
    groups = [[sum(v), len(v)] for v in vals_by_day.values() if v]
    vals = [x for v in vals_by_day.values() for x in v]
    if not vals:
        return None
    b = boot(groups)
    wins = [x for x in vals if x > 0]
    losses = [-x for x in vals if x <= 0]
    mult = (sum(wins) / len(wins)) / (sum(losses) / len(losses)) if wins and losses else None
    return {"n": len(vals), "days": len(groups), "losses": len(losses), "win": len(wins) / len(vals),
            "mult": mult, "mean": b[0] if b else sum(vals) / len(vals),
            "lo": b[1] if b else None, "hi": b[2] if b else None, "p": b[3] if b else 1.0}


def halves(vals_by_day):
    days = sorted(vals_by_day)
    mid = len(days) // 2
    out = []
    for part in (days[:mid], days[mid:]):
        v = [x for d in part for x in vals_by_day[d]]
        out.append(sum(v) / len(v) if v else None)
    return out


def fmt(s):
    if not s:
        return "no positions"
    ci = f"[{s['lo']*1e4:+6.1f}, {s['hi']*1e4:+6.1f}]" if s["lo"] is not None else "[too few days]"
    m = f"{s['mult']:.2f}" if s["mult"] else "-"
    return (f"n {s['n']:5d} d {s['days']:3d} lost {s['losses']:5d}  net {s['mean']*1e4:+6.1f}bp {ci}"
            f"  win {s['win']*100:5.1f}%  x {m}  per $1 {s['mean']:+.5f}")


def wallet_records(train):
    by = defaultdict(list)
    for r in train:
        if r["kind"] in CLOSING and r["closed_pnl"] is not None:
            by[r["addr"]].append((r["t_last"], r["closed_pnl"]))
    rec = {}
    for a, v in by.items():
        v.sort()
        mid = len(v) // 2
        rec[a] = {"n": len(v), "win": sum(1 for _, p in v if p > 0) / len(v),
                  "pnl": sum(p for _, p in v),
                  "h1": sum(p for _, p in v[:mid]), "h2": sum(p for _, p in v[mid:])}
    return rec


def follow(rows, wallets, cost, sign=1):
    by = defaultdict(list)
    for r in rows:
        if r["kind"] in ENTRY and r["addr"] in wallets and r.get(KEY) is not None:
            by[r["day"]].append(sign * r[KEY] - cost)
    return by


def front_run(rows, ks, os_, lat, cost):
    by = defaultdict(list)
    for r in rows:
        if r["kind"] not in ENTRY:
            continue
        k0, p0 = open_at(ks, os_, r["coin"], r["t_first"] + lat)
        k1, p1 = open_at(ks, os_, r["coin"], r["t_last"] + 60)
        if p0 is None or p1 is None or k1 <= k0:
            continue
        by[r["day"]].append(r["dir"] * (p1 / p0 - 1) - cost)
    return by


def main(out):
    rows = load(out)
    train = [r for r in rows if r["day"] < TRAIN_END]
    hold = [r for r in rows if TRAIN_END <= r["day"] <= HOLD_END]
    w = print
    w(f"train {len(train)} events before {TRAIN_END}; holdout {len(hold)} events {TRAIN_END}..{HOLD_END}")
    rec = wallet_records(train)
    eligible = {a: x for a, x in rec.items() if x["n"] >= MIN_CLOSED}
    consistent = {a for a, x in eligible.items() if x["win"] >= 0.60 and x["h1"] > 0 and x["h2"] > 0}
    ranked = sorted(eligible, key=lambda a: eligible[a]["pnl"])
    worst = set(ranked[:max(1, len(ranked) // 10)])
    w(f"wallets with >= {MIN_CLOSED} closed train positions: {len(eligible)}; "
      f"consistent: {len(consistent)}; bottom decile: {len(worst)}")
    ks, os_ = candles(out)

    primary = []
    for cost in COSTS:
        w(f"\n######## copier cost {cost*1e4:.0f} bps round trip ########")
        cells = [
            ("3 consistent, train", follow(train, consistent, cost), False),
            ("3 consistent, HOLDOUT", follow(hold, consistent, cost), True),
            ("4 fade worst, train", follow(train, worst, cost, -1), False),
            ("4 fade worst, HOLDOUT", follow(hold, worst, cost, -1), True),
        ]
        for lat in LATENCIES:
            cells.append((f"5 front-run {lat}s, train", front_run(train, ks, os_, lat, cost), False))
            cells.append((f"5 front-run {lat}s, HOLDOUT", front_run(hold, ks, os_, lat, cost), True))
        for name, by, is_hold in cells:
            s = stats(by)
            h = halves(by) if is_hold else None
            hs = (f"  halves {h[0]*1e4:+.1f} / {h[1]*1e4:+.1f}bp" if h and None not in h else "")
            w(f"  {name:28s} {fmt(s)}{hs}")
            if is_hold and cost == COSTS[0] and (name.startswith("3") or name.startswith("4") or "30s" in name):
                primary.append((name, s, h))
    ps = [s["p"] if s else 1.0 for _, s, _ in primary]
    ps += [1.0] * (N_VARIANTS - len(ps))
    keep = bh(ps)
    w(f"\nSurvival (4 bps, BH q=0.10 over {N_VARIANTS} variants, unscored counted p=1):")
    for i, (name, s, h) in enumerate(primary):
        ok = (i in keep and s and s["lo"] is not None and s["lo"] > 0 and h and None not in h and min(h) > 0)
        w(f"  {name:28s} p {s['p'] if s else 1:.3f}  {'SURVIVES' if ok else 'dead'}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "out"))
