"""Do sharp Polymarket wallets predict outcomes? Variant 6 of the 2026-11-27 whale mutations.

International Polymarket is close-only for a US resident, so it is a DATA SOURCE here: if
wallets that were right in the past keep being right, the signal could be mapped onto the same
question on Kalshi or Polymarket US. That mapping is only worth building if the signal exists,
so this measures the signal first.

Method, with zero lookahead:
- Markets: resolved binary markets (one outcome paid 1) with >= MIN_VOL volume, ended in
  [START, END]. Split by RESOLUTION time: train = the first 60% of days, holdout = the rest.
- Trades: data-api /trades for each market (taker side). A trade's mark-to-resolution value is
  size * (paid - price) for a BUY of an outcome, size * (price - paid) for a SELL.
- Sharp wallets are chosen on TRAIN markets only: >= MIN_MKTS markets, value per $ of cost
  >= MIN_EDGE, positive in both halves of train, and a per-market t-stat >= 2.
- Follow: in HOLDOUT markets, at a sharp wallet's FIRST trade in a market, a copier buys the
  same side at the first later BUY print of that outcome at least `delay` s after it (a price
  somebody actually paid), held to resolution. A SELL of an outcome is followed by buying the
  other outcome. Control: the same follow on wallets that pass the activity bar but not the
  skill bar, so "following anyone" is the baseline the sharp group must beat.

Read-only: GET only, Gamma and data-api, host and path whitelisted.

Run:  python3 -I pm_sharp.py --tag x --shard 0/3 --budget-min 300
      python3 -I pm_sharp.py --tag x --score 'pm_x_*.db'
"""
import argparse, glob, json, math, os, random, sqlite3, statistics as st, sys, time
import urllib.error, urllib.parse, urllib.request
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
ALLOWED = {"gamma-api.polymarket.com": ("/markets",),   # /markets and /markets/keyset "data-api.polymarket.com": ("/v2/trades",)}
START, END = "2026-07-01", "2026-11-20"
MIN_VOL = 50_000
PAGE = 1000          # v2 /trades max; v1 (offset paging) was retired 2026-10-24
MAX_PAGES = 8
MIN_MKTS, MIN_EDGE = 15, 0.05
DELAYS = (60, 300)
WAIT = 3600
TRAIN_FRAC = 0.6


def get(url, params=None):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.hostname not in ALLOWED or not any(
            u.path == p or u.path.startswith(p + "/") for p in ALLOWED[u.hostname]):
        raise RuntimeError(f"refusing a non-whitelisted read: {url}")
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": "research", "Accept": "application/json"})
    for nap in (0, 3, 10, 30):
        if nap:
            time.sleep(nap)
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                time.sleep(0.15)
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                continue
            if e.code in (400, 404, 410, 422):
                return None
            raise
        except (urllib.error.URLError, TimeoutError):
            continue
    return None


def ts(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def jl(x):
    if isinstance(x, str):
        try:
            return json.loads(x)
        except ValueError:
            return None
    return x


def winner(m):
    """Index of the outcome that paid 1, or None (unresolved, 50/50, or not binary)."""
    outs, px = jl(m.get("outcomes")), jl(m.get("outcomePrices"))
    if not outs or not px or len(outs) != 2 or len(px) != 2:
        return None
    try:
        px = [float(p) for p in px]
    except (TypeError, ValueError):
        return None
    if max(px) >= 0.99 and min(px) <= 0.01:
        return px.index(max(px))
    return None


def value(side, price, size, paid):
    """Mark-to-resolution value of one trade in dollars. paid = 1 if the outcome won."""
    return size * (paid - price) if side == "BUY" else size * (price - paid)


def db(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS mkt(cid TEXT PRIMARY KEY, q TEXT, end_ts INTEGER, win INTEGER,
        vol REAL, cat TEXT, done INTEGER DEFAULT 0, trades INTEGER, capped INTEGER);
    CREATE TABLE IF NOT EXISTS tr(cid TEXT, wallet TEXT, side TEXT, oi INTEGER, price REAL,
        size REAL, ts INTEGER, tx TEXT, PRIMARY KEY(cid, tx, wallet, oi, side, price, size));
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    return c


def list_markets(c, shard, nshards, log):
    if c.execute("SELECT v FROM meta WHERE k='listed'").fetchone():
        return
    off, n, kept, cursor, mode, pages = 0, 0, 0, None, "keyset", 0
    flt = {"closed": "true", "end_date_min": START + "T00:00:00Z", "end_date_max": END + "T23:59:59Z",
           "volume_num_min": MIN_VOL, "limit": 100}
    while True:
        if mode == "keyset":
            r = get("https://gamma-api.polymarket.com/markets/keyset",
                    dict(flt, **({"after_cursor": cursor} if cursor else {})))
            if r is None and pages == 0:
                log("keyset listing refused; falling back to offset paging (capped by the server)")
                mode = "offset"
                continue
            if isinstance(r, dict):
                page = r.get("data") or r.get("markets") or r.get("results") or []
                cursor = r.get("next_cursor")
            else:
                page, cursor = (r or []), None
        else:
            page = get("https://gamma-api.polymarket.com/markets",
                       dict(flt, offset=off, order="endDate", ascending="true"))
        pages += 1
        if not page:
            break
        if off == 0:
            log(f"field check, first market keys: {sorted(page[0].keys())[:70]}")
            log(f"  sample: {page[0].get('question')!r} outcomes {page[0].get('outcomes')} "
                f"prices {page[0].get('outcomePrices')} end {page[0].get('endDate')} "
                f"closedTime {page[0].get('closedTime')} cid {page[0].get('conditionId')}")
        for m in page:
            n += 1
            w, cid = winner(m), m.get("conditionId")
            end = ts(m.get("closedTime")) or ts(m.get("endDate"))
            if w is None or not cid or end is None or hash_(cid) % nshards != shard:
                continue
            cat = m.get("category") or ((m.get("events") or [{}])[0].get("category") if m.get("events") else None)
            c.execute("INSERT OR IGNORE INTO mkt(cid, q, end_ts, win, vol, cat) VALUES (?,?,?,?,?,?)",
                      (cid, (m.get("question") or "")[:200], end, w, float(m.get("volumeNum") or 0), cat))
            kept += 1
        off += len(page)
        if (mode == "keyset" and not cursor) or (mode == "offset" and len(page) < 100):
            break
    log(f"listing: {pages} pages by {mode}")
    if n == 0:
        log("nothing listed; not marking the listing done, so a re-run tries again")
        return
    c.execute("INSERT OR REPLACE INTO meta VALUES ('listed', ?)", (str(n),))
    c.commit()
    log(f"listed {n} closed markets >= ${MIN_VOL:,}; this shard kept {kept} resolved binaries")


def hash_(s):
    h = 0
    for ch in s:
        h = (h * 131 + ord(ch)) % 1_000_003
    return h


def pull(c, cid, log, first=False):
    n, capped, cursor = 0, 0, None
    for pg in range(MAX_PAGES):
        params = {"condition": cid, "taker_only": "true"}
        params.update({"cursor": cursor} if cursor else {"limit": PAGE})
        r = get("https://data-api.polymarket.com/v2/trades", params)
        if r is None:
            break
        page, cursor = page_of(r)
        if first and pg == 0:
            log(f"field check, response keys: {sorted(r.keys()) if isinstance(r, dict) else 'list'}; "
                f"first trade: {json.dumps(page[0])[:600] if page else None}")
        rows = []
        for x in page:
            oi = x.get("outcome_index", x.get("outcomeIndex"))
            try:
                oi = int(oi)
                if oi not in (0, 1):
                    continue
                rows.append((cid, (x.get("proxy_wallet") or x.get("proxyWallet") or "").lower(), x.get("side"), oi,
                             float(x.get("price")), float(x.get("size")), int(x.get("timestamp")),
                             x.get("transaction_hash") or x.get("transactionHash") or ""))
            except (TypeError, ValueError):
                continue
        c.executemany("INSERT OR IGNORE INTO tr VALUES (?,?,?,?,?,?,?,?)", rows)
        n += len(rows)
        if not cursor:
            break
    else:
        capped = 1
    c.execute("UPDATE mkt SET done=1, trades=?, capped=? WHERE cid=?", (n, capped, cid))
    c.commit()
    return n


def page_of(r):
    """v2 /trades: rows plus pagination.next_cursor (None on the last page). The row list's key
    is read defensively, and a bare list is accepted, so a shape change shows up as empty pages
    in the field check rather than as a crash."""
    if isinstance(r, list):
        return r, None
    rows = r.get("data") or r.get("trades") or r.get("results") or []
    pg = r.get("pagination") or {}
    nxt = pg.get("next_cursor") if pg.get("has_more", True) else None
    return rows, nxt


def run(a, log=print):
    shard, nshards = map(int, a.shard.split("/"))
    c = db(os.path.join(HERE, f"pm_{a.tag}_{shard}.db"))
    deadline = time.time() + a.budget_min * 60
    list_markets(c, shard, nshards, log)
    todo = [r[0] for r in c.execute("SELECT cid FROM mkt WHERE done=0 ORDER BY vol DESC")]
    log(f"markets to read {len(todo)}")
    first = c.execute("SELECT COUNT(*) FROM mkt WHERE done=1").fetchone()[0] == 0
    for i, cid in enumerate(todo):
        if time.time() > deadline:
            log("budget reached; a re-run resumes here")
            break
        pull(c, cid, log, first=first and i == 0)
        if (i + 1) % 200 == 0:
            log(f"{i + 1} markets read")
    left = c.execute("SELECT COUNT(*) FROM mkt WHERE done=0").fetchone()[0]
    cap = c.execute("SELECT COUNT(*) FROM mkt WHERE capped=1").fetchone()[0]
    with open(os.path.join(HERE, "out", f"pm-{a.tag}-{shard}-coverage.txt"), "w") as fo:
        fo.write(f"unread {left}\ncapped {cap}\n")
    log(f"done: {left} unread, {cap} capped at {MAX_PAGES * PAGE} trades")


# --- scoring ---------------------------------------------------------------------
def boot(vals_by_cluster, n=2000, seed=7):
    keys = list(vals_by_cluster)
    if len(keys) < 3:
        return None, None
    rnd = random.Random(seed)
    tot = lambda ks: (sum(sum(vals_by_cluster[k]) for k in ks) / max(1, sum(len(vals_by_cluster[k]) for k in ks)))
    bs = sorted(tot([rnd.choice(keys) for _ in keys]) for _ in range(n))
    return bs[int(.025 * n)], bs[int(.975 * n)]


def skill(trades, win_of):
    """Per wallet: markets, $ value, $ cost, per-market values (for the t-stat and halves)."""
    w = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for cid, wallet, side, oi, p, s, t in trades:
        paid = 1.0 if oi == win_of[cid] else 0.0
        v = value(side, p, s, paid)
        cost = s * p if side == "BUY" else s * (1 - p)
        w[wallet][cid][0] += v
        w[wallet][cid][1] += cost
    return w


def sharp_set(w, end_of, cut_mid):
    sharp, active = set(), set()
    for wallet, mk in w.items():
        if len(mk) < MIN_MKTS:
            continue
        active.add(wallet)
        v = [x[0] for x in mk.values()]
        cost = sum(x[1] for x in mk.values())
        if cost <= 0 or sum(v) / cost < MIN_EDGE:
            continue
        h1 = sum(x[0] for c, x in mk.items() if end_of[c] < cut_mid)
        h2 = sum(x[0] for c, x in mk.items() if end_of[c] >= cut_mid)
        sd = st.pstdev(v) or 1e-9
        if h1 > 0 and h2 > 0 and st.mean(v) / (sd / math.sqrt(len(v))) >= 2:
            sharp.add(wallet)
    return sharp, active - sharp


def follow(trades_by_mkt, group, win_of, end_of, delay):
    """One follow per (wallet, market): the wallet's first trade there."""
    out = defaultdict(list)   # day -> list of (pnl, price)
    for cid, rows in trades_by_mkt.items():
        rows = sorted(rows, key=lambda r: r[6])
        seen = set()
        buys = defaultdict(list)
        for r in rows:
            if r[2] == "BUY":
                buys[r[3]].append((r[6], r[4]))
        for _, wallet, side, oi, p, s, t in rows:
            if wallet not in group or wallet in seen:
                continue
            seen.add(wallet)
            want = oi if side == "BUY" else 1 - oi
            q = next((px for tt, px in buys[want] if t + delay <= tt <= t + delay + WAIT), None)
            if q is None or not 0.01 <= q <= 0.99:
                continue
            paid = 1.0 if want == win_of[cid] else 0.0
            out[end_of[cid] // 86400].append((paid - q, q, paid))
    return out


def summary(name, d):
    rows = [x for v in d.values() for x in v]
    if not rows:
        return f"  {name}: no follows"
    pnl = {k: [x[0] for x in v] for k, v in d.items()}
    lo, hi = boot(pnl)
    m = st.mean(x[0] for x in rows)
    avgp = st.mean(x[1] for x in rows)
    win = st.mean(x[2] for x in rows)
    ci = f"[{lo * 100:+.2f}, {hi * 100:+.2f}]" if lo is not None else "[n/a]"
    return (f"  {name}: n={len(rows)} days={len(d)} EV {m * 100:+.2f}c/contract {ci} "
            f"win {win:.1%} avg price {avgp:.3f} multiple {win / avgp:.2f} per $1 {m / avgp:+.3f}")


def score(a, log=print):
    win_of, end_of, trades = {}, {}, []
    for p in sorted(glob.glob(a.score)):
        c = sqlite3.connect(p)
        for cid, end, w in c.execute("SELECT cid, end_ts, win FROM mkt WHERE done=1"):
            win_of[cid], end_of[cid] = w, end
        trades += c.execute("SELECT cid, wallet, side, oi, price, size, ts FROM tr").fetchall()
    days = sorted({e // 86400 for e in end_of.values()})
    if len(days) < 10:
        log("too few resolution days to split")
        return
    cut = days[int(len(days) * TRAIN_FRAC)] * 86400
    cut_mid = days[int(len(days) * TRAIN_FRAC / 2)] * 86400
    tr_train = [r for r in trades if end_of.get(r[0], cut) < cut]
    hold = defaultdict(list)
    for r in trades:
        if r[0] in end_of and end_of[r[0]] >= cut:
            hold[r[0]].append(r)
    log(f"markets {len(end_of)} (train {sum(e < cut for e in end_of.values())}, holdout "
        f"{sum(e >= cut for e in end_of.values())}); trades {len(trades)}; cut "
        f"{time.strftime('%Y-%m-%d', time.gmtime(cut))}")
    w = skill(tr_train, win_of)
    sharp, active = sharp_set(w, end_of, cut_mid)
    log(f"wallets with >= {MIN_MKTS} train markets: {len(sharp) + len(active)}; sharp {len(sharp)}")
    for d in DELAYS:
        log(f"delay {d}s, hold to resolution, before fees:")
        log(summary("sharp", follow(hold, sharp, win_of, end_of, d)))
        log(summary("control (active, not sharp)", follow(hold, active, win_of, end_of, d)))
    # price bands for the sharp group at the first delay
    f0 = follow(hold, sharp, win_of, end_of, DELAYS[0])
    for lo, hi in ((0.01, 0.2), (0.2, 0.5), (0.5, 0.8), (0.8, 0.99)):
        sub = {k: [x for x in v if lo <= x[1] < hi] for k, v in f0.items()}
        sub = {k: v for k, v in sub.items() if v}
        log(summary(f"sharp, price {lo:.2f}-{hi:.2f}", sub))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--budget-min", type=int, default=300)
    ap.add_argument("--score")
    a = ap.parse_args()
    os.makedirs(os.path.join(HERE, "out"), exist_ok=True)
    score(a) if a.score else run(a)
