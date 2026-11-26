"""Forward recorder for venues that keep no public price history (ideas P1-P3, P5, P7, P12, P14).

Gemini Predictions keeps no prices on a settled contract and serves no public trades;
Polymarket's trade history endpoints return 410 and its price history is empty once a market
closes; PredictIt serves current prices only. The one way to test a pricing idea on them is to
write the book down while it exists. One run = one snapshot of every venue below:

  gemini     every active event's contracts: best bid/ask, last, buy/sell per side
  gemini_set events Gemini lists as recently settled: resolution side and settlement value
  gemini_trd public trade prints (/v1/trades/{symbol}) for open Crypto contracts expiring soonest.
             Found 2026-11-26: the spot trades endpoint answers for GEMI- prediction symbols while
             the contract is OPEN (it 400s 'not a valid symbol' once closed), so prints exist only
             if recorded live. Each run keeps the newest 500 per symbol; duplicates across runs are
             removed by tid downstream. A page of exactly 500 is logged as possibly saturated.
  gemini_bk  full order book (/v1/book/{symbol}) for the same contracts: YES-space bids and asks.
  kalshi_btc KXBTCD (the live ladder's series) markets closing within 65 minutes: top of book.
  kalshi_bk  full ladders for those markets in the deep-ITM band (yes bid >= 0.80 or yes ask
             <= 0.20): the queue ahead of a 0.90-0.96 resting bid, which no Kalshi endpoint keeps.
             The orderbook carries ONLY bids per side; the YES ask is 1 - best NO bid.
  polymarket active international markets (paged): best bid/ask, last, volume, outcome prices
  pm_us      Polymarket US markets: outcome prices as listed
  pm_us_top  Polymarket US best bid / ask from the listing itself, every open market
  pm_us_bk   Polymarket US full books (/v1/markets/{slug}/book) for markets ending within 24 h.
             Its price-history endpoint keeps top-of-book quotes, so only DEPTH is recorded here.
             Thursday 6-8 AM ET maintenance empties the books: drop that window when scoring.
  predictit  every contract: best buy/sell for YES and NO, last trade

Each row carries `ts`, the moment THAT venue's read returned (venues are read one after another,
seconds apart). A missing price is written empty, never 0: a 0 meaning "no quote" and a 0
meaning "worthless" are indistinguishable downstream (CLAUDE.md, the longshot liquidity error).

Kalshi prices are not recorded: its candlesticks keep 1-minute bid/ask history and its trades are
readable later. Only Kalshi ORDER BOOKS are recorded, because no endpoint keeps them.

Read-only, unauthenticated, GET only, through probe.get's whitelist. A venue that fails is
logged and skipped; the others still record. Output: <out>/<venue>.csv.gz plus run.log.
"""
import argparse, csv, gzip, os, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "platforms"))
import probe as P  # noqa: E402

G = "https://api.gemini.com/v1/prediction-markets"
PM = "https://gamma-api.polymarket.com/markets"
PMUS = "https://gateway.polymarket.us/v1/markets"
PI = "https://www.predictit.org/api/marketdata/all/"
PM_PAGE = 100   # gamma caps a page at 100 whatever `limit` asks: the first run asked 500, got
                # 100 and stopped, reading a full page as the last one
LOG = []


def log(s):
    print(s, flush=True)
    LOG.append(s)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def px(x):
    """A price only if it is one. '' for missing, None, '' or anything outside (0, 1]."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return v if 0 < v <= 1 else ""


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return ""


GEMINI_COLS = ["ts", "event", "category", "event_type", "event_expiry", "event_volume", "agency",
               "index", "symbol", "strike_type", "strike", "contract_expiry", "status", "market_state",
               "bid", "ask", "last", "buy_yes", "buy_no", "sell_yes", "sell_no"]


def gemini_rows(events, ts):
    out = []
    for e in events:
        sd = e.get("sourceDetails") or {}
        for c in e.get("contracts") or []:
            p = c.get("prices") or {}
            st = c.get("strike") or {}
            out.append(dict(
                ts=ts, event=e.get("ticker"), category=e.get("category"), event_type=e.get("type"),
                event_expiry=e.get("expiryDate"), event_volume=num(e.get("volume")),
                agency=sd.get("agency"), index=sd.get("index"), symbol=c.get("instrumentSymbol"),
                strike_type=st.get("type") if isinstance(st, dict) else "",
                strike=num(st.get("value")) if isinstance(st, dict) else "",
                contract_expiry=c.get("expiryDate"), status=c.get("status"),
                market_state=c.get("marketState"), bid=px(p.get("bestBid")), ask=px(p.get("bestAsk")),
                last=px(p.get("lastTradePrice")),
                buy_yes=px((p.get("buy") or {}).get("yes")), buy_no=px((p.get("buy") or {}).get("no")),
                sell_yes=px((p.get("sell") or {}).get("yes")), sell_no=px((p.get("sell") or {}).get("no"))))
    return out


GEMSET_COLS = ["ts", "event", "category", "event_expiry", "settlement", "agency", "index", "symbol",
               "strike_type", "strike", "resolution", "resolved_at"]


def gemini_settled_rows(events, ts):
    out = []
    for e in events:
        sd = e.get("sourceDetails") or {}
        for c in e.get("contracts") or []:
            st = c.get("strike") or {}
            out.append(dict(
                ts=ts, event=e.get("ticker"), category=e.get("category"), event_expiry=e.get("expiryDate"),
                settlement=num((e.get("settlement") or {}).get("value")), agency=sd.get("agency"),
                index=sd.get("index"), symbol=c.get("instrumentSymbol"),
                strike_type=st.get("type") if isinstance(st, dict) else "",
                strike=num(st.get("value")) if isinstance(st, dict) else "",
                resolution=c.get("resolutionSide") or "", resolved_at=c.get("resolvedAt") or ""))
    return out


PM_COLS = ["ts", "id", "condition_id", "slug", "question", "end", "event", "bid", "ask", "last",
           "spread", "volume24h", "liquidity", "outcomes", "outcome_prices", "fee_type",
           "accepting_orders"]


def pm_rows(markets, ts):
    out = []
    for m in markets:
        ev = (m.get("events") or [{}])[0] or {}
        out.append(dict(
            ts=ts, id=m.get("id"), condition_id=m.get("conditionId"), slug=m.get("slug"),
            question=(m.get("question") or "")[:200], end=m.get("endDate"), event=ev.get("slug", ""),
            bid=px(m.get("bestBid")), ask=px(m.get("bestAsk")), last=px(m.get("lastTradePrice")),
            spread=num(m.get("spread")), volume24h=num(m.get("volume24hr")),
            liquidity=num(m.get("liquidityNum") if m.get("liquidityNum") is not None else m.get("liquidity")),
            outcomes=m.get("outcomes") or "", outcome_prices=m.get("outcomePrices") or "",
            fee_type=m.get("feeType") or "", accepting_orders=m.get("acceptingOrders")))
    return out


PMUS_COLS = ["ts", "id", "slug", "question", "market_type", "category", "end", "outcomes",
             "outcome_prices", "closed", "active"]


def pmus_rows(markets, ts):
    out = []
    for m in markets:
        out.append(dict(
            ts=ts, id=m.get("id"), slug=m.get("slug", ""), question=(m.get("question") or "")[:200],
            market_type=m.get("marketType"), category=m.get("category"), end=m.get("endDate"),
            outcomes=m.get("outcomes") or "", outcome_prices=m.get("outcomePrices") or "",
            closed=m.get("closed"), active=m.get("active")))
    return out


PI_COLS = ["ts", "market_id", "market", "contract_id", "contract", "date_end", "status",
           "buy_yes", "buy_no", "sell_yes", "sell_no", "last", "last_close", "pi_time"]


def pi_rows(doc, ts):
    out = []
    for m in (doc or {}).get("markets") or []:
        for c in m.get("contracts") or []:
            out.append(dict(
                ts=ts, market_id=m.get("id"), market=(m.get("shortName") or m.get("name") or "")[:200],
                contract_id=c.get("id"), contract=(c.get("shortName") or c.get("name") or "")[:200],
                date_end=c.get("dateEnd"), status=c.get("status"),
                buy_yes=px(c.get("bestBuyYesCost")), buy_no=px(c.get("bestBuyNoCost")),
                sell_yes=px(c.get("bestSellYesCost")), sell_no=px(c.get("bestSellNoCost")),
                last=px(c.get("lastTradePrice")), last_close=px(c.get("lastClosePrice")),
                pi_time=m.get("timeStamp")))
    return out


def paged(fetch, key_of, limit, max_pages):
    """Pull pages until a short page, an empty page, or a page that repeats the previous one
    (an ignored offset would otherwise loop over page 1 for ever)."""
    rows, seen_first = [], None
    for i in range(max_pages):
        try:
            page = fetch(i * limit)
        except RuntimeError as e:
            if not rows:
                raise
            # gamma answers 422 past offset ~2,000: keep what was read, say where it stopped
            log(f"    stopped at offset {i * limit} with {len(rows)} rows: {str(e)[:80]}")
            break
        if not page:
            break
        first = key_of(page[0])
        if first == seen_first:
            log(f"    page {i} repeats the previous page: offset ignored, stopping")
            break
        seen_first = first
        rows += page
        if len(page) < limit:
            break
    return rows


def fetch_gemini():
    def f(off):
        return P.get(G + "/events", {"status": "active", "limit": 500, "offset": off}).get("data") or []
    LAST_GEMINI[:] = paged(f, lambda e: e.get("ticker"), 500, 10)
    return LAST_GEMINI


def fetch_gemini_settled():
    return P.get(G + "/events/recently-settled").get("data") or []


GTRD_COLS = ["ts", "symbol", "event", "event_expiry", "tid", "timestampms", "price", "amount",
             "taker"]
TRADE_HOURS = 3       # contracts whose event expires within this many hours
TRADE_MAX = 120       # symbols per run; public REST is 120/min, so the loop paces itself
TRADE_PACE = 0.55     # seconds between calls
LAST_GEMINI = []      # the active-events read of this run, reused rather than fetched twice


def trade_targets(events, now_ts, hours=TRADE_HOURS, cap=TRADE_MAX):
    """Open Crypto contracts whose event expires within `hours`, soonest first, at most `cap`."""
    out = []
    for e in events:
        if (e.get("category") or "") != "Crypto":
            continue
        try:
            exp = datetime.fromisoformat((e.get("expiryDate") or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        left = exp - now_ts
        if not 0 < left <= hours * 3600:
            continue
        for c in e.get("contracts") or []:
            if c.get("marketState") == "open" and c.get("instrumentSymbol"):
                out.append((left, c["instrumentSymbol"], e.get("ticker"), e.get("expiryDate")))
    out.sort()
    return out[:cap]


def fetch_gemini_trades():
    if not LAST_GEMINI:
        raise RuntimeError("no active-events read this run")
    targets = trade_targets(LAST_GEMINI, time.time())
    got, saturated = [], 0
    for i, (_, sym, ev, exp) in enumerate(targets):
        if i:
            time.sleep(TRADE_PACE)
        try:
            page = P.get("https://api.gemini.com/v1/trades/" + sym, {"limit_trades": 500})
        except RuntimeError as e:
            log(f"    {sym}: {str(e)[:80]}")
            continue
        if len(page) >= 500:
            saturated += 1
        for t in page:
            got.append((sym, ev, exp, t))
    log(f"    {len(targets)} symbols, {saturated} returned a full 500-print page")
    return got


BOOKS = {}            # symbol -> book, filled alongside the trade reads


def fetch_gemini_books():
    if not LAST_GEMINI:
        raise RuntimeError("no active-events read this run")
    got = []
    for i, (_, sym, ev, exp) in enumerate(trade_targets(LAST_GEMINI, time.time())):
        if i:
            time.sleep(TRADE_PACE)
        try:
            b = P.get("https://api.gemini.com/v1/book/" + sym, {"limit_bids": 0, "limit_asks": 0})
        except RuntimeError as e:
            log(f"    {sym}: {str(e)[:80]}")
            continue
        got.append((sym, ev, exp, time.time(), b))
    return got


GBK_COLS = ["ts", "fetched", "symbol", "event", "event_expiry", "side", "level", "price", "qty"]


def gemini_book_rows(got, ts):
    out = []
    for sym, ev, exp, at, b in got:
        for side in ("bids", "asks"):
            lv = []
            for x in (b or {}).get(side) or []:
                p, q = px(x.get("price")), num(x.get("amount"))
                if p != "" and q != "" and q > 0:
                    lv.append((p, q))
            lv.sort(key=lambda t: -t[0] if side == "bids" else t[0])   # level 0 = best
            out += [dict(ts=ts, fetched=round(at, 2), symbol=sym, event=ev, event_expiry=exp,
                         side=side[:-1], level=i, price=p, qty=q) for i, (p, q) in enumerate(lv)]
    return out


KX = "https://api.elections.kalshi.com/trade-api/v2"
KX_WINDOW_S = 65 * 60
KX_DEPTH_MAX = 80
KX_CHUNK = 20
KX_DEPTH = []         # ladders read this run, handed to the kalshi_bk venue


def kx_listing(series, now_ts):
    out, cur = [], None
    for _ in range(20):
        q = {"series_ticker": series, "status": "open", "limit": 1000}
        if cur:
            q["cursor"] = cur
        d = P.get(KX + "/markets", q)
        out += d.get("markets") or []
        cur = d.get("cursor")
        if not cur:
            break
    keep = []
    for m in out:
        try:
            close = datetime.fromisoformat((m.get("close_time") or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if 0 < close - now_ts <= KX_WINDOW_S:
            keep.append((close, m))
    return keep


def kx_deep(m):
    """Deep in the money on either side: where the ladder rests its 0.90-0.96 bids."""
    b, a = px(m.get("yes_bid_dollars")), px(m.get("yes_ask_dollars"))
    return (b != "" and b >= 0.80) or (a != "" and a <= 0.20)


def kx_books(tickers):
    """Batch ladders, paired by the ticker each entry NAMES. The parameter must be repeated
    (tickers=A&tickers=B); the comma form returns one entry named "A,B" with both sides empty,
    which reads as a market with nothing resting (marketbot-android CLAUDE.md, 2026-11-16)."""
    got = {}
    for i in range(0, len(tickers), KX_CHUNK):
        chunk = tickers[i:i + KX_CHUNK]
        d = P.get(KX + "/markets/orderbooks", {"tickers": chunk})   # doseq: repeated
        at = time.time()
        for b in d.get("orderbooks") or []:
            if b.get("ticker") in chunk:
                got[b["ticker"]] = (at, b)
    return got


def fetch_kalshi_btc():
    now_ts = time.time()
    keep = kx_listing("KXBTCD", now_ts)
    deep = sorted((c, m["ticker"]) for c, m in keep if kx_deep(m))[:KX_DEPTH_MAX]
    KX_DEPTH[:] = []
    try:
        books = kx_books([t for _, t in deep])
        KX_DEPTH.extend((t, at, b) for t, (at, b) in books.items())
        log(f"    {len(keep)} markets in window, {len(deep)} deep, {len(books)} ladders")
    except RuntimeError as e:
        log(f"    ladders failed: {str(e)[:120]}")
    return [(now_ts, c, m) for c, m in keep]


KBTC_COLS = ["ts", "ticker", "event", "close", "lead_s", "strike", "yes_bid", "yes_bid_size",
             "yes_ask", "yes_ask_size", "last", "volume", "open_interest", "exchange_index"]


def kalshi_btc_rows(got, ts):
    return [dict(ts=ts, ticker=m.get("ticker"), event=m.get("event_ticker"), close=m.get("close_time"),
                 lead_s=int(c - now_ts), strike=num(m.get("floor_strike")),
                 yes_bid=px(m.get("yes_bid_dollars")), yes_bid_size=num(m.get("yes_bid_size_fp")),
                 yes_ask=px(m.get("yes_ask_dollars")), yes_ask_size=num(m.get("yes_ask_size_fp")),
                 last=px(m.get("last_price_dollars")), volume=num(m.get("volume_fp")),
                 open_interest=num(m.get("open_interest_fp")), exchange_index=m.get("exchange_index"))
            for now_ts, c, m in got]


def fetch_kalshi_depth():
    return list(KX_DEPTH)


KBK_COLS = ["ts", "fetched", "ticker", "side", "level", "price", "qty"]


def kalshi_depth_rows(got, ts):
    out = []
    for t, at, b in got:
        ob = b.get("orderbook_fp") or {}
        for side, tag in (("yes_dollars", "yes_bid"), ("no_dollars", "no_bid")):
            lv = []
            for x in ob.get(side) or []:            # [] and absent both mean nothing resting
                if not x or len(x) < 2:
                    continue
                p, q = px(x[0]), num(x[1])
                if p != "" and q != "" and q > 0:
                    lv.append((p, q))
            lv.sort(key=lambda z: -z[0])           # level 0 = best bid; never trust server order
            out += [dict(ts=ts, fetched=round(at, 2), ticker=t, side=tag, level=i, price=p, qty=q)
                    for i, (p, q) in enumerate(lv)]
    return out


def gemini_trade_rows(got, ts):
    return [dict(ts=ts, symbol=s, event=ev, event_expiry=exp, tid=t.get("tid"),
                 timestampms=t.get("timestampms"), price=px(t.get("price")), amount=num(t.get("amount")),
                 taker=t.get("type") or "") for s, ev, exp, t in got]


def fetch_pm():
    def f(off):
        r = P.get(PM, {"active": "true", "closed": "false", "limit": PM_PAGE, "offset": off,
                       "order": "volume24hr", "ascending": "false"})
        return r if isinstance(r, list) else (r.get("data") or [])
    return paged(f, lambda m: m.get("id"), PM_PAGE, 60)


PMUS_FROM = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_pmus():
    def f(off):
        # unfiltered, the listing is all CLOSED markets (2,599 of 2,599 on 2026-11-26)
        # and in id order the newest markets fall past any page cap (newest createdAt read: Oct 12),
        # so read from now forward by end date: what closes soonest is what a cap must not lose
        r = P.get(PMUS, {"limit": 200, "offset": off, "closed": "false", "active": "true",
                         "endDateMin": PMUS_FROM, "orderBy": "endDate", "orderDirection": "asc"})
        return r if isinstance(r, list) else (r.get("markets") or r.get("data") or [])
    LAST_PMUS[:] = paged(f, lambda m: m.get("id"), 200, 60)   # 20 pages stopped at exactly 4,000
    return LAST_PMUS


PMUS_BOOK_HOURS = 24
PMUS_BOOK_MAX = 150
LAST_PMUS = []


def pmus_targets(markets, now_ts, hours=PMUS_BOOK_HOURS, cap=PMUS_BOOK_MAX):
    out = []
    for m in markets:
        if m.get("closed") or not m.get("slug"):
            continue
        # a game's endDate can sit days after the game; the game start is when its book matters
        best = None
        for key in ("gameStartTime", "endDate"):
            try:
                t = datetime.fromisoformat((m.get(key) or "").replace("Z", "+00:00")).timestamp()
            except ValueError:
                continue
            if 0 < t - now_ts <= hours * 3600 and (best is None or t < best):
                best = t
        if best is not None:
            out.append((best, m["slug"]))
    out.sort()
    return [slug for _, slug in out[:cap]]


def fetch_pmus_books():
    if not LAST_PMUS:
        raise RuntimeError("no Polymarket US listing this run")
    gs = [m.get("gameStartTime") for m in LAST_PMUS if m.get("gameStartTime")]
    ids = [m.get("id") for m in LAST_PMUS]
    log(f"    {len(LAST_PMUS)} listed; gameStartTime on {len(gs)}, samples {sorted(gs)[-3:]}; "
        f"createdAt last {sorted(str(m.get('createdAt')) for m in LAST_PMUS)[-1:]}; ids {ids[:2]}..{ids[-2:]}; "
        f"categories {sorted({str(m.get('category')) for m in LAST_PMUS})[:12]}")
    got = []
    for i, slug in enumerate(pmus_targets(LAST_PMUS, time.time())):
        if i:
            time.sleep(0.1)          # ~20 req/s allowed; stay well under
        try:
            got.append((slug, time.time(), P.get(f"{PMUS}/{slug}/book")))
        except RuntimeError as e:
            log(f"    {slug}: {str(e)[:80]}")
    return got


PMBK_COLS = ["ts", "fetched", "slug", "raw"]
PMTOP_COLS = ["ts", "slug", "end", "game_start", "best_bid", "best_ask"]


def fetch_pmus_top():
    if not LAST_PMUS:
        raise RuntimeError("no Polymarket US listing this run")
    return LAST_PMUS


def pmus_top_rows(markets, ts):
    """The listing's own bestBidQuote / bestAskQuote, every open market, every run: top of book at
    no extra call. Kept raw for the same reason as the books."""
    import json
    raw = lambda v: "" if v is None else json.dumps(v, separators=(",", ":"))
    return [dict(ts=ts, slug=m.get("slug", ""), end=m.get("endDate") or "", game_start=m.get("gameStartTime") or "",
                 best_bid=raw(m.get("bestBidQuote")), best_ask=raw(m.get("bestAskQuote")))
            for m in markets if not m.get("closed")]


def pmus_book_rows(got, ts):
    """Stored raw (compact JSON): the book's shape was not confirmed live before this was written,
    and a guessed parser that silently drops a side is worse than a payload parsed later."""
    import json
    return [dict(ts=ts, fetched=round(at, 2), slug=slug, raw=json.dumps(b, separators=(",", ":")))
            for slug, at, b in got]


def fetch_pi():
    return P.get(PI)


VENUES = [
    ("gemini", fetch_gemini, gemini_rows, GEMINI_COLS),
    ("gemini_settled", fetch_gemini_settled, gemini_settled_rows, GEMSET_COLS),
    ("gemini_trades", fetch_gemini_trades, gemini_trade_rows, GTRD_COLS),
    ("gemini_books", fetch_gemini_books, gemini_book_rows, GBK_COLS),
    ("kalshi_btc", fetch_kalshi_btc, kalshi_btc_rows, KBTC_COLS),
    ("kalshi_btc_depth", fetch_kalshi_depth, kalshi_depth_rows, KBK_COLS),
    ("polymarket", fetch_pm, pm_rows, PM_COLS),
    ("pm_us", fetch_pmus, pmus_rows, PMUS_COLS),
    ("pm_us_top", fetch_pmus_top, pmus_top_rows, PMTOP_COLS),
    ("pm_us_books", fetch_pmus_books, pmus_book_rows, PMBK_COLS),
    ("predictit", fetch_pi, pi_rows, PI_COLS),
]


def write(path, cols, rows):
    with gzip.open(path, "wt", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def sparse_columns(rows, cols, below=0.95):
    """Columns filled on fewer than `below` of rows. A venue field that never populates reads the
    same as one that says "nothing happened" once it is in a CSV, so it is reported at write time."""
    if not rows:
        return ""
    out = []
    for c in cols:
        share = sum(1 for r in rows if r.get(c) not in (None, "")) / len(rows)
        if share < below:
            out.append(f"{c}={share:.0%}")
    return " ".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="", help="comma list of venues (default all)")
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    only = set(filter(None, a.only.split(",")))
    failed = 0
    log(f"recorder run {now()}")
    for name, fetch, flat, cols in VENUES:
        if only and name not in only:
            continue
        t0 = time.time()
        try:
            raw = fetch()
            ts = now()
            rows = flat(raw, ts)
            write(os.path.join(a.out, f"{name}.csv.gz"), cols, rows)
            log(f"  {name}: {len(rows)} rows in {time.time() - t0:.1f}s")
            thin = sparse_columns(rows, cols)
            if thin:
                log(f"    sparse columns (share filled): {thin}")
        except Exception as e:  # one venue down must not cost the others their snapshot
            failed += 1
            log(f"  {name}: FAILED {type(e).__name__}: {str(e)[:200]}")
    with open(os.path.join(a.out, "run.log"), "w") as fh:
        fh.write("\n".join(LOG) + "\n")
    return 0 if failed < len(VENUES) else 1


if __name__ == "__main__":
    sys.exit(main())
