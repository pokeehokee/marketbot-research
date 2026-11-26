"""Round-2 idea #25: does wholesale gasoline lead the AAA price Kalshi settles on?

Retail pump prices follow wholesale gasoline with a lag of one to three weeks.
If Kalshi's AAA gas markets (KXAAAGASD daily, KXAAAGASW weekly, KXAAAGASM monthly,
and their 2023-25 AAAGAS* predecessors) are priced off the retail trend alone,
a forecast that also uses the wholesale price should beat the traded price.
Counterparty: takers trading the pump-price trend.

Settlement source, checked rather than assumed: every market carries the AAA
figure it settled on (`expiration_value`); the script re-grades every strike from
it and refuses a verdict below 98% agreement. The AAA daily series used as INPUT is
that same set of published figures, one per settled event date.

Wholesale: FRED DGASNYH (New York Harbor) and DGASUSGULF (US Gulf Coast)
conventional gasoline spot, $/gal, public CSV, no key. EIA publishes these with a
lag, so a value dated d is treated as known only from d + WHOLESALE_LAG_D days
(7, pre-registered, longer than EIA's usual lag on purpose).

Zero lookahead, at each real trade's timestamp:
  * an AAA figure for day d is known only from noon ET on d;
  * only trades whose target date is strictly after the last known AAA day are
    used (h >= 1), so no trade is scored where the answer might already be out;
  * the forecast is  A(target) = A(last) + b0*h + b1*m*h + b2*g*min(h,21)
      h  = days from last known AAA day to the target date,
      m  = AAA daily change over the last 5-14 known days,
      g  = mean known wholesale (last 5 obs, both hubs) - A(last);
    NAIVE drops g. Coefficients and the error sd per horizon bucket are fitted on
    the in-sample half of TARGET DATES and used unchanged on the other half.

Bets per real trade (price someone actually traded at), sized min(count, 25):
  TAKE: buy the taker's side at their price plus the taker fee when the model says
        it is cheap by more than `edge`; MAKE: be the resting side (no maker fee on
        `quadratic`) when the model says the taker overpaid by more than `edge`.
        MAKE has no queue model, so it is an upper bound.
Intervals bootstrapped by TARGET DATE: every strike of every series that settles
on one day settles off one number. PASS (out-of-sample only, since in-sample is
where the model was fitted): EV > 0, interval above zero, loss ratio < 0.90, and
>= 30 losing markets.

Run: python3 -I gas_lead.py --markets out/probe-gas-1-markets.csv.gz \
         --trades out/probe-gas-1-trades.csv.gz --tag gas-1
"""
import argparse, bisect, calendar, collections, csv, gzip, math, os, random, re, sys, time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
CAP, BOOT, H_MAX = 25, 2000, 45
WHOLESALE_LAG_D = 7
RNG = random.Random(20261125)
FRED = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}&cosd=2023-01-01"
HUBS = ("DGASNYH", "DGASUSGULF")
SERIES = ("KXAAAGASD", "KXAAAGASW", "KXAAAGASM", "AAAGASD", "AAAGASW", "AAAGASM")
MON = {m: i for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
BUCKETS = ((1, 1), (2, 3), (4, 7), (8, 14), (15, 45))


def target_date(event_ticker):
    m = re.search(r"-(\d\d)([A-Z]{3})(\d\d)[A-Z]*(?:-|$)", event_ticker)
    return date(2000 + int(m.group(1)), MON[m.group(2)], int(m.group(3))) if m else None


def known_from(d):
    """Unix time from which day d's AAA figure counts as known: noon ET on d."""
    return datetime(d.year, d.month, d.day, 12, tzinfo=ET).timestamp()


def utc(s):
    s = (s or "").rstrip("Z").split(".")[0]
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%S")) if s else None


def fetch_fred(log):
    out = {}
    for sid in HUBS:
        body = None
        for nap in (0, 10, 30, 60, 120, 240):
            time.sleep(nap)
            try:
                body = urllib.request.urlopen(urllib.request.Request(
                    FRED.format(sid), headers={"User-Agent": "Mozilla/5.0 research-script",
                                               "Accept": "text/csv"}), timeout=180).read().decode()
                break
            except Exception as e:
                log(f"FRED {sid}: {type(e).__name__}: {e} (retrying)")
        if body is None:
            return None
        rows = list(csv.reader(body.splitlines()))
        log(f"FRED {sid}: header {rows[0]}, {len(rows) - 1} rows, first {rows[1]}, last {rows[-1]}")
        for d, v in rows[1:]:
            try:
                out.setdefault(date.fromisoformat(d), {})[sid] = float(v)
            except ValueError:
                pass  # FRED writes "." for a missing day
    return {d: sum(v.values()) / len(v) for d, v in out.items() if len(v) == len(HUBS)}


def aaa_series(mk, log):
    vals, bad, n = {}, [], 0
    for m in mk:
        if m["result"] not in ("yes", "no"):
            continue
        d = target_date(m["event_ticker"])
        try:
            v, k = float(m["expiration_value"]), float(m["floor_strike"])
        except (TypeError, ValueError):
            continue
        n += 1
        if ("yes" if v > k else "no") != m["result"]:
            bad.append(m["ticker"])
        vals.setdefault(d, collections.Counter())[round(v, 3)] += 1
    aaa = {d: c.most_common(1)[0][0] for d, c in vals.items() if d}
    split = {d: dict(c) for d, c in vals.items() if len(c) > 1}
    log(f"source check: {n - len(bad)} of {n} strikes graded as Kalshi did from expiration_value "
        f"({(n - len(bad)) / max(1, n):.2%}); disagreeing: {bad[:8]}; dates with two figures: {split}")
    return aaa, (n - len(bad)) / max(1, n)


def features(ts, d_target, aaa_days, aaa, wd, ws):
    """(h, A_last, m, g) known at ts, or None. aaa_days / wd are sorted date lists."""
    tday = datetime.fromtimestamp(ts, ET).date()
    i = bisect.bisect_right(aaa_days, tday) - 1
    while i >= 0 and known_from(aaa_days[i]) > ts:
        i -= 1
    if i < 0:
        return None
    last = aaa_days[i]
    h = (d_target - last).days
    if h < 1 or h > H_MAX:
        return None
    j = bisect.bisect_right(aaa_days, last - timedelta(days=5)) - 1
    if j < 0 or (last - aaa_days[j]).days > 14:
        return None
    m = (aaa[last] - aaa[aaa_days[j]]) / (last - aaa_days[j]).days
    k = bisect.bisect_right(wd, tday - timedelta(days=WHOLESALE_LAG_D))
    if k < 5 or (tday - wd[k - 1]).days > WHOLESALE_LAG_D + 10:
        return None
    g = sum(ws[x] for x in wd[k - 5:k]) / 5 - aaa[last]
    return h, aaa[last], m, g


def xrow(f, naive):
    h, _, m, g = f
    return [h, m * h] if naive else [h, m * h, g * min(h, 21)]


def ols(X, y):
    k = len(X[0])
    A = [[sum(r[i] * r[j] for r in X) for j in range(k)] for i in range(k)]
    b = [sum(r[i] * t for r, t in zip(X, y)) for i in range(k)]
    for c in range(k):  # Gauss-Jordan
        p = max(range(c, k), key=lambda r: abs(A[r][c]))
        A[c], A[p], b[c], b[p] = A[p], A[c], b[p], b[c]
        for r in range(k):
            if r != c and A[c][c]:
                f = A[r][c] / A[c][c]
                A[r] = [a - f * x for a, x in zip(A[r], A[c])]
                b[r] -= f * b[c]
    return [b[i] / A[i][i] for i in range(k)]


def bucket(h):
    return next(i for i, (lo, hi) in enumerate(BUCKETS) if lo <= h <= hi)


def phi(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def taker_fee(p):
    return math.ceil(0.07 * 10 * p * (1 - p) * 100 - 1e-9) / 100.0 / 10


def build_rows(mk, tr, aaa, ws, log):
    by_tk = {m["ticker"]: m for m in mk if m["series"] in SERIES and m["result"] in ("yes", "no")}
    aaa_days, wd = sorted(aaa), sorted(ws)
    rows, skip = [], collections.Counter()
    for t in tr:
        m = by_tk.get(t["ticker"])
        if not m:
            continue
        if m.get("strike_type") != "greater":
            skip["strike type"] += 1; continue
        try:
            strike = float(m["floor_strike"])
        except (TypeError, ValueError):
            skip["no strike"] += 1; continue
        ts, d = float(t["ts"]), target_date(m["event_ticker"])
        if d is None:
            skip["no target date"] += 1; continue
        if ts >= (utc(m["close_time"]) or 0):
            skip["after close"] += 1; continue
        f = features(ts, d, aaa_days, aaa, wd, ws)
        if f is None:
            skip["no features (h<1, gap in AAA or wholesale)"] += 1; continue
        side, p = t["taker_side"], float(t["taker_paid"])
        rows.append({"d": d, "tk": m["ticker"], "series": m["series"], "f": f, "strike": strike,
                     "side": side, "p": p, "won": int(m["result"] == side),
                     "yes_won": int(m["result"] == "yes"), "n": min(float(t["count"]), CAP)})
    log(f"trades usable: {len(rows)}; skipped {dict(skip)}")
    return rows


def fit(IS, aaa, naive, log):
    seen, X, y, H = set(), [], [], []
    for r in IS:
        key = (r["d"], r["f"])
        if key in seen or r["d"] not in aaa:
            continue
        seen.add(key)
        X.append(xrow(r["f"], naive)); y.append(aaa[r["d"]] - r["f"][1]); H.append(r["f"][0])
    beta = ols(X, y)
    res = collections.defaultdict(list)
    for x, t, h in zip(X, y, H):
        res[bucket(h)].append(t - sum(a * b for a, b in zip(beta, x)))
    sd = {b: max(0.002, math.sqrt(sum(e * e for e in v) / len(v))) for b, v in res.items() if len(v) >= 10}
    log(f"  {'NAIVE' if naive else 'MODEL'} fitted on {len(X)} information sets: beta "
        f"{[round(b, 5) for b in beta]}; residual sd by horizon: " +
        ", ".join(f"{BUCKETS[b][0]}-{BUCKETS[b][1]}d {100*s:.2f}c (n={len(res[b])})" for b, s in sorted(sd.items())))
    return beta, sd


def prob_yes(r, beta, sd, naive):
    s = sd.get(bucket(r["f"][0]))
    if s is None:
        return None
    mean = r["f"][1] + sum(a * b for a, b in zip(beta, xrow(r["f"], naive)))
    return 1 - phi((r["strike"] - mean) / s)


def report(log, label, bets):
    """bets: {target date: [(pnl/ct, ct, lost_market_key or None, implied)]}"""
    evs = [e for e in bets if bets[e]]
    ct = sum(b[1] for e in evs for b in bets[e])
    if not ct:
        log(f"    {label:<24} no bets"); return None
    pnl = sum(b[0] * b[1] for e in evs for b in bets[e])
    lost = len({b[2] for e in evs for b in bets[e] if b[2]})
    loss_ct = sum(b[1] for e in evs for b in bets[e] if b[2])
    implied = sum(b[3] * b[1] for e in evs for b in bets[e])
    agg = [(sum(b[0] * b[1] for b in bets[e]), sum(b[1] for b in bets[e])) for e in evs]
    boots = []
    for _ in range(BOOT):  # resample whole target dates, each collapsed to (pnl, contracts)
        s = [agg[RNG.randrange(len(agg))] for _ in agg]
        boots.append(sum(x[0] for x in s) / sum(x[1] for x in s))
    boots.sort()
    lo, hi = boots[int(.025 * BOOT)], boots[int(.975 * BOOT) - 1]
    ratio = loss_ct / implied if implied else float("nan")
    ok = pnl > 0 and lo > 0 and ratio < 0.90 and lost >= 30
    log(f"    {label:<24} {ct:>8.0f} ct {len(evs):>4} dates  EV {100*pnl/ct:+6.2f}c [{100*lo:+.2f}, {100*hi:+.2f}]"
        f"  ratio {ratio:.2f}  lost {lost} mkts  {'PASS' if ok else ''}")
    return ok


def score(rows, aaa, log):
    dates = sorted({r["d"] for r in rows})
    half = dates[len(dates) // 2]
    IS = [r for r in rows if r["d"] < half]
    OOS = [r for r in rows if r["d"] >= half]
    log(f"target dates {dates[0]}..{dates[-1]} ({len(dates)}); IS < {half} <= OOS "
        f"({len(IS)} / {len(OOS)} trades)")
    passes = []
    for naive in (True, False):
        nm = "NAIVE (retail trend only)" if naive else "MODEL (trend + wholesale)"
        beta, sd = fit(IS, aaa, naive, log)
        bm = bp = n = 0.0
        hb = collections.defaultdict(lambda: [0.0, 0.0, 0.0, 0.0])
        for r in OOS:
            q = prob_yes(r, beta, sd, naive)
            if q is None:
                continue
            py = r["p"] if r["side"] == "yes" else 1 - r["p"]
            em, ep = (q - r["yes_won"]) ** 2 * r["n"], (py - r["yes_won"]) ** 2 * r["n"]
            bm += em; bp += ep; n += r["n"]
            mean = r["f"][1] + sum(a * b for a, b in zip(beta, xrow(r["f"], naive)))
            a = hb[bucket(r["f"][0])]
            a[0] += r["n"]; a[1] += em; a[2] += ep; a[3] += r["n"] * (aaa[r["d"]] - mean)
        log(f"  {nm}: OOS Brier (contract-weighted) model {bm/max(n,1):.4f}, traded price {bp/max(n,1):.4f}")
        for b, a in sorted(hb.items()):
            log(f"      horizon {BUCKETS[b][0]}-{BUCKETS[b][1]}d: {a[0]:>9.0f} ct  Brier model {a[1]/a[0]:.3f}"
                f"  price {a[2]/a[0]:.3f}  forecast bias {100*a[3]/a[0]:+.2f}c")
        for mode in ("TAKE", "MAKE"):
            for edge in (0.02, 0.05, 0.10):
                for lab, sub in (("IS", IS), ("OOS", OOS)):
                    bets = collections.defaultdict(list)
                    for r in sub:
                        q = prob_yes(r, beta, sd, naive)
                        if q is None:
                            continue
                        qs = q if r["side"] == "yes" else 1 - q
                        if mode == "TAKE":
                            fee = taker_fee(r["p"])
                            if qs - r["p"] - fee > edge:
                                bets[r["d"]].append((r["won"] - r["p"] - fee, r["n"],
                                                     r["tk"] if not r["won"] else None, 1 - r["p"]))
                        elif r["p"] - qs > edge:
                            bets[r["d"]].append((r["p"] - r["won"], r["n"],
                                                 r["tk"] if r["won"] else None, r["p"]))
                    ok = report(log, f"{mode} >{int(100*edge)}c {lab}", bets)
                    if ok and lab == "OOS":
                        passes.append((nm, mode, edge))
    # DELTA: does the wholesale term add anything ON TOP OF the traded price? Lean the way
    # the wholesale term moves the probability (MODEL minus NAIVE), at the traded price
    # plus the taker fee. This asks the real question even though both models are far
    # less informed than the market overall.
    fits = {nv: fit(IS, aaa, nv, lambda s: None) for nv in (True, False)}
    for edge in (0.02, 0.05):
        for lab, sub in (("IS", IS), ("OOS", OOS)):
            bets = collections.defaultdict(list)
            for r in sub:
                qn = prob_yes(r, *fits[True], True)
                qm = prob_yes(r, *fits[False], False)
                if qn is None or qm is None or abs(qm - qn) <= edge:
                    continue
                side = "yes" if qm > qn else "no"
                p_yes = r["p"] if r["side"] == "yes" else 1 - r["p"]
                p = p_yes if side == "yes" else 1 - p_yes
                won = r["yes_won"] if side == "yes" else 1 - r["yes_won"]
                fee = taker_fee(p)
                bets[r["d"]].append((won - p - fee, r["n"], r["tk"] if not won else None, 1 - p))
            ok = report(log, f"DELTA >{int(100*edge)}c {lab}", bets)
            if ok and lab == "OOS":
                passes.append(("DELTA wholesale on top of price", "TAKE", edge))
    log(f"\nPASSING out-of-sample cells: {passes or 'none'}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", nargs="+", required=True)
    ap.add_argument("--trades", nargs="+", required=True)
    ap.add_argument("--tag", required=True)
    a = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    logf = open(os.path.join(OUT, f"gas-{a.tag}.log"), "w")

    def log(s):
        print(s, flush=True); logf.write(s + "\n"); logf.flush()

    log(f"run {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}; wholesale lag {WHOLESALE_LAG_D}d")
    mk = [r for f in a.markets for r in csv.DictReader(gzip.open(f, "rt"))]
    aaa, agree = aaa_series([m for m in mk if m["series"] in SERIES], log)
    log(f"AAA figures on {len(aaa)} dates, {min(aaa)}..{max(aaa)}")
    if agree < 0.98:
        log("SOURCE CHECK FAILED; no verdict."); return
    ws = fetch_fred(log)
    if not ws:
        log("wholesale unreadable; no verdict."); return
    log(f"wholesale (mean of both hubs) on {len(ws)} days, {min(ws)}..{max(ws)}")
    tr = [r for f in a.trades for r in csv.DictReader(gzip.open(f, "rt"))]
    rows = build_rows(mk, tr, aaa, ws, log)
    del tr
    by_s = collections.defaultdict(list)
    for r in rows:
        by_s["KX*" if r["series"].startswith("KX") else "2023-25 AAAGAS*"].append(r)
    for lab, sub in [("ALL SERIES", rows)] + sorted(by_s.items()):
        log(f"\n===== {lab} =====")
        if len(sub) < 200:
            log("too few trades"); continue
        score(sub, aaa, log)


if __name__ == "__main__":
    main()
