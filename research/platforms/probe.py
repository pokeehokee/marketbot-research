"""One cheap, read-only look at what each other platform's PUBLIC data can actually support.

Every idea in edge-discovery/68-plus-other-platforms-idea-list.md needs history to be tested,
and the docs leave the depth of that history open on most venues. This answers it per venue
before any sampler is written: how far back settled events go, whether price history or public
trades exist for a settled market, and at what resolution. No key, no account, GET only, and
`get` refuses any host or method outside ALLOWED. Output: out/probe-<tag>.log.
"""
import argparse, gzip, io, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
ALLOWED = {
    "api.gemini.com": ("/v1/prediction-markets/events", "/v1/prediction-markets/maker-rebate/rates",
                       "/v1/prediction-markets/categories", "/v2/klines",
                       "/v1/trades"),  # /v1/trades/{GEMI-...}: prediction prints while a contract is open
    "gamma-api.polymarket.com": ("/markets", "/events", "/public-search"),
    "clob.polymarket.com": ("/prices-history",),
    "data-api.polymarket.com": ("/trades", "/activity"),
    "gateway.polymarket.us": ("/v1/markets",),
    "forecastex.com": ("/api/download",),
    "www.predictit.org": ("/api/marketdata/all/",),
    "api.crypto.com": ("/exchange/v1/public/get-instruments", "/exchange/v1/public/get-trades"),
}
LOG = []


def log(s=""):
    print(s, flush=True)
    LOG.append(str(s))


def get(url, params=None, raw=False):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" or u.hostname not in ALLOWED or not any(
            u.path == p or u.path.startswith(p + "/") for p in ALLOWED[u.hostname]):
        raise RuntimeError(f"refusing a non-whitelisted read: {url}")
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    req = urllib.request.Request(url, headers={"User-Agent": "research", "Accept": "*/*"}, method="GET")
    for nap in (0, 3, 10):
        if nap:
            time.sleep(nap)
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                time.sleep(0.4)
                return body if raw else json.loads(body)
        except urllib.error.HTTPError as e:
            if e.code in (429, 502, 503, 504):
                continue
            raise RuntimeError(f"HTTP {e.code} {url[:160]}")
    raise RuntimeError(f"gave up after retries: {url[:160]}")


def section(name, fn):
    log(f"\n== {name}")
    try:
        fn()
    except Exception as e:  # a probe that fails is a finding, not a crash
        log(f"  FAILED: {type(e).__name__}: {str(e)[:300]}")


def ms(t):
    return int(t.timestamp() * 1000)


def gemini():
    G = "https://api.gemini.com/v1/prediction-markets"
    for st in ("settled", "active", "closed"):
        p = get(G + "/events", {"status": st, "limit": 500})
        d, pg = p.get("data") or [], p.get("pagination") or {}
        cats = {}
        for e in d:
            cats[e.get("category")] = cats.get(e.get("category"), 0) + 1
        log(f"  events status={st}: total {pg.get('total')}, first page {len(d)}, categories {dict(sorted(cats.items(), key=lambda x: -x[1])[:8])}")
        if d:
            keys = sorted(d[0].keys())
            log(f"    event keys: {keys}")
            dates = sorted(x.get("expiryDate") or x.get("closeDate") or "" for x in d if (x.get("expiryDate") or x.get("closeDate")))
            if dates:
                log(f"    expiry range on this page: {dates[0]} .. {dates[-1]}")
            btc = [e for e in d if "BTC" in (e.get("ticker") or "") or "Bitcoin" in (e.get("title") or "")]
            log(f"    BTC events on this page: {len(btc)}; tickers e.g. {[e.get('ticker') for e in btc[:6]]}")
        if st == "settled" and pg.get("total") and pg["total"] > 500:
            q = get(G + "/events", {"status": st, "limit": 500, "offset": max(0, pg["total"] - 500)})
            dd = [x.get("expiryDate") or "" for x in q.get("data") or []]
            dd = sorted(x for x in dd if x)
            if dd:
                log(f"    last page expiry range: {dd[0]} .. {dd[-1]}  (depth of the settled listing)")
    # one settled BTC event in full: does it carry prices or a book after settlement?
    p = get(G + "/events", {"status": "settled", "limit": 100, "search": "BTC"})
    d = p.get("data") or []
    log(f"  settled search=BTC: total {(p.get('pagination') or {}).get('total')}")
    if d:
        t = d[0].get("ticker")
        e = get(G + f"/events/{urllib.parse.quote(t)}")
        c = (e.get("contracts") or [{}])[0]
        log(f"    {t}: sourceDetails {e.get('sourceDetails')}, settlement {e.get('settlement')}, "
            f"contracts {len(e.get('contracts') or [])}, contractOrderbooks {'yes' if e.get('contractOrderbooks') else 'no'}")
        log(f"    contract keys: {sorted(c.keys())}")
        log(f"    contract prices: {c.get('prices')}")
    r = get(G + "/maker-rebate/rates")
    log(f"  maker rebate rates: {json.dumps(r)[:400]}")
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for back in (1, 30, 90, 365):
        t0 = now - timedelta(days=back)
        try:
            k = get("https://api.gemini.com/v2/klines", {"agency": "lukka", "index": "LS-LRRR-XBTUSD",
                                                          "interval": "1m", "startTime": ms(t0),
                                                          "endTime": ms(t0 + timedelta(hours=1))})
            rows = k if isinstance(k, list) else k.get("data") or k.get("klines") or []
            log(f"  lukka BTC 1m klines {back}d back: {len(rows)} rows; first {rows[:1]}")
        except Exception as ex:
            log(f"  lukka BTC 1m klines {back}d back: {ex}")


def polymarket_intl():
    m = get("https://gamma-api.polymarket.com/markets", {"closed": "true", "limit": 500,
                                                          "order": "endDate", "ascending": "false"})
    log(f"  closed markets, newest 500: {len(m)}")
    if m:
        log(f"    market keys: {sorted(m[0].keys())[:60]}")
    btc = [x for x in m if "bitcoin" in (x.get("question") or "").lower()]
    log(f"    with 'Bitcoin' in the question: {len(btc)}; e.g. {[x.get('question') for x in btc[:5]]}")
    fed = get("https://gamma-api.polymarket.com/markets", {"closed": "true", "limit": 200, "tag_slug": "fed-rates"})
    log(f"    closed tag fed-rates: {len(fed)}; e.g. {[x.get('question') for x in fed[:3]]}")
    pick = (btc or m)[0]
    toks = pick.get("clobTokenIds")
    toks = json.loads(toks) if isinstance(toks, str) else toks
    log(f"    sample: {pick.get('question')} | end {pick.get('endDate')} | resolutionSource {pick.get('resolutionSource')!r}"
        f" | feeType? {pick.get('feeType') or pick.get('fee') or pick.get('makerBaseFee')}")
    if toks:
        h = get("https://clob.polymarket.com/prices-history", {"market": toks[0], "interval": "max", "fidelity": 1})
        hist = h.get("history") or []
        if hist:
            ts = [x["t"] for x in hist]
            gaps = sorted(b - a for a, b in zip(ts, ts[1:])) or [0]
            log(f"    prices-history fidelity=1: {len(hist)} points, span {(ts[-1]-ts[0])/3600:.1f} h, median gap {gaps[len(gaps)//2]} s")
        else:
            log(f"    prices-history: empty ({str(h)[:200]})")
    cid = pick.get("conditionId")
    if cid:
        tr = get("https://data-api.polymarket.com/trades", {"market": cid, "limit": 500})
        if tr:
            ts = sorted(x.get("timestamp") for x in tr if x.get("timestamp"))
            log(f"    data-api trades: {len(tr)} rows, keys {sorted(tr[0].keys())}, span {(ts[-1]-ts[0])/3600 if ts else 0:.1f} h")
        else:
            log("    data-api trades: none")
    old = get("https://gamma-api.polymarket.com/markets", {"closed": "true", "limit": 5, "order": "endDate",
                                                            "ascending": "true"})
    log(f"    oldest closed: {[x.get('endDate') for x in old]}")


def polymarket_us():
    p = get("https://gateway.polymarket.us/v1/markets", {"limit": 50})
    ms_ = p.get("markets") if isinstance(p, dict) else p
    log(f"  /v1/markets: {type(p).__name__}, {len(ms_ or [])} rows; keys {sorted((ms_ or [{}])[0].keys())[:40]}")
    btc = [x for x in ms_ or [] if "btc" in json.dumps(x).lower()[:2000]]
    log(f"    BTC-looking: {len(btc)}")


def forecastex():
    found = {}
    d = datetime.now(timezone.utc).date()
    for i in range(1, 46, 3):
        day = (d - timedelta(days=i)).strftime("%Y%m%d")
        try:
            b = get("https://forecastex.com/api/download", {"type": "summary", "date": day}, raw=True)
            text = b.decode("utf-8", "replace")
            lines = text.strip().splitlines()
            found[day] = (len(lines), lines[0][:200] if lines else "")
        except Exception as e:
            found[day] = (0, str(e)[:80])
    for k in sorted(found):
        log(f"  summary {k}: {found[k][0]} lines | {found[k][1]}")


def predictit():
    p = get("https://www.predictit.org/api/marketdata/all/")
    ms_ = p.get("markets") or []
    nc = sum(len(x.get("contracts") or []) for x in ms_)
    log(f"  snapshot: {len(ms_)} markets, {nc} contracts; keys {sorted((ms_ or [{}])[0].keys())}")
    if ms_:
        c = (ms_[0].get("contracts") or [{}])[0]
        log(f"    contract keys: {sorted(c.keys())}; timeStamp {ms_[0].get('timeStamp')}")


def cryptocom():
    p = get("https://api.crypto.com/exchange/v1/public/get-instruments")
    d = ((p.get("result") or {}).get("data")) or []
    kinds = {}
    for x in d:
        k = (x.get("inst_type"), x.get("product_type"))
        kinds[k] = kinds.get(k, 0) + 1
    log(f"  instruments: {len(d)}; by (inst_type, product_type): {kinds}")


def gemini2():
    """Round 2: a settled BTC event in full, the 24h settled feed, and how deep Lukka klines go."""
    G = "https://api.gemini.com/v1/prediction-markets"
    p = get(G + "/events", {"status": "settled", "limit": 100, "search": "BTC"})
    d = p.get("data") or []
    log(f"  settled search=BTC: total {(p.get('pagination') or {}).get('total')}")
    kinds = {}
    for e in d:
        t = e.get("ticker") or ""
        k = "".join(ch for ch in t if not ch.isdigit())
        kinds[k] = kinds.get(k, 0) + 1
    log(f"    ticker shapes: {kinds}")
    for e in d[:2]:
        t = e.get("ticker")
        full = get(G + f"/events/{urllib.parse.quote(t)}")
        cs = full.get("contracts") or []
        log(f"    {t} '{full.get('title')}': type {full.get('type')}, sourceDetails {full.get('sourceDetails')}, "
            f"settlement {full.get('settlement')}, volume {full.get('volume')}, contracts {len(cs)}")
        for c in cs[:3]:
            log(f"      {c.get('instrumentSymbol')} strike {c.get('strike')} res {c.get('resolutionSide')} "
                f"settle {c.get('settlementValue')} prices {c.get('prices')}")
        if cs:
            log(f"      contract keys: {sorted(cs[0].keys())}")
    try:
        r = get(G + "/events/recently-settled")
        rr = r.get("data") if isinstance(r, dict) else r
        log(f"  recently-settled: {len(rr or [])} events")
    except Exception as ex:
        log(f"  recently-settled: {ex}")
    a = get(G + "/events", {"status": "active", "search": "BTC", "limit": 50})
    for e in (a.get("data") or [])[:3]:
        cs = e.get("contracts") or []
        log(f"  active {e.get('ticker')} '{e.get('title')}' expiry {e.get('expiryDate')} contracts {len(cs)}; "
            f"first prices {(cs or [{}])[0].get('prices')}")
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    for back in (1, 7, 30, 90, 365):
        t0 = now - timedelta(days=back)
        try:
            k = get("https://api.gemini.com/v2/klines", {"agency": "lukka", "index": "LS-LRRR-XBTUSD",
                                                          "interval": "1m", "startTime": ms(t0),
                                                          "endTime": ms(t0 + timedelta(hours=1))})
            rows = k if isinstance(k, list) else (k.get("data") or k.get("klines") or k)
            log(f"  lukka BTC 1m klines {back}d back: {len(rows) if isinstance(rows, list) else type(rows).__name__}; "
                f"sample {str(rows)[:200]}")
        except Exception as ex:
            log(f"  lukka BTC 1m klines {back}d back: {ex}")


def polymarket2():
    """Round 2: find recent closed crypto markets by date, then their price history and trades."""
    since = (datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    m = get("https://gamma-api.polymarket.com/markets", {"closed": "true", "limit": 100,
                                                          "end_date_min": since, "end_date_max": until})
    log(f"  closed, ending in the last 3 days: {len(m)}")
    qs = [x.get("question") or "" for x in m]
    btc = [x for x in m if any(w in (x.get("question") or "") for w in ("Bitcoin", "BTC"))]
    log(f"    crypto-looking: {len(btc)}; questions e.g. {qs[:8]}")
    try:
        ps = get("https://gamma-api.polymarket.com/public-search", {"q": "bitcoin up or down", "limit_per_type": 20,
                                                                     "events_status": "closed"})
        evs = ps.get("events") or []
        log(f"  public-search 'bitcoin up or down' closed: {len(evs)} events; e.g. "
            f"{[(e.get('title'), e.get('endDate')) for e in evs[:5]]}")
        for e in evs:
            for mk in e.get("markets") or []:
                btc.append(mk)
    except Exception as ex:
        log(f"  public-search: {ex}")
    for pick in btc[:2]:
        toks = pick.get("clobTokenIds")
        toks = json.loads(toks) if isinstance(toks, str) else toks
        log(f"    {pick.get('question')} | end {pick.get('endDate')} | resolutionSource {pick.get('resolutionSource')!r} "
            f"| feeType {pick.get('feeType')} feeSchedule {str(pick.get('feeSchedule'))[:120]}")
        if toks:
            h = get("https://clob.polymarket.com/prices-history", {"market": toks[0], "interval": "max", "fidelity": 1})
            hist = h.get("history") or []
            if hist:
                ts = [x["t"] for x in hist]
                gaps = sorted(b - a for a, b in zip(ts, ts[1:])) or [0]
                log(f"      prices-history: {len(hist)} points, span {(ts[-1]-ts[0])/3600:.1f} h, median gap {gaps[len(gaps)//2]} s")
            else:
                log(f"      prices-history: empty")
        cid = pick.get("conditionId")
        for path, q in (("/trades", {"market": cid, "limit": 100}), ("/trades", {"limit": 5}),
                        ("/activity", {"market": cid, "limit": 5})):
            try:
                tr = get("https://data-api.polymarket.com" + path, q)
                log(f"      data-api {path} {list(q)}: {len(tr) if isinstance(tr, list) else str(tr)[:150]}")
            except Exception as ex:
                log(f"      data-api {path} {list(q)}: {ex}")


def forecastex3():
    """Round 3: can a settled ForecastEx contract's outcome be read from the daily files, and is
    there any volume? Every contract expiring between the first and last date gets its rows
    followed to the end."""
    import csv
    d = datetime.now(timezone.utc).date()
    days = [(d - timedelta(days=i)).strftime("%Y%m%d") for i in range(1, 36)]
    rows = {}
    for day in days:
        try:
            b = get("https://forecastex.com/api/download", {"type": "prices", "date": day}, raw=True)
        except Exception as ex:
            log(f"  prices {day}: {ex}")
            continue
        rd = list(csv.DictReader(io.StringIO(b.decode("utf-8", "replace"))))
        rows[day] = rd
        if day in days[:1] or day in days[-1:]:
            log(f"  prices {day}: {len(rd)} rows; header {list(rd[0].keys()) if rd else []}")
            log(f"    sample {rd[:2]}")
    if not rows:
        return
    traded = sum(1 for rd in rows.values() for r in rd if float(r.get("pair_quantity") or 0) > 0)
    total = sum(len(rd) for rd in rows.values())
    log(f"  {len(rows)} daily files, {total} rows, {traded} with pair_quantity > 0")
    by = {}
    for day, rd in rows.items():
        for r in rd:
            by.setdefault((r["event_contract"], r["subtype"]), []).append((day, r))
    exp = {}
    for k, v in by.items():
        e = (v[0][1].get("expiration_date") or "")[:10].replace("-", "")
        if days[-1] < e < days[0]:
            exp[k] = (e, sorted(v))
    log(f"  contract-sides expiring inside the window: {len(exp)}")
    last_end, after = [], 0
    for k, (e, v) in list(exp.items()):
        lastday, r = v[-1]
        if lastday >= e:
            after += 1
        try:
            last_end.append(float(r.get("end_price") or r.get("settlement_price") or "nan"))
        except ValueError:
            pass
    near = sum(1 for x in last_end if x <= 0.03 or x >= 0.97)
    log(f"    last row on or after expiry: {after} of {len(exp)}; last end_price within 3c of 0 or 1: "
        f"{near} of {len(last_end)}")
    for k, (e, v) in list(exp.items())[:4]:
        log(f"    {k} exp {e}: " + "; ".join(f"{dd} end {r.get('end_price')} q {r.get('pair_quantity')}"
                                          for dd, r in v[-4:]))
    vol = {}
    for (c, sd), v in by.items():
        p = c.split("_")[0]
        vol[p] = vol.get(p, 0) + sum(float(r.get("pair_quantity") or 0) for _, r in v)
    top = sorted(vol.items(), key=lambda x: -x[1])[:12]
    log(f"  pairs traded over the window by product (top 12): {top}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--round", type=int, default=1)
    a = ap.parse_args(argv)
    log(f"platform probe {a.tag} at {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    rounds = {3: (("ForecastEx, round 3", forecastex3),), 2: (("Gemini Predictions, round 2", gemini2), ("Polymarket international, round 2", polymarket2))}
    for name, fn in rounds.get(a.round) or (("Gemini Predictions", gemini), ("Polymarket international", polymarket_intl),
                     ("Polymarket US", polymarket_us), ("ForecastEx", forecastex),
                     ("PredictIt", predictit), ("Crypto.com Exchange", cryptocom)):
        section(name, fn)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"probe-{a.tag}.log"), "w") as fh:
        fh.write("\n".join(LOG) + "\n")


if __name__ == "__main__":
    main()
