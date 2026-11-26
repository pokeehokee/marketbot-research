"""Score combo_tape.py's output (round-2 idea #26). Protocol fixed 2026-11-24,
before any of the 28-day sample was read; the only combo data seen beforehand
was round 1's single Sunday-night slate (5 hours), which is why this exists.

Two trades, each scored per price band and pooled over bands 03-80:

  SELL THE PARLAY: rows where the requester bought YES; the quoter holds NO.
     EV/contract to the quoter = (paid - won - maker fee) / contracts.
  BUY NO BY RFQ:   rows where the requester bought NO; that requester is us.
     EV/contract to us = (won - paid - taker fee) / contracts.

Fees are the per-trade rounded-up figures combo_tape recorded (maker 0.035,
taker 0.07, maker 0 on `quadratic` series). Gross is printed beside them.

A cell PASSES only if, on the whole window and on each half (settle-days split
at the median day): EV > 0, the settle-day-clustered 95% interval excludes zero,
and the loss ratio < 0.90 (losses / implied: for the quoter won/paid, for the NO
buyer (C - won)/(C - paid), i.e. how often the side we hold loses against what
its price implied); and the whole window has >= 30 markets on which we lost.

Settle-day clustering matters more here than anywhere in the repo: one game can
settle thousands of combos together. A pass on a quoter's trade is an UPPER
BOUND for a new quoter (a quote fills only when it beats the others).
No control is possible: no live book trades combos. Read-only, no network.
"""
import collections, csv, gzip, os, random, sys

BOOT = 2000
RNG = random.Random(20261124)
BANDS = ["03-05", "06-10", "11-20", "21-35", "36-50", "51-65", "66-80"]
MIN_LOSSES, MAX_RATIO = 30, 0.90


def load(paths):
    rows = []
    for p in paths:
        with gzip.open(p, "rt") as f:
            for r in csv.DictReader(f):
                for k in ("contracts", "paid", "won", "mfee", "tfee"):
                    r[k] = float(r[k])
                for k in ("trades", "markets", "loss_markets"):
                    r[k] = int(r[k])
                rows.append(r)
    return rows


def day_cells(rows, keyf, trade):
    """{key: {day: [ct, our_pnl_gross, our_pnl_net, our_losses_ct, implied_ct, lost_markets, markets]}}"""
    out = collections.defaultdict(dict)
    for r in rows:
        k = keyf(r)
        if k is None:
            continue
        ct, paid, won = r["contracts"], r["paid"], r["won"]
        if trade == "sell":           # we are the quoter, holding NO against a YES buyer
            gross, fee = paid - won, r["mfee"]
            lost_ct, implied = won, paid
            lost_m = r["loss_markets"]
        else:                         # we bought NO as requester
            gross, fee = won - paid, r["tfee"]
            lost_ct, implied = ct - won, ct - paid
            lost_m = r["markets"] - r["loss_markets"]
        d = out[k].setdefault(r["day"], [0.0] * 5 + [0, 0])
        d[0] += ct; d[1] += gross; d[2] += gross - fee; d[3] += lost_ct; d[4] += implied
        d[5] += lost_m; d[6] += r["markets"]
    return out


def stats(dr):
    days = sorted(dr)
    tot = [sum(dr[d][i] for d in days) for i in range(7)]
    if tot[0] <= 0:
        return None
    boots = []
    for _ in range(BOOT):
        s = [dr[RNG.choice(days)] for _ in days]
        c = sum(x[0] for x in s)
        if c > 0:
            boots.append(sum(x[2] for x in s) / c)
    boots.sort()
    return {"ct": tot[0], "gross": tot[1] / tot[0], "ev": tot[2] / tot[0],
            "lo": boots[int(.025 * len(boots))], "hi": boots[int(.975 * len(boots)) - 1],
            "ratio": tot[3] / tot[4] if tot[4] else float("nan"),
            "lost": tot[5], "markets": tot[6], "days": len(days)}


def fmt(s):
    return (f"{s['ct']:>12.0f} ct  gross {100*s['gross']:+6.2f}c  net {100*s['ev']:+6.2f}c "
            f"[{100*s['lo']:+6.2f},{100*s['hi']:+6.2f}]  ratio {s['ratio']:.2f}  "
            f"lost {s['lost']}/{s['markets']} mkts  {s['days']}d")


def verdict(dr, half):
    parts = {"all": dr, "H1": {d: v for d, v in dr.items() if d < half},
             "H2": {d: v for d, v in dr.items() if d >= half}}
    st = {k: (stats(v) if v else None) for k, v in parts.items()}
    ok = all(s and s["ev"] > 0 and s["lo"] > 0 and s["ratio"] < MAX_RATIO for s in st.values())
    return ok and st["all"]["lost"] >= MIN_LOSSES, st


def main(paths, out=print):
    rows = load(paths)
    for p in paths:
        cp = p.replace(".csv.gz", "-coverage.txt")
        if os.path.exists(cp):
            out(f"coverage {os.path.basename(cp)}:\n  " + open(cp).read().strip().replace("\n", "\n  "))
    days = sorted({r["day"] for r in rows})
    if not days:
        out("no data"); return
    half = days[len(days) // 2]
    out(f"\nSettle-days {days[0]}..{days[-1]} ({len(days)}); H1 < {half} <= H2. "
        "Interval = 95% bootstrap clustered by settle-day.")
    passes = []
    for trade, side, label in (("sell", "yes", "SELL THE PARLAY (quoter vs YES buyers)"),
                               ("buy", "no", "BUY NO BY RFQ (us as requester)")):
        for series in sorted({r["series"] for r in rows}) + ["ALL"]:
            sel = (lambda r, s=series: r["taker_side"] == side and (s == "ALL" or r["series"] == s))
            cells = day_cells(rows, lambda r: (r["band"] if sel(r) and r["band"] in BANDS else None), trade)
            pooled = day_cells(rows, lambda r: ("03-80" if sel(r) and r["band"] in BANDS else None), trade)
            out(f"\n=== {label} — {series} ===")
            for b in BANDS + ["03-80"]:
                dr = (pooled if b == "03-80" else cells).get(b)
                if not dr:
                    continue
                ok, st = verdict(dr, half)
                out(f"  {b}  {'PASS' if ok else 'fail'}")
                for k in ("all", "H1", "H2"):
                    out(f"      {k:<3} " + (fmt(st[k]) if st[k] else "none"))
                if ok:
                    passes.append((label, series, b))
    out(f"\nPASSING cells: {len(passes)}")
    for k in passes:
        out(f"  {k}")


if __name__ == "__main__":
    main(sys.argv[1:])
