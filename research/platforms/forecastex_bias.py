"""Idea #69 (P12/P13): is ForecastEx mispriced near settlement, after its $0.01 fee?

ForecastEx (Interactive Brokers' exchange) is the one other venue that PUBLISHES its history: a
daily prices file per day, YES and NO priced separately. The probe (out/probe-probe-3.log)
showed a contract's row on or after its expiry date reads end_price 1.00 or 0.00, which is the
resolution. So the deep-ITM / longshot question asked of Kalshi can be asked here from history.

Per contract (one observation each, so a busy contract cannot dominate):
  entry   the LAST day strictly before the expiry date on which the YES side traded
          (pair_quantity > 0 and vwap > 0); price = that day's YES vwap. No lookahead: the
          outcome row is dated on or after the expiry date, the entry row strictly before it.
  outcome the first row dated on or after expiry whose YES end_price is exactly 1.00 or 0.00
          (the expiry-day row is still the pre-resolution mark), and the NO side's must be its
          complement; anything else is dropped and counted.
  trade   buy the DEAR side (price >= 0.5) at the vwap, pay $0.01, hold to settlement.
          Equivalently: is the cheap side over-priced (the Kalshi ladder's trade)?

vwap is what traded that day, not a quote: a resting bid at vwap is assumed to fill, which is
the optimistic end (adverse selection unmeasured; Kalshi's weather book lost most of its edge
to it). Scored with day-clustered CIs by expiry date, halves by expiry date, loss ratio against
implied, per product and per price band. The coupon (~3%/yr) is ignored: on a contract days
from expiry it is a hundredth of a cent.

Control: CFBTC (BTC on CF Benchmarks) outcomes against Coinbase's BTC-USD 1-minute close at
expiry, for strikes more than $300 from it: the inferred outcome must agree.

Read-only, unauthenticated, GET only (probe.get's whitelist plus Coinbase candles).
Output: out/fx69-<tag>.log and out/fx69-<tag>-obs.csv.gz.
"""
import argparse, csv, gzip, io, math, os, random, sys, time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe as P  # noqa: E402

P.ALLOWED["api.exchange.coinbase.com"] = ("/products/BTC-USD/candles",)
FEE = 0.01
OUT = P.OUT
LOG = []


def log(s=""):
    print(s, flush=True)
    LOG.append(str(s))


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


class Book:
    """Rows go in one at a time and only (end, qty, vwap) is kept: ~90 files x 27k rows would
    not fit as dicts on a runner (the #68 scorer was lost exactly that way)."""

    def __init__(self):
        self.by = defaultdict(dict)            # contract -> {date: {"YES": (end, q, vwap), "NO": ...}}
        self.exp = {}

    def add(self, r):
        c = sys.intern(r["event_contract"])
        self.by[c].setdefault(sys.intern(r["date"]), {})[r["subtype"]] = (
            f(r.get("end_price")), f(r.get("pair_quantity")), f(r.get("vwap")),
            f(r.get("start_price")), f(r.get("high_price")), f(r.get("low_price")))
        self.exp.setdefault(c, r["expiration_date"])


def build(rows):
    """rows: any iterable of csv row dicts -> observations and drop counts. Pure; tested."""
    b = Book()
    for r in rows:
        b.add(r)
    return observe(b)


EXAMPLES = defaultdict(list)


def ok(x):
    """A price only if it is one: no-trade rows carry 0.00, which is not a price."""
    return x if x is not None and 0 < x < 1 else ""


def entry_at(days, before, inclusive=False):
    """Last day strictly before `before` (or on it, inclusive) on which YES traded at a price."""
    for d in reversed(sorted(d for d in days if (d <= before if inclusive else d < before))):
        y = days[d].get("YES")
        if y and y[1] and y[1] > 0 and y[2] and 0 < y[2] < 1:
            return d, y + (None,) * (6 - len(y))
    return None


def observe(b):
    obs, drops = [], defaultdict(int)
    for c, days in b.by.items():
        try:
            e_dt = datetime.fromisoformat(b.exp[c])
        except ValueError:
            drops["bad expiry"] += 1
            continue
        e_day = e_dt.date().isoformat()
        after = sorted(d for d in days if d >= e_day)
        # the expiry-day row is still the pre-resolution mark (SGGDP_Q326_-2: 0.97 on its expiry
        # day, 1.00 the day after). The outcome is the first row on or after expiry where YES and
        # NO read exactly 1/0 or 0/1. A row with YES at 0 or 1 but NO not its complement (fx69-1
        # dropped 73,488 contracts on the FIRST such row) is skipped, not taken as a verdict:
        # a no-trade row can read 0.00 on both sides.
        ye = None
        saw_half = False
        for d in after:
            yv = (days[d].get("YES") or (None,))[0]
            nv = (days[d].get("NO") or (None,))[0]
            if yv in (0.0, 1.0) and nv is not None and abs(yv + nv - 1.0) < 1e-9:
                ye = yv
                break
            if yv in (0.0, 1.0):
                saw_half = True
        if ye is None:
            k = ("YES and NO resolutions disagree" if saw_half else "no 0/1 row after expiry yet")
            drops[k] += 1
            if saw_half and len(EXAMPLES[k]) < 5:
                EXAMPLES[k].append((c, [(d, (days[d].get("YES") or (None,))[0],
                                         (days[d].get("NO") or (None,))[0]) for d in after[:4]]))
            continue
        e_date = e_dt.date()
        last = entry_at(days, e_day)
        if last is None:
            drops["never traded before expiry"] += 1
            continue
        # "early": the last traded day at least two days before the expiry DATE. A daily
        # temperature contract expires the morning after the measured day, so "last" is usually
        # the measured day itself, its vwap mixing trades from before and after the reading.
        early = entry_at(days, (e_date - timedelta(days=2)).isoformat(), inclusive=True)
        o = dict(contract=c, product=c.split("_")[0], expiry=e_day, exp_ts=e_dt.timestamp(),
                 result="yes" if ye == 1.0 else "no")
        for tag, ent in (("", last), ("early_", early)):
            if ent is None:
                o.update({f"{tag}entry_day": "", f"{tag}days_left": "", f"{tag}p_yes": "",
                          f"{tag}qty": "", f"{tag}start": "", f"{tag}high": "", f"{tag}low": ""})
                continue
            d, y = ent
            o.update({f"{tag}entry_day": d, f"{tag}days_left": (e_date - date.fromisoformat(d)).days,
                      f"{tag}p_yes": y[2], f"{tag}qty": y[1], f"{tag}start": ok(y[3]),
                      f"{tag}high": ok(y[4]), f"{tag}low": ok(y[5])})
        obs.append(o)
    return obs, drops


def dear(o, mode="", px="vwap"):
    """The side bought: the one whose vwap is >= 0.5. Returns (price paid, won), or None when
    the price kind is missing. px: "vwap"; "start" (the day's first trade); "worst" (the dear
    side's dearest trade of the day: YES high, or 1 - YES low for NO, since every trade prints
    YES at p and NO at 1 - p)."""
    v = o.get(f"{mode}p_yes")
    if v in ("", None):
        return None
    yes = v >= 0.5
    # the YES print that is worst for the buyer of the dear side
    y = {"vwap": v, "start": o.get(f"{mode}start"),
         "worst": o.get(f"{mode}high") if yes else o.get(f"{mode}low")}[px]
    if y in ("", None):
        return None
    p = y if yes else 1.0 - y
    return (p, o["result"] == "yes") if yes else (p, o["result"] == "no")


def boot(by_day, reps=2000, seed=7):
    days = sorted(by_day)
    if len(days) < 2:
        return None, None
    rng, out = random.Random(seed), []
    for _ in range(reps):
        a = b = 0.0
        for _ in days:
            x, y = by_day[rng.choice(days)]
            a += x; b += y
        if b:
            out.append(a / b)
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))]


def score(obs, label, lo=0.5, hi=1.0, mode="", px="vwap"):
    by_day = defaultdict(lambda: [0.0, 0.0])
    n = lost = implied = pnl = 0.0
    for o in obs:
        d = dear(o, mode, px)
        if d is None:
            continue
        p, won = d
        if not (lo <= p < hi):
            continue
        e = (1.0 if won else 0.0) - p - FEE
        n += 1; pnl += e; lost += 0 if won else 1; implied += 1 - p
        by_day[o["expiry"]][0] += e; by_day[o["expiry"]][1] += 1
    if not n:
        log(f"  {label:<40} none")
        return
    a, b = boot(by_day)
    ci = f"[{a*100:+.2f}, {b*100:+.2f}]" if a is not None else "[n/a]"
    log(f"  {label:<40} n {n:6.0f} days {len(by_day):3d} lost {lost:5.0f} implied {implied:7.1f} "
        f"ratio {lost/implied if implied else float('nan'):.2f}  EV {pnl/n*100:+.2f}c/ct {ci}")


def report(obs):
    days = sorted({o["expiry"] for o in obs})
    log(f"\n{len(obs)} settled contracts with a traded price before expiry, {len(days)} expiry days")
    if len(days) < 4:
        log("too few days; stop")
        return
    half = set(days[: len(days) // 2])
    log("\nBuy the dear side at the last pre-expiry vwap, $0.01 fee, hold:")
    for lo, hi in ((0.5, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 0.95), (0.95, 0.98), (0.98, 1.0)):
        score(obs, f"dear side {lo:.2f}-{hi:.2f}", lo, hi)
    score(obs, "dear side 0.80-0.97 (the ladder band)", 0.8, 0.97)
    score([o for o in obs if o["expiry"] in half], "  first half, 0.80-0.97", 0.8, 0.97)
    score([o for o in obs if o["expiry"] not in half], "  second half, 0.80-0.97", 0.8, 0.97)
    score([o for o in obs if o["days_left"] <= 1], "  1 day or less left, 0.80-0.97", 0.8, 0.97)
    score([o for o in obs if o["days_left"] > 1], "  more than 1 day left, 0.80-0.97", 0.8, 0.97)
    by_p = defaultdict(list)
    for o in obs:
        by_p[o["product"]].append(o)
    log("\nPer product, 0.80-0.97, products with >= 40 contracts:")
    for p in sorted(by_p, key=lambda k: -len(by_p[k])):
        if len(by_p[p]) >= 40:
            score(by_p[p], f"{p}", 0.8, 0.97)


def temp(o):
    return o["product"][:2] in ("UH", "UL")


def stress(obs):
    """fx69-2: does the edge survive (a) entering before the measured day and (b) paying the
    day's first or worst print instead of its vwap? Halves by expiry day, temperature apart."""
    days = sorted({o["expiry"] for o in obs})
    half = set(days[: len(days) // 2])
    log("\nSTRESS: dear side 0.80-0.97, by entry and by the price paid")
    for mode, mlabel in (("", "last day before expiry"), ("early_", "2+ days before expiry")):
        for px in ("vwap", "start", "worst"):
            log(f"\n {mlabel}, at the day's {px}:")
            score(obs, "all", 0.8, 0.97, mode, px)
            score([o for o in obs if o["expiry"] in half], "  first half", 0.8, 0.97, mode, px)
            score([o for o in obs if o["expiry"] not in half], "  second half", 0.8, 0.97, mode, px)
            score([o for o in obs if temp(o)], "  temperature", 0.8, 0.97, mode, px)
            score([o for o in obs if not temp(o)], "  everything else", 0.8, 0.97, mode, px)
    for k, ex in EXAMPLES.items():
        log(f"\nexamples, {k}:")
        for c, rows in ex:
            log(f"  {c}: {rows}")


def btc_control(obs):
    rows = [o for o in obs if o["product"] == "CFBTC"]
    if not rows:
        log("\nCONTROL CFBTC: no CFBTC contracts in the sample")
        return
    log(f"\nCONTROL CFBTC: {len(rows)} contracts, e.g. {[o['contract'] for o in rows[:3]]}")
    agree = disagree = skipped = 0
    for o in rows[:400]:
        k = f(o["contract"].split("_")[-1])
        t = int(o["exp_ts"])
        try:
            c = P.get("https://api.exchange.coinbase.com/products/BTC-USD/candles",
                      {"granularity": 60, "start": datetime.fromtimestamp(t - 120, timezone.utc).isoformat(),
                       "end": datetime.fromtimestamp(t, timezone.utc).isoformat()})
        except Exception as ex:
            skipped += 1
            continue
        # a candle key is the bucket START; the one closing at expiry starts at t - 60
        px = {int(r[0]): r[4] for r in c}
        s = px.get(t - 60)
        if k is None or s is None or abs(s - k) < 300:
            skipped += 1
            continue
        if (s > k) == (o["result"] == "yes"):
            agree += 1
        else:
            disagree += 1
    log(f"  inferred outcome vs Coinbase at expiry (|spot - strike| >= $300): agree {agree}, "
        f"disagree {disagree}, skipped {skipped}")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--days", type=int, default=90)
    a = ap.parse_args(argv)
    today = datetime.now(timezone.utc).date()
    book, missing, nfiles, nrows = Book(), 0, 0, 0
    for i in range(1, a.days + 1):
        day = (today - timedelta(days=i)).strftime("%Y%m%d")
        try:
            b = P.get("https://forecastex.com/api/download", {"type": "prices", "date": day}, raw=True)
        except Exception:
            missing += 1
            continue
        nfiles += 1
        for r in csv.DictReader(io.StringIO(b.decode("utf-8", "replace"))):
            book.add(r); nrows += 1
    log(f"ForecastEx #69 {a.tag}: {nfiles} daily price files, {missing} missing, {nrows} rows")
    try:
        sm = P.get("https://forecastex.com/api/download", {"type": "summary",
                   "date": (today - timedelta(days=1)).strftime("%Y%m%d")}, raw=True)
        names = {r["product_id"]: (r["product_name"], r["product_category"])
                 for r in csv.DictReader(io.StringIO(sm.decode("utf-8", "replace")))}
    except Exception:
        names = {}
    obs, drops = observe(book)
    del book
    log(f"observations {len(obs)}; dropped {dict(drops)}")
    prods = defaultdict(int)
    for o in obs:
        prods[o["product"]] += 1
    log("products (contracts): " + ", ".join(f"{p} {n} ({names.get(p, ('?', '?'))[0][:40]})"
                                             for p, n in sorted(prods.items(), key=lambda x: -x[1])[:20]))
    report(obs)
    stress(obs)
    btc_control(obs)
    os.makedirs(OUT, exist_ok=True)
    with gzip.open(os.path.join(OUT, f"fx69-{a.tag}-obs.csv.gz"), "wt", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(obs[0].keys()) if obs else ["contract"])
        w.writeheader()
        w.writerows(obs)
    with open(os.path.join(OUT, f"fx69-{a.tag}.log"), "w") as fh:
        fh.write("\n".join(LOG) + "\n")


if __name__ == "__main__":
    main()
