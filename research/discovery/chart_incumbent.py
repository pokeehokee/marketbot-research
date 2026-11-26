"""Idea #8: do Spotify daily-chart markets misprice the incumbent?

Counterparty: takers who price "will this song be #1 today" on buzz rather than
on how often the reigning #1 simply holds. Kalshi's own results are the chart
history, so no outside data is needed.

Zero lookahead: the market for chart date D opens the evening of D-1, before
chart D-1 is published, so the incumbent used is the #1 of chart **D-2** (from
Kalshi's own settled result). Every trade on D's markets happens after D-2's
chart is public.

For each real trade on the incumbent's ticker, at the price the taker paid:
  * the incumbent's realised repeat rate against the average YES price paid,
    by price band, first half of days vs second;
  * TAKE: buy the side the taker bought, paying the taker fee;
  * MAKE: be the resting side (fee-free on `quadratic`), queue not modelled.
Intervals bootstrapped by chart date.

Run: python3 -I chart_incumbent.py --markets out/probe-...-markets.csv.gz --trades ... --series KXSPOTIFYD
"""
import argparse, collections, csv, gzip, math, os, random, re
from datetime import date, timedelta

BOOT = 2000
RNG = random.Random(20261124)
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def ev_date(ev):
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", ev)
    return date(2000 + int(m.group(1)), MON.index(m.group(2)) + 1, int(m.group(3))) if m else None


def norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def taker_fee(p):
    return math.ceil(0.07 * 10 * p * (1 - p) * 100 - 1e-9) / 1000.0


def boot(rows, key):
    """(EV per contract, lo, hi) bootstrapped by day; rows are (day, pnl_per_ct, n)."""
    by = collections.defaultdict(list)
    for d, x, n in rows:
        by[d].append((x, n))
    days = list(by)
    ct = sum(n for d in days for _, n in by[d])
    if not ct:
        return None
    ev = sum(x * n for d in days for x, n in by[d]) / ct
    bs = []
    for _ in range(BOOT):
        s = [by[RNG.choice(days)] for _ in days]
        c = sum(n for b in s for _, n in b)
        bs.append(sum(x * n for b in s for x, n in b) / c)
    bs.sort()
    return ev, bs[int(.025 * BOOT)], bs[int(.975 * BOOT) - 1], ct, len(days)


def run(series, mk, tr, log, lag=2):
    ms = [m for m in mk if m["series"] == series and m.get("result") in ("yes", "no")]
    winner = {}
    for m in ms:
        if m["result"] == "yes":
            winner[ev_date(m["event_ticker"])] = norm(m["yes_sub_title"])
    by_tk = {m["ticker"]: m for m in ms}
    rows = []
    for t in tr:
        m = by_tk.get(t["ticker"])
        if not m:
            continue
        d = ev_date(m["event_ticker"])
        inc = winner.get(d - timedelta(lag))
        if not inc or norm(m["yes_sub_title"]) != inc:
            continue
        p = float(t["taker_paid"])
        side = t["taker_side"]
        py = p if side == "yes" else 1 - p
        rows.append({"d": d, "side": side, "p": p, "py": py, "n": min(float(t["count"]), 25),
                     "won": 1 if m["result"] == side else 0, "yes": 1 if m["result"] == "yes" else 0})
    log(f"\n===== {series}: incumbent = #1 of chart D-{lag} =====")
    if len(rows) < 100:
        log(f"{len(rows)} trades on the incumbent; too few")
        return
    days = sorted({r["d"] for r in rows})
    half = days[len(days) // 2]
    log(f"{len(rows)} trades on the incumbent over {len(days)} chart dates {days[0]}..{days[-1]}; "
        f"split at {half}")
    for nm, sub in (("all", rows), ("1st half", [r for r in rows if r["d"] < half]),
                    ("2nd half", [r for r in rows if r["d"] >= half])):
        n = sum(r["n"] for r in sub)
        log(f"  {nm:<9} implied repeat {100*sum(r['py']*r['n'] for r in sub)/n:5.1f}%  "
            f"realised {100*sum(r['yes']*r['n'] for r in sub)/n:5.1f}%  ({n:.0f} ct)")
    log("  by YES price band (all days): implied vs realised repeat, contracts")
    for lo, hi in ((0, .3), (.3, .6), (.6, .8), (.8, .9), (.9, 1.01)):
        sub = [r for r in rows if lo <= r["py"] < hi]
        n = sum(r["n"] for r in sub)
        if n:
            log(f"    {lo:.2f}-{hi:.2f}: {100*sum(r['py']*r['n'] for r in sub)/n:5.1f}% vs "
                f"{100*sum(r['yes']*r['n'] for r in sub)/n:5.1f}%  ({n:.0f} ct, "
                f"{len({r['d'] for r in sub})} days)")
    for strat in ("buy YES on incumbent", "buy NO on incumbent"):
        want = "yes" if strat.startswith("buy YES") else "no"
        for nm, sub in (("1st half", [r for r in rows if r["d"] < half]),
                        ("2nd half", [r for r in rows if r["d"] >= half])):
            take = [(r["d"], r["won"] - r["p"] - taker_fee(r["p"]), r["n"])
                    for r in sub if r["side"] == want]
            make = [(r["d"], r["p"] - r["won"], r["n"]) for r in sub if r["side"] != want]
            for lab, rr in (("take", take), ("rest", make)):
                b = boot(rr, None)
                if b:
                    log(f"  {strat:<21} {lab:<4} {nm:<9} {100*b[0]:+6.2f}c/ct [{100*b[1]:+.2f}, "
                        f"{100*b[2]:+.2f}]  {b[3]:.0f} ct, {b[4]} days")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", required=True)
    ap.add_argument("--trades", required=True)
    ap.add_argument("--series", default="KXSPOTIFYD,KXSPOTIFYARTISTD")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    mk = list(csv.DictReader(gzip.open(a.markets, "rt")))
    tr = list(csv.DictReader(gzip.open(a.trades, "rt")))
    f = open(a.out, "w")

    def log(s):
        print(s); f.write(s + "\n")

    for s in a.series.split(","):
        run(s, mk, tr, log)


if __name__ == "__main__":
    main()
