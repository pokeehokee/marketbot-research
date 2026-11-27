"""Whale flow on Kalshi's 15-minute crypto binaries: does following a big Hyperliquid
wallet's trade by buying the matching side of KXBTC15M / KXETH15M / KXSOL15M pay after
Kalshi's fee? Variant 1 of the 2026-11-27 mutations.

Why binaries: the spot test (whale_flow.py) found the follow signal is real but tiny in
basis points: +1 to +4bp gross. On a perp that is less than the 4bp round-trip fee. A
15-minute up/down binary pays on DIRECTION, so what matters is the win rate, and its fee
is per contract (~1.8c at 50c), not per dollar of notional.

Events: whale_flow.py's exported events (entries = open/add/flip; "all" adds reduces and
closes, signed by the direction of the trade). Entry: a copier who sees the whale's last
fill and acts `delay` s later (plus 5 s of latency) buys the first Kalshi print at or after
that time where a taker bought the side we want, within 120 s; that price is one someone
actually paid. The market is the 15-minute window open at entry, with at least 2 minutes
left. Hold to settlement. Fee: Kalshi's taker fee on a 10-contract order, ceil to the cent.

Read-only: Kalshi public market and trade reads through taker_flow_screen's whitelist.

Run:  python3 -I kalshi_follow.py --tag x --events out/whales-1-*.csv.gz --budget-min 300
      python3 -I kalshi_follow.py --tag x --score out/whales-1-*.csv.gz
"""
import argparse, csv, glob, gzip, math, os, sys, time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "discovery"))
import ladder_tape as L          # noqa: E402
import taker_flow_screen as T    # noqa: E402

SERIES = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M"}
DELAYS = (0, 60, 300)
LATENCY = 5
WAIT = 120
MIN_LEFT = 120
WINDOW = 900
LOT = 10
L.LOOKBACK_S = WINDOW + 300


def fee(p, lot=LOT):
    return math.ceil(0.07 * lot * p * (1 - p) * 100 - 1e-9) / 100.0 / lot


def load_events(paths):
    ev = []
    for p in paths:
        if "-funding" in p or "-candles" in p:
            continue
        with gzip.open(p, "rt") as fo:
            for r in csv.DictReader(fo):
                ev.append({"addr": r["addr"], "coin": r["coin"], "day": r["day"],
                           "t": float(r["t_last"]), "dir": int(r["dir"]), "kind": r["kind"],
                           "notional": float(r["notional"] or 0)})
    return ev


def market_for(closes, te):
    """The 15-minute window open at te with at least MIN_LEFT seconds left: the first
    close >= te + MIN_LEFT, provided the window (close - WINDOW, close] contains te."""
    import bisect
    i = bisect.bisect_left([c for c, _ in closes], te + MIN_LEFT)
    if i < len(closes) and closes[i][0] - WINDOW <= te:
        return closes[i]
    return None


def entry(trades, te, side, wait=WAIT):
    """First print at or after te, within wait s, where the taker bought `side`.
    trades: (ts, taker_side, taker_price) sorted by ts. Returns (ts, price) or None."""
    for ts, s, p in trades:
        if ts < te:
            continue
        if ts > te + wait:
            return None
        if s == side:
            return ts, p
    return None


MAKER_FEE = 0.0175   # Kalshi's maker rate where a series charges one; 0 where it does not


def maker_fee(p, lot=LOT, rate=MAKER_FEE):
    return math.ceil(rate * lot * p * (1 - p) * 100 - 1e-9) / 100.0 / lot


def maker_fill(trades, t0, side, bid, wait=WAIT):
    """Variant 2: a bid for `side` resting at `bid` from t0. Filled only when a LATER print
    (t0 < ts <= t0 + wait) prices `side` strictly below the bid, i.e. trades through it.
    A print AT the bid is not counted: the queue ahead of us may have absorbed it."""
    for ts, s, p in trades:
        if ts <= t0:
            continue
        if ts > t0 + wait:
            return None
        ours = p if s == side else 1.0 - p
        if ours < bid - 1e-9:
            return ts
    return None


def db(path):
    c = L.db(path)
    c.executescript("CREATE TABLE IF NOT EXISTS tr(ticker TEXT, ts REAL, side TEXT, price REAL)")
    return c


def run(a, log=print):
    for p in ("/portfolio/orders", "/portfolio/balance"):
        try:
            T.assert_read_only(p)
        except RuntimeError:
            continue
        raise AssertionError(p)
    c = db(os.path.join(HERE, f"kf_{a.tag}.db"))
    ev = load_events(sorted(glob.glob(a.events)))
    lo, hi = min(e["t"] for e in ev) - 3600, max(e["t"] for e in ev) + 3 * 3600
    cut, err = T.get("/historical/cutoff")
    if not cut:
        raise RuntimeError(f"cutoff unreadable ({err})")
    mc, tc = T.utc(cut.get("market_settled_ts")), T.utc(cut.get("trades_created_ts"))
    for coin, s in SERIES.items():
        row = c.execute("SELECT lo, hi FROM listed WHERE series=?", (s,)).fetchone()
        if not (row and row[0] <= lo + 3600 and row[1] >= hi - 3600):
            n, u = L.list_markets(c, s, lo, hi, mc, log)
            log(f"{s}: {n} settled markets, {u} unresolved")
    one, _ = T.get("/markets", {"series_ticker": "KXBTC15M", "limit": 1})
    if one and one.get("markets"):
        m = one["markets"][0]
        log("semantics check: " + "; ".join(f"{k}={str(m.get(k))[:90]}" for k in
            ("ticker", "title", "yes_sub_title", "strike_type", "floor_strike", "open_time", "close_time")))
    closes = {coin: sorted((r[0], r[1]) for r in c.execute(
        "SELECT close_ts, ticker FROM market WHERE series=?", (s,))) for coin, s in SERIES.items()}
    need = set()
    for e in ev:
        if e["coin"] not in closes:
            continue
        for dl in DELAYS:
            m = market_for(closes[e["coin"]], e["t"] + dl + LATENCY)
            if m:
                need.add(m)
    done = {r[0] for r in c.execute("SELECT ticker FROM market WHERE done=1")}
    todo = sorted(m for m in need if m[1] not in done)
    log(f"{len(ev)} events; {len(need)} windows needed, {len(todo)} to read")
    start = time.time()
    from concurrent.futures import ThreadPoolExecutor
    pool = ThreadPoolExecutor(max_workers=8)
    futs, i, nd = [], 0, 0
    while i < len(todo) or futs:
        while i < len(todo) and len(futs) < 16 and time.time() - start < a.budget_min * 60:
            close, tk = todo[i]
            futs.append(pool.submit(L.fetch_market, tk, close, tc, log)); i += 1
        if not futs:
            log("budget reached; a re-run resumes")
            break
        tk, trades, pages = futs.pop(0).result()
        if trades is None:
            continue
        rows = []
        for t in trades:
            f = T.trade_fields(t)
            if f and f[0] == tk:
                rows.append((tk, f[1], f[2], f[3]))
        c.executemany("INSERT INTO tr VALUES (?,?,?,?)", rows)
        c.execute("UPDATE market SET done=1, trades=?, pages=? WHERE ticker=?", (len(rows), pages, tk))
        c.commit()
        nd += 1
        if nd % 500 == 0:
            log(f"{nd} windows read")
    pool.shutdown(wait=False)
    left = len([m for m in need if m[1] not in {r[0] for r in c.execute("SELECT ticker FROM market WHERE done=1")}])
    log(f"done this run: {nd} read, {left} unread")
    with open(os.path.join(HERE, "out", f"kf-{a.tag}-coverage.txt"), "w") as fo:
        fo.write(f"windows {len(need)}\nunread {left}\n")


# --- scoring ------------------------------------------------------------------------
import random
B = 2000


def boot(by):
    g = [v for v in by.values() if v[1]]
    if len(g) < 3:
        return None
    tot = sum(x[0] for x in g) / sum(x[1] for x in g)
    rnd = random.Random(7)
    ms = []
    for _ in range(B):
        s = n = 0
        for _ in g:
            x = g[rnd.randrange(len(g))]; s += x[0]; n += x[1]
        ms.append(s / n)
    ms.sort()
    return tot, ms[int(.025 * B)], ms[int(.975 * B) - 1], sum(1 for m in ms if m <= 0) / B


def cell(rows):
    by = defaultdict(lambda: [0.0, 0])
    for r in rows:
        g = by[r["day"]]; g[0] += r["pnl"]; g[1] += 1
    b = boot(by)
    if not b:
        return None
    wins = [r for r in rows if r["y"] == 1]
    w = len(wins) / len(rows)
    avg_p = sum(r["p"] for r in rows) / len(rows)
    # payout multiple: what a win pays per dollar at risk, net of fee, averaged
    mult = sum((1 - r["p"] - r["fee"]) / (r["p"] + r["fee"]) for r in rows) / len(rows)
    return {"n": len(rows), "days": len(by), "mean": b[0], "lo": b[1], "hi": b[2], "p": b[3],
            "win": w, "price": avg_p, "mult": mult,
            "per_dollar": sum(r["pnl"] for r in rows) / sum(r["p"] + r["fee"] for r in rows)}


def fmt(c):
    if not c:
        return "too few days"
    return (f"n {c['n']:5d} d {c['days']:3d}  EV {c['mean']*100:+6.2f}c [{c['lo']*100:+6.2f}, {c['hi']*100:+6.2f}]"
            f"  win {c['win']*100:5.1f}% at avg {c['price']*100:4.1f}c  x {c['mult']:.2f}"
            f"  per $1 {c['per_dollar']*100:+6.2f}c")


def bh(ps, q=0.10):
    order = sorted(range(len(ps)), key=lambda i: ps[i])
    k = 0
    for rank, i in enumerate(order, 1):
        if ps[i] <= q * rank / len(ps):
            k = rank
    return set(order[:k])


def build(c, ev, sign=1, rnd=None, maker=None, miss=None):
    trs = defaultdict(list)
    for tk, ts, s, p in c.execute("SELECT ticker, ts, side, price FROM tr ORDER BY ts"):
        trs[tk].append((ts, s, p))
    res = {r[0]: r[1] for r in c.execute("SELECT ticker, result FROM market")}
    closes = {coin: sorted((r[0], r[1]) for r in c.execute(
        "SELECT close_ts, ticker FROM market WHERE series=?", (s,))) for coin, s in SERIES.items()}
    out = []
    for e in ev:
        if e["coin"] not in closes:
            continue
        d = e["dir"] * sign * (rnd.choice((1, -1)) if rnd else 1)
        side = "yes" if d > 0 else "no"
        for dl in DELAYS:
            m = market_for(closes[e["coin"]], e["t"] + dl + LATENCY)
            if not m or m[1] not in trs:
                continue
            x = entry(trs[m[1]], e["t"] + dl + LATENCY, side)
            if not x:
                continue
            y = 1 if res.get(m[1]) == side else 0
            if maker is not None:
                bid = round(x[1] - 0.01, 2)
                tf = maker_fill(trs[m[1]], x[0], side, bid) if bid > 0 else None
                if tf is None:
                    if miss is not None:
                        miss.append(dict(e, delay=dl))
                    continue
                f = maker_fee(bid, rate=maker)
                out.append(dict(e, delay=dl, p=bid, fee=f, y=y, pnl=y - bid - f, left=m[0] - tf))
                continue
            f = fee(x[1])
            out.append(dict(e, delay=dl, p=x[1], fee=f, y=y, pnl=y - x[1] - f, left=m[0] - x[0]))
    return out


FAM = {"entries": lambda r: r["kind"] in ("open", "add", "flip"),
       "entries >= $250k": lambda r: r["kind"] in ("open", "add", "flip") and r["notional"] >= 2.5e5,
       "all directional": lambda r: True,
       "BTC entries": lambda r: r["coin"] == "BTC" and r["kind"] in ("open", "add", "flip"),
       "ETH entries": lambda r: r["coin"] == "ETH" and r["kind"] in ("open", "add", "flip"),
       "SOL entries": lambda r: r["coin"] == "SOL" and r["kind"] in ("open", "add", "flip")}
HOLDOUT_FROM = "2026-11-03"   # the same split as whale_flow's scoring


def score(a):
    c = db(os.path.join(HERE, f"kf_{a.tag}.db"))
    ev = load_events(sorted(glob.glob(a.score)))
    w = print
    if a.maker is not None:
        miss = []
        rows = build(c, ev, maker=a.maker, miss=miss)
        null = build(c, ev, rnd=random.Random(11), maker=a.maker)
        w(f"VARIANT 2, MAKER: bid at the taker price - 1c, filled only on a trade-through within "
          f"{WAIT} s; maker fee rate {a.maker}")
        prim = lambda R: [r for r in R if FAM["entries"](r) and r["delay"] == 0]
        for nm, lo_, hi_ in (("train", "", HOLDOUT_FROM), ("holdout", HOLDOUT_FROM, "9999")):
            f_ = len(prim([r for r in rows if lo_ <= r["day"] < hi_]))
            m_ = len(prim([r for r in miss if lo_ <= r["day"] < hi_]))
            w(f"  fill rate, primary cell, {nm}: {f_} of {f_ + m_} = {f_ / max(1, f_ + m_):.0%}")
    else:
        rows = build(c, ev)
        null = build(c, ev, rnd=random.Random(11))
    w(f"{len(rows)} entries priced from {len(ev)} events; holdout from {HOLDOUT_FROM}")
    w(f"median seconds left at entry {sorted(r['left'] for r in rows)[len(rows)//2] if rows else '-'}")
    w("PRE-REGISTERED primary: entries, delay 0 (+5 s), all coins, "
      f"{'maker' if a.maker is not None else 'taker'}, hold to settlement")
    for nm, R in (("train", [r for r in rows if r["day"] < HOLDOUT_FROM]),
                  ("holdout", [r for r in rows if r["day"] >= HOLDOUT_FROM])):
        w(f"  {nm:8s} {fmt(cell([r for r in R if FAM['entries'](r) and r['delay'] == 0]))}")
    w()
    for nm, R in (("train", [r for r in rows if r["day"] < HOLDOUT_FROM]),
                  ("null", [r for r in null if r["day"] < HOLDOUT_FROM])):
        grid = [(f, dl, cell([r for r in R if FAM[f](r) and r["delay"] == dl])) for f in FAM for dl in DELAYS]
        keep = bh([g[2]["p"] if g[2] else 1.0 for g in grid])
        sel = [g for i, g in enumerate(grid) if i in keep and g[2] and g[2]["lo"] > 0]
        w(f"== {nm}: {len(grid)} cells, selected {len(sel)} ==")
        for f, dl, cc in grid:
            w(f"  {f:18s} late {dl:3d}s  {fmt(cc)}")
        if nm == "train":
            w("-- selected, read on the holdout --")
            for f, dl, cc in sel:
                H = [r for r in rows if r["day"] >= HOLDOUT_FROM and FAM[f](r) and r["delay"] == dl]
                hc = cell(H)
                w(f"  {f:18s} late {dl:3d}s  hold {fmt(hc)}  {'PASS' if hc and hc['lo'] > 0 else 'fail'}")
        w()
    w("-- holdout, every cell (context only; only selected cells can pass) --")
    for f in FAM:
        for dl in DELAYS:
            w(f"  {f:18s} late {dl:3d}s  {fmt(cell([r for r in rows if r['day'] >= HOLDOUT_FROM and FAM[f](r) and r['delay'] == dl]))}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--events")
    ap.add_argument("--score")
    ap.add_argument("--budget-min", type=int, default=300)
    ap.add_argument("--maker", type=float, help="score variant 2 at this maker fee rate")
    a = ap.parse_args(argv)
    score(a) if a.score else run(a)


if __name__ == "__main__":
    main()
