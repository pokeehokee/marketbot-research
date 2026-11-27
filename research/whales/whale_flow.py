"""Do big Hyperliquid wallets' trades lead BTC/ETH/SOL, after the delay a copier faces?

Alpha research, 2026-11-27. Devin saw a "copy the whales at 20-25x" bot. Hyperliquid
publishes every wallet's fills, so the question it rests on can be measured: when a large
wallet opens (or adds to, or flips) a BTC/ETH/SOL perp position, does the price move its way
afterwards, by more than a copier's fees, once the copier is 0-60 s, 1, 5 or 15 minutes late?

Universe: wallets on Hyperliquid's public leaderboard feed (the one its website reads) with
the largest account values. Ranked by ACCOUNT VALUE, never by PnL, because ranking by past
PnL would select wallets whose past trades won by construction. Account value today is still
survivorship (a wallet that blew up is no longer large), and that bias favours finding an
edge: a null result is robust to it, a positive one is suspect and has to clear the holdout.

Fills: info `userFillsByTime`, paged forward from --days ago. The venue keeps only each
wallet's 10,000 most recent fills, so a very active wallet covers days, not weeks; the
coverage file says how far back each one reaches.

Events: a wallet's fills on one coin inside MERGE seconds of each other, in one direction,
are one event. Kind: open (from flat), add, reduce, close, flip; direction = the sign of the
position change. Notional = sum(px * sz). taker share = share of notional with crossed=true.

Price: Coinbase spot 1-minute candles. A candle key is the bucket START (CLAUDE.md trap #4).
A copier acting `delay` seconds after the event's LAST fill enters at the open of the first
bucket that starts at or after last_fill + delay, and exits at the open of the first bucket
starting at or after entry + horizon. Nothing at or before the fill is read as a later price;
test_whale_flow plants that leak and the test fails.

Read-only: POST only to api.hyperliquid.xyz/info (the venue's read endpoint; /exchange, the
write endpoint, is refused) with whitelisted request types, and GET only the two listed
paths. No keys, no account.

Run:  python3 -I whale_flow.py --tag x --shard 0/3 --days 60 --budget-min 300
      python3 -I whale_flow.py --tag x --shard 0/3 --export out/x-0.csv.gz
"""
import argparse, bisect, gzip, json, math, os, sqlite3, sys, time, urllib.error, urllib.parse, urllib.request, zlib
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
COINS = {"BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD"}
UNIVERSE = 600              # largest account values kept
MIN_ACCOUNT = 250_000.0     # USD
MERGE = 300                 # s: fills of one wallet+coin+direction this close are one event
MIN_NOTIONAL = 50_000.0     # USD: smaller events are not exported
DELAYS = (0, 60, 300, 900)  # s after the event's last fill
HORIZONS = (60, 300, 900, 3600, 4 * 3600)
PAGE_MAX = 2000

INFO = "https://api.hyperliquid.xyz/info"
LEADERBOARD = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"
INFO_TYPES = {"userFillsByTime", "fundingHistory", "metaAndAssetCtxs", "meta"}
GET_ALLOWED = {"stats-data.hyperliquid.xyz": ("/Mainnet/leaderboard",),
               "api.exchange.coinbase.com": tuple(f"/products/{p}/candles" for p in COINS.values())}

LOG = []


def log(s=""):
    print(s, flush=True)
    LOG.append(str(s))


# --- read-only http ------------------------------------------------------------
_last = {}


def _pace(host, gap):
    t = time.time()
    w = _last.get(host, 0) + gap - t
    if w > 0:
        time.sleep(w)
    _last[host] = time.time()


def _open(req, host, gap, tries=6):
    for i in range(tries):
        _pace(host, gap)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                time.sleep(min(120, 5 * 2 ** i))
                continue
            raise
        except (urllib.error.URLError, TimeoutError) as e:
            if i < tries - 1:
                time.sleep(min(120, 5 * 2 ** i))
                continue
            raise


def info(body, gap=4.0):
    """POST to the read endpoint only, whitelisted types only."""
    if body.get("type") not in INFO_TYPES:
        raise ValueError(f"refused info type {body.get('type')!r}")
    req = urllib.request.Request(INFO, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "research"})
    return _open(req, "api.hyperliquid.xyz", gap)


def get(url, params=None, gap=0.4):
    u = urllib.parse.urlparse(url)
    if u.scheme != "https" or u.hostname not in GET_ALLOWED or u.path not in GET_ALLOWED[u.hostname]:
        raise ValueError(f"refused {url}")
    if params:
        url += "?" + urllib.parse.urlencode(params)
    return _open(urllib.request.Request(url, headers={"User-Agent": "research"}), u.hostname, gap)


def assert_read_only():
    for bad in ("order", "cancel", "withdraw3", "usdSend", "approveAgent"):
        try:
            info({"type": bad})
        except ValueError:
            continue
        raise AssertionError(bad)
    try:
        get("https://api.hyperliquid.xyz/exchange")
    except ValueError:
        pass
    else:
        raise AssertionError("exchange")


# --- pure functions --------------------------------------------------------------
def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def hkey(s):
    return zlib.crc32(s.lower().encode())


def pick_universe(rows, n=UNIVERSE, min_account=MIN_ACCOUNT, max_account=None):
    """Largest account values in [min_account, max_account). PnL is never read here: the
    leaderboard's PnL is as of today, so ranking on it would select on the test period."""
    out = []
    for r in rows:
        a, v = r.get("ethAddress"), f(r.get("accountValue"))
        if a and v is not None and v >= min_account and (max_account is None or v < max_account):
            out.append((v, a.lower()))
    out.sort(reverse=True)
    return [(a, v) for v, a in out[:n]]


def kind_of(start, end):
    if start == 0:
        return "open"
    if end == 0:
        return "close"
    if (start > 0) != (end > 0):
        return "flip"
    return "add" if abs(end) > abs(start) else "reduce"


def events(fills, merge=MERGE):
    """fills: dicts with coin, px, sz, side ('B'/'A'), time (ms), startPosition, crossed.
    Returns events per wallet-coin: consecutive same-direction fills no more than `merge`
    seconds apart. Position is read from startPosition, not reconstructed, so a fill the
    venue no longer serves cannot corrupt the next event's kind."""
    by = {}
    for x in sorted(fills, key=lambda x: (x["coin"], int(x["time"]))):
        by.setdefault(x["coin"], []).append(x)
    out = []
    for coin, xs in by.items():
        cur = None
        for x in xs:
            sgn = 1 if x["side"] == "B" else -1
            sz, px, t = f(x["sz"]), f(x["px"]), int(x["time"]) / 1000
            sp = f(x.get("startPosition"))
            pnl = f(x.get("closedPnl")) or 0.0
            if sz is None or px is None or sp is None:
                continue
            if cur and cur["dir"] == sgn and t - cur["t_last"] <= merge:
                cur["t_last"] = t
                cur["end"] = sp + sgn * sz
                cur["notional"] += px * sz
                cur["taker"] += px * sz if x.get("crossed") else 0.0
                cur["n"] += 1
                cur["pnl"] += pnl
                continue
            if cur:
                out.append(cur)
            cur = {"coin": coin, "dir": sgn, "t_first": t, "t_last": t, "start": sp,
                   "end": sp + sgn * sz, "notional": px * sz,
                   "taker": px * sz if x.get("crossed") else 0.0, "n": 1, "pnl": pnl, "px_first": px}
        if cur:
            out.append(cur)
    for e in out:
        e["kind"] = kind_of(round(e["start"], 9), round(e["end"], 9))
        e["taker_share"] = e["taker"] / e["notional"] if e["notional"] else None
    return out


def open_at_or_after(keys, opens, t):
    """Open of the first 1-minute bucket that STARTS at or after t. keys are bucket starts."""
    i = bisect.bisect_left(keys, t)
    return (keys[i], opens[i]) if i < len(keys) and keys[i] - t < 600 else (None, None)


def outcome(keys, opens, t_last, d, delays=DELAYS, horizons=HORIZONS):
    """Signed return (fraction) for each (delay, horizon); None where no price exists.
    Also the move in the hour BEFORE the event (context: do whales chase?)."""
    res = {}
    for dl in delays:
        k0, p0 = open_at_or_after(keys, opens, t_last + dl)
        for h in horizons:
            if p0 is None:
                res[(dl, h)] = None
                continue
            k1, p1 = open_at_or_after(keys, opens, k0 + h)
            res[(dl, h)] = None if p1 is None else d * (p1 / p0 - 1)
    kb, pb = open_at_or_after(keys, opens, t_last - 3600)
    ka, pa = open_at_or_after(keys, opens, t_last - 60)   # the bucket containing or just before
    pre = None
    if pb is not None and pa is not None and ka < t_last:
        pre = d * (pa / pb - 1)
    return res, pre


# --- pull --------------------------------------------------------------------------
def db(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS wallet(addr TEXT PRIMARY KEY, account REAL, done INTEGER DEFAULT 0,
        fills INTEGER, first_ms INTEGER, last_ms INTEGER, capped INTEGER);
    CREATE TABLE IF NOT EXISTS fill(addr TEXT, tid TEXT, coin TEXT, px TEXT, sz TEXT, side TEXT,
        time INTEGER, start TEXT, crossed INTEGER, dir TEXT, PRIMARY KEY(addr, tid));
    CREATE TABLE IF NOT EXISTS candle(coin TEXT, k INTEGER, o REAL, PRIMARY KEY(coin, k));
    CREATE TABLE IF NOT EXISTS funding(coin TEXT, t INTEGER, rate REAL, premium REAL, PRIMARY KEY(coin, t));
    """)
    if "pnl" not in [r[1] for r in c.execute("PRAGMA table_info(fill)")]:
        c.execute("ALTER TABLE fill ADD COLUMN pnl TEXT")   # closedPnl; NULL on older pulls
    return c


def meta_get(c, k):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r[0] if r else None


def load_universe(c, shard, nshards, a=None):
    if c.execute("SELECT COUNT(*) FROM wallet").fetchone()[0]:
        return
    try:
        d = get(LEADERBOARD)
    except Exception as ex:
        log(f"leaderboard unreadable: {type(ex).__name__} {str(ex)[:160]}")
        return
    rows = d.get("leaderboardRows", d if isinstance(d, list) else [])
    log(f"leaderboard rows {len(rows)}; keys of the first: {sorted(rows[0].keys()) if rows else None}")
    uni = pick_universe(rows, n=getattr(a, "universe", UNIVERSE),
                        min_account=getattr(a, "min_account", MIN_ACCOUNT),
                        max_account=getattr(a, "max_account", None))
    mine = [(a, v) for a, v in uni if hkey(a) % nshards == shard]
    c.executemany("INSERT OR IGNORE INTO wallet(addr, account) VALUES (?,?)", mine)
    c.commit()
    log(f"universe {len(uni)} wallets with account >= ${getattr(a, 'min_account', MIN_ACCOUNT):,.0f}; this shard {len(mine)}; "
        f"account median ${sorted(v for _, v in uni)[len(uni)//2]:,.0f}" if uni else "universe empty")


def pull_wallet(c, addr, start_ms, end_ms):
    """Pages forward by the last fill's time. Paging from that same millisecond (not +1)
    so fills sharing it are not skipped; tid de-duplicates, and a page adding nothing new
    ends the walk."""
    t, n, pages, first, last = start_ms, 0, 0, None, None
    while t < end_ms and pages < 15:
        page = info({"type": "userFillsByTime", "user": addr, "startTime": t, "endTime": end_ms}, gap=6.0)
        pages += 1
        if not page:
            break
        before = c.total_changes
        keep = [x for x in page if x.get("coin") in COINS]
        c.executemany("INSERT OR IGNORE INTO fill VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      [(addr, str(x.get("tid")), x["coin"], x["px"], x["sz"], x["side"], int(x["time"]),
                        x.get("startPosition"), 1 if x.get("crossed") else 0, x.get("dir"),
                        x.get("closedPnl")) for x in keep])
        n += c.total_changes - before
        times = [int(x["time"]) for x in page]
        first = min(times) if first is None else min(first, min(times))
        nl = max(times)
        if len(page) < 500 or (last is not None and nl <= last):
            last = nl if last is None else max(last, nl)
            break
        last, t = nl, nl
    capped = 1 if first is not None and first > start_ms + 86400_000 else 0
    c.execute("UPDATE wallet SET done=1, fills=?, first_ms=?, last_ms=?, capped=? WHERE addr=?",
              (n, first, last, capped, addr))
    c.commit()
    return n, pages


def pull_candles(c, start, end):
    for coin, prod in COINS.items():
        have = c.execute("SELECT MAX(k) FROM candle WHERE coin=?", (coin,)).fetchone()[0]
        t = max(start, (have or 0) + 60)
        while t < end:
            e = min(end, t + 300 * 60)
            rows = get(f"https://api.exchange.coinbase.com/products/{prod}/candles",
                       {"granularity": 60, "start": datetime.fromtimestamp(t, timezone.utc).isoformat(),
                        "end": datetime.fromtimestamp(e, timezone.utc).isoformat()})
            c.executemany("INSERT OR IGNORE INTO candle VALUES (?,?,?)",
                          [(coin, int(r[0]), float(r[3])) for r in rows])   # [time, low, high, open, close, vol]
            c.commit()
            t = e
        log(f"candles {coin}: {c.execute('SELECT COUNT(*) FROM candle WHERE coin=?', (coin,)).fetchone()[0]}")


def pull_funding(c, start_ms, end_ms):
    for coin in COINS:
        t = start_ms
        while t < end_ms:
            page = info({"type": "fundingHistory", "coin": coin, "startTime": t, "endTime": end_ms})
            if not page:
                break
            c.executemany("INSERT OR IGNORE INTO funding VALUES (?,?,?,?)",
                          [(coin, int(x["time"]), f(x.get("fundingRate")), f(x.get("premium"))) for x in page])
            c.commit()
            nt = max(int(x["time"]) for x in page) + 1
            if nt <= t or len(page) < 100:
                break
            t = nt
        log(f"funding {coin}: {c.execute('SELECT COUNT(*) FROM funding WHERE coin=?', (coin,)).fetchone()[0]}")


def run(a):
    assert_read_only()
    shard, nshards = map(int, a.shard.split("/"))
    c = db(os.path.join(HERE, f"whales_{a.tag}_{shard}.db"))
    if getattr(a, "forward", False):
        # Forward mode: the universe is frozen in the database the first time it runs (so it
        # is chosen BEFORE any of the data it is scored on), and every run moves t_end up to
        # an hour ago and reads each wallet only from its newest stored fill.
        c.execute("INSERT OR REPLACE INTO meta VALUES ('t_end', ?)", (str(int(time.time() // 60 * 60) - 3600),))
        c.execute("UPDATE wallet SET done=0")
        c.commit()
    if meta_get(c, "t_end") is None:
        c.execute("INSERT INTO meta VALUES ('t_end', ?)", (str(int(time.time() // 60 * 60) - 3600),))
        c.commit()
    t_end = int(meta_get(c, "t_end"))
    t0 = t_end - a.days * 86400
    deadline = time.time() + a.budget_min * 60
    load_universe(c, shard, nshards, a)
    todo = [r[0] for r in c.execute("SELECT addr FROM wallet WHERE done=0 ORDER BY account DESC")]
    log(f"wallets to read {len(todo)}")
    k = 0
    for addr in todo:
        if time.time() > deadline - 900:
            log("budget reached; a re-run resumes here")
            break
        try:
            start = t0 * 1000
            if getattr(a, "forward", False):
                last = c.execute("SELECT MAX(time) FROM fill WHERE addr=?", (addr,)).fetchone()[0]
                start = max(start, last or 0)
            n, pages = pull_wallet(c, addr, start, t_end * 1000)
        except Exception as ex:
            log(f"  {addr[:10]} failed: {type(ex).__name__} {str(ex)[:120]}")
            continue
        k += 1
        if k % 20 == 0:
            log(f"{k} wallets read")
    if time.time() < deadline - 300:
        pull_candles(c, t0 - 3600, min(t_end + 5 * 3600, int(time.time() // 60 * 60) - 60))
    if shard == 0 and time.time() < deadline - 120:
        pull_funding(c, (t0 - 86400) * 1000, t_end * 1000)
    left = c.execute("SELECT COUNT(*) FROM wallet WHERE done=0").fetchone()[0]
    log(f"done this run: {k} wallets read, {left} unread")


# --- export -----------------------------------------------------------------------
COLS = (["addr", "coin", "day", "t_last", "dir", "kind", "notional", "taker_share", "n_fills",
         "account", "wallet_capped", "pre_1h", "t_first", "px_first", "closed_pnl"] +
        [f"r_d{dl}_h{h}" for dl in DELAYS for h in HORIZONS])


def export(a, path):
    shard, nshards = map(int, a.shard.split("/"))
    c = db(os.path.join(HERE, f"whales_{a.tag}_{shard}.db"))
    t_end = int(meta_get(c, "t_end") or 0)
    cand = {}
    for coin in COINS:
        rows = c.execute("SELECT k, o FROM candle WHERE coin=? ORDER BY k", (coin,)).fetchall()
        cand[coin] = ([r[0] for r in rows], [r[1] for r in rows])
    n = 0
    with gzip.open(path, "wt") as fo:
        fo.write(",".join(COLS) + "\n")
        for addr, acct, capped in c.execute("SELECT addr, account, capped FROM wallet WHERE done=1").fetchall():
            fills = [{"coin": r[0], "px": r[1], "sz": r[2], "side": r[3], "time": r[4], "startPosition": r[5],
                      "crossed": r[6], "closedPnl": r[7]} for r in c.execute(
                "SELECT coin, px, sz, side, time, start, crossed, pnl FROM fill WHERE addr=?", (addr,))]
            pnl_known = any(x["closedPnl"] is not None for x in fills)
            for e in events(fills):
                if e["notional"] < getattr(a, "min_notional", MIN_NOTIONAL) or e["t_last"] > t_end:
                    continue
                keys, opens = cand[e["coin"]]
                res, pre = outcome(keys, opens, e["t_last"], e["dir"])
                row = [addr, e["coin"], time.strftime("%Y-%m-%d", time.gmtime(e["t_last"])), int(e["t_last"]),
                       e["dir"], e["kind"], round(e["notional"]), e["taker_share"], e["n"], round(acct or 0),
                       capped, pre, int(e["t_first"]), e["px_first"], e["pnl"] if pnl_known else None] + [res[(dl, h)] for dl in DELAYS for h in HORIZONS]
                fo.write(",".join("" if v is None else (f"{v:.6g}" if isinstance(v, float) else str(v))
                                  for v in row) + "\n")
                n += 1
    w = c.execute("SELECT COUNT(*), SUM(done), SUM(capped), SUM(fills) FROM wallet").fetchone()
    with open(path.replace(".csv.gz", "-coverage.txt"), "w") as fo:
        fo.write(f"events {n}\nwallets {w[0]} read {w[1] or 0} unread {(w[0] or 0) - (w[1] or 0)}\n"
                 f"capped_by_10000_fill_limit {w[2] or 0}\nfills_btc_eth_sol {w[3] or 0}\n")
        for coin in COINS:
            k = cand[coin][0]
            fo.write(f"candles {coin} {len(k)} {k[0] if k else ''} {k[-1] if k else ''}\n")
    if c.execute("SELECT COUNT(*) FROM funding").fetchone()[0]:
        with gzip.open(path.replace(".csv.gz", "-funding.csv.gz"), "wt") as fo:
            fo.write("coin,t,rate,premium\n")
            for r in c.execute("SELECT coin, t, rate, premium FROM funding ORDER BY coin, t"):
                fo.write(",".join("" if v is None else str(v) for v in r) + "\n")
        with gzip.open(path.replace(".csv.gz", "-candles.csv.gz"), "wt") as fo:
            fo.write("coin,k,o\n")
            for r in c.execute("SELECT coin, k, o FROM candle ORDER BY coin, k"):
                fo.write(",".join(str(v) for v in r) + "\n")
    log(f"exported {n} events to {path}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--budget-min", type=int, default=300)
    ap.add_argument("--export")
    ap.add_argument("--universe", type=int, default=UNIVERSE)
    ap.add_argument("--min-account", type=float, default=MIN_ACCOUNT)
    ap.add_argument("--max-account", type=float, default=None)
    ap.add_argument("--min-notional", type=float, default=MIN_NOTIONAL)
    ap.add_argument("--forward", action="store_true", help="incremental daily pull on a frozen universe")
    a = ap.parse_args(argv)
    if a.export:
        export(a, a.export)
    else:
        run(a)


if __name__ == "__main__":
    main()
