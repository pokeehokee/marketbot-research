"""Per-series history: every settled market of named series, and every trade on it.

The taker-flow screen samples the whole exchange over 28 days. Some ideas need
the opposite shape: ONE series, as far back as Kalshi keeps it, with each
market's own terms (strike, rules, result) so a model can be priced against it.
TSA throughput, box office, mention markets and the like settle a handful of
times a month, so 28 days is a few events and nothing more.

Read-only and unauthenticated, through taker_flow_screen.get (whitelisted paths,
429 retried). Outputs, under out/:

  probe-<tag>.log               what was found, the rules text, the counts
  probe-<tag>-markets.csv.gz    one row per settled market, with its terms
  probe-<tag>-trades.csv.gz     one row per trade: ticker, ts, taker side, price paid, count

`--find REGEX` lists every series whose ticker, title or category matches, so
an idea is checked against what Kalshi actually lists before anything is pulled.

Run: python3 -I series_probe.py --tag tsa-1 --find 'TSA|AIRLINE|TRAVEL' --series KXTSAW
"""
import argparse, csv, gzip, json, os, re, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taker_flow_screen as T  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
TERM_FIELDS = ("ticker", "event_ticker", "title", "subtitle", "yes_sub_title", "strike_type",
               "floor_strike", "cap_strike", "open_time", "close_time", "expiration_time",
               "result", "status", "settlement_value_dollars", "expiration_value")


def pages(path, key, params, log, cap=200):
    """Every page of a cursor listing, or log the error and stop. Never silent."""
    out, cursor, n = [], None, 0
    while n < cap:
        q = dict(params)
        if cursor:
            q["cursor"] = cursor
        p, err = T.get(path, q)
        if err:
            log(f"  {path} {params}: {err} after {len(out)} rows")
            return out, err
        out.extend(p.get(key) or [])
        cursor = p.get("cursor")
        n += 1
        if not cursor:
            return out, None
    log(f"  {path} {params}: stopped at the {cap}-page cap with {len(out)} rows")
    return out, "page cap"


def find(regex, log):
    rows, err = pages("/series", "series", {"limit": 1000}, log)
    rx = re.compile(regex, re.I)
    hits = [s for s in rows if rx.search(" ".join(str(s.get(k) or "") for k in
                                                   ("ticker", "title", "category")))]
    log(f"/series: {len(rows)} listed, {len(hits)} match /{regex}/")
    for s in sorted(hits, key=lambda s: s.get("ticker") or ""):
        log(f"  {s.get('ticker'):<24} {s.get('category') or '?':<22} {s.get('frequency') or '?':<10} "
            f"{s.get('fee_type') or '?':<26} {s.get('title')}")
    return hits


def settled_markets(series, log):
    """Live tier, then the historical tier; de-duplicated by ticker."""
    live, _ = pages("/markets", "markets", {"series_ticker": series, "status": "settled",
                                             "limit": 1000}, log)
    hist, herr = pages("/historical/markets", "markets", {"series_ticker": series,
                                                          "limit": 1000}, log)
    seen, out = set(), []
    for m in live + hist:
        t = m.get("ticker")
        if t and t not in seen and (m.get("result") in ("yes", "no")):
            seen.add(t); out.append(m)
    log(f"{series}: {len(live)} live settled, {len(hist)} historical"
        f"{' (historical read failed: ' + herr + ')' if herr else ''}, {len(out)} with a result")
    return out


def trades_for(ticker, cutoff_ts, close_ts, log):
    rows, _ = pages("/markets/trades", "trades", {"ticker": ticker, "limit": 1000}, log)
    if cutoff_ts and close_ts and close_ts < cutoff_ts + 86400 * 2:
        old, _ = pages("/historical/trades", "trades", {"ticker": ticker, "limit": 1000}, log)
        ids = {r.get("trade_id") for r in rows}
        rows += [r for r in old if r.get("trade_id") not in ids]
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--find", default="")
    ap.add_argument("--series", default="")
    ap.add_argument("--rules-only", default="", help="series to print one market's rules for")
    ap.add_argument("--max-markets", type=int, default=4000)
    ap.add_argument("--ticker-re", default="", help="pull trades only for markets whose ticker matches")
    a = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    logf = open(os.path.join(OUT, f"probe-{a.tag}.log"), "w")

    def log(s):
        print(s); logf.write(s + "\n"); logf.flush()

    p, err = T.get("/historical/cutoff")
    cutoff_ts = T.utc(p.get("trades_created_ts")) if p and isinstance(p.get("trades_created_ts"), str) else None
    log(f"cutoff: {json.dumps(p) if p else err}")
    hits = find(a.find, log) if a.find else []

    for s in [x for x in a.rules_only.split(",") if x]:
        ms, _ = pages("/markets", "markets", {"series_ticker": s, "limit": 5}, log, cap=1)
        if ms:
            m = ms[0]
            log(f"\nRULES {s} ({m.get('ticker')}):\n  primary: {m.get('rules_primary')}\n"
                f"  secondary: {m.get('rules_secondary')}")
        else:
            log(f"\nRULES {s}: no market listed")

    series = [x for x in a.series.split(",") if x and x != "FOUND"]
    if "FOUND" in a.series.split(","):
        # Only series that settle often enough to test; the rest are listed above.
        found = [h["ticker"] for h in hits if (h.get("frequency") or "") in
                 ("daily", "weekly", "hourly", "monthly")]
        log(f"FOUND -> pulling {found[:10]}" + (f" (of {len(found)})" if len(found) > 10 else ""))
        series += found[:10]
    if not series:
        return
    mf = gzip.open(os.path.join(OUT, f"probe-{a.tag}-markets.csv.gz"), "wt", newline="")
    tf = gzip.open(os.path.join(OUT, f"probe-{a.tag}-trades.csv.gz"), "wt", newline="")
    mw, tw = csv.writer(mf), csv.writer(tf)
    mw.writerow(("series",) + TERM_FIELDS)
    tw.writerow(("ticker", "ts", "taker_side", "taker_paid", "count"))
    for s in series:
        ms = settled_markets(s, log)
        if a.ticker_re:
            rx = re.compile(a.ticker_re)
            n0 = len(ms)
            ms = [m for m in ms if rx.search(m.get("ticker") or "")]
            log(f"  {s}: {len(ms)} of {n0} markets match /{a.ticker_re}/")
        ms = ms[:a.max_markets]
        if ms:
            log(f"  rules ({ms[0].get('ticker')}): {ms[0].get('rules_primary')}")
        ntr = skipped = 0
        for m in ms:
            mw.writerow((s,) + tuple(m.get(k) for k in TERM_FIELDS))
            for t in trades_for(m["ticker"], cutoff_ts, T.utc(m.get("close_time")), log):
                f = T.trade_fields(t)
                if f is None:
                    skipped += 1
                    continue
                tw.writerow((f[0], f"{f[1]:.0f}", f[2], f"{f[3]:.4f}", f"{f[4]:g}"))
                ntr += 1
        log(f"  {s}: {ntr} trades written, {skipped} skipped (missing price or side)")
    mf.close(); tf.close()


if __name__ == "__main__":
    main()
