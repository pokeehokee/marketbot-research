"""Idea #18: price Kalshi's TSA screening markets from TSA's own daily counts.

Counterparty: takers who trade a weekly (or daily) TSA count on a feel for
"busy week" rather than on the same weekday last year, scaled by the run-rate,
plus whatever days of the week TSA has already published.

Settlement source, checked rather than assumed: the rules name "the TSA"; this
script fetches TSA's published daily checkpoint numbers and recomputes every
settled event's `expiration_value` from them under each candidate week
definition. The definition used is the one that reproduces Kalshi's own figure,
and the agreement rate is printed. Below 95% agreement no verdict is printed.

Zero lookahead:
  * a day's count is treated as known only from noon ET on the first business
    day after it (TSA posts weekday mornings; weekend days appear on Monday);
  * the run-rate uses only known days;
  * forecast-error spreads are fitted on the in-sample half of EVENTS and used
    unchanged on the out-of-sample half.

Bets, per real trade on the tape (the price at which someone actually traded):
  * TAKE: buy the side the taker bought, at their price, plus the taker fee,
    when the model says that side is cheap by more than `edge`;
  * MAKE: be the resting side of that trade (fee-free on `quadratic`), when the
    model says the taker overpaid by more than `edge`. Queue position is not
    modelled, so MAKE is an upper bound on what resting would have earned.
Grading is Kalshi's `result` field. Each trade is sized min(count, CAP).
Intervals are bootstrapped by EVENT (all strikes of one week settle together).

Run: python3 -I tsa_model.py --markets out/probe-tsa-1-markets.csv.gz --trades ... --tag tsa-1
"""
import argparse, bisect, calendar, collections, csv, gzip, html, math, os, random, re, sys, time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
CAP = 25
BOOT = 2000
RNG = random.Random(20261124)
TSA_URL = "https://www.tsa.gov/travel/passenger-volumes"


def fetch_tsa(years, log):
    """{date: count} from TSA's passenger-volume pages (the current-year page
    and one page per past year). Any page that cannot be read is logged."""
    out = {}
    for y in [None] + list(years):
        url = TSA_URL + (f"/{y}" if y else "")
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) research-script",
                "Accept": "text/html"})
            body = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")
        except Exception as e:
            log(f"TSA {url}: {type(e).__name__}: {e}")
            continue
        n = 0
        for row in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S | re.I):
            cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip()
                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)]
            if len(cells) < 2:
                continue
            m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", cells[0])
            v = cells[1].replace(",", "")
            if m and v.isdigit():
                d = date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
                out.setdefault(d, int(v)); n += 1
        log(f"TSA {url}: {n} daily rows")
        time.sleep(1)
    if out:
        log(f"TSA total: {len(out)} days, {min(out)} .. {max(out)}")
    return out


def event_date(ev):
    """KXTSAW-26NOV15 / TSAW-21JUL17 -> date(2026,11,15)."""
    m = re.search(r"-(\d{2})([A-Z]{3})(\d{2})$", ev)
    if not m:
        return None
    mon = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    return date(2000 + int(m.group(1)), mon.index(m.group(2)) + 1, int(m.group(3)))


WINDOWS = {
    "7d ending on the date": lambda d: [d - timedelta(k) for k in range(6, -1, -1)],
    "7d ending the day before": lambda d: [d - timedelta(k) for k in range(7, 0, -1)],
    "the one day": lambda d: [d],
}


def pick_window(ms, tsa, log):
    """The window under which TSA's counts grade Kalshi's strikes the way Kalshi did.

    Checked on the RESULT of every `greater` strike, which is what settles, rather
    than on `expiration_value`, which Kalshi writes in three formats (2,317,821 /
    2.459 / "Above 2.6 million"). Returns (name, agreement, n, {bad events})."""
    best = None
    for name, f in WINDOWS.items():
        ok = tot = 0
        bad = set()
        for m in ms:
            if m.get("strike_type") != "greater":
                continue
            d = event_date(m["event_ticker"])
            days = f(d) if d else None
            if not days or any(x not in tsa for x in days):
                continue
            avg = sum(tsa[x] for x in days) / len(days)
            tot += 1
            if (avg > float(m["floor_strike"])) == (m["result"] == "yes"):
                ok += 1
            else:
                bad.add(m["event_ticker"])
        rate = ok / tot if tot else 0
        log(f"  window '{name}': TSA's counts grade {ok}/{tot} strikes as Kalshi did")
        if tot and (best is None or rate > best[1]):
            best = (name, rate, tot, bad)
    return best


def federal_holidays(y):
    """US federal holidays as observed (a Saturday holiday moves to Friday, Sunday to Monday)."""
    def nth(month, weekday, n):
        d = date(y, month, 1)
        d += timedelta((weekday - d.weekday()) % 7)
        return d + timedelta(7 * (n - 1))

    def last(month, weekday):
        d = date(y, month + 1, 1) - timedelta(1) if month < 12 else date(y, 12, 31)
        return d - timedelta((d.weekday() - weekday) % 7)

    def obs(d):
        return d - timedelta(1) if d.weekday() == 5 else d + timedelta(1) if d.weekday() == 6 else d

    return {obs(date(y, 1, 1)), nth(1, 0, 3), nth(2, 0, 3), last(5, 0), obs(date(y, 6, 19)),
            obs(date(y, 7, 4)), nth(9, 0, 1), nth(10, 0, 2), obs(date(y, 11, 11)), nth(11, 3, 4),
            obs(date(y, 12, 25))}


def publish_time(d):
    """When day d's count is treated as public: noon ET on the first business day after d.

    TSA posts weekday mornings, so a Saturday's and a Sunday's counts appear on
    Monday. Deliberately late (noon, and holidays skipped): a model that learns a
    day too late is merely weaker, one that learns it too early is lookahead."""
    p = d + timedelta(1)
    while p.weekday() >= 5 or p in federal_holidays(p.year):
        p += timedelta(1)
    return datetime(p.year, p.month, p.day, 12, tzinfo=ET).timestamp()


def known_days(ts):
    """The last day d such that every day up to d is public at unix time ts."""
    d = datetime.fromtimestamp(ts, ET).date()
    while publish_time(d) > ts:
        d -= timedelta(1)
    return d


def forecast(days, ts, tsa):
    """(mean of the window, number of unknown days, run-rate) at time ts."""
    last = known_days(ts)
    kn = [tsa[d] for d in days if d <= last and d in tsa]
    unk = [d for d in days if d > last]
    if len(kn) + len(unk) != len(days):
        return None
    # run-rate: last 14 known days against the same weekdays 364 days earlier
    cur = [last - timedelta(k) for k in range(14)]
    prev = [d - timedelta(364) for d in cur]
    if any(d not in tsa for d in cur + prev):
        return None
    rr = sum(tsa[d] for d in cur) / sum(tsa[d] for d in prev)
    fc = []
    for d in unk:
        ly = d - timedelta(364)
        if ly not in tsa:
            return None
        fc.append(tsa[ly] * rr)
    return (sum(kn) + sum(fc)) / len(days), len(unk), rr


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def taker_fee(p):
    return math.ceil(0.07 * 10 * p * (1 - p) * 100 - 1e-9) / 100.0 / 10


ERAS = (("2026 events: TSA's current page, which reproduces Kalshi's figures exactly",
         date(2026, 1, 1), date(2100, 1, 1)),
        ("2023-2025 events: TSA's yearly pages, revised about 0.02% after first print",
         date(2023, 1, 1), date(2026, 1, 1)))


def run(series, mk, tr, tsa, log):
    log(f"\n===== {series} =====")
    ms = [m for m in mk if m["series"] == series and m.get("result") in ("yes", "no")]
    best = pick_window(ms, tsa, log)
    if not best or best[1] < 0.98:
        log(f"SOURCE CHECK FAILED: best window {best and best[:3]}; no verdict printed.")
        return
    name, rate, tot, bad = best
    log(f"using '{name}': {rate:.1%} of {tot} strikes graded as Kalshi did; "
        f"disagreeing events kept, not dropped (dropping them would select on the result): "
        f"{sorted(bad)[:12]}")
    wf = WINDOWS[name]
    for label, lo, hi in ERAS:
        log(f"\n--- {series}, {label} ---")
        score_era(ms, tr, tsa, wf, lo, hi, log)


def score_era(ms, tr, tsa, wf, lo, hi, log):
    by_tk = {m["ticker"]: m for m in ms}
    rows = []
    skipped = collections.Counter()
    for t in tr:
        m = by_tk.get(t["ticker"])
        if not m:
            continue
        try:
            strike = float(m["floor_strike"])
        except (TypeError, ValueError):
            skipped["no strike"] += 1
            continue
        if m.get("strike_type") != "greater":
            skipped["strike type"] += 1
            continue
        d = event_date(m["event_ticker"])
        if not (lo <= d < hi):
            continue
        ts = float(t["ts"])
        if ts >= T_close(m):
            skipped["after close"] += 1
            continue
        f = forecast(wf(d), ts, tsa)
        if f is None:
            skipped["no forecast"] += 1
            continue
        mean, k, rr = f
        side, p = t["taker_side"], float(t["taker_paid"])
        won = 1 if (m["result"] == side) else 0
        rows.append({"ev": m["event_ticker"], "d": d, "k": k, "mean": mean, "strike": strike,
                     "side": side, "p": p, "won": won, "yes_won": 1 if m["result"] == "yes" else 0,
                     "n": min(float(t["count"]), CAP)})
    log(f"trades usable: {len(rows)}; skipped {dict(skipped)}")
    if len(rows) < 200:
        log("too few trades for a verdict")
        return

    evd = sorted({r["d"] for r in rows})
    half = evd[len(evd) // 2]
    IS = [r for r in rows if r["d"] < half]
    OOS = [r for r in rows if r["d"] >= half]
    log(f"events {evd[0]}..{evd[-1]}, IS < {half} <= OOS ({len(IS)} / {len(OOS)} trades)")

    # Forecast error by unknown-day count, fitted on IN-SAMPLE events only:
    # relative error of the final window average vs the forecast made with k unknown.
    final = {}
    for r in rows:
        days = wf(r["d"])
        if all(x in tsa for x in days):
            final[r["ev"]] = sum(tsa[x] for x in days) / len(days)
    err = collections.defaultdict(list)
    seen = set()
    for r in IS:
        key = (r["ev"], r["k"])
        if key in seen or r["ev"] not in final:
            continue
        seen.add(key)
        err[r["k"]].append(final[r["ev"]] / r["mean"] - 1)
    sig = {}
    for k in range(0, 8):
        e = err.get(k, [])
        if len(e) >= 8:
            sig[k] = max(1e-4, math.sqrt(sum(x * x for x in e) / len(e)))
    log("in-sample forecast error (relative sd) by unknown days: " +
        ", ".join(f"k={k}: {100*s:.2f}% (n={len(err[k])})" for k, s in sorted(sig.items())))

    def prob_yes(r):
        s = sig.get(r["k"])
        if s is None:
            return None
        if r["k"] == 0:
            return 1.0 if r["mean"] > r["strike"] else 0.0
        return 1 - phi((r["strike"] / r["mean"] - 1) / s)

    # Calibration on OOS: model vs the traded price as predictors of the result.
    bm = bp = n = 0.0
    for r in OOS:
        q = prob_yes(r)
        if q is None:
            continue
        py = r["p"] if r["side"] == "yes" else 1 - r["p"]
        bm += (q - r["yes_won"]) ** 2 * r["n"]; bp += (py - r["yes_won"]) ** 2 * r["n"]; n += r["n"]
    if n:
        log(f"OOS Brier (contract-weighted): model {bm/n:.4f}, traded price {bp/n:.4f}")

    for mode in ("TAKE", "MAKE"):
        for edge in (0.02, 0.05, 0.10):
            for nm, sub in (("IS", IS), ("OOS", OOS)):
                bets = collections.defaultdict(list)
                for r in sub:
                    q = prob_yes(r)
                    if q is None:
                        continue
                    qs = q if r["side"] == "yes" else 1 - q
                    if mode == "TAKE":
                        fee = taker_fee(r["p"])
                        if qs - r["p"] - fee > edge:
                            bets[r["ev"]].append(((r["won"] - r["p"] - fee), r["n"]))
                    else:
                        if r["p"] - qs > edge:
                            bets[r["ev"]].append(((r["p"] - r["won"]), r["n"]))
                report(log, f"{mode} edge>{int(100*edge)}c {nm}", bets)


def report(log, label, bets):
    evs = [e for e in bets if bets[e]]
    ct = sum(n for e in evs for _, n in bets[e])
    if not ct:
        log(f"  {label:<20} no bets")
        return
    pnl = sum(x * n for e in evs for x, n in bets[e])
    boots = []
    for _ in range(BOOT):
        s = [bets[RNG.choice(evs)] for _ in evs]
        c = sum(n for b in s for _, n in b)
        boots.append(sum(x * n for b in s for x, n in b) / c)
    boots.sort()
    log(f"  {label:<20} {ct:>8.0f} ct on {len(evs):>3} events  EV {100*pnl/ct:+6.2f}c/ct "
        f"[{100*boots[int(.025*BOOT)]:+.2f}, {100*boots[int(.975*BOOT)-1]:+.2f}]  "
        f"${pnl/max(1,len(evs)):+.2f}/event")


def T_close(m):
    s = (m.get("close_time") or "").rstrip("Z").split(".")[0]
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S"))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", required=True)
    ap.add_argument("--trades", nargs="+", required=True)
    ap.add_argument("--series", default="KXTSAW,KXTSAD")
    ap.add_argument("--tag", required=True)
    ap.add_argument("--daily", default="", help="a daily CSV written by an earlier run, instead of fetching")
    a = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    logf = open(os.path.join(OUT, f"tsa-{a.tag}.log"), "w")

    def log(s):
        print(s); logf.write(s + "\n"); logf.flush()

    mk = [r for f in a.markets for r in csv.DictReader(gzip.open(f, "rt"))]
    tr = [r for f in a.trades for r in csv.DictReader(gzip.open(f, "rt"))]
    if a.daily:
        tsa = {date.fromisoformat(r["date"]): int(r["screened"])
               for r in csv.DictReader(gzip.open(a.daily, "rt"))}
        log(f"TSA daily counts from {a.daily}: {len(tsa)} days, {min(tsa)} .. {max(tsa)}")
    else:
        tsa = fetch_tsa(range(2026, 2019, -1), log)
    if not tsa:
        log("TSA data unreadable; can't test")
        return
    with gzip.open(os.path.join(OUT, f"tsa-{a.tag}-daily.csv.gz"), "wt") as f:  # noqa
        f.write("date,screened\n")
        for d in sorted(tsa):
            f.write(f"{d.isoformat()},{tsa[d]}\n")
    for s in a.series.split(","):
        run(s, mk, tr, tsa, log)


if __name__ == "__main__":
    main()
