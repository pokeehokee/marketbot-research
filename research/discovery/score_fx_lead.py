"""Score idea #67: does free FX spot beat the 15-minute FX price inside the window?

Reads fx-<tag>-trades.csv.gz. For a print at time ts in window [t0, t1]:

    need = ln(open / spot) / (rv * sqrt(minutes left))     -- all known at the print
    Z    = ln(close / spot) / (rv * sqrt(minutes left))     -- the outcome; train half only
    p_yes = P(Z >= need)

Z's distribution is fitted on one half of the days (one sample per market per minute, not
per print) and applied to the other half, both ways round. Then the same copy-the-print
strategy, fee, Brier comparison and day-clustered bootstrap as score_wx_index.
"""
import csv, gzip, math, os, sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score_wx_index as W  # noqa: E402

ET = ZoneInfo("America/New_York")
TAU_EDGES = (0, 1, 3, 6, 10, 16)


def tbucket(m):
    for lo, hi in zip(TAU_EDGES, TAU_EDGES[1:]):
        if lo <= m < hi:
            return lo
    return None


def load(path):
    rows = []
    with gzip.open(path, "rt") as fh:
        for r in csv.DictReader(fh):
            if not r["spot_mid"] or not r["rv"] or r["result"] not in ("yes", "no"):
                continue
            rv = float(r["rv"])
            if rv <= 0:
                continue
            ts, t1 = float(r["trade_ts"]), float(r["t1"])
            tau = (t1 - ts) / 60.0
            if tau <= 0:
                continue
            spot, o, c = float(r["spot_mid"]), float(r["open_mid"]), float(r["close_mid"])
            scale = rv * math.sqrt(tau)
            rows.append(dict(series=r["series"], ticker=r["ticker"], ts=ts, tau=tau, tb=tbucket(tau),
                             day=datetime.fromtimestamp(t1, ET).date().isoformat(),
                             need=math.log(o / spot) / scale, z=math.log(c / spot) / scale,
                             side=r["taker_side"], paid=float(r["taker_paid"]), n=float(r["count"]),
                             result=r["result"]))
    return rows


def fit(train):
    seen, d = set(), defaultdict(list)
    for r in train:
        k = (r["ticker"], int(r["ts"] // 60))
        if k in seen:
            continue
        seen.add(k)
        d[(r["series"], r["tb"])].append(r["z"])
        d[("*", r["tb"])].append(r["z"])
    return {k: sorted(v) for k, v in d.items() if len(v) >= 30}


def p_yes(r, dist):
    zs = dist.get((r["series"], r["tb"])) or dist.get(("*", r["tb"]))
    if not zs:
        return None
    import bisect
    i = bisect.bisect_left(zs, r["need"])          # count of z < need
    return (len(zs) - i + 0.5) / (len(zs) + 1)


def main(path):
    rows = load(path)
    days = sorted({r["day"] for r in rows})
    print(f"{len(rows)} prints with spot and vol known, {len(days)} days, "
          f"{len({r['ticker'] for r in rows})} markets")
    half = set(days[: len(days) // 2])
    A = [r for r in rows if r["day"] in half]
    B = [r for r in rows if r["day"] not in half]
    for train, test in ((A, B), (B, A)):
        dist = fit(train)
        for r in test:
            r["p"] = p_yes(r, dist)
    scored = [r for r in rows if r.get("p") is not None]
    print(f"{len(scored)} prints priced out of sample")

    print("\nBrier per market per minute (last print), market vs spot model:")
    last = {}
    for r in sorted(scored, key=lambda r: r["ts"]):
        last[(r["ticker"], int(r["ts"] // 60))] = r
    agg = defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
    for r in last.values():
        y = 1.0 if r["result"] == "yes" else 0.0
        m = r["paid"] if r["side"] == "yes" else 1 - r["paid"]
        for k in ("all", f"{r['series']}", f"tau {r['tb']:>2}+ min"):
            a = agg[k]; a[0] += (m - y) ** 2; a[1] += (r["p"] - y) ** 2; a[2] += 1
            a[3] += ((m + r["p"]) / 2 - y) ** 2
    for k in sorted(agg):
        a = agg[k]
        print(f"  {k:<14} n {a[2]:6.0f}  market {a[0]/a[2]:.4f}  model {a[1]/a[2]:.4f}  blend {a[3]/a[2]:.4f}")

    print("\nCopy the print's side when model - price - taker fee >= threshold:")
    for thr in (0.0, 0.03, 0.05, 0.10):
        W.score(scored, thr, f"all, edge >= {thr*100:.0f}c")
    W.score(scored, 0.05, "edge >= 5c, first print per market", one_per_market=True)
    for name, sub in (("first half", [r for r in scored if r["day"] in half]),
                      ("second half", [r for r in scored if r["day"] not in half])):
        W.score(sub, 0.05, f"{name}, edge >= 5c")
    for s in sorted({r["series"] for r in scored}):
        W.score([r for r in scored if r["series"] == s], 0.05, f"{s}, edge >= 5c")
    for tb in TAU_EDGES[:-1]:
        W.score([r for r in scored if r["tb"] == tb], 0.05, f"{tb}+ min left, edge >= 5c")


if __name__ == "__main__":
    main(sys.argv[1])
