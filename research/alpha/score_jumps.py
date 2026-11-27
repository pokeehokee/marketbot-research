"""Score the jump list from jumps.py: after a 15c+ move, follow it or fade it?

For a jump in direction d (+1 up, -1 down) at price p0, entering at price e
(p0, or the price 15 or 60 minutes later):

  follow  = buy the side the price moved TOWARD:   d * (y - e)   per contract
  fade    = buy the side the price moved AWAY from: -d * (y - e)

minus the taker fee ceil(0.07 x 10 x q x (1-q)) / 10 on the side bought at q,
a 10-lot. e is a trade VWAP, not the ask, so every taker figure is an upper
bound by about half a spread; the maker line (no fee) is what a resting order
earns IF it fills at e, which is the other bound.

CIs are bootstrapped clustered by EVENT (all strikes of one event resolve off
one outcome, so they are one observation), B=2000. A cell passes only if the
whole window and each half (split at the median jump day) are positive with
intervals clear of zero, it carries >= 30 losing bets, and losses run below 0.9
of what the prices implied.

Run: python3 -I score_jumps.py out/jumps-x.csv.gz [more.csv.gz] > out/jumps-x-score.txt
"""
import csv, gzip, math, random, sys

B = 2000
MIN_LOSSES = 30
MAX_RATIO = 0.90
BANDS = [(0.01, 0.05), (0.05, 0.10), (0.10, 0.20), (0.20, 0.35), (0.35, 0.65),
         (0.65, 0.80), (0.80, 0.90), (0.90, 0.97)]


def fee10(q):
    return math.ceil(round(0.07 * 10 * q * (1 - q) * 100, 6)) / 100 / 10


def fnum(s):
    return None if s in ("", None) else float(s)


def load(paths):
    rows = []
    for p in paths:
        with gzip.open(p, "rt") as f:
            for r in csv.DictReader(f):
                r["dir"] = int(r["dir"]); r["y"] = int(r["y"])
                for k in ("p_ref", "p0", "p_1h", "p_6h", "p_24h", "entry_15m", "entry_60m", "ttc_h",
                          "contracts_jump_bar"):
                    r[k] = fnum(r.get(k))
                rows.append(r)
    return rows


def bet(r, entry_key, play):
    """(price paid q, won 0/1, pnl_taker, pnl_maker) or None when no entry price exists."""
    e = r["p0"] if entry_key == "p0" else r[entry_key]
    if e is None:
        return None
    s = r["dir"] if play == "follow" else -r["dir"]
    q = e if s > 0 else 1.0 - e            # buying YES at e, or NO at 1-e
    if not (0.0 < q < 1.0):
        return None
    won = (r["y"] == 1) if s > 0 else (r["y"] == 0)
    gross = (1.0 if won else 0.0) - q
    return q, int(won), gross - fee10(q), gross


def boot(units, B=B, seed=7):
    """units: list of (sum_pnl, n). Ratio-of-sums mean per contract, resampled by unit."""
    if not units:
        return None
    n = sum(u[1] for u in units)
    est = sum(u[0] for u in units) / n
    rng = random.Random(seed)
    k, out = len(units), []
    for _ in range(B):
        s = c = 0.0
        for _ in range(k):
            u = units[rng.randrange(k)]
            s += u[0]; c += u[1]
        out.append(s / c if c else 0.0)
    out.sort()
    return est, out[int(0.025 * B)], out[int(0.975 * B) - 1]


def cell(bets, which):
    """bets: (event, day, q, won, taker, maker). which: 0 taker, 1 maker -> stats dict."""
    by = {}
    for b in bets:
        a = by.setdefault(b[0], [0.0, 0])
        a[0] += b[4 + which]; a[1] += 1
    r = boot(list(by.values()))
    losses = sum(1 for b in bets if not b[3])
    implied = sum(1 - b[2] for b in bets)
    return {"n": len(bets), "events": len(by), "losses": losses, "implied": implied,
            "ratio": losses / implied if implied else float("nan"),
            "win": sum(b[3] for b in bets) / len(bets) if bets else float("nan"),
            "mult": (sum(1 / b[2] for b in bets) / len(bets)) if bets else float("nan"),
            "est": r[0] if r else None, "lo": r[1] if r else None, "hi": r[2] if r else None,
            "per_dollar": (sum(b[4 + which] for b in bets) / sum(b[2] for b in bets)) if bets else None}


def passes(full, h1, h2):
    return all(x["n"] and x["lo"] is not None and x["lo"] > 0 for x in (full, h1, h2)) \
        and full["losses"] >= MIN_LOSSES and full["ratio"] < MAX_RATIO


def fmt(s):
    if not s["n"]:
        return "n 0"
    return (f"n {s['n']:5d} ev {s['events']:4d} win {s['win']*100:5.1f}% x{s['mult']:5.1f} "
            f"lost {s['losses']:4d}/{s['implied']:6.1f} r {s['ratio']:.2f} "
            f"{s['est']*100:+6.2f}c [{s['lo']*100:+6.2f}, {s['hi']*100:+6.2f}] "
            f"per$ {s['per_dollar']*100:+6.1f}%")


def band_of(q):
    for lo, hi in BANDS:
        if lo <= q < hi:
            return f"{lo:.2f}-{hi:.2f}"
    return None


def main(paths, out=sys.stdout):
    rows = load(paths)
    w = lambda s="": print(s, file=out)
    if not rows:
        w("no jumps"); return
    days = sorted({r["day"] for r in rows})
    mid = days[len(days) // 2]
    w(f"{len(rows)} jumps, {len({r['event'] for r in rows})} events, {len(days)} days "
      f"({days[0]} to {days[-1]}), halves split at {mid}")
    cats = {}
    for r in rows:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    w("by category: " + ", ".join(f"{k} {v}" for k, v in sorted(cats.items(), key=lambda x: -x[1])))
    up = sum(1 for r in rows if r["dir"] > 0)
    w(f"up {up}, down {len(rows) - up}")
    w()
    w("== Where did the price go next? continuation in the jump's direction, cents ==")
    for h in ("p_1h", "p_6h", "p_24h"):
        vals = [(r["event"], r["dir"] * (r[h] - r["p0"])) for r in rows if r[h] is not None]
        by = {}
        for ev, v in vals:
            a = by.setdefault(ev, [0.0, 0]); a[0] += v; a[1] += 1
        b = boot(list(by.values()))
        w(f"{h:6s} n {len(vals):5d} mean {b[0]*100:+6.2f}c [{b[1]*100:+6.2f}, {b[2]*100:+6.2f}]"
          "  (+ keeps going, - snaps back)" if b else f"{h} n 0")
    w()
    found = []
    for entry in ("p0", "entry_15m", "entry_60m"):
        for play in ("follow", "fade"):
            for fee_name, which in (("taker", 0), ("maker", 1)):
                w(f"== {play} at {entry}, {fee_name} ==")
                allb = []
                for r in rows:
                    b = bet(r, entry, play)
                    if b:
                        allb.append((r["event"], r["day"], b[0], b[1], b[2], b[3], r["category"]))
                groups = [("all", allb)]
                for name in sorted({band_of(b[2]) for b in allb if band_of(b[2])}):
                    groups.append((f"bought {name}", [b for b in allb if band_of(b[2]) == name]))
                if entry == "p0" and fee_name == "taker":
                    for c in sorted({b[6] for b in allb}):
                        groups.append((f"cat {c}", [b for b in allb if b[6] == c]))
                for name, bs in groups:
                    bs4 = [b[:6] for b in bs]
                    full = cell(bs4, which)
                    h1 = cell([b for b in bs4 if b[1] < mid], which)
                    h2 = cell([b for b in bs4 if b[1] >= mid], which)
                    ok = passes(full, h1, h2)
                    w(f"  {name:18s} {fmt(full)}{'  PASS' if ok else ''}")
                    if full["n"] and (h1["n"] or h2["n"]):
                        w(f"  {'':18s}   halves: {fmt(h1)}")
                        w(f"  {'':18s}           {fmt(h2)}")
                    if ok:
                        found.append(f"{play} {entry} {fee_name} {name}")
                w()
    w("cells passing every test: " + (", ".join(found) if found else "none"))


if __name__ == "__main__":
    main(sys.argv[1:])
