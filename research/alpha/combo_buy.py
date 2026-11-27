"""Buying cheap combos (parlays) on Kalshi: does anything known at the buy beat the price?

Devin, 2026-11-27: people buy high-multiple combos, and they are not betting blind.
Round 2's #26 found combos settle about as often as priced (loss ratio 1.00 over 48
sampled hours) but scored only the quoter's side and buying NO by RFQ. This reads the
BUYER's side: combo YES bought at 15c or less in sampled hours, the legs each combo was
built from, and the legs' own single-market prices just before the buy.

The price is the RFQ fill the buyer actually paid, so unlike the trade-VWAP rows of the
other alpha screens there is no half-spread hiding in it.

Features, all known at the buy (nothing after the trade's timestamp is read):
  n_legs, n_games       legs, and distinct games (an event ticker's part after the series
                        prefix, so KXNFLGAME-26NOV23KCDEN and KXNFLSPREAD-26NOV23KCDEN are
                        one game)
  same_game_pairs       leg pairs from one game: correlated legs a quoter might price as if
                        they were independent
  gap                   log(combo price / product of the legs' own prices). Negative means the
                        combo costs LESS than its legs multiplied out
  d_indep60             log change in that product over the hour before the buy: legs that
                        moved since the quote
  leg_age_max_m         minutes since the stalest leg price was set
  ttc_first_h/last_h    hours from the buy to the first and last leg's scheduled close
  size, hour_et         log1p(contracts bought), ET hour of the buy
  cat_mix, has_same_game  categorical
A leg's price is the mid of the last 1-minute candle that ENDED at or before the moment,
at most an hour old; an empty book is never a price, and one missing leg leaves gap None.

Read-only and unauthenticated (research/discovery/taker_flow_screen.get).
  python3 -I combo_buy.py --tag x --shard 0/3 --days 30 --budget-min 300
  python3 -I combo_buy.py --tag x --shard 0/3 --export out/combo/x-0.csv.gz
"""
import argparse, gzip, json, math, os, sqlite3, sys, time, urllib.parse, zlib
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "discovery"))
import taker_flow_screen as T    # noqa: E402

try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo("America/New_York")
except Exception:                 # pragma: no cover - runner always has tzdata
    ET = None

CHEAP_MAX = 0.15
STRIDE = 7            # coprime with 24: every hour of the day is visited equally
SETTLE_LAG_D = 3
BATCH = 50
MAX_COMBOS = 1200     # per sampled hour, chosen by ticker hash so the choice is not by outcome
LEG_MAX_AGE = 3600
COLS = ("source", "series", "category", "event", "ticker", "day", "q", "won", "n_legs", "n_games",
        "same_game_pairs", "gap", "d_indep60", "leg_age_max_m", "ttc_first_h", "ttc_last_h", "size",
        "hour_et", "cat_mix", "has_same_game")


def is_combo(t):
    return bool(t) and t.startswith("KXMVE")


def hkey(ticker):
    return zlib.crc32(ticker.encode())


def game_key(event_ticker):
    return event_ticker.split("-", 1)[1] if event_ticker and "-" in event_ticker else event_ticker


def series_of(event_ticker):
    return event_ticker.split("-", 1)[0] if event_ticker else None


def hour_et(ts):
    if ET is None:
        return int(((ts - 5 * 3600) % 86400) // 3600)
    import datetime
    return datetime.datetime.fromtimestamp(ts, ET).hour


# --- pure ---------------------------------------------------------------------
def parse_candles(d):
    """[(end_ts, bid, ask)] for the leg's YES; the empty-book sentinels become None."""
    out = []
    for x in (d or {}).get("candlesticks") or []:
        def f(k):
            try:
                return float((x.get(k) or {}).get("close_dollars"))
            except (TypeError, ValueError):
                return None
        bid, ask = f("yes_bid"), f("yes_ask")
        if ask is not None and ask >= 1.0:
            ask = None
        if bid is not None and bid <= 0.0:
            bid = None
        e = x.get("end_period_ts")
        if e is not None:
            out.append((int(e), bid, ask))
    return out


def leg_prob_at(candles, side, t):
    """(probability of the leg's side, age in seconds) from the last candle that ended at or
    before t and no more than LEG_MAX_AGE old. (None, None) if there is none or its book was
    empty on either side."""
    cs = [c for c in candles if t - LEG_MAX_AGE <= c[0] <= t]
    if not cs:
        return None, None
    e, bid, ask = max(cs, key=lambda c: c[0])
    if bid is None or ask is None or ask < bid:
        return None, None
    m = (bid + ask) / 2
    p = m if side == "yes" else 1 - m
    return (p, t - e) if 0 < p < 1 else (None, None)


def features(legs, leg_close, leg_candles, q, t, cats):
    """legs: [{market_ticker, event_ticker, side}]. Every value None when it is not known."""
    games = [game_key(l.get("event_ticker")) for l in legs]
    pairs = sum(1 for i in range(len(games)) for j in range(i + 1, len(games)) if games[i] == games[j])
    now = [leg_prob_at(leg_candles.get(l["market_ticker"], []), l.get("side"), t) for l in legs]
    then = [leg_prob_at(leg_candles.get(l["market_ticker"], []), l.get("side"), t - 3600) for l in legs]
    ok = bool(legs) and all(p is not None for p, _ in now)
    indep = math.prod(p for p, _ in now) if ok else None
    ok60 = bool(legs) and all(p is not None for p, _ in then)
    indep60 = math.prod(p for p, _ in then) if ok60 else None
    closes = [leg_close.get(l["market_ticker"]) for l in legs]
    closes = [c for c in closes if c is not None]
    lc = sorted({cats.get(series_of(l.get("event_ticker"))) or "?" for l in legs})
    return {
        "n_legs": len(legs), "n_games": len(set(games)), "same_game_pairs": pairs,
        "gap": math.log(q / indep) if indep else None,
        "d_indep60": math.log(indep / indep60) if indep and indep60 else None,
        "leg_age_max_m": max(a for _, a in now) / 60 if ok else None,
        "ttc_first_h": (min(closes) - t) / 3600 if closes else None,
        "ttc_last_h": (max(closes) - t) / 3600 if closes else None,
        "cat_mix": (lc[0] if len(lc) == 1 else "mixed") if legs else None,
        "has_same_game": (1 if pairs else 0) if legs else None,
    }


def first_cheap_buys(trades, cap, keep=lambda t: True):
    """{ticker: (ts, price, contracts)}: each combo's FIRST YES buy at CHEAP_MAX or less, then at
    most `cap` tickers by hash. One row per combo, and the features are taken at that trade."""
    first = {}
    for f in trades:
        if not f:
            continue
        tk, ts, side, price, count = f
        if not is_combo(tk) or side != "yes" or price > CHEAP_MAX or not keep(tk):
            continue
        if tk not in first or ts < first[tk][0]:
            first[tk] = (ts, price, count)
    pick = sorted(first, key=hkey)[:cap]
    return {t: first[t] for t in pick}


def grid(t_end, days, shard, nshards):
    hs = [h for h in range(int(t_end) - 3600, int(t_end - days * 86400) - 1, -3600)
          if (h // 3600) % STRIDE == 0]
    return [h for i, h in enumerate(hs) if i % nshards == shard]


# --- pull -----------------------------------------------------------------------
def db(path):
    c = sqlite3.connect(path)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
    CREATE TABLE IF NOT EXISTS hour_done(hour INTEGER PRIMARY KEY, combo_trades INTEGER, cheap INTEGER,
        rows INTEGER);
    CREATE TABLE IF NOT EXISTS crow(ticker TEXT PRIMARY KEY, hour INTEGER, ts REAL, q REAL,
        contracts REAL, series TEXT, event TEXT, close_ts REAL, result TEXT, feats TEXT);
    CREATE TABLE IF NOT EXISTS cat(series TEXT PRIMARY KEY, category TEXT);
    """)
    return c


def window_end(c, days):
    r = c.execute("SELECT v FROM meta WHERE k='t_end'").fetchone()
    if r:
        return int(r[0])
    t = int(math.floor(time.time() / 3600) * 3600 - SETTLE_LAG_D * 86400)
    c.execute("INSERT INTO meta VALUES('t_end', ?)", (str(t),)); c.commit()
    return t


def load_cats(c, log):
    if c.execute("SELECT 1 FROM cat LIMIT 1").fetchone():
        return dict(c.execute("SELECT series, category FROM cat").fetchall())
    out, cursor = {}, None
    for _ in range(50):
        q = {"limit": 1000}
        if cursor:
            q["cursor"] = cursor
        d, err = T.get("/series", q)
        if not d:
            raise RuntimeError(f"series listing failed: {err}")
        for s in d.get("series") or []:
            out[s["ticker"]] = s.get("category")
        cursor = d.get("cursor")
        if not cursor or not d.get("series"):
            break
    c.executemany("INSERT OR REPLACE INTO cat VALUES(?,?)", out.items()); c.commit()
    log(f"{len(out)} series categories")
    return out


def batch_markets(tickers, pool, log):
    """{ticker: market} via /markets?tickers=, discarding any entry that was not asked for,
    then single reads for whatever the batch did not return."""
    def one(b):
        p, err = T.get("/markets", {"tickers": ",".join(b), "limit": 1000})
        if not p:
            log(f"batch: {err}"); return {}
        return {m["ticker"]: m for m in p.get("markets") or [] if m.get("ticker") in set(b)}
    tl = sorted(tickers)
    got = {}
    for g in pool.map(one, [tl[i:i + BATCH] for i in range(0, len(tl), BATCH)]):
        got.update(g)

    def single(t):
        p, _ = T.get(f"/markets/{urllib.parse.quote(t)}")
        return t, (p or {}).get("market")
    for t, m in pool.map(single, [t for t in tl if t not in got]):
        if m and m.get("ticker") == t:
            got[t] = m
    return got


def read_hour(hour, cutoff_ts, log):
    path = "/markets/trades" if (cutoff_ts is None or hour >= cutoff_ts) else "/historical/trades"
    out, cursor, combo = [], None, 0
    while True:
        q = {"limit": 1000, "min_ts": int(hour), "max_ts": int(hour + 3599)}
        if cursor:
            q["cursor"] = cursor
        p, err = T.get(path, q)
        if err:
            log(f"{hour}: {err}"); return None, combo
        rows = p.get("trades") or []
        for t in rows:
            f = T.trade_fields(t)
            if f and is_combo(f[0]):
                combo += 1
                out.append(f)
        cursor = p.get("cursor")
        if not cursor or not rows:
            return out, combo


def resettle(c, pool, log):
    """Combos whose legs had not all closed at the first read: read their result again."""
    open_ = [r[0] for r in c.execute("SELECT ticker FROM crow WHERE result IS NULL")]
    if not open_:
        return
    got = batch_markets(open_, pool, log)
    n = 0
    for t, m in got.items():
        res = (m.get("result") or "").lower()
        if res in ("yes", "no"):
            c.execute("UPDATE crow SET result=? WHERE ticker=?", (res, t)); n += 1
    c.commit()
    log(f"resettle: {n} of {len(open_)} open combos now settled")


def run(path, days, budget_min, shard, nshards, log=print):
    start = time.time()
    c = db(path)
    cats = load_cats(c, log)
    p, err = T.get("/historical/cutoff")
    cutoff = T.utc(p.get("trades_created_ts")) if p and isinstance(p.get("trades_created_ts"), str) else None
    t_end = window_end(c, days)
    pool = ThreadPoolExecutor(max_workers=8)
    resettle(c, pool, log)
    done = {r[0] for r in c.execute("SELECT hour FROM hour_done")}
    hours = [h for h in grid(t_end, days, shard, nshards) if h not in done]
    log(f"{len(hours)} hours to read on shard {shard}/{nshards}, {len(done)} done, window ends "
        f"{time.strftime('%Y-%m-%dT%H', time.gmtime(t_end))}")
    for h in hours:
        if time.time() - start > budget_min * 60:
            log("budget reached; a re-run resumes here"); break
        trades, combo = read_hour(h, cutoff, log)
        if trades is None:
            continue
        buys = first_cheap_buys(trades, MAX_COMBOS)
        meta = batch_markets(set(buys), pool, log)
        legs_all = {}
        for tk, m in meta.items():
            for l in m.get("mve_selected_legs") or []:
                if l.get("market_ticker") and l.get("event_ticker"):
                    legs_all[l["market_ticker"]] = l["event_ticker"]
        lm = batch_markets(set(legs_all), pool, log)
        leg_close = {t: T.utc(m.get("close_time")) for t, m in lm.items()}

        def candles(t):
            d, _ = T.get(f"/series/{series_of(legs_all[t])}/markets/{urllib.parse.quote(t)}/candlesticks",
                         {"start_ts": int(h - 2 * 3600 - 60), "end_ts": int(h + 3600),
                          "period_interval": 1})
            return t, parse_candles(d)
        leg_c = dict(pool.map(candles, sorted(legs_all)))
        rows = 0
        for tk, (ts, qv, cnt) in buys.items():
            m = meta.get(tk)
            legs = (m or {}).get("mve_selected_legs") or []
            if not m or not legs:
                continue
            res = (m.get("result") or "").lower()
            f = features(legs, leg_close, leg_c, qv, ts, cats)
            f["size"] = math.log1p(cnt)
            f["hour_et"] = hour_et(ts)
            c.execute("INSERT OR REPLACE INTO crow VALUES(?,?,?,?,?,?,?,?,?,?)",
                      (tk, h, ts, qv, cnt, series_of(m.get("event_ticker")), m.get("event_ticker"),
                       T.utc(m.get("close_time")), res if res in ("yes", "no") else None, json.dumps(f)))
            rows += 1
        c.execute("INSERT OR REPLACE INTO hour_done VALUES(?,?,?,?)", (h, combo, len(buys), rows))
        c.commit()
        log(f"{time.strftime('%Y-%m-%dT%H', time.gmtime(h))}: {combo} combo trades, {len(buys)} cheap "
            f"YES buys taken, {len(legs_all)} legs, {rows} rows; {(time.time() - start) / 60:.1f} min")
    pool.shutdown(wait=False)


def export(path, out, days, shard, nshards):
    c = db(path)
    n = 0
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with gzip.open(out, "wt") as f:
        f.write(",".join(COLS) + "\n")
        for tk, ts, q, s, ev, close, res, feats in c.execute(
                "SELECT ticker, ts, q, series, event, close_ts, result, feats FROM crow "
                "WHERE result IS NOT NULL ORDER BY ticker"):
            ft = json.loads(feats)
            day = time.strftime("%Y-%m-%d", time.gmtime(close or ts))
            row = ("combo", s, "Combo", ev, tk, day, q, 1 if res == "yes" else 0) + tuple(
                ft.get(k) for k in COLS[8:])
            f.write(",".join("" if v is None else (f"{v:.6g}" if isinstance(v, float) else str(v))
                             for v in row) + "\n")
            n += 1
    t_end = window_end(c, days)
    done = {r[0] for r in c.execute("SELECT hour FROM hour_done")}
    unread = len([h for h in grid(t_end, days, shard, nshards) if h not in done])
    opened = c.execute("SELECT COUNT(*) FROM crow WHERE result IS NULL").fetchone()[0]
    with open(out.replace(".csv.gz", "-coverage.txt"), "w") as f:
        f.write(f"rows {n}\nhours_done {len(done)}\nunread {unread}\nunsettled {opened}\n")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--budget-min", type=float, default=300)
    ap.add_argument("--shard", default="0/1")
    ap.add_argument("--export")
    a = ap.parse_args(argv)
    sh, n = (int(x) for x in a.shard.split("/"))
    path = os.path.join(HERE, f"combo_{a.tag}_{sh}.db")
    if a.export:
        print("rows", export(path, a.export, a.days, sh, n)); return
    run(path, a.days, a.budget_min, sh, n, log=lambda s: print(s, flush=True))


if __name__ == "__main__":
    main()
