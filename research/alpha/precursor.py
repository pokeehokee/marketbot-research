"""Pull the public trade tape for a precursor screen, and export feature rows.

Alpha research, round 1 (2026-11-27). Instead of testing one edge idea at a time,
build features from what the tape showed before a decision time and screen them
against what happened after, on a training slice, with a locked holdout
(screen.py). This file only reads and stores.

Reuses the ladder-tape pull (research/discovery/ladder_tape.py), which already
does the hard parts correctly and is tested: both live and historical tiers, a
paged read followed to the end, a failed read retried on resume and never taken
as "no trades", settled yes/no results only, and the read-only whitelist.

Differences from the ladder pull:
  * Raw trades are stored, not aggregated, so features can be recomputed
    without re-reading the exchange.
  * A deterministic 1-in-K sample per series (crc32 of the ticker), because the
    BTC hourly ladder alone lists ~120k markets a month.
  * A 4-hour lookback, so the 120-minute decision time still has an hour of
    history behind it.

Run:  python3 -I precursor.py --series KXBTCD --sample 6 --days 28 --budget-min 300 --tag x
      python3 -I precursor.py --export out/alpha-x.csv.gz --tag x
"""
import argparse, gzip, os, sqlite3, sys, time, zlib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "discovery"))
sys.path.insert(0, HERE)
import ladder_tape as L          # noqa: E402
import taker_flow_screen as T    # noqa: E402
import features as F             # noqa: E402

L.LOOKBACK_S = 4 * 3600          # fetch_market reads this module global at call time


def db(path):
    c = L.db(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS trade(ticker TEXT, ts REAL, side TEXT, yes_price REAL,
        count REAL, trade_id TEXT, PRIMARY KEY(ticker, trade_id));
    CREATE TABLE IF NOT EXISTS sampled(series TEXT PRIMARY KEY, k INTEGER);
    """)
    return c


def keep(ticker, k):
    return k <= 1 or zlib.crc32(ticker.encode()) % k == 0


def store(c, ticker, trades, pages):
    n = 0
    for t in trades:
        f = T.trade_fields(t)
        if not f or f[0] != ticker:
            continue
        _, ts, side, price, count = f
        tid = t.get("trade_id") or f"{ts}-{side}-{price}-{count}"
        c.execute("INSERT OR IGNORE INTO trade VALUES(?,?,?,?,?,?)",
                  (ticker, ts, side, F.yes_price(side, price), count, tid))
        n += 1
    c.execute("UPDATE market SET done=1, trades=?, pages=? WHERE ticker=?", (n, pages, ticker))
    c.commit()


def run(series_list, k, days, budget_min, dbpath, log=print):
    from concurrent.futures import ThreadPoolExecutor
    import math
    start = time.time()
    c = db(dbpath)
    L.load_fees(c, series_list, log)
    p, err = T.get("/historical/cutoff")
    if not p:
        raise RuntimeError(f"cutoff unreadable ({err}); cannot route tiers")
    mc = T.utc(p.get("market_settled_ts")); tc = T.utc(p.get("trades_created_ts"))
    hi = math.floor(time.time() / 3600) * 3600 - 86400
    lo = hi - days * 86400
    for s in series_list:
        row = c.execute("SELECT lo, hi FROM listed WHERE series=?", (s,)).fetchone()
        if not (row and row[0] <= lo + 3600 and row[1] >= hi - 3600):
            n, u = L.list_markets(c, s, lo, hi, mc, log)
            log(f"{s}: {n} settled markets, {u} without a yes/no result (skipped)")
        c.execute("INSERT OR REPLACE INTO sampled VALUES(?,?)", (s, k))
        # Unsampled markets are marked done with trades NULL so coverage tells them apart.
        for (tk,) in c.execute("SELECT ticker FROM market WHERE series=? AND done=0",
                               (s,)).fetchall():
            if not keep(tk, k):
                c.execute("UPDATE market SET done=2 WHERE ticker=?", (tk,))
        c.commit()
    todo = c.execute("SELECT ticker, close_ts FROM market WHERE done=0 ORDER BY close_ts DESC"
                     ).fetchall()
    log(f"{len(todo)} sampled markets to read")
    pool = ThreadPoolExecutor(max_workers=8)
    futs, i, nd, failed = [], 0, 0, 0
    while i < len(todo) or futs:
        while i < len(todo) and len(futs) < 16 and time.time() - start < budget_min * 60:
            tk, close_ts = todo[i]
            futs.append(pool.submit(L.fetch_market, tk, close_ts, tc, log)); i += 1
        if not futs:
            log("budget reached; stopping (a re-run resumes here)")
            break
        tk, trades, pages = futs.pop(0).result()
        if trades is None:
            failed += 1
            continue
        store(c, tk, trades, pages)
        nd += 1
        if nd % 500 == 0:
            log(f"{nd} markets read in {(time.time()-start)/60:.1f} min, {failed} failed")
    pool.shutdown(wait=False)
    left = c.execute("SELECT COUNT(*) FROM market WHERE done=0").fetchone()[0]
    log(f"done this run: {nd} read, {failed} failed (retried on resume), {left} still unread")
    return left


COLS = ("series", "category", "ticker", "day", "close_ts", "offset_min", "p", "p_last", "y",
        "move") + F.FEATURES


def export(dbpath, path):
    c = db(dbpath)
    cats = dict(c.execute("SELECT ticker, category FROM series").fetchall())
    n = 0
    with gzip.open(path, "wt") as f:
        f.write(",".join(COLS) + "\n")
        for tk, series, close_ts, result in c.execute(
                "SELECT ticker, series, close_ts, result FROM market WHERE done=1 "
                "ORDER BY close_ts").fetchall():
            trades = c.execute("SELECT ts, side, yes_price, count FROM trade WHERE ticker=?",
                               (tk,)).fetchall()
            day = time.strftime("%Y-%m-%d", time.gmtime(close_ts))
            for r in F.rows_for_market(trades, close_ts, result):
                r.update(series=series, category=cats.get(series) or "", ticker=tk, day=day,
                         close_ts=int(close_ts))
                f.write(",".join("" if r[k] is None else
                                 (f"{r[k]:.5g}" if isinstance(r[k], float) else str(r[k]))
                                 for k in COLS) + "\n")
                n += 1
    cov = []
    for s, k in c.execute("SELECT series, k FROM sampled ORDER BY series").fetchall():
        listed, read, unread, tr = c.execute(
            "SELECT COUNT(*), SUM(done=1), SUM(done=0), SUM(COALESCE(trades,0)) FROM market "
            "WHERE series=?", (s,)).fetchone()
        days = c.execute("SELECT COUNT(DISTINCT date(close_ts,'unixepoch')) FROM market "
                         "WHERE series=? AND done=1", (s,)).fetchone()[0]
        cov.append((s, k, listed, read or 0, unread or 0, tr or 0, days))
    with open(path.replace(".csv.gz", "-coverage.txt"), "w") as f:
        f.write(f"rows {n}\nseries sample_1_in listed read unread trades days_read\n")
        for r in cov:
            f.write(" ".join(str(x) for x in r) + "\n")
    return n, cov


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="")
    ap.add_argument("--sample", type=int, default=1)
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--budget-min", type=float, default=300)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--export")
    a = ap.parse_args()
    dbpath = os.path.join(HERE, f"alpha_{a.tag}.db")
    if a.export:
        n, cov = export(dbpath, a.export)
        print("rows", n)
        for r in cov:
            print(*r)
        sys.exit(0)
    series_list = [s.strip() for s in a.series.split(",") if s.strip()]
    run(series_list, a.sample, a.days, a.budget_min, dbpath, log=lambda s: print(s, flush=True))
