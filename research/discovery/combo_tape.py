"""Combo (parlay) trade tape: who earns on Kalshi's KXMVE combos? (round-2 idea #26)

Combos trade by RFQ. A requester asks, quoters (makers) answer with a YES and a
NO price, and the requester accepts one side. The trade record names the side
the requester ACQUIRED (`taker_outcome_side`) and the price paid, so the
quoter's result per contract is `paid - won`, the same tape arithmetic as
round 1. Two trades are measured:

  sell the parlay:  quote combos and be filled when the requester buys YES;
                    the quoter holds NO. Upper bound on what a NEW quoter earns,
                    because a quote only fills when it beats the others.
  buy NO by RFQ:    be the requester and accept the NO side; result to us is
                    `won - paid - taker fee`.

Fees, from Kalshi's fee schedule (effective 2026-07-07), row "KXMVE | Combos
(excluding uncorrelated NFL combos) | maker 2 | taker 1": maker
ceil(0.035 x C x P x (1-P)), taker ceil(0.07 x C x P x (1-P)), rounded up per
order. The series' own fee_type decides which applies (`quadratic` = the
uncorrelated-NFL collection, no maker fee). Each trade's fee is rounded up on
its own: conservative, because the per-order accumulator rebates some of it.

Sampling: hours of tape across a 28-day window that ends 3 days back, so
multi-day combos have settled; the stride is coprime with 24 so every hour of
the day is visited equally. Combos run to ~100k markets an hour, so tickers are
kept by a deterministic hash (`--keep-pct`) to bound the metadata reads; the
trade pages are read in full either way. Unsettled markets are counted, never
graded.

Read-only and unauthenticated, through taker_flow_screen.get (whitelisted paths,
429 retried). Run: python3 -I combo_tape.py --tag combo-1 --shard 0/2
"""
import argparse, json, math, os, sys, time, zlib, gzip
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import taker_flow_screen as T  # noqa: E402

MAKER_K, TAKER_K = 0.035, 0.07
STRIDE = 13          # coprime with 24: ~52 hours over 28 days
SETTLE_LAG_D = 3


def is_combo(t):
    return bool(t) and t.startswith("KXMVE")


def kept(ticker, pct):
    return zlib.crc32(ticker.encode()) % 100 < pct


def fee_ceil(k, count, price):
    return math.ceil(k * count * price * (1 - price) * 100 - 1e-9) / 100.0


def db(path):
    c = T.db(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS ccell(series TEXT, ticker TEXT, day TEXT, band TEXT, taker_side TEXT,
        contracts REAL, paid REAL, won REAL, mfee REAL, tfee REAL, trades INTEGER,
        PRIMARY KEY(series, ticker, day, band, taker_side));
    CREATE TABLE IF NOT EXISTS cfee(series TEXT PRIMARY KEY, fee_type TEXT, fee_multiplier REAL, raw TEXT);
    CREATE TABLE IF NOT EXISTS hour_extra(hour INTEGER PRIMARY KEY, combo_trades INTEGER,
        sampled INTEGER, unsettled INTEGER, other INTEGER);
    """)
    return c


def fetch_hour(hour, cutoff_ts, pct, log):
    """{(ticker, side, price): [count, n, mfee_ceil, tfee_ceil, last_ts]} for sampled combo trades."""
    path = "/markets/trades" if (cutoff_ts is None or hour >= cutoff_ts) else "/historical/trades"
    comp, cursor, pages, combo, other = {}, None, 0, 0, 0
    while True:
        q = {"limit": 1000, "min_ts": int(hour), "max_ts": int(hour + 3599)}
        if cursor:
            q["cursor"] = cursor
        p, err = T.get(path, q)
        if err:
            log(f"{path} {hour}: {err} after {pages} pages")
            return hour, None, pages, combo, other
        pages += 1
        rows = p.get("trades") or []
        for t in rows:
            f = T.trade_fields(t)
            if f is None:
                continue
            tk, ts, side, price, count = f
            if not is_combo(tk):
                other += 1
                continue
            combo += 1
            if not kept(tk, pct):
                continue
            a = comp.setdefault((tk, side, price), [0.0, 0, 0.0, 0.0, 0.0])
            a[0] += count; a[1] += 1
            a[2] += fee_ceil(MAKER_K, count, price); a[3] += fee_ceil(TAKER_K, count, price)
            a[4] = max(a[4], ts)
        cursor = p.get("cursor")
        if not cursor or not rows:
            break
    return hour, comp, pages, combo, other


def aggregate(c, hour, comp, pages, combo, other, markets):
    agg, sampled, unsettled = {}, 0, 0
    for (tk, side, price), (count, n, mf, tf, ts) in comp.items():
        m = markets.get(tk)
        if not m or m[3] is None:
            unsettled += n
            continue
        event, series, close_ts, result = m
        if close_ts is not None and ts > close_ts:
            continue
        band = T.bucket(T.BAND_EDGES, T.BAND_NAMES, price)
        if band is None:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(close_ts or ts))
        a = agg.setdefault((series, tk, day, band, side), [0.0, 0.0, 0.0, 0.0, 0.0, 0])
        a[0] += count; a[1] += count * price; a[2] += count if side == result else 0.0
        a[3] += mf; a[4] += tf; a[5] += n
        sampled += n
    for k, v in agg.items():
        c.execute("""INSERT INTO ccell VALUES(?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(series,ticker,day,band,taker_side) DO UPDATE SET
            contracts=contracts+excluded.contracts, paid=paid+excluded.paid, won=won+excluded.won,
            mfee=mfee+excluded.mfee, tfee=tfee+excluded.tfee, trades=trades+excluded.trades""",
                  k + tuple(v))
    c.execute("INSERT OR REPLACE INTO hour_extra VALUES(?,?,?,?,?)",
              (hour, combo, sampled, unsettled, other))
    c.execute("INSERT OR REPLACE INTO hour_done VALUES(?,?,?,?,?)",
              (hour, combo + other, sampled, unsettled, pages))
    c.commit()
    return sampled, unsettled


def load_fees(c, series, log):
    have = {r[0] for r in c.execute("SELECT series FROM cfee")}
    for s in sorted(set(series) - have):
        p, err = T.get(f"/series/{s}")
        if not p or not p.get("series"):
            raise RuntimeError(f"/series/{s} unreadable: {err}")
        x = p["series"]
        log(f"series {s}: fee_type={x.get('fee_type')} fee_multiplier={x.get('fee_multiplier')}")
        c.execute("INSERT OR REPLACE INTO cfee VALUES(?,?,?,?)",
                  (s, x.get("fee_type"), T.num(x, "fee_multiplier"), json.dumps(x)[:2000]))
    c.commit()


def hours_for(now, days, shard, nshards, done=()):
    t_end = math.floor(now / 3600) * 3600 - SETTLE_LAG_D * 86400
    t_start = t_end - days * 86400
    hs = [h for h in range(int(t_end) - 3600, int(t_start) - 1, -3600) if (h // 3600) % STRIDE == 0]
    return t_start, t_end, [h for i, h in enumerate(hs) if i % nshards == shard and h not in done]


def run(path, days, budget_min, pct, shard, nshards, log=print):
    start = time.time()
    c = db(path)
    p, err = T.get("/historical/cutoff")
    cutoff_ts = T.utc(p.get("trades_created_ts")) if p and isinstance(p.get("trades_created_ts"), str) else None
    log(f"cutoff: {json.dumps(p) if p else err}")
    done = {r[0] for r in c.execute("SELECT hour FROM hour_done")}
    t0, t1, hours = hours_for(time.time(), days, shard, nshards, done)
    log(f"window {time.strftime('%Y-%m-%dT%H', time.gmtime(t0))}..{time.strftime('%Y-%m-%dT%H', time.gmtime(t1))}"
        f", shard {shard}/{nshards}, {len(hours)} hours to read, {len(done)} done, keep {pct}% of tickers")
    markets = {r[0]: r[1:] for r in c.execute("SELECT ticker, event, series, close_ts, result FROM market")}
    pool = ThreadPoolExecutor(max_workers=8)
    hp = ThreadPoolExecutor(max_workers=2)
    futs, i, nd, raw_logged = [], 0, 0, False
    while i < len(hours) or futs:
        while i < len(hours) and len(futs) < 2 and time.time() - start < budget_min * 60:
            futs.append(hp.submit(fetch_hour, hours[i], cutoff_ts, pct, log)); i += 1
        if not futs:
            log("budget reached; stopping (a re-run resumes here)")
            break
        hour, comp, pages, combo, other = futs.pop(0).result()
        if comp is None:
            continue
        tickers = {k[0] for k in comp}
        T.resolve(c, tickers, markets, log, pool=pool)
        load_fees(c, {markets[t][1] for t in tickers if markets.get(t) and markets[t][1]}, log)
        if not raw_logged and tickers:
            # Source check: one market and its terms verbatim, so the fields are read, not assumed.
            t = sorted(tickers)[0]
            mp, _ = T.get(f"/markets/{t}")
            m = (mp or {}).get("market") or {}
            log("raw market sample: " + json.dumps({k: m.get(k) for k in (
                "ticker", "event_ticker", "status", "result", "close_time", "yes_bid_dollars",
                "yes_ask_dollars", "volume_fp", "mve_collection_ticker", "mve_selected_legs")})[:1500])
            raw_logged = True
        s, u = aggregate(c, hour, comp, pages, combo, other, markets)
        nd += 1
        log(f"{time.strftime('%Y-%m-%dT%H', time.gmtime(hour))}: {pages} pages, {combo} combo trades "
            f"({other} other), {s} sampled+settled, {u} unsettled; {nd} hours in "
            f"{(time.time() - start) / 60:.1f} min")


def export(path, out):
    c = db(path)
    with gzip.open(out, "wt") as f:
        f.write("series,fee_type,day,band,taker_side,contracts,paid,won,mfee,tfee,trades,markets,loss_markets\n")
        for r in c.execute("""SELECT ccell.series, cfee.fee_type, day, band, taker_side, SUM(contracts),
                SUM(paid), SUM(won), SUM(mfee), SUM(tfee), SUM(trades), COUNT(*), SUM(won > 0)
                FROM ccell LEFT JOIN cfee ON cfee.series = ccell.series
                GROUP BY ccell.series, day, band, taker_side"""):
            f.write(",".join("" if v is None else (f"{v:.6f}" if isinstance(v, float) else str(v))
                             for v in r) + "\n")
    tot = c.execute("SELECT COUNT(*), SUM(combo_trades), SUM(sampled), SUM(unsettled), SUM(other) "
                    "FROM hour_extra").fetchone()
    with open(out.replace(".csv.gz", "-coverage.txt"), "w") as f:
        f.write(f"hours {tot[0]} combo_trades {tot[1]} sampled_settled {tot[2]} "
                f"sampled_unsettled {tot[3]} other_trades {tot[4]}\n")
        for r in c.execute("SELECT series, fee_type, fee_multiplier FROM cfee ORDER BY series"):
            f.write(f"fee {r[0]} {r[1]} {r[2]}\n")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--budget-min", type=float, default=300)
    ap.add_argument("--keep-pct", type=int, default=25)
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--export")
    a = ap.parse_args(argv)
    path = os.path.join(HERE, f"combo_{a.tag}.db")
    if a.export:
        export(path, a.export); return
    sh, n = (int(x) for x in a.shard.split("/"))
    run(path, a.days, a.budget_min, a.keep_pct, sh, n)


if __name__ == "__main__":
    main()
