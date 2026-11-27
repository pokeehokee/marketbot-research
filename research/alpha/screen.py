"""Screen precursor features against what happened next, with a locked holdout.

Question: does anything the tape showed before time t predict, BEYOND THE PRICE,
(a) how the market settled, or (b) where the price went next?

  adj_resid = y - cal(p)      cal = train-slice settle rate in the price's 5c bin
  adj_move  = move - mean move in that price bin and time-to-close group

The adjustment matters. This repo's precursor record is 0 for 9 (hour of day,
volatility, a loss classifier, a post-fill path model, Kronos, ...) and every one
died the same way: the price already contained the state. A feature that only
re-states the price, or the favourite-longshot bias every Kalshi series carries,
is removed by cal(p) and cannot pass here.

Method, fixed before any data was read (2026-11-27):
  * Rows with 0.03 <= p <= 0.97 only (contested). Settle-days sorted; the first
    60% are TRAIN, the rest HOLDOUT. `--phase train` never loads a holdout row:
    the filter is applied as each line is read.
  * One test per (feature, target, time-to-close group): mean target in the
    feature's top train quintile minus its bottom quintile, in cents. Interval
    and p-value from a bootstrap that resamples whole settle-days, because one
    price move settles a day's markets together.
  * Benjamini-Hochberg at q = 0.10 across every real test. Survivors are written
    to out/<tag>-selected.json with their train cut-points and calibration, and
    that file is committed BEFORE `--phase holdout` is run.
  * Holdout: the same cut-points, the same sign required, interval clear of zero,
    and a tradeable figure: buying the side the feature points to at p, after the
    taker fee, must still be positive. Marked as an upper bound: p is a recent
    trade price, not the ask.

Controls that can fail (CLAUDE.md: a control that cannot fail is not a control):
  * ctl_leak = y + noise. It is the answer with fog on it; if the screen does not
    find it in every group, the screen is blind and nothing it says is a kill.
  * ctl_noise = noise. If it passes, the screen is lying.
  * A null run: targets shuffled within each day, every test repeated. BH
    discoveries on the null should be about zero; more means the p-values are
    too small and survivors are not trusted.

Known false positive, kept visible: `last_yes` (side of the last taker) predicts
the next move through bid-ask bounce, which no one can trade. It is reported,
and any survivor that is mostly last_yes in disguise should be read that way.
"""
import argparse, csv, gzip, hashlib, json, math, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import features as F    # noqa: E402

TRAIN_SHARE = 0.60
Q = 0.10
B = 2000
P_LO, P_HI = 0.03, 0.97
MIN_ROWS = 400
GROUPS = ("long", "mid", "short")
TARGETS = ("adj_resid", "adj_move")
CONTROLS = ("ctl_leak", "ctl_noise")


def taker_fee(p):
    """Kalshi's quadratic taker fee per contract, rounded up to a cent per 1-lot order."""
    return math.ceil(0.07 * p * (1 - p) * 100 - 1e-9) / 100.0


def noise(ticker, off, salt):
    h = hashlib.sha256(f"{salt}|{ticker}|{off}".encode()).digest()
    u1 = (int.from_bytes(h[:8], "big") + 1) / 2**64
    u2 = int.from_bytes(h[8:16], "big") / 2**64
    return math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)


def num(s):
    return None if s in ("", None) else float(s)


def split_days(paths):
    days = set()
    for p in paths:
        with gzip.open(p, "rt") as f:
            for r in csv.DictReader(f):
                days.add(r["day"])
    days = sorted(days)
    k = max(1, int(round(len(days) * TRAIN_SHARE)))
    return days[:k], days[k:]


def load(paths, keep_days):
    keep_days = set(keep_days)
    rows = []
    for p in paths:
        with gzip.open(p, "rt") as f:
            for r in csv.DictReader(f):
                if r["day"] not in keep_days:     # holdout rows never enter train memory
                    continue
                pr = float(r["p"])
                if not (P_LO <= pr <= P_HI):
                    continue
                off = int(r["offset_min"])
                d = {"series": r["series"], "ticker": r["ticker"], "day": r["day"],
                     "off": off, "g": F.group_of(off), "p": pr, "y": float(r["y"]),
                     "move": num(r["move"])}
                for k in F.FEATURES:
                    d[k] = num(r[k])
                d["ctl_leak"] = d["y"] + noise(r["ticker"], off, "leak")
                d["ctl_noise"] = noise(r["ticker"], off, "noise")
                rows.append(d)
    return rows


def pbin(p):
    return min(19, int(p * 20))


def fit_calibration(rows):
    cal, mv = {}, {}
    for r in rows:
        a = cal.setdefault(pbin(r["p"]), [0.0, 0])
        a[0] += r["y"]; a[1] += 1
        if r["move"] is not None:
            m = mv.setdefault(f"{r['g']}|{pbin(r['p'])}", [0.0, 0])
            m[0] += r["move"]; m[1] += 1
    return ({str(k): v[0] / v[1] for k, v in cal.items()},
            {k: v[0] / v[1] for k, v in mv.items()})


def adjust(rows, cal, mv):
    for r in rows:
        c = cal.get(str(pbin(r["p"])))
        r["adj_resid"] = None if c is None else r["y"] - c
        m = mv.get(f"{r['g']}|{pbin(r['p'])}")
        r["adj_move"] = None if (m is None or r["move"] is None) else r["move"] - m


def cuts(vals):
    """(lo, hi): the bottom and top quintile edges, or the extreme values of a
    feature with few distinct values (a binary splits 0 against 1)."""
    s = sorted(vals)
    distinct = sorted(set(s))
    if len(distinct) < 2:
        return None
    if len(distinct) <= 5:
        return distinct[0], distinct[-1]
    lo, hi = s[len(s) // 5], s[(4 * len(s)) // 5]
    if lo >= hi:
        return None
    return lo, hi


def by_day(rows):
    d = {}
    for r in rows:
        d.setdefault(r["day"], []).append(r)
    return d


def boot(rows, feat, tgt, lo, hi, rng, b=B):
    """Effect, 95% interval and two-sided p, resampling whole settle-days. Per-day
    sums are taken once, so a draw costs one pass over the days, not the rows."""
    per = {}
    for r in rows:
        a = per.setdefault(r["day"], [0.0, 0, 0.0, 0])
        if r[feat] >= hi:
            a[0] += r[tgt]; a[1] += 1
        if r[feat] <= lo:
            a[2] += r[tgt]; a[3] += 1
    keys = sorted(per)

    def eff(ds):
        st = sum(per[d][0] for d in ds); nt = sum(per[d][1] for d in ds)
        sb = sum(per[d][2] for d in ds); nb = sum(per[d][3] for d in ds)
        if nt < 20 or nb < 20:
            return None
        return (st / nt - sb / nb) * 100.0
    est = eff(keys)
    if est is None:
        return None
    draws = []
    for _ in range(b):
        e = eff([keys[rng.randrange(len(keys))] for _ in keys])
        if e is not None:
            draws.append(e)
    if len(draws) < b // 2:
        return None
    draws.sort()
    pos = sum(1 for x in draws if x <= 0) / len(draws)
    neg = sum(1 for x in draws if x >= 0) / len(draws)
    pval = min(1.0, 2 * min(pos, neg) + 1.0 / len(draws))
    return est, draws[int(0.025 * len(draws))], draws[int(0.975 * len(draws)) - 1], pval


def bh(pvals, q=Q):
    """Indices that survive Benjamini-Hochberg at level q."""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    k = 0
    for rank, i in enumerate(order, 1):
        if pvals[i] <= q * rank / m:
            k = rank
    return set(order[:k])


def tests(rows, rng, feats):
    out = []
    for g in GROUPS:
        gr = [r for r in rows if r["g"] == g]
        for tgt in TARGETS:
            for feat in feats:
                sub = [r for r in gr if r[feat] is not None and r[tgt] is not None]
                if len(sub) < MIN_ROWS:
                    continue
                c = cuts([r[feat] for r in sub])
                if not c:
                    continue
                res = boot(sub, feat, tgt, c[0], c[1], rng)
                if res is None:
                    continue
                out.append({"group": g, "target": tgt, "feature": feat, "lo": c[0], "hi": c[1],
                            "n": len(sub), "days": len(by_day(sub)), "effect_c": res[0],
                            "ci": [res[1], res[2]], "p": res[3]})
    return out


def shuffled(rows, rng):
    """Targets permuted within each settle-day: every real relationship destroyed."""
    out = []
    for day, rs in sorted(by_day(rows).items()):
        for tgt in TARGETS:
            vals = [r[tgt] for r in rs]
            rng.shuffle(vals)
            for r, v in zip(rs, vals):
                r[f"_{tgt}"] = v
        for r in rs:
            c = dict(r)
            for tgt in TARGETS:
                c[tgt] = r[f"_{tgt}"]
            out.append(c)
    return out


def fmt(t):
    return (f"  {t['group']:5s} {t['target']:9s} {t['feature']:9s} n {t['n']:6d} d {t['days']:2d}  "
            f"Q5-Q1 {t['effect_c']:+6.2f}c [{t['ci'][0]:+6.2f}, {t['ci'][1]:+6.2f}]  p {t['p']:.4f}")


def phase_train(paths, tag, outdir, log=print):
    train_days, hold_days = split_days(paths)
    log(f"settle-days: {len(train_days)} train ({train_days[0]}..{train_days[-1]}), "
        f"{len(hold_days)} holdout ({hold_days[0] if hold_days else '-'}..), holdout NOT loaded")
    rows = load(paths, train_days)
    log(f"train rows (0.03 <= p <= 0.97): {len(rows)}")
    cal, mv = fit_calibration(rows)
    adjust(rows, cal, mv)
    rng = random.Random(20261127)
    real = tests(rows, rng, F.FEATURES)
    ctl = tests(rows, rng, CONTROLS)
    keep = bh([t["p"] for t in real])
    null = tests(shuffled(rows, random.Random(7)), random.Random(8), F.FEATURES)
    null_disc = len(bh([t["p"] for t in null])) if null else 0
    leak_ok = all(t["p"] < 0.01 and t["effect_c"] > 0 for t in ctl if t["feature"] == "ctl_leak"
                  and t["target"] == "adj_resid") and any(t["feature"] == "ctl_leak" for t in ctl)
    noise_bad = [t for t in ctl if t["feature"] == "ctl_noise" and t["p"] < 0.01]
    log("\n=== CONTROLS ===")
    for t in ctl:
        log(fmt(t))
    log(f"  null run: {null_disc} BH discoveries of {len(null)} shuffled tests (should be ~0)")
    trusted = leak_ok and not noise_bad and null_disc <= max(1, int(0.02 * len(null)))
    log(f"  screen {'TRUSTED' if trusted else 'NOT TRUSTED: read nothing below as a finding or a kill'}")
    log(f"\n=== {len(real)} REAL TESTS, BH q={Q}: {len(keep)} survive ===")
    for i, t in sorted(enumerate(real), key=lambda x: x[1]["p"]):
        log(("* " if i in keep else "  ") + fmt(t)[2:])
    sel = {"tag": tag, "train_days": train_days, "holdout_days": hold_days, "q": Q,
           "trusted": trusted, "calibration": cal, "move_means": mv,
           "selected": [real[i] for i in sorted(keep)]}
    path = os.path.join(outdir, f"{tag}-selected.json")
    with open(path, "w") as f:
        json.dump(sel, f, indent=1, sort_keys=True)
    log(f"\nwrote {path}: commit it before running --phase holdout")
    return sel


def phase_holdout(paths, tag, outdir, log=print):
    path = os.path.join(outdir, f"{tag}-selected.json")
    if not os.path.exists(path):
        raise SystemExit(f"{path} missing: run --phase train and commit its selection first")
    sel = json.load(open(path))
    if not sel["selected"]:
        log("train phase selected nothing; the holdout stays sealed and unused")
        return []
    rows = load(paths, sel["holdout_days"])
    adjust(rows, sel["calibration"], sel["move_means"])
    log(f"holdout rows: {len(rows)} over {len(sel['holdout_days'])} settle-days")
    rng = random.Random(1127)
    out = []
    for s in sel["selected"]:
        sub = [r for r in rows if r["g"] == s["group"] and r[s["feature"]] is not None
               and r[s["target"]] is not None]
        res = boot(sub, s["feature"], s["target"], s["lo"], s["hi"], rng)
        if res is None:
            log(f"  {s['group']} {s['target']} {s['feature']}: too few holdout rows")
            continue
        sign = 1 if s["effect_c"] > 0 else -1
        held = sign * res[0] > 0 and (res[1] > 0 if sign > 0 else res[2] < 0)
        # Tradeable figure, per tail: where the feature says YES is relatively cheap, buy
        # YES at p; where it says YES is dear, buy NO at 1-p. Taker fee paid, held to
        # settlement. Both tails reported; picking the better one would be holdout snooping.
        tails = []
        if s["target"] == "adj_resid":
            for name, pick, buy_yes in (("top", lambda v: v >= s["hi"], sign > 0),
                                        ("bottom", lambda v: v <= s["lo"], sign < 0)):
                tr = [r for r in sub if pick(r[s["feature"]])]
                net = losses = 0
                for r in tr:
                    pay = r["p"] if buy_yes else 1 - r["p"]
                    win = r["y"] if buy_yes else 1 - r["y"]
                    net += win - pay - taker_fee(pay)
                    losses += 1 if win == 0 else 0
                if tr:
                    tails.append((name, "yes" if buy_yes else "no", 100 * net / len(tr),
                                  len(tr), losses))
        log(f"  {s['group']} {s['target']} {s['feature']}: train {s['effect_c']:+.2f}c, "
            f"holdout {res[0]:+.2f}c [{res[1]:+.2f}, {res[2]:+.2f}] "
            f"{'HOLDS' if held else 'fails'}")
        for name, side, per, n, losses in tails:
            log(f"      {name} tail, buy {side}: {per:+.2f}c/ct after taker fee on {n} rows, "
                f"{losses} losses (upper bound: p is a trade price, not the ask)")
        out.append({**s, "holdout": res, "holds": held, "tails": tails})
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", choices=("train", "holdout"), required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("inputs", nargs="+")
    a = ap.parse_args()
    outdir = os.path.join(HERE, "out")
    lines = []

    def log(s):
        print(s, flush=True); lines.append(s)
    (phase_train if a.phase == "train" else phase_holdout)(a.inputs, a.tag, outdir, log)
    with open(os.path.join(outdir, f"{a.tag}-{a.phase}.txt"), "w") as f:
        f.write("\n".join(lines) + "\n")
