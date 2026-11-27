"""Which signals, seen at the moment of buying, make a CHEAP contract hit more often
than its price says? (alpha research, the main question from 2026-11-27)

A cheap contract is the side of a market trading at 15c or less, i.e. a 6.7x payout
or better. On average these are OVERpriced (the longshot bias: this repo's ladder earns
by selling them), so the baseline is expected to lose and the question is whether any
condition known at buy time flips that.

Rows come from two places, both written with nothing after the decision time:
  * tape  - research/alpha/precursor.py exports (hourly and 15-minute markets, 2-120 min
            before close, features from the public trade tape);
  * news  - research/alpha/jumps.py --export-cheap (markets in the news categories open a
            day or more, 6h/1d/3d/7d before close, features from 5-minute bars, including
            whether the cheap side just jumped or crashed).
Every price and every feature is turned to face the cheap side.

The bet is a $1 stake bought at the observed price: floor(1/q) contracts, Kalshi's taker
fee ceil(0.07 x C x q x (1-q)) on the order, return per dollar staked. The observed price
is a trade VWAP, not the ask, so every figure is an upper bound; the holdout repeats each
survivor on the rows where the last trade was a cheap-side BUY (so the price was the ask).

Method, fixed before any result was seen:
  1. Settle-days sorted; the first 60% are train. The holdout is not loaded in phase train.
  2. Cells per (source, time-to-close group): all rows, price band, category, and the top
     and bottom fifth of each numeric feature (cut points from train), each value of a
     categorical one. A cell needs 200 rows and 10 wins on train to be tested.
  3. Per cell: mean return per $1, bootstrap resampling whole settle-days (B=2000), one-sided
     p for "beats the price after the fee". Benjamini-Hochberg at q=0.10 across all cells.
  4. Controls: a leak feature (won + noise) must be found; a noise feature must not; and a
     null run where every row wins with exactly its break-even probability must find almost
     nothing. Otherwise the run is not TRUSTED and nothing is taken to the holdout.
  5. Holdout, once: each selected cell with its train cut points. A survivor keeps a 95%
     interval above zero, has 30+ wins (the rare outcome is what decides it), and holds on
     the ask-side rows where those exist.
"""
import argparse, csv, gzip, json, math, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
CHEAP_MAX = 0.15
TRAIN_SHARE = 0.60
Q = 0.10
B = 2000
MIN_ROWS, MIN_WINS = 200, 10
HOLD_MIN_WINS = 30
BANDS = ((0.0, 0.025, "01-02c"), (0.025, 0.055, "03-05c"), (0.055, 0.105, "06-10c"),
         (0.105, 0.155, "11-15c"))
TAPE_NUM = ("age", "dq5", "dq15", "dq60", "flowc15", "flowc60", "vol15", "vol60", "burst",
            "ntr15", "size60", "rng60", "hour_et")
TAPE_CAT = ("last_cheap", "cheap_yes")
NEWS_NUM = ("age_h", "dq1h", "dq6h", "dq24h", "vol24", "n24", "rng24")
NEWS_CAT = ("jump24", "cheap_yes")
CONTROLS = ("ctl_leak", "ctl_noise")


def fnum(s):
    return None if s in ("", None) else float(s)


def ret_per_dollar(q, won):
    """Return on a $1 stake bought at q, after the taker fee on the one order."""
    c = math.floor(1.0 / q + 1e-9)
    fee = math.ceil(round(0.07 * c * q * (1 - q) * 100, 6)) / 100.0
    return (c * ((1.0 if won else 0.0) - q) - fee) / (c * q)


def breakeven(q):
    c = math.floor(1.0 / q + 1e-9)
    fee = math.ceil(round(0.07 * c * q * (1 - q) * 100, 6)) / 100.0
    return min(1.0, q + fee / c)


def band(q):
    for lo, hi, n in BANDS:
        if lo < q <= hi:
            return n
    return None


def _noise(key, salt):
    h = int.from_bytes(__import__("hashlib").sha256(f"{key}|{salt}".encode()).digest()[:8], "big")
    return random.Random(h).gauss(0, 1)


def tape_row(r):
    """A precursor export row -> a cheap-side row, or None if neither side is cheap."""
    p, y = fnum(r["p"]), fnum(r["y"])
    if p is None or y is None or not (0 < p < 1):
        return None
    if p <= CHEAP_MAX:
        s = 1
    elif p >= 1 - CHEAP_MAX:
        s = -1
    else:
        return None
    q = p if s > 0 else 1 - p
    off = int(float(r["offset_min"]))
    g = "long" if off >= 60 else ("mid" if off >= 15 else "short")
    f = {}
    for k in ("age", "vol15", "vol60", "burst", "ntr15", "size60", "rng60", "hour_et"):
        f[k] = fnum(r.get(k))
    for k in ("dp5", "dp15", "dp60"):
        v = fnum(r.get(k)); f["dq" + k[2:]] = None if v is None else s * v
    for k in ("flow15", "flow60"):
        v = fnum(r.get(k)); f["flowc" + k[4:]] = None if v is None else s * v
    ly = fnum(r.get("last_yes"))
    f["last_cheap"] = None if ly is None else int(ly if s > 0 else 1 - ly)
    f["cheap_yes"] = 1 if s > 0 else 0
    won = int(y) if s > 0 else 1 - int(y)
    return {"source": "tape", "g": g, "category": r.get("category") or "", "series": r["series"],
            "event": r["ticker"].rsplit("-", 1)[0], "ticker": r["ticker"], "off": off,
            "day": r["day"], "q": q, "won": won, "f": f}


def news_row(r):
    q, won = fnum(r["q"]), int(r["won"])
    if q is None or not (0 < q <= CHEAP_MAX):
        return None
    f = {k: fnum(r.get(k)) for k in NEWS_NUM}
    for k in NEWS_CAT:
        v = fnum(r.get(k)); f[k] = None if v is None else int(v)
    return {"source": "news", "g": f"{r['offset_h']}h", "category": r.get("category") or "",
            "series": r["series"], "event": r["event"], "ticker": r["ticker"],
            "off": int(r["offset_h"]), "day": r["day"], "q": q, "won": won, "f": f}


def iter_rows(paths):
    for p in paths:
        with gzip.open(p, "rt") as fh:
            rd = csv.DictReader(fh)
            news = "q" in (rd.fieldnames or [])
            for r in rd:
                x = news_row(r) if news else tape_row(r)
                if x:
                    yield x


def split_days(paths):
    """Per source: the first 60% of that source's settle-days are train. For the tape rows this
    is the same split as screen.py's, so the two screens share one sealed holdout."""
    by = {}
    for r in iter_rows(paths):
        by.setdefault(r["source"], set()).add(r["day"])
    train, hold = {}, {}
    for src, ds in by.items():
        ds = sorted(ds)
        k = max(1, int(len(ds) * TRAIN_SHARE))
        train[src], hold[src] = ds[:k], ds[k:]
    return train, hold


def load(paths, keep):
    keep = {k: set(v) for k, v in keep.items()}
    rows = []
    for r in iter_rows(paths):
        if r["day"] in keep.get(r["source"], ()):
            k = f"{r['ticker']}|{r['off']}"
            r["f"]["ctl_leak"] = r["won"] + 0.3 * _noise(k, "leak")
            r["f"]["ctl_noise"] = _noise(k, "noise")
            r["ret"] = ret_per_dollar(r["q"], r["won"])
            rows.append(r)
    return rows


def fit_cal(rows):
    """Settle rate per (source, price band) on train: what the pool itself does at a price."""
    acc = {}
    for r in rows:
        a = acc.setdefault(f"{r['source']}|{band(r['q'])}", [0, 0])
        a[0] += r["won"]; a[1] += 1
    return {k: v[0] / v[1] for k, v in acc.items()}


def apply_cal(rows, cal):
    """adj: excess return per $1 over what the pool at the same price did. Zero on average
    for the pool by construction, so a cell with adj > 0 knows something the price band
    does not."""
    for r in rows:
        c = cal.get(f"{r['source']}|{band(r['q'])}")
        r["adj"] = None if c is None else (r["won"] - c) / r["q"]
    return [r for r in rows if r["adj"] is not None]


def numfeats(src):
    return (TAPE_NUM if src == "tape" else NEWS_NUM) + CONTROLS


def catfeats(src):
    return TAPE_CAT if src == "tape" else NEWS_CAT


def quint(vals):
    v = sorted(vals)
    if len(set(v)) < 5:
        return None
    return v[int(0.2 * (len(v) - 1))], v[int(0.8 * (len(v) - 1))]


def make_specs(rows):
    """Cell definitions from train rows only: (source, g, kind, key, a, b)."""
    specs = []
    groups = sorted({(r["source"], r["g"]) for r in rows})
    for src, g in groups:
        gr = [r for r in rows if r["source"] == src and r["g"] == g]
        specs.append((src, g, "all", "", None, None))
        for _, _, n in BANDS:
            specs.append((src, g, "band", n, None, None))
        cats = {}
        for r in gr:
            cats[r["category"]] = cats.get(r["category"], 0) + 1
        for c in sorted(cats):
            specs.append((src, g, "cat", c, None, None))
        for k in numfeats(src):
            vals = [r["f"].get(k) for r in gr if r["f"].get(k) is not None]
            qq = quint(vals) if vals else None
            if qq:
                specs.append((src, g, "top", k, qq[1], None))
                specs.append((src, g, "bot", k, qq[0], None))
        for k in catfeats(src):
            for v in sorted({r["f"].get(k) for r in gr if r["f"].get(k) is not None}):
                specs.append((src, g, "val", k, v, None))
    return specs


def member(spec, r):
    src, g, kind, key, a, _ = spec
    if r["source"] != src or r["g"] != g:
        return False
    if kind == "all":
        return True
    if kind == "band":
        return band(r["q"]) == key
    if kind == "cat":
        return r["category"] == key
    v = r["f"].get(key)
    if v is None:
        return False
    if kind == "top":
        return v >= a
    if kind == "bot":
        return v <= a
    return v == a


def stats(rows, rng, b=B, target="ret"):
    """Mean of target per row, resampling whole settle-days. (est, lo, hi, p_one_sided)."""
    per = {}
    for r in rows:
        a = per.setdefault(r["day"], [0.0, 0])
        a[0] += r[target]; a[1] += 1
    keys = list(per)
    if not keys:
        return None
    n = sum(per[d][1] for d in keys)
    est = sum(per[d][0] for d in keys) / n
    draws = []
    for _ in range(b):
        s = c = 0.0
        for _ in keys:
            a = per[keys[rng.randrange(len(keys))]]
            s += a[0]; c += a[1]
        draws.append(s / c)
    draws.sort()
    p = (sum(1 for x in draws if x <= 0) + 1) / (b + 1)
    return est, draws[int(0.025 * b)], draws[int(0.975 * b) - 1], p


def describe(cell, st, sa=None):
    n = len(cell); w = sum(r["won"] for r in cell); imp = sum(r["q"] for r in cell)
    mult = sum(1 / r["q"] for r in cell) / n
    s = (f"n {n:6d} days {len({r['day'] for r in cell}):3d} won {w:5d} vs price {imp:7.1f} "
         f"(x{w / imp if imp else float('nan'):.2f}) hit {100 * w / n:5.2f}% pays x{mult:5.1f}")
    if st:
        s += f"  per $1 {st[0] * 100:+6.1f}% [{st[1] * 100:+6.1f}, {st[2] * 100:+6.1f}]"
    if sa:
        s += f"  vs pool {sa[0] * 100:+6.1f}% [{sa[1] * 100:+6.1f}, {sa[2] * 100:+6.1f}] p {sa[3]:.4f}"
    return s


def name(spec):
    src, g, kind, key, a, _ = spec
    if kind in ("top", "bot"):
        return f"{src}/{g} {key} {'>=' if kind == 'top' else '<='} {a:.4g}"
    if kind == "val":
        return f"{src}/{g} {key} = {a}"
    return f"{src}/{g} {kind} {key}".rstrip()


def run_tests(rows, specs, rng, min_rows=MIN_ROWS, min_wins=MIN_WINS):
    out, skipped = [], 0
    for sp in specs:
        cell = [r for r in rows if member(sp, r)]
        if len(cell) < min_rows or sum(r["won"] for r in cell) < min_wins:
            skipped += 1
            continue
        st = stats(cell, rng)
        sa = stats(cell, rng, target="adj")
        if st and sa:
            out.append((sp, st, cell, sa))
    return out, skipped


def bh(pvals, q=Q):
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    k = 0
    for rank, i in enumerate(order, 1):
        if pvals[i] <= q * rank / m:
            k = rank
    return set(order[:k])


def baseline(rows, rng, w):
    w("== Baseline: how often cheap contracts hit against their price ==")
    for src in sorted({r["source"] for r in rows}):
        for _, _, bn in BANDS:
            cell = [r for r in rows if r["source"] == src and band(r["q"]) == bn]
            if cell:
                w(f"  {src:4s} {bn}  {describe(cell, stats(cell, rng, b=400))}")
        cats = {}
        for r in rows:
            if r["source"] == src:
                cats.setdefault(r["category"], []).append(r)
        for c, cell in sorted(cats.items(), key=lambda x: -len(x[1]))[:12]:
            w(f"  {src:4s} {c[:22]:22s} {describe(cell, stats(cell, rng, b=400))}")
    w()


def phase_train(paths, tag, outdir, log=print):
    lines = []
    w = lambda s="": (lines.append(s), log(s))
    train, hold = split_days(paths)
    rows = load(paths, train)
    rng = random.Random(11)
    w(f"cheap-contract screen {tag}: {len(rows)} cheap rows on train; holdout sealed")
    for src in sorted(train):
        w(f"  {src}: train {len(train[src])} days {train[src][0]}..{train[src][-1]}, "
          f"holdout {len(hold[src])} days")
    baseline(rows, rng, w)
    cal = fit_cal(rows)
    rows = apply_cal(rows, cal)
    specs = [sp for sp in make_specs(rows) if sp[2] not in ("all", "band")]
    res, skipped = run_tests(rows, specs, rng)
    real = [x for x in res if x[0][3] not in CONTROLS]
    ctl = [x for x in res if x[0][3] in CONTROLS]
    keep = bh([x[3][3] for x in real])
    leak_ok = any(x[0][3] == "ctl_leak" and x[0][2] == "top" and x[3][3] < 0.01 and x[3][0] > 0
                  for x in ctl)
    noise_bad = [name(x[0]) for x in ctl
                 if x[0][3] == "ctl_noise" and x[3][3] < Q / max(1, len(real))]
    # null: every row wins with exactly its break-even probability
    nrng = random.Random(23)
    null = []
    for r in rows:
        x = dict(r); x["won"] = 1 if nrng.random() < breakeven(r["q"]) else 0
        x["ret"] = ret_per_dollar(r["q"], x["won"]); null.append(x)
    null = apply_cal(null, fit_cal(null))
    nres, _ = run_tests(null, [sp for sp in specs if sp[3] not in CONTROLS], random.Random(29))
    nd = len(bh([x[3][3] for x in nres]))
    trusted = leak_ok and not noise_bad and nd <= max(1, int(0.02 * len(nres)))
    w(f"== Controls: leak found {leak_ok}; noise passing {noise_bad or 'none'}; null run "
      f"{nd} discoveries of {len(nres)} cells -> {'TRUSTED' if trusted else 'NOT TRUSTED'} ==")
    w(f"{len(real)} cells tested, {skipped} too thin to test; {len(keep)} beat the pool at their "
      f"price after BH at q={Q}")
    w()
    w("== Train: cells that beat the pool at their price (BH) AND whose own return clears zero ==")
    sel = []
    for i in sorted(keep, key=lambda i: real[i][3][3]):
        sp, st, cell, sa = real[i]
        if sa[0] <= 0:
            continue
        tradeable = st[1] > 0
        w(f"  {name(sp):42s} {describe(cell, st, sa)}{'' if tradeable else '  (signal, not tradeable)'}")
        if tradeable:
            sel.append(list(sp))
    if not sel:
        w("  none tradeable")
    w()
    w("== Ten cells with the highest return on train, BH or not (a record, not a selection) ==")
    for sp, st, cell, sa in sorted(real, key=lambda x: -x[1][0])[:10]:
        w(f"  {name(sp):42s} {describe(cell, st, sa)}")
    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, f"{tag}-selected.json"), "w") as f:
        json.dump({"train_days": train, "holdout_days": hold, "trusted": trusted, "cal": cal,
                   "selected": sel if trusted else []}, f, indent=1)
    with open(os.path.join(outdir, f"{tag}-train.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return trusted, sel


def phase_holdout(paths, tag, outdir, log=print):
    sp = os.path.join(outdir, f"{tag}-selected.json")
    if not os.path.exists(sp):
        raise SystemExit("no train selection committed; run phase train first")
    meta = json.load(open(sp))
    lines = []
    w = lambda s="": (lines.append(s), log(s))
    rows = apply_cal(load(paths, meta["holdout_days"]), meta["cal"])
    rng = random.Random(31)
    w(f"cheap-contract holdout {tag}: {sum(len(v) for v in meta['holdout_days'].values())} source-days, "
      f"{len(rows)} cheap rows; "
      f"train run {'TRUSTED' if meta['trusted'] else 'NOT TRUSTED'}")
    baseline(rows, rng, w)
    w("== Selected cells on the holdout ==")
    found = []
    for s in meta["selected"]:
        spec = tuple(s)
        cell = [r for r in rows if member(spec, r)]
        if not cell:
            w(f"  {name(spec):42s} no rows"); continue
        st = stats(cell, rng); sa = stats(cell, rng, target="adj")
        wins = sum(r["won"] for r in cell)
        ask = [r for r in cell if r["f"].get("last_cheap") == 1]
        ast = stats(ask, rng) if ask else None
        ok = st[1] > 0 and sa[1] > 0 and wins >= HOLD_MIN_WINS and (not ask or (ast and ast[1] > 0))
        w(f"  {name(spec):42s} {describe(cell, st, sa)}{'  SURVIVES' if ok else ''}")
        if ask:
            w(f"  {'  ask-side rows only':42s} {describe(ask, ast)}")
        if ok:
            found.append(name(spec))
    w()
    w("survivors: " + (", ".join(found) if found else "none"))
    with open(os.path.join(outdir, f"{tag}-holdout.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
    return found


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("train", "holdout"), required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("inputs", nargs="+")
    a = ap.parse_args()
    out = os.path.join(HERE, "out")
    (phase_train if a.phase == "train" else phase_holdout)(a.inputs, a.tag, out,
                                                           log=lambda s: print(s, flush=True))
