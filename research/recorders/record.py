"""Forward recorder for venues that keep no public price history (ideas P1-P3, P5, P7, P12, P14).

Gemini Predictions keeps no prices on a settled contract and serves no public trades;
Polymarket's trade history endpoints return 410 and its price history is empty once a market
closes; PredictIt serves current prices only. The one way to test a pricing idea on them is to
write the book down while it exists. One run = one snapshot of every venue below:

  gemini     every active event's contracts: best bid/ask, last, buy/sell per side
  gemini_set events Gemini lists as recently settled: resolution side and settlement value
  polymarket active international markets (paged): best bid/ask, last, volume, outcome prices
  pm_us      Polymarket US markets: outcome prices as listed
  predictit  every contract: best buy/sell for YES and NO, last trade

Each row carries `ts`, the moment THAT venue's read returned (venues are read one after another,
seconds apart). A missing price is written empty, never 0: a 0 meaning "no quote" and a 0
meaning "worthless" are indistinguishable downstream (CLAUDE.md, the longshot liquidity error).

Kalshi is not recorded: its own candlesticks keep 1-minute bid/ask history, so the Kalshi side
of any comparison is read afterwards.

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
        page = fetch(i * limit)
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
    return paged(f, lambda e: e.get("ticker"), 500, 10)


def fetch_gemini_settled():
    return P.get(G + "/events/recently-settled").get("data") or []


def fetch_pm():
    def f(off):
        r = P.get(PM, {"active": "true", "closed": "false", "limit": PM_PAGE, "offset": off,
                       "order": "volume24hr", "ascending": "false"})
        return r if isinstance(r, list) else (r.get("data") or [])
    return paged(f, lambda m: m.get("id"), PM_PAGE, 60)


def fetch_pmus():
    def f(off):
        r = P.get(PMUS, {"limit": 200, "offset": off})
        return r if isinstance(r, list) else (r.get("markets") or r.get("data") or [])
    return paged(f, lambda m: m.get("id"), 200, 20)


def fetch_pi():
    return P.get(PI)


VENUES = [
    ("gemini", fetch_gemini, gemini_rows, GEMINI_COLS),
    ("gemini_settled", fetch_gemini_settled, gemini_settled_rows, GEMSET_COLS),
    ("polymarket", fetch_pm, pm_rows, PM_COLS),
    ("pm_us", fetch_pmus, pmus_rows, PMUS_COLS),
    ("predictit", fetch_pi, pi_rows, PI_COLS),
]


def write(path, cols, rows):
    with gzip.open(path, "wt", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


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
        except Exception as e:  # one venue down must not cost the others their snapshot
            failed += 1
            log(f"  {name}: FAILED {type(e).__name__}: {str(e)[:200]}")
    with open(os.path.join(a.out, "run.log"), "w") as fh:
        fh.write("\n".join(LOG) + "\n")
    return 0 if failed < len(VENUES) else 1


if __name__ == "__main__":
    sys.exit(main())
