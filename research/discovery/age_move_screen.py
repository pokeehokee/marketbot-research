"""Ideas #13 (fading overreactions) and #20 (getting into new listings first),
measured on Kalshi's public trade tape the same way as taker_flow_screen.py.

Read-only and unauthenticated; it reuses that screen's whitelisted GET, its
trade parser and its hour fetcher, and keeps its own database.

Per trade, two things the other screen did not record:

  * AGE: seconds since the market's own `open_time` (known in advance, so no
    lookahead). #20 asks whether the first trades after a listing pay a maker
    or a taker more than later ones.
  * CHASE: what this taker paid minus the volume-weighted price that takers of
    the SAME side paid 5-15 minutes EARLIER on the same market, in cents of the
    acquired side. Positive means the taker is buying something that just got
    dearer (chasing a move); negative means buying something that just got
    cheaper (fading it). Comparing like with like (yes-takers to yes-takers)
    keeps the bid-ask bounce out: a yes taker always pays the ask, so measuring
    against all trades would read half the spread as a "move". Only trades
    strictly before the current one are used.

Both are scored as the maker's P&L held to settlement, by settle-day, exactly
as score_taker_flow.py does; the taker's side is that number's negative less
the taker fee.
"""
import argparse, gzip, os, sqlite3, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taker_flow_screen as T  # noqa: E402
from concurrent.futures import ThreadPoolExecutor  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "age_move.db")

AGE_EDGES = [0, 600, 3600, 6 * 3600, 86400, 3 * 86400, 10 ** 9]
AGE_NAMES = ["<10m", "10-60m", "1-6h", "6-24h", "1-3d", "3d+"]
CHASE_EDGES = [-1.01, -0.10, -0.04, 0.04, 0.10, 1.01]
CHASE_NAMES = ["fade>=10", "fade4-10", "flat", "chase4-10", "chase>=10"]
REF_LO, REF_HI = 900, 300   # reference window: [t-900s, t-300s]


def db(path=DB):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS market(ticker TEXT PRIMARY KEY, event TEXT, series TEXT,
        open_ts REAL, close_ts REAL, result TEXT);
    CREATE TABLE IF NOT EXISTS series(ticker TEXT PRIMARY KEY, category TEXT, fee_type TEXT,
        frequency TEXT, fee_multiplier REAL);
    CREATE TABLE IF NOT EXISTS cell(kind TEXT, series TEXT, event TEXT, day TEXT, bucket TEXT,
        band TEXT, taker_side TEXT, contracts REAL, paid REAL, taker_won REAL, trades INTEGER,
        PRIMARY KEY(kind, series, event, day, bucket, band, taker_side));
    CREATE TABLE IF NOT EXISTS hour_done(hour INTEGER PRIMARY KEY, trades INTEGER,
        kept INTEGER, unknown INTEGER, pages INTEGER);
    """)
    return c


def conv(m):
    res = (m.get("result") or "").lower()
    ev = m.get("event_ticker")
    return (ev, T.series_of(ev), T.utc(m.get("open_time")), T.utc(m.get("close_time")),
            res if res in ("yes", "no") else None)


ABSENT = (None, None, None, None, None)


def resolve(c, tickers, markets, log, skip, pool=None):
    """As T.resolve, plus open_time. A returned ticker that was not asked for is
    discarded; a requested one the batch missed is retried alone."""
    todo = [t for t in tickers if t not in markets]
    rows = {t: ABSENT for t in todo if skip(t)}
    todo = [t for t in todo if not skip(t)]

    def one_batch(batch):
        p, err = T.get("/markets", {"tickers": ",".join(batch), "limit": 1000})
        got = {}
        if p:
            for m in p.get("markets") or []:
                if m.get("ticker") in batch:
                    got[m["ticker"]] = conv(m)
        else:
            log(f"resolve batch: {err}")
        return got, [t for t in batch if t not in got]

    def one(t):
        import urllib.parse
        p, err = T.get(f"/markets/{urllib.parse.quote(t)}")
        return t, (conv(p["market"]) if p and p.get("market") else ABSENT)

    batches = [todo[i:i + T.RESOLVE_BATCH] for i in range(0, len(todo), T.RESOLVE_BATCH)]
    mapper = pool.map if pool else map
    missing = []
    for got, miss in mapper(one_batch, batches):
        rows.update(got); missing += miss
    for t, row in mapper(one, missing):
        rows[t] = row
    for t, row in rows.items():
        markets[t] = row
        c.execute("INSERT OR REPLACE INTO market VALUES(?,?,?,?,?,?)", (t,) + row)
    c.commit()
    return len(todo), len(missing)


def chase_of(path, ts, side):
    """path: [(ts, side, price, count)] of ONE market, any order. The price this
    side's takers paid in [ts-900, ts-300], volume-weighted; None if none."""
    ct = px = 0.0
    for t, s, p, n in path:
        if s == side and ts - REF_LO <= t <= ts - REF_HI:
            ct += n; px += n * p
    return (px / ct) if ct > 0 else None


def aggregate_hour(c, hour, comp, pages, trades, markets):
    """comp: {(ticker, side, price, ts//10): (contracts, n_trades)} from T.fetch_hour."""
    by_tk = {}
    for (tk, side, price, t10), (count, n) in comp.items():
        by_tk.setdefault(tk, []).append((t10 * 10 + 5, side, price, count, n))
    agg = {}
    kept = unknown = 0
    for tk, rows in by_tk.items():
        m = markets.get(tk)
        if not m or m[4] is None:
            unknown += sum(r[4] for r in rows)
            continue
        event, series, open_ts, close_ts, result = m
        path = [(r[0], r[1], r[2], r[3]) for r in rows]
        for ts, side, price, count, n in rows:
            if close_ts is None or ts > close_ts:
                continue
            band = T.bucket(T.BAND_EDGES, T.BAND_NAMES, price)
            if band is None:
                continue
            day = time.strftime("%Y-%m-%d", time.gmtime(close_ts))
            won = count if side == result else 0.0
            keys = []
            if open_ts is not None and ts >= open_ts:
                keys.append(("age", T.bucket(AGE_EDGES, AGE_NAMES, ts - open_ts)))
            ref = chase_of(path, ts, side)
            if ref is not None:
                keys.append(("move", T.bucket(CHASE_EDGES, CHASE_NAMES, price - ref)))
            for kind, b in keys:
                if b is None:
                    continue
                k = (kind, series, event, day, b, band, side)
                a = agg.setdefault(k, [0.0, 0.0, 0.0, 0])
                a[0] += count; a[1] += count * price; a[2] += won; a[3] += n
            kept += n
    for k, (ct, paid, won, n) in agg.items():
        c.execute("""INSERT INTO cell VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(kind,series,event,day,bucket,band,taker_side) DO UPDATE SET
            contracts=contracts+excluded.contracts, paid=paid+excluded.paid,
            taker_won=taker_won+excluded.taker_won, trades=trades+excluded.trades""",
                  k + (ct, paid, won, n))
    c.execute("INSERT OR REPLACE INTO hour_done VALUES(?,?,?,?,?)",
              (hour, trades, kept, unknown, pages))
    c.commit()
    return trades, kept, unknown, pages


def sample(days, budget_min, stride=5, log=print):
    import math
    start = time.time()
    c = db()
    if not c.execute("SELECT 1 FROM series LIMIT 1").fetchone():
        T.load_series(c, log)
    p, err = T.get("/historical/cutoff")
    cutoff_ts = None
    if p:
        cutoff_ts = T.utc(p.get("trades_created_ts")) if isinstance(p.get("trades_created_ts"), str) \
            else T.num(p, "trades_created_ts")
    else:
        log(f"cutoff unreadable: {err}")
    t_end = math.floor(time.time() / 3600) * 3600 - 86400
    t_start = t_end - days * 86400
    markets = {r[0]: r[1:] for r in c.execute(
        "SELECT ticker, event, series, open_ts, close_ts, result FROM market")}
    fees = {r[0]: (r[1] or "") for r in c.execute("SELECT ticker, fee_type FROM series")}
    skip = lambda t: t.startswith("KXMVE") or "combo" in fees.get(t.split("-", 1)[0], "")
    done = {r[0] for r in c.execute("SELECT hour FROM hour_done")}
    # The same systematic hour sample as the other screen (stride coprime with
    # 24), offset by 2 so it reads DIFFERENT hours: a second, independent draw.
    hours = [h for h in range(int(t_end), int(t_start) - 1, -3600)
             if ((h // 3600) % stride == 2) and h not in done]
    log(f"window {time.strftime('%Y-%m-%dT%H', time.gmtime(t_start))}.."
        f"{time.strftime('%Y-%m-%dT%H', time.gmtime(t_end))}, {len(hours)} hours to fetch")
    pool = ThreadPoolExecutor(max_workers=8)
    hp = ThreadPoolExecutor(max_workers=4)
    futs, i, nd = [], 0, 0
    while i < len(hours) or futs:
        while i < len(hours) and len(futs) < 4 and time.time() - start < budget_min * 60:
            futs.append(hp.submit(T.fetch_hour, hours[i], cutoff_ts, log)); i += 1
        if not futs:
            log("budget reached; stopping (resume picks up here)")
            break
        hour, comp, pages, trades = futs.pop(0).result()
        if comp is None:
            continue
        resolve(c, {k[0] for k in comp}, markets, log, skip, pool)
        r = aggregate_hour(c, hour, comp, pages, trades, markets)
        nd += 1
        if nd <= 3 or nd % 10 == 0:
            log(f"{time.strftime('%Y-%m-%dT%H', time.gmtime(hour))}: {r[0]} trades, {r[1]} kept, "
                f"{r[2]} unknown; {nd} hours in {(time.time()-start)/60:.1f} min")
    n = c.execute("SELECT COUNT(*), SUM(trades), SUM(kept), SUM(unknown) FROM hour_done").fetchone()
    log(f"hours done {n[0]}, trades {n[1]}, kept {n[2]}, unknown-market {n[3]}")
    # Source check: how many traded markets carried an open_time at all.
    tot, has_open = c.execute("SELECT COUNT(*), SUM(open_ts IS NOT NULL) FROM market "
                              "WHERE result IS NOT NULL").fetchone()
    log(f"source check: {has_open} of {tot} settled markets carry open_time")


def export(path, kind):
    """The aggregate for one kind as the CSV shape score_taker_flow.load reads,
    with the bucket in the `ttc` column."""
    c = db()
    with gzip.open(path, "wt") as f:
        f.write("series,category,fee_type,day,ttc,band,taker_side,contracts,paid,taker_won,"
                "trades,events,loss_events\n")
        for r in c.execute("""SELECT cell.series, s.category, s.fee_type, day, bucket, band,
                taker_side, SUM(contracts), SUM(paid), SUM(taker_won), SUM(trades), COUNT(*),
                SUM(CASE WHEN taker_won > 0 THEN 1 ELSE 0 END) FROM cell
                LEFT JOIN series s ON s.ticker = cell.series WHERE kind = ?
                GROUP BY cell.series, day, bucket, band, taker_side""", (kind,)):
            f.write(",".join("" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))
                             for v in r) + "\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--budget-min", type=float, default=280)
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--export", help="write <prefix>-age.csv.gz and <prefix>-move.csv.gz, exit")
    a = ap.parse_args()
    if a.export:
        export(a.export + "-age.csv.gz", "age")
        export(a.export + "-move.csv.gz", "move")
        sys.exit(0)
    sample(a.days, a.budget_min, a.stride, log=lambda s: print(s, flush=True))
