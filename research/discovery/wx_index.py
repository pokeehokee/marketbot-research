"""Idea #66: does Kalshi's own published temperature index lead the hourly temperature markets?

The KXTEMP*HS / KXTEMPMIAH series settle on the Kalshi Weather Index at a named minute
("the temperature ... at 12 PM EST as reported by Synoptic Data ... Kalshi Weather Index
Methodology"). Kalshi publishes that index itself, minute by minute, on a PUBLIC endpoint:

    GET /live_data/weather/{city}?from=<ms>&to=<ms>[&detailed=true]

Each point is an event minute `t` with a value `v` (deg F, 0.01) published at t + 300 s and
never revised (CFTC filing ptc09092624308). So at any moment a trader can see the index as
of five minutes ago, and the market settles on the value at the settlement minute. This
script records, for every print in the last 90 minutes of each settled market, what the
index said at that moment -- with no lookahead -- so the scorer can ask whether the price
already contained it.

No lookahead, stated as a rule the tests freeze: a point is KNOWN at time X only if
`t + PUBLISH_LAG_S <= X`. The published lag is the methodology's D = 300 s; `detailed=true`
also returns each station's `received_at_ms`, which is logged so the real lag is measured
rather than assumed.

Read-only and unauthenticated. Uses taker_flow_screen.get, whose whitelist is widened here
by exactly one read-only prefix, /live_data/weather. Outputs under out/:

  wx-<tag>.log                what was found: cities, history depth, close times, the control
  wx-<tag>-trades.csv.gz      one row per print with the index known at the print
"""
import argparse, csv, gzip, json, os, re, sys, time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taker_flow_screen as T  # noqa: E402
import series_probe as P  # noqa: E402

T.ALLOWED = T.ALLOWED + ("/live_data/weather",)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
PUBLISH_LAG_S = 300        # methodology deadline D: the point for minute t is published at t + D
SETTLE_LOOKBACK_S = 3600   # settlement value: greatest t <= settlement time, within 60 minutes
WINDOW_S = 90 * 60         # prints kept: the last 90 minutes before settlement
ET = ZoneInfo("America/New_York")
MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}

# Candidate city slugs per series; the first that answers 200 is used and logged.
CITY_GUESSES = {
    "KXTEMPNYCHS": ("new_york", "nyc", "new-york", "newyork", "new_york_city"),
    "KXTEMPNYCH": ("new_york", "nyc", "new-york", "newyork", "new_york_city"),
    "KXTEMPMIAH": ("miami", "mia"),
    "KXTEMPCHIHS": ("chicago", "chi", "chicago_metro", "chicago-metro"),
    "KXTEMPCHIMETROHS": ("chicago", "chicago_metro", "chicago-metro", "chi"),
    "KXTEMPLAXHS": ("los_angeles", "coastal_la", "la", "lax", "los-angeles", "coastal_los_angeles"),
    "KXTEMPLAHS": ("coastal_la", "los_angeles", "la", "lax", "los-angeles"),
}


def settle_ts_from_ticker(ticker):
    """KXTEMPNYCHS-26NOV2512-T45.99 -> unix seconds of 2026-11-25 12:00 in New York.

    The rules say "12 PM EST" for every city, Chicago and LA included, so the clock is
    New York's. Returns None when the event part does not parse."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})(\d{2})(?:-|$)", ticker or "")
    if not m or m.group(2) not in MONTHS:
        return None
    yy, mon, dd, hh = int(m.group(1)), MONTHS[m.group(2)], int(m.group(3)), int(m.group(4))
    return int(datetime(2000 + yy, mon, dd, hh, 0, tzinfo=ET).timestamp())


def known_at(points, ts):
    """The latest point published by `ts`: greatest t with t + lag <= ts and a value.

    points: list of (t_seconds, value_or_None), any order. Returns (t, v) or None."""
    best = None
    for t, v in points:
        if v is None or t + PUBLISH_LAG_S > ts:
            continue
        if best is None or t > best[0]:
            best = (t, v)
    return best


def settlement_value(points, settle_ts):
    """Greatest event time t <= settle_ts within 60 minutes, with a value (the filing's rule)."""
    best = None
    for t, v in points:
        if v is None or t > settle_ts or t < settle_ts - SETTLE_LOOKBACK_S:
            continue
        if best is None or t > best[0]:
            best = (t, v)
    return best


def implied_result(strike_type, floor, cap, value):
    """yes/no the index value implies under Kalshi's strike semantics, or None if unknown."""
    if value is None:
        return None
    st = (strike_type or "").lower()
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


def parse_points(payload):
    out, lags = [], []
    for p in (payload or {}).get("timeseries") or []:
        t = p.get("t")
        if t is None:
            continue
        v = p.get("v")
        out.append((int(t) // 1000, float(v) if v is not None else None))
        for s in p.get("stations") or []:
            ot, rt = s.get("obs_time_ms"), s.get("received_at_ms")
            if ot is not None and rt is not None:
                lags.append((int(rt) - int(ot)) / 1000.0)
    return out, lags


def index_window(city, start_s, end_s, detailed=False):
    q = {"from": int(start_s * 1000), "to": int(end_s * 1000)}
    if detailed:
        q["detailed"] = "true"
    return T.get(f"/live_data/weather/{city}", q)


def pick_city(series, log):
    for c in CITY_GUESSES.get(series, ()):
        p, err = T.get(f"/live_data/weather/{c}", {"last_sec": 900})
        n = len((p or {}).get("timeseries") or [])
        log(f"  city {c!r} for {series}: {err or 'ok'}; {n} points in the last 15 min"
            + (f"; config {p.get('config_version')}" if p else ""))
        if p and n:
            return c
    return None


def depth(city, log):
    now = time.time()
    for days in (1, 7, 21, 45, 90, 180):
        end = now - days * 86400
        p, err = index_window(city, end - 3600, end)
        pts, _ = parse_points(p)
        vals = sum(1 for _, v in pts if v is not None)
        log(f"  history {city} {days:>3}d ago: {err or 'ok'}; {len(pts)} points, {vals} with a value")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--series", default="KXTEMPNYCHS,KXTEMPMIAH,KXTEMPCHIHS,KXTEMPLAXHS")
    ap.add_argument("--days", type=int, default=21)
    ap.add_argument("--max-markets", type=int, default=3000)
    a = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    logf = open(os.path.join(OUT, f"wx-{a.tag}.log"), "w")

    def log(s):
        print(s); logf.write(s + "\n"); logf.flush()

    p, err = T.get("/historical/cutoff")
    cutoff_ts = T.utc(p.get("trades_created_ts")) if p and isinstance(p.get("trades_created_ts"), str) else None
    log(f"cutoff: {json.dumps(p) if p else err}")
    since = time.time() - a.days * 86400

    tf = gzip.open(os.path.join(OUT, f"wx-{a.tag}-trades.csv.gz"), "wt", newline="")
    tw = csv.writer(tf)
    tw.writerow(("series", "ticker", "settle_ts", "close_ts", "strike_type", "floor", "cap", "result",
                 "final_t", "final_v", "trade_ts", "taker_side", "taker_paid", "count",
                 "known_t", "known_v"))
    for s in [x for x in a.series.split(",") if x]:
        log(f"\n== {s}")
        city = pick_city(s, log)
        if not city:
            log(f"  {s}: no city slug answered; skipped")
            continue
        depth(city, log)
        ms = [m for m in P.settled_markets(s, log) if (T.utc(m.get("close_time")) or 0) >= since]
        ms = ms[:a.max_markets]
        log(f"  {s}: {len(ms)} settled markets closing in the last {a.days} days")
        events = {}
        for m in ms:
            st = settle_ts_from_ticker(m.get("ticker"))
            if st is not None:
                events.setdefault(st, []).append(m)
        gaps = sorted(st - (T.utc(m.get("close_time")) or st) for st, mm in events.items() for m in mm[:1])
        if gaps:
            log(f"  settlement minus close_time, seconds: min {gaps[0]:.0f} median "
                f"{gaps[len(gaps)//2]:.0f} max {gaps[-1]:.0f} over {len(gaps)} events")
        agree = disagree = unknown = ntr = 0
        lag_sample = []
        for st in sorted(events):
            p, err = index_window(city, st - WINDOW_S - 3600, st + 60, detailed=not lag_sample)
            if err:
                log(f"  index {city} @{st}: {err}")
                continue
            pts, lags = parse_points(p)
            lag_sample.extend(lags)
            fin = settlement_value(pts, st)
            for m in events[st]:
                fl, cp = T.num(m, "floor_strike"), T.num(m, "cap_strike")
                imp = implied_result(m.get("strike_type"), fl, cp, fin[1] if fin else None)
                if imp is None:
                    unknown += 1
                elif imp == m.get("result"):
                    agree += 1
                else:
                    disagree += 1
                    if disagree <= 10:
                        log(f"  DISAGREE {m['ticker']}: index {fin} {m.get('strike_type')} "
                            f"{fl}/{cp} -> {imp}, exchange {m.get('result')}")
                close_ts = T.utc(m.get("close_time"))
                for t in P.trades_for(m["ticker"], cutoff_ts, close_ts, log):
                    f = T.trade_fields(t)
                    if f is None or f[1] < st - WINDOW_S:
                        continue
                    k = known_at(pts, f[1])
                    tw.writerow((s, m["ticker"], st, f"{close_ts:.0f}" if close_ts else "",
                                 m.get("strike_type"), fl, cp, m.get("result"),
                                 fin[0] if fin else "", fin[1] if fin else "",
                                 f"{f[1]:.0f}", f[2], f"{f[3]:.4f}", f"{f[4]:g}",
                                 k[0] if k else "", k[1] if k else ""))
                    ntr += 1
        log(f"  CONTROL {s}: index-implied result agrees with the exchange on {agree}, "
            f"disagrees on {disagree}, unknown {unknown}")
        if lag_sample:
            lag_sample.sort()
            n = len(lag_sample)
            log(f"  station receipt lag (received - observed), s: p10 {lag_sample[n//10]:.0f} "
                f"median {lag_sample[n//2]:.0f} p90 {lag_sample[9*n//10]:.0f} n={n}")
        log(f"  {s}: {ntr} prints written from the last {WINDOW_S//60} minutes")
    tf.close()


if __name__ == "__main__":
    main()
