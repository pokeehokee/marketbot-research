"""After a Kalshi market jumps, does the price keep running or snap back?

Alpha research, round 1b (2026-11-27). Devin's question was whether news and
posts move markets in ways that can be predicted. The part of it that can be
measured without any news feed at all: take every sharp move a market made,
and look at what the price did next and how the market settled.

  * If big moves OVERSHOOT, the side the market just ran from is too cheap,
    and the cheap side right after a crash is a large-multiple bet with an edge.
  * If big moves UNDERSHOOT and keep drifting, whoever reads the news even
    fifteen minutes late can follow it. That is the case where a news feed
    (GDELT, 15-minute updates) would be worth wiring in, and the delayed-entry
    columns below say how much of the drift survives the delay.

Universe: every series in the news-driven categories, markets that settled
yes/no in the window and lived at least a day (an hourly ladder's moves are
the underlying's, measured elsewhere). Trades are bucketed into 5-minute VWAP
bars when read, so a long election market does not cost gigabytes.

A jump: the 5-minute VWAP moved at least JUMP (15c) from the last bar at or
before 30 minutes earlier, with that reference no older than 2 hours. One jump
per market per 6 hours (the first), none in the last hour before close (that is
resolution, not news). p0 is the VWAP of the bar the jump completed in.

Everything after p0 is read only by `outcomes`, which is tested to ignore any
bar at or before the jump. Missing is never zero: no trade after the jump
means no later price, not "no move".

Read-only and unauthenticated (research/discovery/taker_flow_screen.get).

Run:  python3 -I jumps.py --categories Politics,Elections --days 45 --budget-min 300 --tag x
      python3 -I jumps.py --export out/jumps-x.csv.gz --tag x
"""
import argparse, gzip, math, os, sqlite3, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "discovery"))
import taker_flow_screen as T    # noqa: E402

BAR = 300
JUMP = 0.15
REF_LAG = 1800
REF_MAX_AGE = 7200
GAP = 6 * 3600
NO_JUMP_BEFORE_CLOSE = 3600
LOOKBACK = 21 * 86400
MIN_LIFE = 86400
HORIZONS = (3600, 6 * 3600, 24 * 3600)
DELAYS = (900, 3600)     # entry 15 and 60 minutes after the jump


# --- pure: bars, jumps, outcomes -------------------------------------------
def bars_from(trades):
    """trades (ts, yes_price, count) -> sorted [(bar_start, vwap, contracts, n)]."""
    acc = {}
    for ts, p, c in trades:
        b = int(ts // BAR) * BAR
        a = acc.setdefault(b, [0.0, 0.0, 0])
        a[0] += p * c; a[1] += c; a[2] += 1
    return [(b, a[0] / a[1], a[1], a[2]) for b, a in sorted(acc.items()) if a[1] > 0]


def _idx_at_or_before(bars, t):
    lo, hi = 0, len(bars)
    while lo < hi:
        m = (lo + hi) // 2
        if bars[m][0] <= t:
            lo = m + 1
        else:
            hi = m
    return lo - 1


def find_jumps(bars, close_ts):
    """[(i, direction, p_ref, p0)] for each jump bar index i."""
    out, last = [], -1e18
    for i, (b, p, _, _) in enumerate(bars):
        if b + BAR > close_ts - NO_JUMP_BEFORE_CLOSE:
            break
        if b - last < GAP:
            continue
        j = _idx_at_or_before(bars, b - REF_LAG)
        if j < 0 or b - bars[j][0] > REF_MAX_AGE:
            continue
        d = p - bars[j][1]
        if abs(d) >= JUMP:
            out.append((i, 1 if d > 0 else -1, bars[j][1], p))
            last = b
    return out


def price_after(bars, i, t):
    """VWAP of the last bar at or before t, provided it is AFTER bar i. Else None."""
    j = _idx_at_or_before(bars, t)
    return bars[j][1] if j > i else None


def outcomes(bars, i, close_ts):
    """Later prices for a jump at bar i: at each horizon, and the entry price after each
    delay. Reads only bars after i."""
    b = bars[i][0] + BAR        # the jump bar's END: nothing in it is "after"
    later = {h: price_after(bars, i, min(b + h, close_ts)) for h in HORIZONS}
    entry = {dl: price_after(bars, i, min(b + dl, close_ts)) for dl in DELAYS}
    return later, entry


# --- pull --------------------------------------------------------------------
def db(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS series(ticker TEXT PRIMARY KEY, category TEXT, fee_type TEXT,
        listed INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS market(ticker TEXT PRIMARY KEY, event TEXT, series TEXT,
        open_ts REAL, close_ts REAL, result TEXT, done INTEGER DEFAULT 0, trades INTEGER);
    CREATE TABLE IF NOT EXISTS bar(ticker TEXT, b INTEGER, vwap REAL, contracts REAL, n INTEGER,
        PRIMARY KEY(ticker, b));
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    return c


def paged(path, params, key, log, cap=400):
    out, cursor, n = [], None, 0
    while n < cap:
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        d, err = T.get(path, q)
        if err or d is None:
            log(f"{path} {params.get('series_ticker') or params.get('ticker') or ''}: {err}")
            return None
        out += d.get(key) or []
        cursor = d.get("cursor"); n += 1
        if not cursor or not d.get(key):
            return out
    log(f"{path}: page cap {cap} reached"); return None


def list_all(c, categories, days, log):
    if c.execute("SELECT v FROM meta WHERE k='window'").fetchone() is None:
        hi = math.floor(time.time() / 3600) * 3600 - 86400
        c.execute("INSERT INTO meta VALUES('window', ?)", (f"{hi - days * 86400},{hi}",))
        c.commit()
    lo, hi = map(float, c.execute("SELECT v FROM meta WHERE k='window'").fetchone()[0].split(","))
    if not c.execute("SELECT 1 FROM series LIMIT 1").fetchone():
        rows = paged("/series", {"limit": 1000}, "series", log)
        if rows is None:
            raise RuntimeError("series listing failed; refusing a partial universe")
        cats = {}
        for s in rows:
            cats[s.get("category") or "?"] = cats.get(s.get("category") or "?", 0) + 1
        log("categories on the exchange: " + ", ".join(f"{k} {v}" for k, v in sorted(cats.items())))
        want = {x.lower() for x in categories}
        for s in rows:
            if (s.get("category") or "").lower() in want:
                c.execute("INSERT OR IGNORE INTO series(ticker, category, fee_type) VALUES(?,?,?)",
                          (s["ticker"], s.get("category"), s.get("fee_type")))
        c.commit()
    p, err = T.get("/historical/cutoff")
    if not p:
        raise RuntimeError(f"cutoff unreadable ({err})")
    mc = T.utc(p.get("market_settled_ts"))
    todo = [r[0] for r in c.execute("SELECT ticker FROM series WHERE listed=0").fetchall()]
    log(f"{len(todo)} series to list")
    for k, s in enumerate(todo):
        got = {}
        paths = ["/markets"] + (["/historical/markets"] if mc and lo < mc else [])
        ok = True
        for path in paths:
            prm = {"series_ticker": s, "min_close_ts": int(lo), "max_close_ts": int(hi), "limit": 1000}
            if path == "/markets":
                prm["status"] = "settled"
            rows = paged(path, prm, "markets", log)
            if rows is None:
                ok = False; break
            for m in rows:
                if (m.get("result") or "").lower() in ("yes", "no"):
                    got[m["ticker"]] = m
        if not ok:
            continue       # retried on resume, never taken as "no markets"
        for tk, m in got.items():
            o, cl = T.utc(m.get("open_time")), T.utc(m.get("close_time"))
            if o is None or cl is None or cl - o < MIN_LIFE:
                continue
            c.execute("INSERT OR IGNORE INTO market(ticker, event, series, open_ts, close_ts, result)"
                      " VALUES(?,?,?,?,?,?)", (tk, m.get("event_ticker"), s, o, cl, m["result"].lower()))
        c.execute("UPDATE series SET listed=1 WHERE ticker=?", (s,))
        c.commit()
        if k % 200 == 0:
            log(f"listed {k}/{len(todo)} series")


def fetch(ticker, open_ts, close_ts, trade_cutoff, log):
    lo = max(open_ts, close_ts - LOOKBACK)
    tiers = []
    if trade_cutoff is None or close_ts >= trade_cutoff:
        tiers.append(("/markets/trades", max(lo, trade_cutoff or lo)))
    if trade_cutoff is not None and lo < trade_cutoff:
        tiers.append(("/historical/trades", lo))
    seen, out = set(), []
    for path, start in tiers:
        rows = paged(path, {"ticker": ticker, "min_ts": int(start), "max_ts": int(close_ts) + 1,
                            "limit": 1000}, "trades", log, cap=2000)
        if rows is None:
            return ticker, None
        for t in rows:
            tid = t.get("trade_id")
            if tid and tid in seen:
                continue
            seen.add(tid)
            f = T.trade_fields(t)
            if f and f[0] == ticker and f[1] <= close_ts:
                _, ts, side, price, count = f
                out.append((ts, price if side == "yes" else 1.0 - price, count))
    return ticker, out


def run(categories, days, budget_min, dbpath, log=print):
    from concurrent.futures import ThreadPoolExecutor
    start = time.time()
    c = db(dbpath)
    list_all(c, categories, days, log)
    p, _ = T.get("/historical/cutoff")
    tc = T.utc(p.get("trades_created_ts")) if p else None
    todo = c.execute("SELECT ticker, open_ts, close_ts FROM market WHERE done=0 ORDER BY close_ts DESC"
                     ).fetchall()
    log(f"{len(todo)} markets to read")
    pool = ThreadPoolExecutor(max_workers=8)
    futs, i, nd, failed = [], 0, 0, 0
    while i < len(todo) or futs:
        while i < len(todo) and len(futs) < 16 and time.time() - start < budget_min * 60:
            futs.append(pool.submit(fetch, *todo[i], tc, log)); i += 1
        if not futs:
            log("budget reached; stopping (a re-run resumes here)"); break
        tk, trades = futs.pop(0).result()
        if trades is None:
            failed += 1; continue
        for b, v, ct, n in bars_from(trades):
            c.execute("INSERT OR REPLACE INTO bar VALUES(?,?,?,?,?)", (tk, b, v, ct, n))
        c.execute("UPDATE market SET done=1, trades=? WHERE ticker=?", (len(trades), tk))
        c.commit(); nd += 1
        if nd % 500 == 0:
            log(f"{nd} markets read in {(time.time()-start)/60:.1f} min, {failed} failed")
    pool.shutdown(wait=False)
    left = c.execute("SELECT COUNT(*) FROM market WHERE done=0").fetchone()[0]
    sl = c.execute("SELECT COUNT(*) FROM series WHERE listed=0").fetchone()[0]
    log(f"done this run: {nd} read, {failed} failed, {left} markets and {sl} series still unread")


COLS = ("category", "series", "event", "ticker", "jump_ts", "day", "dir", "p_ref", "p0",
        "contracts_jump_bar", "ttc_h", "y", "p_1h", "p_6h", "p_24h", "entry_15m", "entry_60m")


def export(dbpath, path):
    c = db(dbpath)
    cat = dict(c.execute("SELECT ticker, category FROM series").fetchall())
    n = 0
    with gzip.open(path, "wt") as f:
        f.write(",".join(COLS) + "\n")
        for tk, ev, s, close_ts, res in c.execute(
                "SELECT ticker, event, series, close_ts, result FROM market WHERE done=1").fetchall():
            bars = c.execute("SELECT b, vwap, contracts, n FROM bar WHERE ticker=? ORDER BY b",
                             (tk,)).fetchall()
            for i, d, pref, p0 in find_jumps(bars, close_ts):
                later, entry = outcomes(bars, i, close_ts)
                jt = bars[i][0] + BAR
                row = (cat.get(s) or "", s, ev, tk, int(jt), time.strftime("%Y-%m-%d", time.gmtime(jt)),
                       d, pref, p0, bars[i][2], (close_ts - jt) / 3600, 1 if res == "yes" else 0,
                       later[3600], later[21600], later[86400], entry[900], entry[3600])
                f.write(",".join("" if v is None else (f"{v:.5g}" if isinstance(v, float) else str(v))
                                 for v in row) + "\n")
                n += 1
    listed, read = c.execute("SELECT COUNT(*), SUM(done) FROM market").fetchone()
    sl = c.execute("SELECT COUNT(*), SUM(listed) FROM series").fetchone()
    with open(path.replace(".csv.gz", "-coverage.txt"), "w") as f:
        f.write(f"jumps {n}\nseries {sl[0]} listed {sl[1] or 0}\nmarkets {listed} read {read or 0} "
                f"unread {listed - (read or 0)}\n"
                f"unread_total {(listed - (read or 0) + sl[0] - (sl[1] or 0)) if sl[0] else 1}\n")
    return n


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--categories", default="")
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--budget-min", type=float, default=300)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--export")
    a = ap.parse_args()
    dbpath = os.path.join(HERE, f"jumps_{a.tag}.db")
    if a.export:
        print("jumps", export(dbpath, a.export)); sys.exit(0)
    run([x.strip() for x in a.categories.split(",") if x.strip()], a.days, a.budget_min, dbpath,
        log=lambda s: print(s, flush=True))
