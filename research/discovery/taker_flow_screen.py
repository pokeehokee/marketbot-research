"""Where do Kalshi TAKERS systematically overpay? An exchange-wide screen.

The only thing that has ever made money in these repos is residual premium on an
almost-decided contract, collected as a MAKER on a fee-free (`quadratic`) series:
the BTC deep-ITM ladder. Every maker fill is the other side of a real taker
trade, and Kalshi publishes every trade. So instead of guessing which other
series might carry the same edge, read the whole tape:

  for each settled market, every public trade, priced at the price the taker
  paid, against the market's result.

A taker who bought side X at price p loses p if X loses; the maker who sold it
earns p - 1{X wins} per contract. Summed over a cell (series x price band x
time-to-close), that is exactly what a maker resting at those prices would have
earned from the flow that actually arrived, adverse selection included, because
these ARE the takers who arrive. It does not tell us whether WE would have been
at the front of the queue; capacity is reported as a share of that flow and
calibrated against the ladder's own measured capture.

Rules this obeys (all from the repo's CLAUDE.md):
  * Read-only, unauthenticated. `assert_read_only` whitelists the market, trade,
    series and cutoff reads and refuses anything else.
  * `side` traps: `taker_outcome_side` is the outcome the TAKER ACQUIRED. A NO
    taker at no_price q paid q for NO. Never infer direction from anything else.
  * Paged reads are followed to the end, and a 429 is retried in get(), because
    a rate-limited listing silently looks like "no markets that day".
  * Everything one event settles on moves together, so the scorer clusters by
    settle DAY (and the event is kept in the key so it can cluster by event).
  * Missing values are never zero: a trade on a market we do not hold a settled
    result for is skipped and counted, never graded.

Run:  python3 -I taker_flow_screen.py --days 28 --budget-min 280
      python3 -I taker_flow_screen.py --score     (scores the cached DB)
"""
import argparse, calendar, gzip, json, math, os, random, sqlite3, sys, time
import urllib.error, urllib.parse, urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "taker_flow.db")

ALLOWED = ("/markets", "/series", "/historical/markets", "/historical/trades",
           "/historical/cutoff")


def assert_read_only(path):
    p = path.split("?", 1)[0]
    if not any(p == a or p.startswith(a + "/") for a in ALLOWED):
        raise RuntimeError(f"refusing a non-whitelisted path: {path}")
    for bad in ("/portfolio", "/orders", "/order_groups", "/rfq", "/quotes", "/balance"):
        if bad in p:
            raise RuntimeError(f"refusing a write-shaped path: {path}")


RETRY_SLEEPS = (1, 3, 8, 20, 45)
import threading
from concurrent.futures import ThreadPoolExecutor

RATE = 16.0          # reads/s across all threads, under the Basic tier's budget
_gate = threading.Lock()
_next = [0.0]


def _throttle(_sleep=time.sleep):
    with _gate:
        now = time.time()
        wait = _next[0] - now
        _next[0] = max(now, _next[0]) + 1.0 / RATE
    if wait > 0:
        _sleep(wait)


def get(path, params=None, _sleep=time.sleep):
    """Unauthenticated GET, retried on 429 and transient errors. (payload, err)."""
    if params:
        path = path + "?" + urllib.parse.urlencode(params)
    assert_read_only(path)
    last = None
    for nap in (0,) + RETRY_SLEEPS:
        if nap:
            _sleep(nap)
        _throttle(_sleep)
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                BASE + path, headers={"User-Agent": "research", "Accept": "application/json"}),
                timeout=60)
            return json.load(r), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                return None, last
        except Exception as e:  # timeouts, resets
            last = f"{type(e).__name__}: {e}"
    return None, last


def utc(s):
    if not s:
        return None
    s = s.replace("Z", "")
    frac = 0.0
    if "." in s:
        s, f = s.split(".", 1)
        frac = float("0." + "".join(ch for ch in f if ch.isdigit()) or "0")
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) + frac


def num(d, *keys):
    """First present, parseable numeric field. Missing is None, never 0."""
    for k in keys:
        v = d.get(k)
        if v is None or v == "":
            continue
        try:
            return float(v)
        except (TypeError, ValueError):
            continue
    return None


def trade_fields(t):
    """(ticker, ts, taker_side, taker_price, count) or None.

    taker_price is what the taker paid for the outcome they ACQUIRED. Prices in
    `*_dollars` are dollars; the legacy integer `yes_price` is cents."""
    side = t.get("taker_outcome_side") or t.get("taker_side")
    if side not in ("yes", "no"):
        return None
    yp = num(t, "yes_price_dollars")
    if yp is None:
        c = num(t, "yes_price")
        yp = c / 100.0 if c is not None else None
    if yp is None:
        return None
    count = num(t, "count_fp", "count")
    ts = utc(t.get("created_time"))
    if not count or count <= 0 or ts is None:
        return None
    price = yp if side == "yes" else 1.0 - yp
    price = round(price, 4)
    if not (0.0 < price < 1.0):
        return None
    return t.get("ticker"), ts, side, price, count


# --- buckets --------------------------------------------------------------
TTC_EDGES = [0, 60, 300, 900, 3000, 3600 * 3, 3600 * 12, 86400, 86400 * 3, 86400 * 10**4]
TTC_NAMES = ["<1m", "1-5m", "5-15m", "15-50m", "50m-3h", "3-12h", "12-24h", "1-3d", "3d+"]
BAND_EDGES = [0, .025, .055, .105, .205, .355, .505, .655, .805, .905, .955, 1.0001]
BAND_NAMES = ["01-02", "03-05", "06-10", "11-20", "21-35", "36-50",
              "51-65", "66-80", "81-90", "91-95", "96-99"]


def bucket(edges, names, x):
    for i in range(len(names)):
        if edges[i] <= x < edges[i + 1]:
            return names[i]
    return None


def series_of(event_ticker):
    return (event_ticker or "").split("-", 1)[0]


# --- storage --------------------------------------------------------------
def db(path=DB):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS market(ticker TEXT PRIMARY KEY, event TEXT, series TEXT,
        close_ts REAL, result TEXT);
    CREATE TABLE IF NOT EXISTS series(ticker TEXT PRIMARY KEY, category TEXT, fee_type TEXT,
        frequency TEXT, fee_multiplier REAL);
    CREATE TABLE IF NOT EXISTS cell(series TEXT, event TEXT, day TEXT, ttc TEXT, band TEXT,
        taker_side TEXT, contracts REAL, paid REAL, taker_won REAL, trades INTEGER,
        PRIMARY KEY(series, event, day, ttc, band, taker_side));
    CREATE TABLE IF NOT EXISTS hour_done(hour INTEGER PRIMARY KEY, trades INTEGER,
        kept INTEGER, unknown INTEGER, pages INTEGER);
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    return c


def load_series(c, log):
    cursor, n = None, 0
    while True:
        params = {"limit": 1000}
        if cursor:
            params["cursor"] = cursor
        p, err = get("/series", params)
        if err:
            log(f"series listing: {err}")
            break
        for s in p.get("series") or []:
            c.execute("INSERT OR REPLACE INTO series VALUES(?,?,?,?,?)",
                      (s.get("ticker"), s.get("category"), s.get("fee_type"),
                       s.get("frequency"), num(s, "fee_multiplier")))
            n += 1
        cursor = p.get("cursor")
        if not cursor:
            break
    c.commit()
    log(f"series: {n}")
    return n


RESOLVE_BATCH = 50


def resolve(c, tickers, markets, log, skip=lambda t: False, pool=None):
    """Fetch metadata for tickers not yet known, in batches, via /markets?tickers=.

    Guard against the comma-batch trap this repo found on /markets/orderbooks: a
    returned ticker that was not requested is discarded, and a requested ticker
    the batch did not return is retried alone once before it is marked absent.
    Tickers `skip` rejects (combo series, which trade by RFQ and cannot be
    rested on) are marked absent without a call."""
    todo = [t for t in tickers if t not in markets]
    absent = (None, None, None, None)
    rows = {}
    for t in [t for t in todo if skip(t)]:
        rows[t] = absent
    todo = [t for t in todo if not skip(t)]

    def conv(m):
        res = (m.get("result") or "").lower()
        ev = m.get("event_ticker")
        return (ev, series_of(ev), utc(m.get("close_time")),
                res if res in ("yes", "no") else None)

    def one_batch(batch):
        p, err = get("/markets", {"tickers": ",".join(batch), "limit": 1000})
        got = {}
        if p:
            for m in p.get("markets") or []:
                if m.get("ticker") in batch:
                    got[m["ticker"]] = conv(m)
        else:
            log(f"resolve batch: {err}")
        return got, [t for t in batch if t not in got]

    def one(t):
        p, err = get(f"/markets/{urllib.parse.quote(t)}")
        return t, (conv(p["market"]) if p and p.get("market") else absent)

    batches = [todo[i:i + RESOLVE_BATCH] for i in range(0, len(todo), RESOLVE_BATCH)]
    mapper = pool.map if pool else map
    missing = []
    for got, miss in mapper(one_batch, batches):
        rows.update(got); missing += miss
    for t, row in mapper(one, missing):
        rows[t] = row
    for t, row in rows.items():
        markets[t] = row
        c.execute("INSERT OR REPLACE INTO market VALUES(?,?,?,?,?)", (t,) + row)
    c.commit()
    return len(todo), len(missing)


def fetch_hour(hour, cutoff_ts, log):
    """All exchange trades in [hour, hour+3600), compressed to
    {(ticker, side, price, ts//10): contracts}. Runs in a worker thread."""
    path = "/markets/trades" if (cutoff_ts is None or hour >= cutoff_ts) else "/historical/trades"
    comp, cursor, pages, trades = {}, None, 0, 0
    while True:
        params = {"limit": 1000, "min_ts": int(hour), "max_ts": int(hour + 3599)}
        if cursor:
            params["cursor"] = cursor
        p, err = get(path, params)
        if err:
            log(f"{path} {hour}: {err}")
            return hour, None, pages, trades
        pages += 1
        rows = p.get("trades") or []
        for t in rows:
            trades += 1
            f = trade_fields(t)
            if f:
                tk, ts, side, price, count = f
                k = (tk, side, price, int(ts // 10))
                a = comp.get(k)
                comp[k] = (a[0] + count, a[1] + 1) if a else (count, 1)
        cursor = p.get("cursor")
        if not cursor or not rows:
            break
    return hour, comp, pages, trades


def aggregate_hour(c, hour, comp, pages, trades, markets):
    agg = {}
    kept, unknown = 0, 0
    for (tk, side, price, t10), (count, n) in comp.items():
        m = markets.get(tk)
        if not m or m[3] is None:
            unknown += n
            continue
        event, series, close_ts, result = m
        ts = t10 * 10 + 5
        if close_ts is None or ts > close_ts:
            continue
        ttc = bucket(TTC_EDGES, TTC_NAMES, max(0.0, close_ts - ts))
        band = bucket(BAND_EDGES, BAND_NAMES, price)
        if ttc is None or band is None:
            continue
        day = time.strftime("%Y-%m-%d", time.gmtime(close_ts))
        k = (series, event, day, ttc, band, side)
        a = agg.setdefault(k, [0.0, 0.0, 0.0, 0])
        a[0] += count
        a[1] += count * price
        a[2] += count if side == result else 0.0
        a[3] += n
        kept += n
    for k, (ct, paid, won, n) in agg.items():
        c.execute("""INSERT INTO cell VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(series,event,day,ttc,band,taker_side) DO UPDATE SET
            contracts=contracts+excluded.contracts, paid=paid+excluded.paid,
            taker_won=taker_won+excluded.taker_won, trades=trades+excluded.trades""",
                  k + (ct, paid, won, n))
    c.execute("INSERT OR REPLACE INTO hour_done VALUES(?,?,?,?,?)",
              (hour, trades, kept, unknown, pages))
    c.commit()
    return trades, kept, unknown, pages


def scan_hour(c, hour, markets, cutoff_ts, log, resolver=None):
    """Serial form, kept for the tests: fetch, resolve, aggregate one hour."""
    hour, comp, pages, trades = fetch_hour(hour, cutoff_ts, log)
    if comp is None:
        return None
    if resolver:
        resolver({k[0] for k in comp})
    return aggregate_hour(c, hour, comp, pages, trades, markets)


def sample(days, budget_min, stride=5, log=print):
    start = time.time()
    c = db()
    if not c.execute("SELECT 1 FROM series LIMIT 1").fetchone():
        load_series(c, log)
    p, err = get("/historical/cutoff")
    cutoff_ts = None
    if p:
        log(f"cutoff: {json.dumps(p)}")
        cutoff_ts = utc(p.get("trades_created_ts")) if isinstance(p.get("trades_created_ts"), str) \
            else num(p, "trades_created_ts")
    else:
        log(f"cutoff unreadable: {err}")
    now = time.time()
    # End a day back, so markets that closed in the window have settled; the
    # live tier starts at the cutoff, so a window inside it needs no archive.
    t_end = math.floor(now / 3600) * 3600 - 86400
    t_start = t_end - days * 86400
    if cutoff_ts and t_start < cutoff_ts:
        log("window reaches into the historical tier; trades there use /historical/trades")
    c.execute("INSERT OR REPLACE INTO meta VALUES('window', ?)", (f"{t_start},{t_end}",))
    markets = {r[0]: r[1:] for r in c.execute(
        "SELECT ticker, event, series, close_ts, result FROM market")}
    log(f"markets cached: {len(markets)}")
    fees = {r[0]: (r[1] or "", r[2] or "") for r in c.execute(
        "SELECT ticker, fee_type, category FROM series")}
    # Combos trade by RFQ, never on a book a maker can rest on. Some carry the
    # `combo` fee type; the multivariate collections (KXMVE...) do not, and the
    # one-day probe "found" four survivors in KXMVECROSSCATEGORY for that reason.
    skip = lambda t: (t.startswith("KXMVE")
                      or "combo" in fees.get(t.split("-", 1)[0], ("", ""))[0])
    done = {r[0] for r in c.execute("SELECT hour FROM hour_done")}
    # A systematic sample of hours. The stride is coprime with 24 so every
    # hour of the day is visited equally often across the window; sampling by
    # time is unbiased for the per-trade economics a cell measures.
    hours = [h for h in range(int(t_end), int(t_start) - 1, -3600)
             if ((h // 3600) % stride == 0) and h not in done]
    log(f"window {time.strftime('%Y-%m-%dT%H', time.gmtime(t_start))}.."
        f"{time.strftime('%Y-%m-%dT%H', time.gmtime(t_end))}, stride {stride}, "
        f"{len(hours)} hours to fetch, {len(done)} already done")
    res_stats = [0, 0]
    pool = ThreadPoolExecutor(max_workers=8)
    hp = ThreadPoolExecutor(max_workers=4)
    futs, i, nd = [], 0, 0
    while i < len(hours) or futs:
        while i < len(hours) and len(futs) < 4 and time.time() - start < budget_min * 60:
            futs.append(hp.submit(fetch_hour, hours[i], cutoff_ts, log)); i += 1
        if not futs:
            log("budget reached; stopping (resume picks up here)")
            break
        hour, comp, pages, trades = futs.pop(0).result()
        if comp is None:
            continue
        a, b = resolve(c, {k[0] for k in comp}, markets, log, skip, pool)
        res_stats[0] += a; res_stats[1] += b
        r = aggregate_hour(c, hour, comp, pages, trades, markets)
        nd += 1
        if nd <= 3 or nd % 10 == 0:
            log(f"{time.strftime('%Y-%m-%dT%H', time.gmtime(hour))}: {r[0]} trades, {r[1]} kept, "
                f"{r[2]} unknown, {r[3]} pages; {nd} hours in {(time.time()-start)/60:.1f} min, "
                f"resolved {res_stats[0]} (batch-missed {res_stats[1]})")
    n = c.execute("SELECT COUNT(*), SUM(trades), SUM(kept), SUM(unknown) FROM hour_done").fetchone()
    log(f"hours done {n[0]}, trades {n[1]}, kept {n[2]}, unknown-market {n[3]}")


def export(path):
    """The aggregate table as gzipped CSV, small enough to commit."""
    c = db()
    # Collapsed over events (the per-event detail survives as two counts), so
    # the committed file stays small: series x close-day x ttc x band x side.
    with gzip.open(path, "wt") as f:
        f.write("series,category,fee_type,day,ttc,band,taker_side,contracts,paid,taker_won,"
                "trades,events,loss_events\n")
        for r in c.execute("""SELECT cell.series, s.category, s.fee_type, day, ttc, band, taker_side,
                SUM(contracts), SUM(paid), SUM(taker_won), SUM(trades), COUNT(*),
                SUM(CASE WHEN taker_won > 0 THEN 1 ELSE 0 END) FROM cell
                LEFT JOIN series s ON s.ticker = cell.series
                GROUP BY cell.series, day, ttc, band, taker_side"""):
            f.write(",".join("" if v is None else (f"{v:.4f}" if isinstance(v, float) else str(v))
                             for v in r) + "\n")
    hd = c.execute("SELECT COUNT(*), MIN(hour), MAX(hour), SUM(trades), SUM(kept), SUM(unknown) "
                   "FROM hour_done").fetchone()
    return hd


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=28)
    ap.add_argument("--budget-min", type=float, default=280)
    ap.add_argument("--stride", type=int, default=5)
    ap.add_argument("--export", help="write the aggregate CSV.gz here and exit")
    a = ap.parse_args()
    if a.export:
        print(export(a.export))
        sys.exit(0)
    sample(a.days, a.budget_min, a.stride, log=lambda s: print(s, flush=True))
