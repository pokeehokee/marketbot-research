"""Idea #15: does Kalshi's game-winner price disagree with the sportsbook line,
and does trading the disagreement pay after Kalshi's fee?

Counterparty: Kalshi sports takers who price off team loyalty or a headline
rather than off the betting market. The sportsbook closing line is the most
informed public price there is, so where Kalshi sits away from it, someone on
Kalshi is wrong -- either the Kalshi crowd, or us for trusting the book.

Sources, both public and keyless:
  * ESPN's scoreboard / summary JSON: the moneyline ESPN publishes for each game
    (its odds provider, usually DraftKings), open and close, plus the result.
    This is a large retail book, NOT a sharp one (Pinnacle). A "free sharp line"
    does not exist without an API key, and getting one means signing up, which
    this work may not do. So the test asks the honest version of the question:
    is Kalshi far enough from a major book's line, often enough, to pay?
  * Kalshi: the settled game markets of each league's game-winner series, and
    the one-minute candle ENDING at or before the scheduled start.

Zero lookahead: the book's CLOSE is the last pre-game line, and it is compared
with Kalshi's quote at the same moment (candle end <= start), never earlier.
A Kalshi quote older than MAX_AGE_S at the start is dropped, not stretched:
the commodity work found sparse candle histories quoting hours-old books.

Grading: every bet is graded by Kalshi's own `result` field. Verdict rules as
in the taker-flow screen: split game DAYS in half, the rule must be positive
with a day-clustered interval above zero in each half, after the taker fee.

Run: python3 -I sports_lines.py --days 45 --tag sports-1
"""
import argparse, calendar, collections, csv, gzip, json, math, os, random, re, sys, time
import urllib.error, urllib.parse, urllib.request
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taker_flow_screen as T  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
ET = ZoneInfo("America/New_York")
ESPN = "https://site.api.espn.com"
MAX_AGE_S = 600
LOT = 10
BOOT = 2000
RNG = random.Random(20261124)

# Kalshi game-winner series -> ESPN sport/league. Verified against /series in the
# log; a series Kalshi does not list is reported, never silently skipped.
LEAGUES = {
    "KXNBAGAME": ("basketball", "nba"),
    "KXNHLGAME": ("hockey", "nhl"),
    "KXNFLGAME": ("football", "nfl"),
    "KXNCAAFGAME": ("football", "college-football"),
    "KXNCAAMBGAME": ("basketball", "mens-college-basketball"),
}


def espn_get(path, params=None, _sleep=time.sleep):
    p = path.split("?", 1)[0]
    if not re.fullmatch(r"/apis/site/v2/sports/[a-z-]+/[a-z-]+/(scoreboard|summary)", p):
        raise RuntimeError(f"refusing a non-whitelisted ESPN path: {path}")
    url = ESPN + p + ("?" + urllib.parse.urlencode(params) if params else "")
    last = None
    for nap in (0, 2, 5, 15):
        if nap:
            _sleep(nap)
        _sleep(0.25)
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "research", "Accept": "application/json"}), timeout=60)
            return json.load(r), None
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                return None, last
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
    return None, last


def espn_ts(s):
    """ESPN writes '2026-11-24T00:00Z' (no seconds); Kalshi writes seconds. Both UTC."""
    if not s:
        return None
    s = s.rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return calendar.timegm(time.strptime(s.split(".")[0], fmt))
        except ValueError:
            continue
    return None


def american_to_prob(v):
    """'-150' -> 0.600, '+130' -> 0.435. 'EVEN'/'EV' -> 0.5. Unparseable -> None."""
    if v is None:
        return None
    s = str(v).strip().upper()
    if s in ("EVEN", "EV", "PK"):
        return 0.5
    try:
        x = float(s)
    except ValueError:
        return None
    if x == 0 or -100 < x < 100:
        return None
    return -x / (-x + 100) if x < 0 else 100 / (x + 100)


def novig(ph, pa):
    if ph is None or pa is None or ph + pa <= 0:
        return None
    return ph / (ph + pa)


def odds_block_lines(o):
    """(home_close, away_close, home_open, away_open, provider) from one ESPN odds
    entry, accepting both shapes ESPN uses. Missing is None."""
    prov = ((o.get("provider") or {}).get("name"))
    ml = o.get("moneyline") or {}

    def leg(side, when):
        return (((ml.get(side) or {}).get(when) or {}).get("odds"))
    hc, ac = leg("home", "close"), leg("away", "close")
    if hc is None:
        hc = (o.get("homeTeamOdds") or {}).get("moneyLine")
    if ac is None:
        ac = (o.get("awayTeamOdds") or {}).get("moneyLine")
    return hc, ac, leg("home", "open"), leg("away", "open"), prov


def team(c):
    t = c.get("team") or {}
    return {"abbr": (t.get("abbreviation") or "").upper(), "loc": t.get("location") or "",
            "name": t.get("name") or "", "short": t.get("shortDisplayName") or "",
            "disp": t.get("displayName") or ""}


def espn_games(sport, league, days, end, log):
    games, no_odds, summaries = [], 0, 0
    for k in range(days):
        d = time.strftime("%Y%m%d", time.gmtime(end - 86400 * k))
        params = {"dates": d, "limit": 400}
        if league == "mens-college-basketball" or league == "college-football":
            params["groups"] = "50" if league.endswith("basketball") else "80"
        p, err = espn_get(f"/apis/site/v2/sports/{sport}/{league}/scoreboard", params)
        if err:
            log(f"  ESPN {league} {d}: {err}")
            continue
        for e in p.get("events") or []:
            comp = (e.get("competitions") or [{}])[0]
            if espn_ts(e.get("date")) is None or not ((comp.get("status") or {}).get("type") or {}).get("completed"):
                continue
            cs = comp.get("competitors") or []
            home = next((c for c in cs if c.get("homeAway") == "home"), None)
            away = next((c for c in cs if c.get("homeAway") == "away"), None)
            if not home or not away:
                continue
            odds = comp.get("odds") or []
            if not odds:
                s, serr = espn_get(f"/apis/site/v2/sports/{sport}/{league}/summary", {"event": e["id"]})
                summaries += 1
                odds = (s or {}).get("pickcenter") or []
            if not odds:
                no_odds += 1
                continue
            hc, ac, ho, ao, prov = odds_block_lines(odds[0])
            games.append({"league": league, "id": e["id"], "start": espn_ts(e.get("date")),
                          "home": team(home), "away": team(away),
                          "home_won": bool(home.get("winner")), "away_won": bool(away.get("winner")),
                          "hc": hc, "ac": ac, "ho": ho, "ao": ao, "prov": prov})
    log(f"ESPN {league}: {len(games)} completed games with odds, {no_odds} without, "
        f"{summaries} summary reads; providers {collections.Counter(g['prov'] for g in games).most_common(3)}")
    return games


def norm(s):
    return re.sub(r"[^a-z0-9 ]", "", (s or "").lower()).strip()


def side_matches(sub, t):
    s = norm(sub)
    if not s:
        return False
    for f in (t["disp"], t["short"], t["loc"], t["name"], t["abbr"]):
        f = norm(f)
        if f and (s == f or s.startswith(f) or f.startswith(s)):
            return True
    return False


def kalshi_markets(series, since, log):
    ms, cursor = [], None
    while True:
        q = {"series_ticker": series, "status": "settled", "limit": 1000, "min_close_ts": int(since)}
        if cursor:
            q["cursor"] = cursor
        p, err = T.get("/markets", q)
        if err:
            log(f"  {series} listing: {err} after {len(ms)}")
            break
        ms += p.get("markets") or []
        cursor = p.get("cursor")
        if not cursor:
            break
    return [m for m in ms if m.get("result") in ("yes", "no")]


def match(events, games, log, league):
    """{market ticker: (game, 'home'|'away')}. An event maps to the one ESPN game on
    its ET date whose two teams both match its two markets' sub-titles."""
    by_day = collections.defaultdict(list)
    for g in games:
        by_day[datetime.fromtimestamp(g["start"], ET).date()].append(g)
    out, unmatched, ambiguous = {}, 0, 0
    for ev, mk in events.items():
        dates = set()
        for m in mk:
            t = T.utc(m.get("close_time"))
            if t:
                for dd in (-1, 0):
                    dates.add(datetime.fromtimestamp(t + dd * 86400, ET).date())
        cands = []
        for dte in dates:
            for g in by_day.get(dte, []):
                sides = {}
                for m in mk:
                    hs, as_ = side_matches(m.get("yes_sub_title"), g["home"]), \
                        side_matches(m.get("yes_sub_title"), g["away"])
                    if hs != as_:
                        sides[m["ticker"]] = "home" if hs else "away"
                if len(sides) == len(mk) and len(set(sides.values())) == len(mk):
                    cands.append((g, sides))
        uniq = {c[0]["id"]: c for c in cands}
        if len(uniq) == 1:
            g, sides = next(iter(uniq.values()))
            for tk, sd in sides.items():
                out[tk] = (g, sd)
        elif uniq:
            ambiguous += 1
        else:
            unmatched += 1
    log(f"  {league}: {len(events)} Kalshi events -> {len({v[0]['id'] for v in out.values()})} matched, "
        f"{unmatched} unmatched, {ambiguous} ambiguous")
    return out


def quote_at(series, ticker, mark):
    q = (f"/series/{series}/markets/{ticker}/candlesticks"
         f"?start_ts={int(mark) - 3600}&end_ts={int(mark)}&period_interval=1")
    d, e = T.get(q)
    if d is None:
        return None
    cs = [x for x in (d.get("candlesticks") or []) if x.get("end_period_ts", 1 << 62) <= mark]
    if not cs:
        return None
    k = max(cs, key=lambda x: x["end_period_ts"])

    def f(side):
        v = (k.get(side) or {}).get("close_dollars")
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    ask, bid = f("yes_ask"), f("yes_bid")
    if ask is not None and ask >= 1.0:
        ask = None          # empty-book sentinel, never a price
    if bid is not None and bid <= 0.0:
        bid = None
    return ask, bid, mark - k["end_period_ts"]


def taker_fee(p, mult):
    """Kalshi's taker estimate ceil(0.07 x mult x C x P x (1-P)) per order of LOT.
    `mult` is the series' fee_multiplier, read as a multiple of the standard 0.07
    (1.0 = standard); it is printed per series in the log so the reading can be checked."""
    return math.ceil(0.07 * mult * LOT * p * (1 - p) * 100 - 1e-9) / 100.0 / LOT


def score(rows, mult_by_series, log):
    """Bet the side Kalshi underprices vs the book by more than `edge` after fee."""
    days = sorted({r["day"] for r in rows})
    if len(days) < 4:
        log("too few days to split; no verdict")
        return
    half = days[len(days) // 2]
    log(f"\nrows {len(rows)}, days {days[0]}..{days[-1]}, IS < {half} <= OOS")

    # Calibration first: who predicts the result better, the book or Kalshi's mid?
    bk = [(r["p_book"] - r["won"]) ** 2 for r in rows]
    km = [(r["mid"] - r["won"]) ** 2 for r in rows if r["mid"] is not None]
    if km:
        log(f"Brier: book {sum(bk)/len(bk):.4f} on {len(bk)}, Kalshi mid {sum(km)/len(km):.4f} on {len(km)}")
    gaps = [abs(r["mid"] - r["p_book"]) for r in rows if r["mid"] is not None]
    if gaps:
        gaps.sort()
        log(f"|Kalshi mid - book|: median {100*gaps[len(gaps)//2]:.2f}c, p90 {100*gaps[int(.9*len(gaps))]:.2f}c")

    for edge in (0.0, 0.02, 0.04, 0.06):
        bets = collections.defaultdict(list)
        for r in rows:
            mult = mult_by_series.get(r["series"], 1.0)
            if r["ask"] is not None:
                f = taker_fee(r["ask"], mult)
                if r["p_book"] - r["ask"] - f > edge:
                    bets[r["day"]].append(r["won"] - r["ask"] - f)
        for nm, sel in (("all", days), ("IS", [d for d in days if d < half]),
                        ("OOS", [d for d in days if d >= half])):
            dd = [d for d in sel if bets.get(d)]
            n = sum(len(bets[d]) for d in dd)
            if not n:
                log(f"  edge>{100*edge:.0f}c {nm:<3} no bets")
                continue
            ev = sum(sum(bets[d]) for d in dd) / n
            boots = []
            for _ in range(BOOT):
                s = [bets[RNG.choice(dd)] for _ in dd]
                c = sum(len(x) for x in s)
                boots.append(sum(sum(x) for x in s) / c)
            boots.sort()
            log(f"  edge>{100*edge:.0f}c {nm:<3} {n:>5} bets on {len(dd):>3} days  EV {100*ev:+6.2f}c/ct "
                f"[{100*boots[int(.025*BOOT)]:+.2f}, {100*boots[int(.975*BOOT)-1]:+.2f}]  "
                f"won {sum(1 for d in dd for x in bets[d] if x > 0)}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=45)
    ap.add_argument("--tag", required=True)
    a = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    logf = open(os.path.join(OUT, f"sports-{a.tag}.log"), "w")

    def log(s):
        print(s); logf.write(s + "\n"); logf.flush()

    end = time.time() - 86400
    since = end - a.days * 86400
    mult_by_series, rows = {}, []
    for series, (sport, league) in LEAGUES.items():
        sp, err = T.get(f"/series/{series}")
        s = (sp or {}).get("series") or {}
        if not s:
            log(f"{series}: not listed by Kalshi ({err}); skipped")
            continue
        try:
            mult_by_series[series] = float(s.get("fee_multiplier"))
        except (TypeError, ValueError):
            pass
        log(f"\n{series}: '{s.get('title')}' fee_type {s.get('fee_type')} "
            f"multiplier {s.get('fee_multiplier')} settlement sources {s.get('settlement_sources')}")
        ms = kalshi_markets(series, since, log)
        if ms:
            log(f"  rules ({ms[0].get('ticker')}): {ms[0].get('rules_primary')}")
        events = collections.defaultdict(list)
        for m in ms:
            events[m.get("event_ticker")].append(m)
        games = espn_games(sport, league, a.days + 1, end, log)
        mm = match(events, games, log, league)
        stale = sentinel = 0
        for m in ms:
            if m["ticker"] not in mm:
                continue
            g, sd = mm[m["ticker"]]
            pb = novig(american_to_prob(g["hc"]), american_to_prob(g["ac"]))
            if pb is None:
                continue
            p_side = pb if sd == "home" else 1 - pb
            q = quote_at(series, m["ticker"], g["start"])
            if q is None:
                sentinel += 1
                continue
            ask, bid, age = q
            if age > MAX_AGE_S:
                stale += 1
                continue
            won = 1 if m["result"] == "yes" else 0
            if won != (1 if g[f"{sd}_won"] else 0):
                log(f"  RESULT DISAGREES: {m['ticker']} kalshi {m['result']} espn {sd}_won={g[sd + '_won']}")
            mid = (ask + bid) / 2 if ask is not None and bid is not None else None
            rows.append({"series": series, "ticker": m["ticker"], "day": datetime.fromtimestamp(
                g["start"], ET).date().isoformat(), "p_book": p_side, "ask": ask, "bid": bid,
                "mid": mid, "age": age, "won": won, "prov": g["prov"]})
        log(f"  quotes: {sum(1 for r in rows if r['series'] == series)} usable, {stale} older than "
            f"{MAX_AGE_S}s at start, {sentinel} with no candle")
    with gzip.open(os.path.join(OUT, f"sports-{a.tag}.csv.gz"), "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["series", "ticker", "day", "p_book", "ask", "bid", "mid",
                                          "age", "won", "prov"])
        w.writeheader(); w.writerows(rows)
    for series in LEAGUES:
        sub = [r for r in rows if r["series"] == series]
        if sub:
            log(f"\n=== {series} ===")
            score(sub, mult_by_series, log)
    if rows:
        log("\n=== ALL LEAGUES POOLED ===")
        score(rows, mult_by_series, log)


if __name__ == "__main__":
    main()
