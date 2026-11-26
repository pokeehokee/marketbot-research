"""Idea #68: does Kalshi's last BTC minute pay at ANY speed?

KXBTCD (hourly) and KXBTC15M settle on the average of CF Benchmarks' BRTI over the 60 seconds
before close. Inside that minute part of the average is already fixed. A trader who knows the
fixed part, the current price and the volatility can price the rest. If prints inside the
minute happen at prices the fixed part already contradicts, the last minute pays and speed is
worth buying. If not, a faster server buys nothing. This is the test to run BEFORE anyone pays
for a server.

BRTI itself is not free. The proxy is Kraken's public trade tape (one of BRTI's constituent
venues; no account, no key), sampled to one price per second. Two controls stop the proxy from
being silently trusted:

  * proxy 60-second average against the exchange's own `expiration_value` (the real BRTI
    average): the gap's median and spread are logged, and the scorer may only use a gap fitted
    on the TRAINING half;
  * proxy-implied result against Kalshi's result, by distance from the strike.

No lookahead: a trade at Kraken time u is usable at u + LAG_S. For each Kalshi print at ts:
  grid      the 60 settlement seconds t1-59 .. t1, each priced by the last trade at or before it
  locked    the grid seconds <= ts - LAG_S (known at the print): their count and sum
  spot      last trade at or before ts - LAG_S
  rv        std of 1-second log changes over the RV_S seconds before ts - LAG_S
  fut_avg   the average of the grid seconds NOT yet known (outcome; scorer's train half only)

Read-only. Kalshi through taker_flow_screen.get (whitelisted, unauthenticated); Kraken through
kraken_get, which refuses anything but /0/public/Trades. Outputs under out/:
  btc-<tag>.log, btc-<tag>-trades.csv.gz
"""
import argparse, bisect, csv, gzip, json, math, os, sys, time, urllib.error, urllib.parse, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taker_flow_screen as T  # noqa: E402
import series_probe as P  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
KRAKEN = "https://api.kraken.com"
KRAKEN_ALLOWED = ("/0/public/Trades",)
LAG_S = 1.0          # a Kraken trade is usable one second after its timestamp
AVG_S = 60           # settlement average length
WINDOW_S = 120       # prints kept: the last two minutes (the first minute is the no-lock baseline)
RV_S = 300
PRE_S = AVG_S + WINDOW_S + RV_S + 30


def kraken_get(path, params, _sleep=time.sleep):
    if path not in KRAKEN_ALLOWED:
        raise RuntimeError(f"refusing a non-whitelisted Kraken path: {path}")
    url = KRAKEN + path + "?" + urllib.parse.urlencode(params)
    last = None
    for nap in (0, 2, 5, 12, 30, 60):
        if nap:
            _sleep(nap)
        try:
            p = json.load(urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "research"}), timeout=60))
            errs = p.get("error") or []
            if errs:
                last = ";".join(errs)
                if any("Too many" in e or "Unavailable" in e or "Busy" in e for e in errs):
                    continue
                return None, last
            return p.get("result") or {}, None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504, 520):
                return None, last
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
    return None, last


def parse_trades(result):
    """(rows [(time_s, price, trade_id)], last_cursor) from a Kraken Trades result. Pair key is
    whatever key is not 'last' (XBTUSD comes back as XXBTZUSD). `last` is a nanosecond string;
    pages overlap by one trade at the boundary, so callers de-duplicate on trade_id."""
    rows = []
    for k, v in (result or {}).items():
        if k == "last" or not isinstance(v, list):
            continue
        for t in v:
            try:
                rows.append((float(t[2]), float(t[0]), t[6] if len(t) > 6 else None))
            except (TypeError, ValueError, IndexError):
                continue
    return rows, (result or {}).get("last")


class Tape:
    """One price per second from Kraken trades, fetched per close window and cached."""

    def __init__(self, pair, log, pace=1.1):
        self.pair, self.log, self.pace = pair, log, pace
        self.ts, self.px = [], []
        self.calls = self.fails = 0

    def fetch(self, start, end):
        cursor = str(int(start * 1e9))
        got, seen = [], set()
        for _ in range(40):
            time.sleep(self.pace)
            self.calls += 1
            res, err = kraken_get("/0/public/Trades", {"pair": self.pair, "since": cursor, "count": 1000})
            if err:
                self.fails += 1
                if self.fails <= 5:
                    self.log(f"  kraken {start:.0f}: {err}")
                break
            rows, nxt = parse_trades(res)
            for r in rows:
                if r[0] >= start - 1 and (r[2] is None or r[2] not in seen):
                    seen.add(r[2]); got.append(r[:2])
            if not rows or not nxt or nxt == cursor or rows[-1][0] > end:
                break
            cursor = nxt
        got.sort()
        self.ts = [r[0] for r in got]
        self.px = [r[1] for r in got]
        return len(got)

    def at(self, t, max_age=30.0):
        i = bisect.bisect_right(self.ts, t) - 1
        if i < 0 or t - self.ts[i] > max_age:
            return None
        return self.px[i]

    def rv(self, t):
        pts = [self.at(t - k, max_age=60.0) for k in range(RV_S, -1, -1)]
        rets = [math.log(b / a) for a, b in zip(pts, pts[1:]) if a and b]
        if len(rets) < RV_S // 2:
            return None
        m = sum(rets) / len(rets)
        v = sum((r - m) ** 2 for r in rets) / (len(rets) - 1)
        return math.sqrt(v) if v > 0 else None


def grid(t1):
    """The 60 settlement seconds, oldest first: t1-59 .. t1."""
    return [t1 - AVG_S + 1 + k for k in range(AVG_S)]


def lock_state(tape, t1, ts):
    """(n_locked, locked_sum, fut_avg) for a print at ts, or None if a grid second is unpriced."""
    g = grid(t1)
    vals = [tape.at(s) for s in g]
    if any(v is None for v in vals):
        return None
    known = [v for s, v in zip(g, vals) if s <= ts - LAG_S]
    fut = vals[len(known):]
    if not fut:
        return None
    return len(known), sum(known), sum(fut) / len(fut)


def implied(strike_type, floor, cap, value):
    st = (strike_type or "").lower()
    if value is None:
        return None
    if st == "greater" and floor is not None:
        return "yes" if value > floor else "no"
    if st == "greater_or_equal" and floor is not None:
        return "yes" if value >= floor else "no"
    if st == "less" and cap is not None:
        return "yes" if value < cap else "no"
    if st == "less_or_equal" and cap is not None:
        return "yes" if value <= cap else "no"
    if st == "between" and floor is not None and cap is not None:
        return "yes" if floor <= value <= cap else "no"
    return None


def window_trades(ticker, t1, cutoff_ts, log):
    q = {"ticker": ticker, "min_ts": int(t1 - WINDOW_S - 1), "max_ts": int(t1 + 1), "limit": 1000}
    rows, _ = P.pages("/markets/trades", "trades", q, log)
    if cutoff_ts and t1 < cutoff_ts + 86400 * 2:
        old, _ = P.pages("/historical/trades", "trades", q, log)
        ids = {r.get("trade_id") for r in rows}
        rows += [r for r in old if r.get("trade_id") not in ids]
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--series", default="KXBTC15M,KXBTCD")
    ap.add_argument("--days", type=int, default=10)
    ap.add_argument("--near", type=float, default=400.0, help="KXBTCD strikes within $ of spot at t1-180")
    ap.add_argument("--pair", default="XBTUSD")
    ap.add_argument("--budget", type=float, default=6000.0,
                    help="stop fetching after this many seconds so the file closes cleanly and the "
                         "controls still print (the FX run was killed mid-write and left a truncated gzip)")
    a = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    logf = open(os.path.join(OUT, f"btc-{a.tag}.log"), "w")

    def log(s):
        print(s); logf.write(s + "\n"); logf.flush()

    p, err = T.get("/historical/cutoff")
    cutoff_ts = T.utc(p.get("trades_created_ts")) if p and isinstance(p.get("trades_created_ts"), str) else None
    since = time.time() - a.days * 86400
    by_close = {}
    for s in [x for x in a.series.split(",") if x]:
        # Filter by close time at the exchange: KXBTCD lists ~100 strikes an hour, so paging the
        # whole settled history first (series_probe.settled_markets) would spend the run's budget
        # before a single trade is read. The same filter is used by sports_lines.py.
        rows, _ = P.pages("/markets", "markets", {"series_ticker": s, "status": "settled",
                                                  "min_close_ts": int(since), "limit": 1000}, log)
        ms = [m for m in rows if m.get("result") in ("yes", "no")
              and (T.utc(m.get("close_time")) or 0) >= since]
        log(f"  {s}: {len(ms)} settled markets closing in the last {a.days} days")
        if ms:
            m0 = ms[0]
            log(f"  sample {m0.get('ticker')}: strike_type {m0.get('strike_type')} floor "
                f"{m0.get('floor_strike')} cap {m0.get('cap_strike')} expiration_value "
                f"{m0.get('expiration_value')}\n  rules: {(m0.get('rules_primary') or '')[:400]}"
                f"\n  rules2: {(m0.get('rules_secondary') or '')[:400]}")
        for m in ms:
            by_close.setdefault(int(T.utc(m["close_time"])), []).append((s, m))
    log(f"{len(by_close)} distinct close times")

    tf = gzip.open(os.path.join(OUT, f"btc-{a.tag}-trades.csv.gz"), "wt", newline="")
    tw = csv.writer(tf)
    tw.writerow(("series", "ticker", "t1", "strike_type", "floor", "cap", "result", "expiration_value",
                 "proxy_avg", "trade_ts", "taker_side", "taker_paid", "count",
                 "n_locked", "locked_sum", "spot", "rv", "fut_avg"))
    tape = Tape(a.pair, log)
    agree = disagree = unpriced = ntr = nmk = 0
    gaps, band = [], {}
    t_start = time.time()
    for i, t1 in enumerate(sorted(by_close)):
        if time.time() - t_start > a.budget:
            log(f"BUDGET reached after {i} of {len(by_close)} close times: stopping cleanly")
            break
        n = tape.fetch(t1 - PRE_S, t1 + 2)
        vals = [tape.at(s) for s in grid(t1)]
        if n == 0 or any(v is None for v in vals):
            unpriced += len(by_close[t1])
            continue
        pavg = sum(vals) / AVG_S
        ref = tape.at(t1 - WINDOW_S - 60)
        for s, m in by_close[t1]:
            fl, cp = T.num(m, "floor_strike"), T.num(m, "cap_strike")
            if s == "KXBTCD":
                k = fl if fl is not None else cp
                if ref is None or k is None or abs(k - ref) > a.near:
                    continue
            ev = T.num(m, "expiration_value")
            imp = implied(m.get("strike_type"), fl, cp, pavg)
            if imp is not None:
                k = fl if fl is not None else cp
                d = abs(pavg - k)
                b = "<$10" if d < 10 else "$10-50" if d < 50 else "$50-200" if d < 200 else ">$200"
                cell = band.setdefault((s, b), [0, 0])
                cell[0] += int(imp == m.get("result")); cell[1] += 1
                agree += int(imp == m.get("result")); disagree += int(imp != m.get("result"))
            if ev is not None:
                gaps.append(ev - pavg)
            nmk += 1
            for t in window_trades(m["ticker"], t1, cutoff_ts, log):
                f = T.trade_fields(t)
                if f is None or not (t1 - WINDOW_S <= f[1] < t1):
                    continue
                ls = lock_state(tape, t1, f[1])
                spot = tape.at(f[1] - LAG_S)
                rv = tape.rv(f[1] - LAG_S)
                if ls is None or spot is None:
                    continue
                tw.writerow((s, m["ticker"], t1, m.get("strike_type"), fl, cp, m.get("result"),
                             "" if ev is None else f"{ev:.2f}", f"{pavg:.2f}", f"{f[1]:.3f}", f[2],
                             f"{f[3]:.4f}", f"{f[4]:g}", ls[0], f"{ls[1]:.2f}", f"{spot:.2f}",
                             f"{rv:.8f}" if rv else "", f"{ls[2]:.2f}"))
                ntr += 1
        if i % 100 == 0:
            log(f"  [{i}/{len(by_close)}] {time.time() - t_start:.0f}s: markets {nmk}, prints {ntr}, "
                f"kraken calls {tape.calls}")
    tf.close()
    log(f"\nCONTROL proxy-implied result vs Kalshi: agree {agree}, disagree {disagree}, "
        f"no proxy data {unpriced}")
    for k in sorted(band):
        log(f"  {k[0]:<9} |proxy avg - strike| {k[1]:<8} agree {band[k][0]} of {band[k][1]}")
    if gaps:
        gaps.sort(); n = len(gaps)
        log(f"CONTROL expiration_value - proxy 60s avg, $: p10 {gaps[n//10]:+.2f} median "
            f"{gaps[n//2]:+.2f} p90 {gaps[9*n//10]:+.2f} n={n}")
    log(f"markets kept {nmk}, prints written {ntr}, kraken calls {tape.calls}, fails {tape.fails}")


if __name__ == "__main__":
    main()
