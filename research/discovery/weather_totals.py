"""Idea #17: does game-time wind move football totals more than Kalshi's price allows?

The classic claim: wind suppresses scoring in outdoor football and the crowd under-adjusts, so
the UNDER is cheap on windy days. Kalshi lists over/under ladders per game (`greater` strikes on
total points). This scores them against the weather at the stadium.

Zero lookahead, by construction:
  - price: trades in the 30 minutes BEFORE the scheduled kickoff, never after;
  - weather: the reanalysis wind at least one hour before kickoff (Open-Meteo archive, keyless),
    i.e. conditions a trader could see from the airport observation before the game. Reanalysis is
    a smoothed estimate of what happened, not a forecast, which is said in the write-up.

Source check: a strike is graded from ESPN's final score (total > floor_strike) and compared with
Kalshi's own `result`. Fewer than 98% agreeing prints no verdict.

Inputs are a series_probe pull (markets + trades). Reads ESPN scoreboards (keyless) and Open-Meteo
(keyless). Never touches anything that writes. Output: out/weather-totals-<tag>.txt.
"""
import argparse, calendar, collections, csv, gzip, json, math, os, random, re, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sports_lines as SL  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
ET = ZoneInfo("America/New_York")
WINDOW_S = 1800
CAP = 25            # per-trade contract cap, so one block print cannot carry a game
BOOT = 2000
RNG = random.Random(20261124)
MON = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
LEAGUE = {"KXNFLTOTAL": ("football", "nfl"), "KXNCAAFTOTAL": ("football", "college-football")}
WIND_BUCKETS = ((0, 10, "calm <10 mph"), (10, 15, "breezy 10-15"), (15, 99, "windy 15+"))
ALIAS = {"WSH": "WAS", "JAX": "JAC", "LAR": "LA", "LV": "LVR"}


def ev_parse(ev):
    """'KXNFLTOTAL-25SEP07BUFBAL' -> (date(2025,9,7), 'BUFBAL').
    A team code may itself hold a hyphen (Miami (OH) is 'M-OH'), so the code
    part allows one; the date is still anchored on the first hyphen after it."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})([A-Z0-9-]*)$", ev or "")
    if not m or m.group(2) not in MON:
        return None, None
    return date(2000 + int(m.group(1)), MON.index(m.group(2)) + 1, int(m.group(3))), m.group(4)


def codes(abbr):
    a = (abbr or "").upper()
    out = {a}
    for k, v in ALIAS.items():
        if a == k:
            out.add(v)
        if a == v:
            out.add(k)
    return out


def matches(code, title, g):
    """Both teams must be found: by abbreviation pair in the ticker, else by name in the title."""
    for x in codes(g["away_abbr"]):
        for y in codes(g["home_abbr"]):
            if code in (x + y, y + x):
                return True
    t = SL.norm(title)
    def named(side):
        return any(n and SL.norm(n) in t for n in g[side + "_names"])
    return named("away") and named("home")


def total_over(m, total):
    """Kalshi `greater` strike: YES when the total exceeds floor_strike."""
    return total > float(m["floor_strike"])


def taker_fee(p, mult=1.0):
    return math.ceil(0.07 * mult * 10 * p * (1 - p) * 100 - 1e-9) / 1000.0


def weather_hour(kickoff_ts):
    """The archive hour to read: wind_speed_10m is an INSTANT value at its UTC label, so the label
    must be at least one hour before kickoff -- floor(kickoff - 1h) to the hour."""
    return datetime.fromtimestamp((kickoff_ts - 3600) // 3600 * 3600, timezone.utc)


def om_get(host, path, params, _sleep=time.sleep):
    if host not in ("archive-api.open-meteo.com", "geocoding-api.open-meteo.com") or \
            path not in ("/v1/archive", "/v1/search"):
        raise RuntimeError(f"refusing a non-whitelisted weather read: {host}{path}")
    url = f"https://{host}{path}?" + urllib.parse.urlencode(params)
    last = None
    for nap in (0, 3, 10, 30):
        if nap:
            _sleep(nap)
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "research"}),
                                       timeout=60)
            return json.load(r), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                return None, last
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
    return None, last


_GEO = {}
STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
          "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
          "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
          "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
          "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
          "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
          "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
          "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
          "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
          "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
          "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico"}


def pick_geo(results, state):
    """The first geocoder result in the venue's US state, or None. ESPN gives a postal code
    ('OH'); Open-Meteo gives the full name ('Ohio') in admin1. No state match is NO location:
    taking the first result put Athens, OH and Bowling Green, KY wherever the geocoder liked
    (2026-11-24, every venue missed this way)."""
    want = STATES.get((state or "").strip().upper(), state)
    if not want:
        return None
    for r in results or []:
        if SL.norm(r.get("admin1") or "") == SL.norm(want):
            return (r["latitude"], r["longitude"])
    return None


def geocode(city, state, log):
    k = (city, state)
    if k in _GEO:
        return _GEO[k]
    p, err = om_get("geocoding-api.open-meteo.com", "/v1/search",
                    {"name": city, "count": 10, "countryCode": "US", "format": "json"})
    hit = pick_geo((p or {}).get("results"), state)
    if hit is None:
        log(f"  geocode {city}, {state}: {err or 'no result in that state'}; game left without wind")
    _GEO[k] = hit
    return hit


def winds(games, log):
    """{game id: wind mph} for outdoor geocoded games: ONE archive read per venue covering
    every game there, then the hour weather_hour() names for each kickoff."""
    by_ll = collections.defaultdict(list)
    for g in games:
        ll = geocode(g["city"], g["state"], log) if g.get("city") else None
        if ll:
            by_ll[ll].append(g)
    out = {}
    for (lat, lon), gs in by_ll.items():
        labs = {g["id"]: weather_hour(g["start"]) for g in gs}
        d0 = min(labs.values()).strftime("%Y-%m-%d")
        d1 = max(labs.values()).strftime("%Y-%m-%d")
        p, err = om_get("archive-api.open-meteo.com", "/v1/archive",
                        {"latitude": lat, "longitude": lon, "start_date": d0, "end_date": d1,
                         "hourly": "wind_speed_10m", "wind_speed_unit": "mph", "timezone": "GMT"})
        if err or not p:
            log(f"  weather {lat},{lon} {d0}..{d1}: {err}")
            continue
        out.update(wind_lookup(p.get("hourly") or {}, labs))
    return out


def wind_lookup(hourly, labs):
    idx = {t: i for i, t in enumerate(hourly.get("time") or [])}
    ws = hourly.get("wind_speed_10m") or []
    out = {}
    for gid, lab in labs.items():
        i = idx.get(lab.strftime("%Y-%m-%dT%H:%M"))
        if i is not None and i < len(ws) and ws[i] is not None:
            out[gid] = float(ws[i])
    return out


def espn_week(sport, league, days, log):
    """Completed games on the given ET dates, with venue and final total."""
    games = []
    seen = set()
    # College: FBS (80) AND FCS (81). Kalshi lists FCS games too; FBS alone matched 1,086 of
    # 1,938 events on 2026-11-24.
    reads = [(d, grp) for d in sorted(days) for grp in (("80", "81") if league == "college-football" else (None,))]
    for d, grp in reads:
        params = {"dates": d.strftime("%Y%m%d"), "limit": 400}
        if grp:
            params["groups"] = grp
        p, err = SL.espn_get(f"/apis/site/v2/sports/{sport}/{league}/scoreboard", params)
        if err:
            log(f"  ESPN {league} {d} {grp or ''}: {err}")
            continue
        for e in p.get("events") or []:
            if e.get("id") in seen:
                continue
            seen.add(e.get("id"))
            comp = (e.get("competitions") or [{}])[0]
            if not ((comp.get("status") or {}).get("type") or {}).get("completed"):
                continue
            cs = comp.get("competitors") or []
            home = next((c for c in cs if c.get("homeAway") == "home"), None)
            away = next((c for c in cs if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            try:
                total = float(home.get("score")) + float(away.get("score"))
            except (TypeError, ValueError):
                continue
            v = comp.get("venue") or {}
            addr = v.get("address") or {}

            def names(c):
                t = c.get("team") or {}
                return [t.get("displayName"), t.get("shortDisplayName"), t.get("location"), t.get("name")]
            games.append({"id": e["id"], "start": SL.espn_ts(e.get("date")), "total": total,
                          "away_abbr": (away.get("team") or {}).get("abbreviation"),
                          "home_abbr": (home.get("team") or {}).get("abbreviation"),
                          "away_names": names(away), "home_names": names(home),
                          "indoor": v.get("indoor"), "venue": v.get("fullName"),
                          "city": addr.get("city"), "state": addr.get("state")})
    log(f"ESPN {league}: {len(games)} completed games on {len(days)} dates; indoor flag missing on "
        f"{sum(1 for g in games if g['indoor'] is None)}")
    return games


def boot(rows):
    """rows: (game, pnl_per_ct, n). EV per contract and 95% CI, resampled by game."""
    by = collections.defaultdict(list)
    for gid, x, n in rows:
        by[gid].append((x, n))
    keys = list(by)
    ct = sum(n for k in keys for _, n in by[k])
    if not ct:
        return None
    ev = sum(x * n for k in keys for x, n in by[k]) / ct
    bs = []
    for _ in range(BOOT):
        s = [by[RNG.choice(keys)] for _ in keys]
        c = sum(n for b in s for _, n in b)
        bs.append(sum(x * n for b in s for x, n in b) / c)
    bs.sort()
    return ev, bs[int(.025 * BOOT)], bs[int(.975 * BOOT) - 1], ct, len(keys)


def bucket(g):
    if g["indoor"]:
        return "indoor (control)"
    for lo, hi, nm in WIND_BUCKETS:
        if lo <= g["wind"] < hi:
            return nm
    return None


def score(rows, games, log, fee_mult):
    """rows: one per pre-kickoff trade: game id, day, taker side, price paid, n, over (bool)."""
    log("\nCalibration: implied P(over) vs realised, contracts weighted (trade price, capped)")
    for b in ["indoor (control)"] + [nm for _, _, nm in WIND_BUCKETS]:
        sub = [r for r in rows if bucket(games[r["g"]]) == b]
        n = sum(r["n"] for r in sub)
        if n:
            log(f"  {b:<17} implied {100*sum(r['py']*r['n'] for r in sub)/n:5.1f}%  realised "
                f"{100*sum(r['over']*r['n'] for r in sub)/n:5.1f}%  {n:8.0f} ct  "
                f"{len({r['g'] for r in sub})} games")
    days = sorted({r["d"] for r in rows})
    if len(days) < 4:
        log("too few days to split")
        return
    half = days[len(days) // 2]
    log(f"\nStrategy: buy UNDER (NO) by bucket; split at {half}; CIs resampled by game")
    for b in ["indoor (control)"] + [nm for _, _, nm in WIND_BUCKETS]:
        for nm, sub in (("1st half", [r for r in rows if r["d"] < half]),
                        ("2nd half", [r for r in rows if r["d"] >= half])):
            sub = [r for r in sub if bucket(games[r["g"]]) == b]
            take = [(r["g"], (0 if r["over"] else 1) - r["p"] - taker_fee(r["p"], fee_mult), r["n"])
                    for r in sub if r["side"] == "no"]
            rest = [(r["g"], (0 if r["over"] else 1) - (1 - r["p"]), r["n"])
                    for r in sub if r["side"] == "yes"]
            for how, xs in (("take", take), ("rest", rest)):
                res = boot(xs)
                if res:
                    ev, lo, hi, ct, ng = res
                    log(f"  {b:<17} {how} {nm}  {100*ev:+6.2f}c/ct [{100*lo:+6.2f}, {100*hi:+6.2f}]  "
                        f"{ct:.0f} ct, {ng} games")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", required=True)
    ap.add_argument("--trades", required=True)
    ap.add_argument("--tag", required=True)
    ap.add_argument("--fee-mult", type=float, default=1.0)
    a = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    logf = open(os.path.join(OUT, f"weather-totals-{a.tag}.txt"), "w")

    def log(s):
        print(s); logf.write(s + "\n"); logf.flush()

    with gzip.open(a.markets, "rt") as f:
        mk = [m for m in csv.DictReader(f) if m["series"] in LEAGUE and m.get("result") in ("yes", "no")
              and m.get("strike_type") == "greater" and m.get("floor_strike")]
    log(f"{len(mk)} settled `greater` total markets: {collections.Counter(m['series'] for m in mk)}")
    if mk:
        log(f"  sample title: {mk[0].get('title')!r} ticker {mk[0]['ticker']}")
    games, gm = {}, {}
    for series, (sport, league) in LEAGUE.items():
        ms = [m for m in mk if m["series"] == series]
        evd = {m["event_ticker"]: ev_parse(m["event_ticker"]) for m in ms}
        undated = [e for e, (d, _) in evd.items() if d is None]
        if undated:
            log(f"{series}: {len(undated)} event tickers with no readable date, left unmatched: {undated[:6]}")
        evd = {e: v for e, v in evd.items() if v[0] is not None}
        dates = {d for d, _ in evd.values() if d} | {d + timedelta(1) for d, _ in evd.values() if d}
        gs = espn_week(sport, league, dates, log) if dates else []
        unmatched = []
        for ev, (d, code) in evd.items():
            title = next((m.get("title") for m in ms if m["event_ticker"] == ev), "")
            cand = [g for g in gs if g["start"] and
                    abs((datetime.fromtimestamp(g["start"], ET).date() - d).days) <= 1 and matches(code, title, g)]
            if len(cand) == 1:
                gm[ev] = cand[0]["id"]
                games[cand[0]["id"]] = cand[0]
            else:
                unmatched.append((ev, len(cand)))
        log(f"{series}: {len(evd)} events, {sum(1 for e in evd if e in gm)} matched to ESPN; "
            f"unmatched sample {unmatched[:6]}")

    agree = dis = 0
    for m in mk:
        g = games.get(gm.get(m["event_ticker"]))
        if not g:
            continue
        if (m["result"] == "yes") == total_over(m, g["total"]):
            agree += 1
        else:
            dis += 1
            if dis <= 5:
                log(f"  RESULT DISAGREES {m['ticker']} strike {m['floor_strike']} kalshi {m['result']} "
                    f"espn total {g['total']}")
    rate = agree / max(1, agree + dis)
    log(f"source check: {agree}/{agree + dis} strikes graded as Kalshi ({100*rate:.1f}%)")

    no_wx = 0
    wmap = winds([g for g in games.values() if not g["indoor"]], log)
    for g in list(games.values()):
        g["wind"] = None if g["indoor"] else wmap.get(g["id"])
        if not g["indoor"] and g["wind"] is None:
            no_wx += 1
    out_with = [g for g in games.values() if not g["indoor"] and g["wind"] is not None]
    log(f"weather: {len(out_with)} outdoor games with wind, {no_wx} outdoor without, "
        f"{sum(1 for g in games.values() if g['indoor'])} indoor")
    if out_with:
        ws = sorted(g["wind"] for g in out_with)
        log(f"  wind mph p10 {ws[len(ws)//10]:.1f} median {ws[len(ws)//2]:.1f} p90 {ws[9*len(ws)//10]:.1f} "
            f"max {ws[-1]:.1f}")

    by_tk = {m["ticker"]: m for m in mk}
    rows = []
    with gzip.open(a.trades, "rt") as f:
        for t in csv.DictReader(f):
            m = by_tk.get(t["ticker"])
            g = games.get(gm.get(m["event_ticker"])) if m else None
            if not g or (not g["indoor"] and g["wind"] is None):
                continue
            ts = float(t["ts"])
            if not (g["start"] - WINDOW_S <= ts < g["start"]):
                continue
            p = float(t["taker_paid"])
            py = p if t["taker_side"] == "yes" else 1 - p
            rows.append({"g": g["id"], "d": datetime.fromtimestamp(g["start"], ET).date(),
                         "side": t["taker_side"], "p": p, "py": py, "n": min(float(t["count"]), CAP),
                         "over": 1 if m["result"] == "yes" else 0})
    log(f"{len(rows)} trades in the {WINDOW_S // 60} minutes before kickoff, "
        f"{len({r['g'] for r in rows})} games")
    if rate < 0.98:
        log("SOURCE CHECK FAILED (< 98%): no verdict printed")
        return
    score(rows, games, log, a.fee_mult)


if __name__ == "__main__":
    main()
