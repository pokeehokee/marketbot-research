"""Score whale_flow.py's events: does copying a big wallet's trade pay after a copier's delay
and fees? Sealed holdout, multiple-testing correction, controls. Output to stdout.

Cells: family x delay x horizon. Train = the first 60% of the days that have events; holdout
the rest. A cell is SELECTED on train when its net mean's day-clustered bootstrap lower bound
is above zero AND it survives Benjamini-Hochberg at q=0.10 across every train cell. Only
selected cells are read on the holdout, and a cell PASSES only if the holdout's lower bound
is also above zero. The copy list (the TikTok strategy: follow the wallets that won) is built
on train only and read on the holdout only.

Net = signed return minus COST round trip (a copier pays the taker fee twice). Reported at
two costs: 4 bps (Coinbase's US perps quote 0.02% a side) and 10 bps (Hyperliquid base taker
0.045% a side plus a little slippage). Win rate, payout multiple (mean win / mean loss) and
profit per dollar are reported separately. Leverage multiplies profit per dollar of margin and
its spread by the same factor, so it changes nothing about whether an edge exists, only how
fast a bad run liquidates you.

Controls: random-sign null (the same events with a coin-flip direction; it must select ~nothing),
and the hour BEFORE the event (do these wallets chase moves?).
"""
import csv, gzip, math, random, sys
from collections import defaultdict

DELAYS = (0, 60, 300, 900)
HORIZONS = (60, 300, 900, 3600, 4 * 3600)
COSTS = (0.0004, 0.0010)
TRAIN_FRAC = 0.6
Q = 0.10
B = 2000
MIN_WALLET_TRAIN = 15


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load(paths):
    rows = []
    for p in paths:
        if "-funding" in p or "-candles" in p:
            continue
        with gzip.open(p, "rt") as fo:
            for r in csv.DictReader(fo):
                for k in list(r):
                    if k.startswith("r_d") or k in ("notional", "taker_share", "pre_1h", "t_last", "account"):
                        r[k] = fnum(r[k])
                r["dir"] = int(r["dir"])
                rows.append(r)
    return rows


def boot(groups, seed=7):
    """groups: [[sum, n]] per day. Mean, 2.5%, 97.5%, share of resamples <= 0."""
    groups = [g for g in groups if g[1]]
    if len(groups) < 3:
        return None
    tot = sum(g[0] for g in groups) / sum(g[1] for g in groups)
    rnd = random.Random(seed)
    ms = []
    for _ in range(B):
        s = n = 0
        for _ in groups:
            g = groups[rnd.randrange(len(groups))]
            s += g[0]; n += g[1]
        ms.append(s / n)
    ms.sort()
    return tot, ms[int(0.025 * B)], ms[int(0.975 * B) - 1], sum(1 for m in ms if m <= 0) / B


def cell(rows, key, cost):
    by = defaultdict(lambda: [0.0, 0])
    vals = []
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        v -= cost
        vals.append(v)
        g = by[r["day"]]; g[0] += v; g[1] += 1
    if not vals:
        return None
    b = boot(list(by.values()))
    if not b:
        return None
    wins = [v for v in vals if v > 0]
    losses = [-v for v in vals if v <= 0]
    mult = (sum(wins) / len(wins)) / (sum(losses) / len(losses)) if wins and losses else None
    return {"n": len(vals), "days": len(by), "mean": b[0], "lo": b[1], "hi": b[2], "p": b[3],
            "win": len(wins) / len(vals), "mult": mult,
            "wallets": len({r["addr"] for r in rows if r.get(key) is not None})}


def fmt(c):
    if not c:
        return "too few days"
    m = f"{c['mult']:.2f}" if c["mult"] else "-"
    return (f"n {c['n']:6d} d {c['days']:3d} w {c['wallets']:4d}  net {c['mean']*1e4:+7.1f}bp "
            f"[{c['lo']*1e4:+7.1f}, {c['hi']*1e4:+7.1f}]  win {c['win']*100:5.1f}%  x {m}")


def bh(ps, q=Q):
    """Indices that survive Benjamini-Hochberg."""
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    k = 0
    for rank, i in enumerate(order, 1):
        if ps[i] <= q * rank / len(ps):
            k = rank
    return set(order[:k])


ENTRY = ("open", "add", "flip")
FAMILIES = {
    "opens+adds+flips": lambda r: r["kind"] in ENTRY,
    "opens and flips only": lambda r: r["kind"] in ("open", "flip"),
    ">= $1M": lambda r: r["kind"] in ENTRY and (r["notional"] or 0) >= 1e6,
    ">= $250k taker": lambda r: r["kind"] in ENTRY and (r["notional"] or 0) >= 2.5e5 and (r["taker_share"] or 0) >= 0.8,
    "closes and reduces": lambda r: r["kind"] in ("close", "reduce"),
    "BTC entries": lambda r: r["kind"] in ENTRY and r["coin"] == "BTC",
    "ETH entries": lambda r: r["kind"] in ENTRY and r["coin"] == "ETH",
    "SOL entries": lambda r: r["kind"] in ENTRY and r["coin"] == "SOL",
}


def split(rows):
    days = sorted({r["day"] for r in rows})
    cut = days[int(len(days) * TRAIN_FRAC)] if days else None
    return [r for r in rows if r["day"] < cut], [r for r in rows if r["day"] >= cut], cut, days


def copy_list(train, key, cost):
    """Wallets with >= MIN_WALLET_TRAIN train entries whose train net mean is > 0 at t >= 2."""
    by = defaultdict(list)
    for r in train:
        if r["kind"] in ENTRY and r.get(key) is not None:
            by[r["addr"]].append(r[key] - cost)
    keep = set()
    for a, v in by.items():
        if len(v) < MIN_WALLET_TRAIN:
            continue
        m = sum(v) / len(v)
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / (len(v) - 1)) or 1e-12
        if m > 0 and m / (sd / math.sqrt(len(v))) >= 2:
            keep.add(a)
    return keep


def main(paths):
    rows = load(paths)
    train, hold, cut, days = split(rows)
    w = print
    w(f"{len(rows)} events from {len({r['addr'] for r in rows})} wallets over {len(days)} days "
      f"({days[0] if days else '-'} to {days[-1] if days else '-'}); holdout from {cut}")
    kinds = defaultdict(int)
    for r in rows:
        kinds[r["kind"]] += 1
    w("kinds: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    w()
    w("== Context: the hour BEFORE an entry, in the trade's direction (do they chase?) ==")
    pre = [dict(r, pre=r["pre_1h"]) for r in rows if r["kind"] in ENTRY]
    w("  " + fmt(cell(pre, "pre", 0.0)) + "  (cost 0; + means they buy after a rise)")
    w()
    for cost in COSTS:
        w(f"######## copier cost {cost*1e4:.0f} bps round trip ########")
        grid = []
        for fam, fn in FAMILIES.items():
            for dl in DELAYS:
                for h in HORIZONS:
                    k = f"r_d{dl}_h{h}"
                    grid.append((fam, dl, h, k, cell([r for r in train if fn(r)], k, cost)))
        ps = [g[4]["p"] if g[4] else 1.0 for g in grid]
        keep = bh(ps)
        sel = [g for i, g in enumerate(grid) if i in keep and g[4] and g[4]["lo"] > 0]
        # null: random signs on the same train events
        rnd = random.Random(11)
        null = [dict(r, **{k: (r[k] if r[k] is None else r[k] * rnd.choice((1, -1)))
                           for k in r if k.startswith("r_d")}) for r in train]
        ng = []
        for fam, fn in FAMILIES.items():
            for dl in DELAYS:
                for h in HORIZONS:
                    ng.append(cell([r for r in null if fn(r)], f"r_d{dl}_h{h}", cost))
        nsel = [i for i in bh([c["p"] if c else 1.0 for c in ng]) if ng[i] and ng[i]["lo"] > 0]
        w(f"train cells {len(grid)}; selected (BH q={Q}, lower bound > 0): {len(sel)}; "
          f"random-sign null selects {len(nsel)}")
        w()
        w("-- Train, the one pre-registered cell per family: 60 s late, 1 hour hold --")
        for fam, fn in FAMILIES.items():
            c = next(g[4] for g in grid if g[0] == fam and g[1] == 60 and g[2] == 3600)
            w(f"  {fam:22s} {fmt(c)}")
        w()
        w("-- How much survives the delay (opens+adds+flips, train), net per horizon --")
        for dl in DELAYS:
            line = "  ".join(f"{h//60:>4d}m {(next(g[4] for g in grid if g[0]=='opens+adds+flips' and g[1]==dl and g[2]==h) or {}).get('mean', float('nan'))*1e4:+6.1f}bp"
                             for h in HORIZONS)
            w(f"  late {dl:4d}s: {line}")
        w()
        w("-- Selected on train, read on the HOLDOUT --")
        if not sel:
            w("  nothing selected")
        for fam, dl, h, k, c in sel:
            hc = cell([r for r in hold if FAMILIES[fam](r)], k, cost)
            verdict = "PASS" if hc and hc["lo"] > 0 else "fail"
            w(f"  {fam:22s} late {dl:4d}s hold {h//60:4d}m  train {fmt(c)}")
            w(f"  {'':22s} {'':20s}  hold  {fmt(hc)}  {verdict}")
        w()
        w("-- Copy list (wallets that won on train, t >= 2, >= 15 entries), HOLDOUT only, BH over its cells --")
        cl = []
        for dl in DELAYS:
            for h in HORIZONS:
                k = f"r_d{dl}_h{h}"
                lst = copy_list(train, k, cost)
                hc = cell([r for r in hold if r["kind"] in ENTRY and r["addr"] in lst], k, cost)
                cl.append((dl, h, len(lst), hc))
        keepc = bh([c[3]["p"] if c[3] else 1.0 for c in cl])
        for i, (dl, h, nl, hc) in enumerate(cl):
            ok = "PASS" if i in keepc and hc and hc["lo"] > 0 else ""
            w(f"  late {dl:4d}s hold {h//60:4d}m  list {nl:3d} wallets  {fmt(hc)}  {ok}")
        w()


if __name__ == "__main__":
    main(sys.argv[1:])
