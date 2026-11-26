"""Round-2 ideas #21-#24: does the BTC ladder's trade pay on OTHER series?

The ladder rests a bid on the side priced 90-99c of an almost-decided contract
and is filled when a taker buys the 1-10c side. Round 1's whole-exchange screen
could not resolve that trade: 80 sampled hours left KXBTCD's own control at
+0.58c / +1.95c with every interval through zero, against the account's real
+1.49c. So this pulls the FULL tape, every trade of every settled market, for a
fixed list of series, instead of sampling hours exchange-wide.

What is scored is the same as round 1 (score_taker_flow.py): a maker who sold
the side the taker bought, held to settlement, earns `paid - taker_won` per
contract. Every trade counted is a real fill that happened, so the adverse
selection a resting order suffers is in the number. Queue position is not:
that is what the forward fill test (research/fills/) is for.

Differences from the round-1 screen, each deliberate:
  * Per MARKET, not per hour: `/markets/trades?ticker=` over the last
    LOOKBACK_S before each market's close. Cheaper for a series list, and every
    market is either fully read or not at all (resume is exact).
  * The `event` column holds the MARKET ticker, so `events` / `loss_events`
    count markets and markets on which the maker lost. The repo's bar is 30
    losses, counted as positions, and one market is one position.
  * Finer time-to-close buckets, because a 15-minute market's whole life fits
    in round 1's "5-15m" bucket.
  * Early closes are detected, not assumed away: round 1 refused in-game cells
    because a market that closes early moves its trades between ttc buckets on
    the future. These series close on a fixed grid; every close off its grid
    is counted and printed, and the scorer refuses a series that has any.

Rules kept from the repo's CLAUDE.md:
  * Read-only and unauthenticated (taker_flow_screen.assert_read_only).
  * `taker_outcome_side` is the outcome the taker ACQUIRED; a NO taker paid
    1 - yes_price (taker_flow_screen.trade_fields, which is tested).
  * A paged read is followed to the end; a failed read marks the market
    unread (retried on resume), never as "no trades".
  * A market with no settled result is skipped and counted, never graded.
  * Kalshi partitions markets and trades into live and historical tiers. The
    window is checked against /historical/cutoff and both tiers are read when
    it reaches back past the cutoff.

Run:  python3 -I ladder_tape.py --series KXBTC15M --days 28 --budget-min 300 --tag x
      python3 -I ladder_tape.py --export out/ladder-x.csv.gz --tag x
"""
import argparse, gzip, math, os, sqlite3, sys, time, urllib.parse
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import taker_flow_screen as T  # noqa: E402

LOOKBACK_S = 3 * 3600
TTC_EDGES = [0, 30, 60, 120, 180, 300, 600, 900, 1800, 3000, LOOKBACK_S + 1]
TTC_NAMES = ["<30s", "30-60s", "1-2m", "2-3m", "3-5m", "5-10m", "10-15m",
             "15-30m", "30-50m", "50m-3h"]
# The fixed close grid each series family settles on, in seconds.
GRID = {"15M": 900, "U": 3600, "D": 3600}


def grid_of(series):
    if series.endswith("15M"):
        return GRID["15M"]
    if series.endswith("U") or series in ("KXBTCD", "KXBTC"):   # KXBTC: hourly ranges (#40)
        return GRID["U"]
    return None   # daily or other: not checked


def db(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS market(ticker TEXT PRIMARY KEY, event TEXT, series TEXT,
        close_ts REAL, result TEXT, done INTEGER DEFAULT 0, trades INTEGER, pages INTEGER);
    CREATE TABLE IF NOT EXISTS series(ticker TEXT PRIMARY KEY, category TEXT, fee_type TEXT);
    CREATE TABLE IF NOT EXISTS cell(series TEXT, event TEXT, day TEXT, ttc TEXT, band TEXT,
        taker_side TEXT, contracts REAL, paid REAL, taker_won REAL, trades INTEGER,
        PRIMARY KEY(series, event, day, ttc, band, taker_side));
    CREATE TABLE IF NOT EXISTS listed(series TEXT PRIMARY KEY, lo REAL, hi REAL, n INTEGER,
        unresolved INTEGER);
    """)
    return c


def paged(path, params, log, key):
    """Every page of a listing, or None if any page failed (never a partial)."""
    out, cursor, pages = [], None, 0
    while True:
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        d, err = T.get(path, p)
        if err or d is None:
            log(f"{path} {params.get('series_ticker') or params.get('ticker')}: {err}")
            return None, pages
        pages += 1
        out += d.get(key) or []
        cursor = d.get("cursor")
        if not cursor or not d.get(key):
            return out, pages


def list_markets(c, series, lo, hi, market_cutoff, log):
    """Settled markets of one series closing in [lo, hi), from both tiers."""
    paths = ["/markets"]
    if market_cutoff and lo < market_cutoff:
        paths.append("/historical/markets")
        log(f"{series}: window reaches the historical market tier")
    seen, unresolved = {}, 0
    for path in paths:
        params = {"series_ticker": series, "min_close_ts": int(lo), "max_close_ts": int(hi),
                  "limit": 1000}
        if path == "/markets":
            params["status"] = "settled"
        rows, _ = paged(path, params, log, "markets")
        if rows is None:
            raise RuntimeError(f"listing {series} on {path} failed; refusing a partial listing")
        for m in rows:
            res = (m.get("result") or "").lower()
            if res not in ("yes", "no"):
                unresolved += 1
                continue
            seen[m["ticker"]] = (m.get("event_ticker"), T.utc(m.get("close_time")), res)
    for tk, (ev, close, res) in seen.items():
        c.execute("INSERT OR IGNORE INTO market(ticker, event, series, close_ts, result) "
                  "VALUES(?,?,?,?,?)", (tk, ev, series, close, res))
    c.execute("INSERT OR REPLACE INTO listed VALUES(?,?,?,?,?)",
              (series, lo, hi, len(seen), unresolved))
    c.commit()
    return len(seen), unresolved


def fetch_market(ticker, close_ts, trade_cutoff, log):
    """Every trade on one market in its last LOOKBACK_S, from whichever tiers
    the window touches. None if any read failed."""
    lo = close_ts - LOOKBACK_S
    tiers = []
    if trade_cutoff is None or close_ts >= trade_cutoff:
        tiers.append(("/markets/trades", max(lo, trade_cutoff or lo)))
    if trade_cutoff is not None and lo < trade_cutoff:
        tiers.append(("/historical/trades", lo))
    trades, pages = [], 0
    for path, start in tiers:
        rows, n = paged(path, {"ticker": ticker, "min_ts": int(start),
                               "max_ts": int(close_ts) + 1, "limit": 1000}, log, "trades")
        pages += n
        if rows is None:
            return ticker, None, pages
        trades += rows
    return ticker, trades, pages


def aggregate(c, ticker, series, close_ts, result, trades, pages):
    """One market's trades into cells, and the market marked done, in one commit."""
    day = time.strftime("%Y-%m-%d", time.gmtime(close_ts))
    agg, seen = {}, set()
    for t in trades:
        # A trade that sits on a tier boundary can come back from both tiers.
        tid = t.get("trade_id")
        if tid:
            if tid in seen:
                continue
            seen.add(tid)
        f = T.trade_fields(t)
        if not f or f[0] != ticker:
            continue
        _, ts, side, price, count = f
        if ts > close_ts:
            continue
        ttc = T.bucket(TTC_EDGES, TTC_NAMES, max(0.0, close_ts - ts))
        band = T.bucket(T.BAND_EDGES, T.BAND_NAMES, price)
        if ttc is None or band is None:
            continue
        a = agg.setdefault((ttc, band, side), [0.0, 0.0, 0.0, 0])
        a[0] += count; a[1] += count * price
        a[2] += count if side == result else 0.0
        a[3] += 1
    for (ttc, band, side), (ct, paid, won, n) in agg.items():
        c.execute("INSERT OR REPLACE INTO cell VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (series, ticker, day, ttc, band, side, ct, paid, won, n))
    c.execute("UPDATE market SET done=1, trades=?, pages=? WHERE ticker=?",
              (len(seen) or len(trades), pages, ticker))
    c.commit()


def load_fees(c, series_list, log):
    for s in series_list:
        if c.execute("SELECT 1 FROM series WHERE ticker=?", (s,)).fetchone():
            continue
        d, err = T.get(f"/series/{urllib.parse.quote(s)}")
        if not d or not d.get("series"):
            raise RuntimeError(f"series {s} unreadable ({err}); refusing to guess its fee type")
        sr = d["series"]
        c.execute("INSERT OR REPLACE INTO series VALUES(?,?,?)",
                  (s, sr.get("category"), sr.get("fee_type")))
        log(f"{s}: category {sr.get('category')}, fee_type {sr.get('fee_type')}")
    c.commit()


def run(series_list, days, budget_min, dbpath, log=print):
    start = time.time()
    c = db(dbpath)
    load_fees(c, series_list, log)
    p, err = T.get("/historical/cutoff")
    if not p:
        raise RuntimeError(f"cutoff unreadable ({err}); cannot route tiers")
    log(f"cutoff: {p}")
    mc = T.utc(p.get("market_settled_ts")); tc = T.utc(p.get("trades_created_ts"))
    # End a day back so everything in the window has settled.
    hi = math.floor(time.time() / 3600) * 3600 - 86400
    lo = hi - days * 86400
    for s in series_list:
        row = c.execute("SELECT lo, hi FROM listed WHERE series=?", (s,)).fetchone()
        if row and row[0] <= lo + 3600 and row[1] >= hi - 3600:
            continue   # listed on a previous run of this window
        n, u = list_markets(c, s, lo, hi, mc, log)
        log(f"{s}: {n} settled markets, {u} without a yes/no result (skipped)")
    todo = c.execute("SELECT ticker, series, close_ts, result FROM market WHERE done=0 "
                     "ORDER BY close_ts DESC").fetchall()
    log(f"{len(todo)} markets to read")
    pool = ThreadPoolExecutor(max_workers=8)
    futs, i, nd, failed = [], 0, 0, 0
    meta = {r[0]: r for r in todo}
    while i < len(todo) or futs:
        while i < len(todo) and len(futs) < 16 and time.time() - start < budget_min * 60:
            tk, _, close_ts, _ = todo[i]
            futs.append(pool.submit(fetch_market, tk, close_ts, tc, log)); i += 1
        if not futs:
            log("budget reached; stopping (a re-run resumes here)")
            break
        tk, trades, pages = futs.pop(0).result()
        if trades is None:
            failed += 1
            continue
        _, series, close_ts, result = meta[tk]
        aggregate(c, tk, series, close_ts, result, trades, pages)
        nd += 1
        if nd % 500 == 0:
            log(f"{nd} markets read in {(time.time()-start)/60:.1f} min, {failed} failed")
    pool.shutdown(wait=False)
    left = c.execute("SELECT COUNT(*) FROM market WHERE done=0").fetchone()[0]
    log(f"done this run: {nd} read, {failed} failed (retried on resume), {left} still unread")
    return left


def coverage(c):
    """Per series: markets listed, read, trades, closes off the grid."""
    out = []
    for s, in c.execute("SELECT DISTINCT series FROM market ORDER BY series").fetchall():
        n, done, tr = c.execute("SELECT COUNT(*), SUM(done), SUM(COALESCE(trades,0)) "
                                "FROM market WHERE series=?", (s,)).fetchone()
        g = grid_of(s)
        off = 0
        if g:
            off = sum(1 for (ct,) in c.execute("SELECT close_ts FROM market WHERE series=?", (s,))
                      if ct is not None and int(round(ct)) % g != 0)
        days = c.execute("SELECT COUNT(DISTINCT date(close_ts, 'unixepoch')) FROM market "
                         "WHERE series=? AND done=1", (s,)).fetchone()[0]
        out.append((s, n, done or 0, tr or 0, off, g, days))
    return out


def export(dbpath, path):
    """Same CSV shape as taker_flow_screen.export, so the round-1 scorer reads it."""
    c = db(dbpath)
    with gzip.open(path, "wt") as f:
        f.write("series,category,fee_type,day,ttc,band,taker_side,contracts,paid,taker_won,"
                "trades,events,loss_events\n")
        for r in c.execute("""SELECT cell.series, s.category, s.fee_type, cell.day, cell.ttc, cell.band,
                cell.taker_side, SUM(cell.contracts), SUM(cell.paid), SUM(cell.taker_won),
                SUM(cell.trades), COUNT(*),
                SUM(CASE WHEN cell.taker_won > 0 THEN 1 ELSE 0 END) FROM cell
                JOIN market m ON m.ticker = cell.event AND m.done = 1
                LEFT JOIN series s ON s.ticker = cell.series
                GROUP BY cell.series, cell.day, cell.ttc, cell.band, cell.taker_side"""):
            f.write(",".join("" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))
                             for v in r) + "\n")
    cov = coverage(c)
    with open(path.replace(".csv.gz", "-coverage.txt"), "w") as f:
        f.write("series markets read trades off_grid_closes grid_s days_read\n")
        for r in cov:
            f.write(" ".join(str(x) for x in r) + "\n")
    return cov


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="")
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--budget-min", type=float, default=300)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--export")
    a = ap.parse_args()
    dbpath = os.path.join(HERE, f"ladder_{a.tag}.db")
    if a.export:
        for r in export(dbpath, a.export):
            print(*r)
        sys.exit(0)
    series_list = [s.strip() for s in a.series.split(",") if s.strip()]
    left = run(series_list, a.days, a.budget_min, dbpath, log=lambda s: print(s, flush=True))
    sys.exit(0)
