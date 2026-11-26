"""Round-2 idea #44, the COST half: do liquidity-pool rewards outweigh being picked off?

lip_scan.py measured what a small resting order would be PAID by Kalshi's liquidity pools
(lip-1: ~$8-9/day on $50 of collateral once one-sided snapshots and the $1 floor are
applied). Resting orders also get FILLED, and a fill by someone who knows more is the cost.
This measures that cost from the tape, on markets whose pools have already closed and
settled.

The quantity, per market m, over the time its pool(s) ran:

    E_m = pool_m x f_s  +  maker_pnl_m  -  maker_fees_m

  pool_m       the pool money attributable to m while it was open: each programme's
               period_reward x (overlap of [start, end) with the market's life) / period.
  f_s          the series' two-sided fraction measured in lip-1 (a snapshot pays only when
               BOTH sides reach Target Size; help.kalshi.com, read 2026-11-25). Conservative
               where the lip-1 evening was thin; reported alongside E with f = 1.
  maker_pnl_m  sum over every trade of (taker price - 1 if the taker's side won), i.e. what
               the resting side of every real fill made, held to settlement.

E_m is what ALL makers on m earned together, net. A small order holding share s of the
qualifying size earns about s x pool and, IF its fills are proportional to its resting
share, s x the fills, so its net is about s x E_m. That proportionality is the assumption
this cannot test: an order at the Reference Price sits behind better levels and is reached
only when a taker sweeps through them, which is the worst flow. A positive E here is
necessary, not sufficient; the forward test is what would settle it.

Pre-registered (LIPCOST_SERIES, from lip-1 reward data only, before any tape was read):
  * the 30 series with the highest two-sided reward yield per collateral dollar;
  * PASS needs total E (with f_s) > 0 with the day-clustered 95% lower bound > 0 on all
    days, H1 and H2 (split at the median settle day), AND >= 30 markets on which makers
    lost money gross (the repo's 30-loss bar, so the losses are actually sampled).

Read-only and unauthenticated. Run:
  python3 -I lip_cost.py --series-file LIPCOST_SERIES --days 28 --budget-min 320 --tag x
  python3 -I lip_cost.py --score --tag x
"""
import argparse, collections, gzip, math, os, random, sqlite3, statistics, sys, time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import taker_flow_screen as T  # noqa: E402
import ladder_tape as L  # noqa: E402
import lip_scan as P  # noqa: E402

OUT = os.path.join(HERE, "out")
LAST_HOUR = 3600


def read_series_file(path):
    out = {}
    for line in open(path):
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        out[parts[0]] = float(parts[1]) if len(parts) > 1 else 1.0
    return out


def db(path):
    c = L.db(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS prog(id TEXT PRIMARY KEY, market TEXT, series TEXT,
        start_ts REAL, end_ts REAL, reward REAL, cap REAL, status TEXT);
    CREATE TABLE IF NOT EXISTS lipm(ticker TEXT PRIMARY KEY, series TEXT, day TEXT,
        lo REAL, close_ts REAL, contracts REAL, maker_pnl REAL, maker_fee REAL, pool REAL,
        last_hour_ct REAL, last_hour_pnl REAL, trades INTEGER);
    """)
    return c


def load_programmes(c, series, log):
    """Every liquidity programme the exchange lists, all statuses, kept for our series."""
    if c.execute("SELECT COUNT(*) FROM prog").fetchone()[0]:
        log("programmes already loaded (resume)")
        return
    n_all = 0
    for status in ("closed", "paid_out", "active"):
        cursor, pages = None, 0
        while True:
            q = {"status": status, "type": "liquidity", "limit": 10000}
            if cursor:
                q["cursor"] = cursor
            p, err = P.get("/incentive_programs", q)
            if err or p is None:
                raise RuntimeError(f"/incentive_programs {status}: {err}; refusing a partial list")
            rows = p.get("incentive_programs") or []
            pages += 1
            n_all += len(rows)
            for r in rows:
                if r.get("incentive_type") != "liquidity":
                    continue
                s = r.get("series_ticker") or ""
                tk = r.get("market_ticker") or ""
                if s not in series and not any(tk.startswith(x + "-") for x in series):
                    continue
                if not s:
                    s = next(x for x in series if tk.startswith(x + "-"))
                c.execute("INSERT OR IGNORE INTO prog VALUES(?,?,?,?,?,?,?,?)",
                          (r["id"], tk, s, T.utc(r.get("start_date")), T.utc(r.get("end_date")),
                           (r.get("period_reward") or 0) / 10000.0,
                           (r.get("max_reward_per_account") or 0) / 10000.0, status))
            cursor = p.get("next_cursor")
            if not cursor or not rows or pages > 500:
                break
        log(f"/incentive_programs status={status}: {pages} pages")
    c.commit()
    k = c.execute("SELECT COUNT(*), COUNT(DISTINCT market) FROM prog").fetchone()
    log(f"{n_all} programmes listed in total; {k[0]} on our series, {k[1]} markets")


def pool_for(progs, open_lo, close_ts):
    """Pool dollars attributable to one market: each programme pro rata over the part of
    its period during which the market was still open."""
    tot = 0.0
    for start, end, reward in progs:
        if start is None or end is None or end <= start:
            continue
        ov = max(0.0, min(end, close_ts) - max(start, open_lo))
        tot += reward * ov / (end - start)
    return tot


def maker_fee(fee_type, count, taker_price):
    """Kalshi maker fee for one fill, rounded up: ceil(0.0175 x C x P x (1-P)) in cents,
    charged only on the `*_maker_fees` fee types. Symmetric in P, so the taker's price
    serves for the maker's."""
    if not fee_type or "maker" not in fee_type:
        return 0.0
    p = taker_price
    return math.ceil(0.0175 * count * p * (1 - p) * 100 - 1e-9) / 100.0


def fetch(ticker, lo, close_ts, trade_cutoff, log):
    tiers = []
    if trade_cutoff is None or close_ts >= trade_cutoff:
        tiers.append(("/markets/trades", max(lo, trade_cutoff or lo)))
    if trade_cutoff is not None and lo < trade_cutoff:
        tiers.append(("/historical/trades", lo))
    trades, pages = [], 0
    for path, start in tiers:
        rows, n = L.paged(path, {"ticker": ticker, "min_ts": int(start),
                                 "max_ts": int(close_ts) + 1, "limit": 1000}, log, "trades")
        pages += n
        if rows is None:
            return ticker, None
        trades += rows
    return ticker, trades


def aggregate(ticker, trades, result, close_ts, fee_type):
    """(contracts, maker_pnl, maker_fee, last_hour_ct, last_hour_pnl, n) for one market."""
    seen = set()
    ct = pnl = fee = lh_ct = lh_pnl = 0.0
    n = 0
    for t in trades:
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
        m = count * (price - (1.0 if side == result else 0.0))
        ct += count; pnl += m; fee += maker_fee(fee_type, count, price); n += 1
        if close_ts - ts <= LAST_HOUR:
            lh_ct += count; lh_pnl += m
    return ct, pnl, fee, lh_ct, lh_pnl, n


def run(series, days, budget_min, dbpath, log=print):
    start = time.time()
    c = db(dbpath)
    L.load_fees(c, list(series), log)
    load_programmes(c, series, log)
    p, err = T.get("/historical/cutoff")
    if not p:
        raise RuntimeError(f"cutoff unreadable ({err})")
    mc = T.utc(p.get("market_settled_ts")); tc = T.utc(p.get("trades_created_ts"))
    hi = math.floor(time.time() / 3600) * 3600 - 86400
    lo = hi - days * 86400
    for s in series:
        row = c.execute("SELECT lo, hi FROM listed WHERE series=?", (s,)).fetchone()
        if row and row[0] <= lo + 3600 and row[1] >= hi - 3600:
            continue
        n, u = L.list_markets(c, s, lo, hi, mc, log)
        log(f"{s}: {n} settled markets, {u} unresolved (skipped)")
    fees = dict(c.execute("SELECT ticker, fee_type FROM series").fetchall())
    progs = collections.defaultdict(list)
    for mk, st, en, rw in c.execute("SELECT market, start_ts, end_ts, reward FROM prog"):
        progs[mk].append((st, en, rw))
    todo = []
    no_pool = 0
    for tk, s, close_ts, res in c.execute(
            "SELECT ticker, series, close_ts, result FROM market WHERE ticker NOT IN "
            "(SELECT ticker FROM lipm)").fetchall():
        pr = progs.get(tk)
        if not pr:
            no_pool += 1
            continue
        starts = [x[0] for x in pr if x[0] is not None]
        if not starts:
            no_pool += 1
            continue
        mlo = min(starts)
        if mlo >= close_ts:
            no_pool += 1
            continue
        todo.append((tk, s, mlo, close_ts, res))
    log(f"{len(todo)} settled markets with a pool to read; {no_pool} settled markets never had one")
    pool = ThreadPoolExecutor(max_workers=8)
    futs, i, nd, failed = [], 0, 0, 0
    meta = {r[0]: r for r in todo}
    while i < len(todo) or futs:
        while i < len(todo) and len(futs) < 16 and time.time() - start < budget_min * 60:
            tk, s, mlo, close_ts, res = todo[i]
            futs.append(pool.submit(fetch, tk, mlo, close_ts, tc, log)); i += 1
        if not futs:
            log("budget reached; stopping (a re-run resumes here)")
            break
        tk, trades = futs.pop(0).result()
        if trades is None:
            failed += 1
            continue
        _, s, mlo, close_ts, res = meta[tk]
        ct, pnl, fee, lh_ct, lh_pnl, n = aggregate(tk, trades, res, close_ts, fees.get(s))
        c.execute("INSERT OR REPLACE INTO lipm VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                  (tk, s, time.strftime("%Y-%m-%d", time.gmtime(close_ts)), mlo, close_ts,
                   ct, pnl, fee, pool_for(progs[tk], mlo, close_ts), lh_ct, lh_pnl, n))
        c.commit()
        nd += 1
        if nd % 200 == 0:
            log(f"{nd} markets read in {(time.time() - start) / 60:.1f} min, {failed} failed")
    pool.shutdown(wait=False)
    log(f"done this run: {nd} read, {failed} failed (retried on resume)")


# ---------------------------------------------------------------- scoring

def market_rows(c, f):
    return [dict(ticker=r[0], series=r[1], day=r[2], contracts=r[3], pnl=r[4], fee=r[5],
                 pool=r[6], lh_ct=r[7], lh_pnl=r[8], f=f.get(r[1], 1.0))
            for r in c.execute("SELECT ticker, series, day, contracts, maker_pnl, maker_fee, "
                               "pool, last_hour_ct, last_hour_pnl FROM lipm")]


def e_of(r, use_f=True):
    return r["pool"] * (r["f"] if use_f else 1.0) + r["pnl"] - r["fee"]


def boot(rows, stat, draws=2000, seed=7):
    """Day-clustered bootstrap: resample settle days with replacement."""
    byday = collections.defaultdict(list)
    for r in rows:
        byday[r["day"]].append(r)
    days = sorted(byday)
    if len(days) < 2:
        return stat(rows), None, None
    rng = random.Random(seed)
    vals = []
    for _ in range(draws):
        smp = []
        for _ in days:
            smp += byday[rng.choice(days)]
        vals.append(stat(smp))
    vals.sort()
    return stat(rows), vals[int(.025 * draws)], vals[int(.975 * draws) - 1]


def halves(rows):
    days = sorted({r["day"] for r in rows})
    if len(days) < 2:
        return rows, []
    cut = days[len(days) // 2]
    return [r for r in rows if r["day"] < cut], [r for r in rows if r["day"] >= cut]


def total_e(use_f=True):
    return lambda rs: sum(e_of(r, use_f) for r in rs)


def verdict(rows):
    """PASS iff total E (with f) has a positive lower bound on all, H1 and H2, and makers
    lost gross on >= 30 markets."""
    h1, h2 = halves(rows)
    out, ok = {}, True
    for name, rs in (("all", rows), ("H1", h1), ("H2", h2)):
        v, lo, hi = boot(rs, total_e(True))
        out[name] = (v, lo, hi, len(rs), len({r["day"] for r in rs}))
        ok = ok and lo is not None and lo > 0
    losers = sum(1 for r in rows if r["pnl"] - r["fee"] < 0)
    out["losers"] = losers
    out["pass"] = ok and losers >= 30
    return out


def score(dbpath, series_file, log=print):
    c = sqlite3.connect(dbpath)
    f = read_series_file(series_file)
    rows = market_rows(c, f)
    log(f"{len(rows)} settled markets with a pool, read in full")
    if not rows:
        log("NOTHING TO SCORE")
        return None
    ct = sum(r["contracts"] for r in rows)
    pool = sum(r["pool"] for r in rows)
    poolf = sum(r["pool"] * r["f"] for r in rows)
    pnl = sum(r["pnl"] for r in rows)
    fee = sum(r["fee"] for r in rows)
    days = sorted({r["day"] for r in rows})
    log(f"settle days {days[0]} to {days[-1]} ({len(days)} days)")
    log(f"contracts traded {ct:,.0f}; pool ${pool:,.2f} (x two-sided fraction: ${poolf:,.2f}); "
        f"maker P&L ${pnl:,.2f}; maker fees ${fee:,.2f}")
    if ct:
        log(f"per contract traded: pool {100 * poolf / ct:+.2f}c, maker P&L {100 * pnl / ct:+.2f}c, "
            f"fees {-100 * fee / ct:+.2f}c, net {100 * (poolf + pnl - fee) / ct:+.2f}c")
    lh = sum(r["lh_ct"] for r in rows)
    if lh:
        lhp = sum(r["lh_pnl"] for r in rows)
        rest = ct - lh
        log(f"maker P&L in the last hour before close: {100 * lhp / lh:+.2f}c/ct on {lh:,.0f} ct; "
            f"earlier: {100 * (pnl - lhp) / rest if rest else 0:+.2f}c/ct on {rest:,.0f} ct")
    v = verdict(rows)
    log("\nE = pool x two-sided fraction + maker P&L - maker fees, summed over markets, "
        "95% CI clustered by settle day:")
    for k in ("all", "H1", "H2"):
        e, lo, hi, n, nd = v[k]
        ci = f"[{lo:+,.2f}, {hi:+,.2f}]" if lo is not None else "[n/a]"
        log(f"  {k:<4} ${e:+,.2f} {ci} on {n} markets, {nd} days")
    e1, lo1, hi1 = boot(rows, total_e(False))
    log(f"  with the pool paid in full (f = 1): ${e1:+,.2f} "
        f"[{lo1:+,.2f}, {hi1:+,.2f}]" if lo1 is not None else "  f = 1: n/a")
    log(f"markets on which makers lost money gross: {v['losers']} (bar: 30)")
    log("\nby series:")
    log(f"  {'series':<24}{'mkts':>5}{'days':>5}{'ct':>10}{'pool*f $':>10}{'maker $':>10}"
        f"{'E $':>10}{'E c/ct':>8}{'lost':>5}  95% CI on E")
    by = collections.defaultdict(list)
    for r in rows:
        by[r["series"]].append(r)
    for s in sorted(by, key=lambda s: -sum(e_of(r) for r in by[s])):
        rs = by[s]
        e, lo, hi = boot(rs, total_e(True))
        sct = sum(r["contracts"] for r in rs)
        ci = f"[{lo:+.2f}, {hi:+.2f}]" if lo is not None else "[one day]"
        log(f"  {s:<24}{len(rs):>5}{len({r['day'] for r in rs}):>5}{sct:>10,.0f}"
            f"{sum(r['pool'] * r['f'] for r in rs):>10.2f}{sum(r['pnl'] for r in rs):>10.2f}"
            f"{e:>10.2f}{(100 * e / sct if sct else 0):>8.2f}"
            f"{sum(1 for r in rs if r['pnl'] - r['fee'] < 0):>5}  {ci}")
    log(f"\nVERDICT: {'PASS' if v['pass'] else 'FAIL'} (pre-registered: lower bound > 0 on all, H1 "
        f"and H2, and >= 30 maker-losing markets)")
    return v


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--series-file", default=os.path.join(HERE, "LIPCOST_SERIES"))
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--budget-min", type=float, default=320)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--score", action="store_true")
    a = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    dbpath = os.path.join(HERE, f"lipcost-{a.tag}.db")
    name = "score" if a.score else "run"
    f = open(os.path.join(OUT, f"lipcost-{a.tag}-{name}.txt"), "a")

    def log(s):
        print(s, flush=True); f.write(s + "\n"); f.flush()

    log(f"--- {name} at {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    if a.score:
        score(dbpath, a.series_file, log)
    else:
        run(read_series_file(a.series_file), a.days, a.budget_min, dbpath, log)


if __name__ == "__main__":
    main()
