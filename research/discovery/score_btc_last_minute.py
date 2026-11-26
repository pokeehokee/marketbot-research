"""Score idea #68: does the already-fixed part of the settlement average beat the last-minute price?

Reads btc-<tag>-trades.csv.gz. Settlement A = (locked_sum + m * fut_avg) / 60, m = 60 - n_locked.
For a "greater" strike K:  A > K  <=>  fut_avg > T = (60 * K' - locked_sum) / m,
where K' = K - b and b runs over the deciles of (expiration_value - proxy_avg) measured on the
TRAINING half only; p is the average over those ten. The proxy is Kraken, the settlement is BRTI,
and the gap is measured, never assumed zero -- nor assumed constant: on the first full pull its
p10-p90 spread was about $11, so a point basis would make the model overconfident in exactly the
under-$10 band where the proxy disagrees with Kalshi's result (decided before any score was read).

    need = ln(T / spot) / (rv * sqrt(seconds left))     -- all known at the print
    Z    = ln(fut_avg / spot) / (rv * sqrt(seconds left))   -- outcome; train half only
    p_yes = P(Z > need)          (T <= 0: the fixed part alone already decides it)

Z is fitted per seconds-left bucket on one half of the days and applied to the other, both ways
round. Then copy-the-print with taker fee, Brier against the market, day-clustered bootstrap
(score_wx_index.score). A synthetic check (--selftest) confirms the scorer can find an edge
when prices are random and declines one when prices equal the model.

A real pull is ~8 million prints, too many to hold as dicts (the first run's scorer was killed by
the runner at ~70 s). `report_stream` reads the file twice and keeps no rows: pass 1 fits both
halves, pass 2 prices each print and feeds running accumulators. `report` is the in-memory
reference it must match exactly (test_btc_last_minute.TestStreamingMatchesReference).
"""
import bisect, csv, gzip, math, os, random, sys
from collections import defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score_wx_index as W  # noqa: E402

ET = ZoneInfo("America/New_York")
S_EDGES = (0, 5, 10, 20, 30, 45, 60, 90, 121)


def sbucket(s):
    for lo, hi in zip(S_EDGES, S_EDGES[1:]):
        if lo <= s < hi:
            return lo
    return None


def rows_from(path):
    f = lambda x: float(x) if x not in ("", None, "None") else None
    days, intern = {}, sys.intern
    with gzip.open(path, "rt") as fh:
        for r in csv.DictReader(fh):
            rv, spot, fut = f(r["rv"]), f(r["spot"]), f(r["fut_avg"])
            if not rv or not spot or not fut or r["result"] not in ("yes", "no"):
                continue
            t1, ts = float(r["t1"]), float(r["trade_ts"])
            left = t1 - ts
            if left <= 0:
                continue
            day = days.get(t1)
            if day is None:
                day = days[t1] = datetime.fromtimestamp(t1, ET).date().isoformat()
            n_locked = int(r["n_locked"])
            yield dict(series=intern(r["series"]), ticker=intern(r["ticker"]), t1=t1, ts=ts, left=left,
                       sb=sbucket(left), day=day, st=(r["strike_type"] or "").lower(),
                       floor=f(r["floor"]), cap=f(r["cap"]),
                       result=r["result"], ev=f(r["expiration_value"]), pavg=f(r["proxy_avg"]),
                       n_locked=n_locked, m=60 - n_locked, locked=float(r["locked_sum"]),
                       spot=spot, rv=rv, fut=fut, side=r["taker_side"],
                       paid=float(r["taker_paid"]), n=float(r["count"]))


def load(path):
    return list(rows_from(path))


def zscore(r):
    return math.log(r["fut"] / r["spot"]) / (r["rv"] * math.sqrt(r["left"]))


def p_above(zs, r, k):
    """P(A > k) for this print under the fitted Z sample."""
    t = (60.0 * k - r["locked"]) / r["m"]
    if t <= 0:
        return 1.0
    need = math.log(t / r["spot"]) / (r["rv"] * math.sqrt(r["left"]))
    i = bisect.bisect_right(zs, need)           # count of z <= need
    return (len(zs) - i + 0.5) / (len(zs) + 1)


def p_yes(r, dist, basis):
    """basis: one value, or a list (the training half's deciles) to average over."""
    zs = dist.get((r["series"], r["sb"])) or dist.get(("*", r["sb"]))
    if not zs:
        return None
    if isinstance(basis, (list, tuple)):
        ps = [p_yes(r, dist, b) for b in basis]
        return None if any(p is None for p in ps) else sum(ps) / len(ps)
    st, fl, cp = r["st"], r["floor"], r["cap"]
    if st in ("greater", "greater_or_equal") and fl is not None:
        return p_above(zs, r, fl - basis)
    if st in ("less", "less_or_equal") and cp is not None:
        return 1.0 - p_above(zs, r, cp - basis)
    if st == "between" and fl is not None and cp is not None:
        return max(0.0, p_above(zs, r, fl - basis) - p_above(zs, r, cp - basis))
    return None


def deciles(gaps):
    g = sorted(gaps)
    if not g:
        return [0.0]
    return [g[min(len(g) - 1, int((i + 0.5) * len(g) / 10))] for i in range(10)]


def fit_from(zfirst, gap_by_t1):
    """zfirst: (series, sb, z) per (t1, second), first print seen; gap_by_t1: t1 -> ev - proxy avg."""
    d = defaultdict(list)
    for series, sb, z in zfirst:
        d[(series, sb)].append(z)
        d[("*", sb)].append(z)
    dist = {k: sorted(v) for k, v in d.items() if len(v) >= 30}
    return dist, deciles(list(gap_by_t1))


def fit(train):
    seen, zfirst, gaps = set(), [], {}
    for r in train:
        k = (r["t1"], int(r["ts"]))
        if k not in seen:
            seen.add(k)
            zfirst.append((r["series"], r["sb"], zscore(r)))
        if r["ev"] is not None:
            gaps[r["t1"]] = r["ev"] - r["pavg"]
    return fit_from(zfirst, gaps.values())


def price_out_of_sample(rows):
    days = sorted({r["day"] for r in rows})
    half = set(days[: len(days) // 2])
    A = [r for r in rows if r["day"] in half]
    B = [r for r in rows if r["day"] not in half]
    bases = []
    for train, test in ((A, B), (B, A)):
        dist, basis = fit(train)
        bases.append(basis)
        for r in test:
            r["p"] = p_yes(r, dist, basis)
    return half, bases


def report(rows):
    days = sorted({r["day"] for r in rows})
    print(f"{len(rows)} prints with proxy, spot and vol known, {len(days)} days, "
          f"{len({r['ticker'] for r in rows})} markets")
    if len(days) < 4:
        print("too few days to split; stop")
        return
    half, bases = price_out_of_sample(rows)
    print(basis_line(bases))
    scored = [r for r in rows if r.get("p") is not None]
    print(f"{len(scored)} prints priced out of sample")

    print("\nBrier per market per second (last print), market vs locked-average model:")
    last = {}
    for r in sorted(scored, key=lambda r: r["ts"]):
        last[(r["ticker"], int(r["ts"]))] = r
    agg = defaultdict(lambda: [0.0, 0.0, 0.0])
    for r in last.values():
        y = 1.0 if r["result"] == "yes" else 0.0
        mk = r["paid"] if r["side"] == "yes" else 1 - r["paid"]
        for k in ("all", r["series"], f"{r['sb']:>3}+ s left"):
            a = agg[k]; a[0] += (mk - y) ** 2; a[1] += (r["p"] - y) ** 2; a[2] += 1
    for k in sorted(agg):
        a = agg[k]
        print(f"  {k:<14} n {a[2]:7.0f}  market {a[0]/a[2]:.4f}  model {a[1]/a[2]:.4f}")

    print("\nCopy the print's side when model - price - taker fee >= threshold:")
    for thr in (0.0, 0.03, 0.05, 0.10):
        W.score(scored, thr, f"all, edge >= {thr*100:.0f}c")
    W.score(scored, 0.05, "edge >= 5c, first print per market", one_per_market=True)
    for name, sub in (("first half", [r for r in scored if r["day"] in half]),
                      ("second half", [r for r in scored if r["day"] not in half])):
        W.score(sub, 0.05, f"{name}, edge >= 5c")
    for s in sorted({r["series"] for r in scored}):
        W.score([r for r in scored if r["series"] == s], 0.05, f"{s}, edge >= 5c")
    for sb in S_EDGES[:-1]:
        W.score([r for r in scored if r["sb"] == sb], 0.05, f"{sb}+ s left, edge >= 5c")
    print("\nSame, inside the averaging minute only (n_locked >= 1):")
    W.score([r for r in scored if r["n_locked"] >= 1], 0.05, "locked >= 1s, edge >= 5c")
    W.score([r for r in scored if r["n_locked"] >= 30], 0.05, "locked >= 30s, edge >= 5c")
    W.score([r for r in scored if r["n_locked"] >= 50], 0.05, "locked >= 50s, edge >= 5c")


def basis_line(bases):
    fmt = lambda b: f"median {b[len(b) // 2]:+.2f}, deciles {b[0]:+.2f}..{b[-1]:+.2f}"
    return (f"basis (expiration_value - proxy avg), each training half: "
            f"{fmt(bases[0])} | {fmt(bases[1])}")


class Acc:
    """W.score as a running sum, so no print has to be kept. Same output, same bootstrap."""

    def __init__(self, label, thr, keep=lambda r: True, one_per_market=False):
        self.label, self.thr, self.keep, self.opm = label, thr, keep, one_per_market
        self.by_day = defaultdict(lambda: [0.0, 0.0])
        self.ct = self.pnl = self.lost = self.implied = 0.0
        self.lost_pos = self.n = 0
        self.first = {}                   # one_per_market: ticker -> earliest qualifying print

    def add(self, r, order):
        if not self.keep(r):
            return
        q = r["p"] if r["side"] == "yes" else 1 - r["p"]
        fee = W.fee_per_ct(r["paid"], r["n"])
        if q - r["paid"] - fee < self.thr:
            return
        won = 1.0 if r["result"] == r["side"] else 0.0
        rec = (r["ts"], order, r["day"], won, r["paid"], fee, r["n"])
        if self.opm:
            cur = self.first.get(r["ticker"])
            if cur is None or rec[:2] < cur[:2]:
                self.first[r["ticker"]] = rec
        else:
            self._take(rec)

    def _take(self, rec):
        _, _, day, won, paid, fee, c = rec
        e = (won - paid - fee) * c
        self.ct += c; self.pnl += e; self.n += 1
        self.lost += (1 - won) * c; self.implied += (1 - paid) * c
        self.lost_pos += int(won == 0)
        self.by_day[day][0] += e; self.by_day[day][1] += c

    def finish(self):
        for rec in sorted(self.first.values()):
            self._take(rec)
        if not self.ct:
            print(f"  {self.label:<34} nothing taken")
            return
        lo, hi = W.boot({d: self.by_day[d] for d in sorted(self.by_day)})
        ci = f"[{lo*100:+.2f}, {hi*100:+.2f}]" if lo is not None else "[n/a]"
        print(f"  {self.label:<34} prints {self.n:6d} ct {self.ct:9.0f} days {len(self.by_day):3d} "
              f"losing prints {self.lost_pos:5d} ratio "
              f"{self.lost/self.implied if self.implied else float('nan'):.2f}  "
              f"EV {self.pnl/self.ct*100:+.2f}c {ci}")


def report_stream(path):
    """report(), reading the file twice and holding no prints."""
    days, tickers, nrows = set(), set(), 0
    zfirst, gaps = {}, {}
    for r in rows_from(path):
        nrows += 1
        days.add(r["day"]); tickers.add(r["ticker"])
        k = (r["t1"], int(r["ts"]))
        if k not in zfirst:
            zfirst[k] = (r["day"], r["series"], r["sb"], zscore(r))
        if r["ev"] is not None:
            gaps[r["t1"]] = (r["day"], r["ev"] - r["pavg"])
    days = sorted(days)
    print(f"{nrows} prints with proxy, spot and vol known, {len(days)} days, {len(tickers)} markets")
    if len(days) < 4:
        print("too few days to split; stop")
        return
    half = set(days[: len(days) // 2])
    fits = {}
    for h in (True, False):
        fits[h] = fit_from([v[1:] for v in zfirst.values() if (v[0] in half) == h],
                           [g for d, g in gaps.values() if (d in half) == h])
    del zfirst, gaps
    print(basis_line([fits[True][1], fits[False][1]]))

    series = set()
    accs = [Acc(f"all, edge >= {thr*100:.0f}c", thr) for thr in (0.0, 0.03, 0.05, 0.10)]
    accs.append(Acc("edge >= 5c, first print per market", 0.05, one_per_market=True))
    accs.append(Acc("first half, edge >= 5c", 0.05, lambda r: r["day"] in half))
    accs.append(Acc("second half, edge >= 5c", 0.05, lambda r: r["day"] not in half))
    per_series = {}
    per_sb = [Acc(f"{sb}+ s left, edge >= 5c", 0.05, lambda r, sb=sb: r["sb"] == sb) for sb in S_EDGES[:-1]]
    locked = [Acc(f"locked >= {k}s, edge >= 5c", 0.05, lambda r, k=k: r["n_locked"] >= k) for k in (1, 30, 50)]
    last, scored = {}, 0
    for order, r in enumerate(rows_from(path)):
        dist, basis = fits[r["day"] not in half]          # price with the OTHER half's fit
        r["p"] = p_yes(r, dist, basis)
        if r["p"] is None:
            continue
        scored += 1
        k = (r["ticker"], int(r["ts"]))
        cur = last.get(k)
        if cur is None or (r["ts"], order) >= cur[:2]:
            mk = r["paid"] if r["side"] == "yes" else 1 - r["paid"]
            last[k] = (r["ts"], order, r["series"], r["sb"], mk, r["p"],
                       1.0 if r["result"] == "yes" else 0.0)
        if r["series"] not in per_series:
            per_series[r["series"]] = Acc(f"{r['series']}, edge >= 5c", 0.05,
                                          lambda x, s=r["series"]: x["series"] == s)
        for a in accs + per_sb + locked + [per_series[r["series"]]]:
            a.add(r, order)
    print(f"{scored} prints priced out of sample")

    print("\nBrier per market per second (last print), market vs locked-average model:")
    agg = defaultdict(lambda: [0.0, 0.0, 0.0])
    for _, _, s, sb, mk, p, y in last.values():
        for k in ("all", s, f"{sb:>3}+ s left"):
            a = agg[k]; a[0] += (mk - y) ** 2; a[1] += (p - y) ** 2; a[2] += 1
    for k in sorted(agg):
        a = agg[k]
        print(f"  {k:<14} n {a[2]:7.0f}  market {a[0]/a[2]:.4f}  model {a[1]/a[2]:.4f}")

    print("\nCopy the print's side when model - price - taker fee >= threshold:")
    for a in accs:
        a.finish()
    for s in sorted(per_series):
        per_series[s].finish()
    for a in per_sb:
        a.finish()
    print("\nSame, inside the averaging minute only (n_locked >= 1):")
    for a in locked:
        a.finish()


def synthetic(price_mode, seed=3):
    """A tape where the truth is known: random-walk seconds, strike near spot."""
    rng = random.Random(seed)
    rows = []
    for d in range(12):
        day = f"2026-11-{d + 1:02d}"
        for e in range(40):
            t1 = 1_800_000_000 + d * 86400 + e * 900
            path = [65000.0]
            for _ in range(180):
                path.append(path[-1] * math.exp(rng.gauss(0, 0.0002)))
            g = path[-60:]
            avg = sum(g) / 60
            k = path[60] + rng.gauss(0, 15)
            res = "yes" if avg > k else "no"
            for left in (100, 50, 25, 8, 3):
                idx = 180 - left
                nl = max(0, min(59, 60 - left))
                locked = sum(g[:nl])
                fut = sum(g[nl:]) / (60 - nl)
                rows.append(dict(series="SYN", ticker=f"S{d}-{e}", t1=t1, ts=t1 - left, left=left,
                                 sb=sbucket(left), day=day, st="greater", floor=k, cap=None,
                                 result=res, ev=avg, pavg=avg, n_locked=nl, m=60 - nl, locked=locked,
                                 spot=path[idx], rv=0.0002, fut=fut, side="yes", paid=0.5, n=10.0))
    price_out_of_sample(rows)
    for r in rows:
        if r.get("p") is None:
            continue
        r["paid"] = round(min(0.99, max(0.01, rng.random() if price_mode == "random" else r["p"])), 2)
    return rows


if __name__ == "__main__":
    if sys.argv[1:] == ["--selftest"]:
        for mode in ("random", "truth"):
            print(f"\n== synthetic, prices {mode}")
            rs = synthetic(mode)
            W.score([r for r in rs if r.get("p") is not None], 0.05, f"synthetic {mode}, edge >= 5c")
    else:
        report_stream(sys.argv[1])
