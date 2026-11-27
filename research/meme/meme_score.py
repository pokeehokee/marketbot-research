"""Score the "Grok Bot" overnight meme-coin prompt on rows meme_rec.py wrote at decision time.

Signal (the prompt's rules that free data can check; the rest are listed as UNTESTED):
  universe   launched or migrated <= 6h ago, reserve >= $40k, 5m volume >= $25k
  holders    >= 300, and growing since the previous token read (if two reads exist)
  rug check  top-10 holders < 35%, no mint authority, no freeze authority,
             LP locked/burned (locked_liquidity_percentage >= 90) -- see LOCK_UNKNOWN
  trigger    5m volume > 5x the 1h average per 5m; buys / (buys + sells) in 5m > 70%
  skip       price up > 300% in the last hour
  window     23:00-07:00 Central (05:00-13:00 UTC)
UNTESTED: 3+ of 40 smart-money wallets buying in 10 min, KOL wallets, dev wallet < 5%,
bundled launch, honeypot sell simulation. Nothing below assumes they would have helped.

Exit model, from GeckoTerminal 1-minute OHLCV after the signal (bucket keys are STARTS; entry is
the open of the first bucket starting >= signal time + LATENCY):
  entry      open x (1 + 2.5%) slippage (the prompt's max, which MEV takes), split in 2 parts
  costs      AMM fee 0.25% each way, priority fee + tip $0.05 per swap, exit slippage 1%
  take profit 40% at 2x, 30% at 4x, the rest on a 25% trailing stop from the peak
  hard stop  -12%; time stop 25 minutes; when a bucket touches both a stop and a target,
             the STOP is assumed first (the conservative order)
  gaps       a stop fills at the bucket's LOW when the low is through it, never at the stop
  rug        reserve falls > 70% between snapshots, or price -90% from entry: the remainder
             is worth zero (sells into a pulled pool fail)
Reported separately: win rate, payout multiple (mean exit / entry), profit per $1, rug share.

Run:  python3 -I meme_score.py --rec DIR_OF_ARTIFACTS [--fetch] --out out/score.txt
"""
import argparse, glob, gzip, json, math, os, statistics as st, sys, time, urllib.request, urllib.error
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import meme_rec as R   # noqa: E402

R.ALLOWED = R.ALLOWED + ("/networks/solana/pools/",)
SLIP_IN, SLIP_OUT, AMM, PRIO = 0.025, 0.01, 0.0025, 0.05
LATENCY = 30
TP = ((2.0, 0.40), (4.0, 0.30))
TRAIL, STOP, TIME_STOP = 0.25, -0.12, 25 * 60
STAKE = 20.0


def rows(pattern):
    for p in sorted(glob.glob(pattern, recursive=True)):
        with gzip.open(p, "rt") as fo:
            for line in fo:
                try:
                    yield json.loads(line)
                except ValueError:
                    continue


def night(ts):
    h = time.gmtime(ts).tm_hour
    return 5 <= h < 13


def signals(pools, tokens, lock_unknown_ok):
    """First qualifying snapshot per pool. tokens: token -> sorted [(ts, row)]."""
    out, done = [], set()
    for p in sorted(pools, key=lambda r: r["ts"]):
        a = p.get("addr")
        if not a or a in done or not R.universe(p, p["ts"]) or not night(p["ts"]):
            continue
        h1 = p.get("vol_h1")
        b, s = p.get("buys_m5"), p.get("sells_m5")
        if h1 is None or not b or s is None:
            continue
        if not (p["vol_m5"] > 5 * h1 / 12 and b / (b + s) > 0.70):
            continue
        if (p.get("chg_h1") or 0) > 300:
            continue
        lk = p.get("locked")
        if lk is None and not lock_unknown_ok:
            continue
        if lk is not None and lk < 90:
            continue
        tr = [t for ts_, t in tokens.get(p.get("token"), []) if ts_ <= p["ts"] and p["ts"] - ts_ <= 900]
        if not tr:
            continue
        t = tr[-1]
        if (t.get("holders") or 0) < 300 or t.get("top10") is None or t["top10"] >= 35:
            continue
        if t.get("mint_auth") not in (None, "no", "") or t.get("freeze_auth") not in (None, "no", ""):
            continue
        prev = [x for ts_, x in tokens.get(p["token"], []) if ts_ < p["ts"] - 120]
        if len(prev) >= 1 and (prev[-1].get("holders") or 0) >= t["holders"]:
            continue
        done.add(a)
        out.append(p)
    return out


def simulate(candles, t0, reserves=()):
    """candles: sorted [(start, o, h, l, c)]. reserves: [(ts, reserve)] for the rug test.
    Returns dict(exit_value per $1 staked, rugged, held_s)."""
    c = [x for x in candles if x[0] >= t0 + LATENCY]
    if not c:
        return None
    entry = c[0][1] * (1 + SLIP_IN)
    left, value, peak, tp_i = 1.0, 0.0, entry, 0
    r0 = next((r for ts_, r in reserves if ts_ <= c[0][0]), None)
    for k, o, h, l, cl in c:
        if k - c[0][0] > TIME_STOP:
            value += left * o / entry * (1 - SLIP_OUT)
            return {"x": value, "rug": False, "held": k - c[0][0]}
        rv = [r for ts_, r in reserves if k <= ts_ < k + 60]
        if (r0 and rv and min(rv) < 0.3 * r0) or l <= entry * 0.10:
            return {"x": value, "rug": True, "held": k - c[0][0]}
        stop = max(entry * (1 + STOP), peak * (1 - TRAIL) if tp_i >= len(TP) else 0)
        if l <= stop:   # stop first, filled at the worse of stop and low
            value += left * min(stop, max(l, 0)) / entry * (1 - SLIP_OUT) if o > stop else left * o / entry * (1 - SLIP_OUT)
            return {"x": value, "rug": False, "held": k - c[0][0]}
        while tp_i < len(TP) and h >= entry * TP[tp_i][0]:
            value += TP[tp_i][1] * TP[tp_i][0] * (1 - SLIP_OUT)
            left -= TP[tp_i][1]
            tp_i += 1
        peak = max(peak, h)
    value += left * c[-1][4] / entry * (1 - SLIP_OUT)
    return {"x": value, "rug": False, "held": c[-1][0] - c[0][0]}


def net(x, stake=STAKE):
    """Per $1 after AMM fees each way and priority fees on 2 entries + up to 3 exits."""
    return x * (1 - AMM) * (1 - AMM) - 5 * PRIO / stake


def fetch_ohlcv(cl, addr, t0):
    r = cl.get(f"/networks/solana/pools/{addr}/ohlcv/minute",
               {"aggregate": 1, "before_timestamp": t0 + 3 * 3600, "limit": 300, "currency": "usd"})
    lst = (((r or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
    return sorted((int(x[0]), float(x[1]), float(x[2]), float(x[3]), float(x[4])) for x in lst)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rec", required=True)
    ap.add_argument("--cache", default=os.path.join(HERE, "ohlcv_cache.json"))
    ap.add_argument("--fetch", action="store_true")
    a = ap.parse_args()
    pools = list(rows(os.path.join(a.rec, "**", "pools.jsonl.gz")))
    toks = defaultdict(list)
    for t in rows(os.path.join(a.rec, "**", "tokens.jsonl.gz")):
        toks[t["token"]].append((t["ts"], t))
    for v in toks.values():
        v.sort(key=lambda x: x[0])
    res = defaultdict(list)
    for p in pools:
        if p.get("reserve") is not None:
            res[p["addr"]].append((p["ts"], p["reserve"]))
    days = sorted({time.strftime("%Y-%m-%d", time.gmtime(p["ts"])) for p in pools})
    print(f"rows {len(pools)} pools {len(res)} token reads {sum(map(len, toks.values()))} days {days}")
    uni = {p["addr"] for p in pools if R.universe(p, p["ts"])}
    print(f"pools ever in the prompt's universe: {len(uni)}")
    cache = json.load(open(a.cache)) if os.path.exists(a.cache) else {}
    cl = R.Client() if a.fetch else None
    for lock_ok in (False, True):
        sig = signals(pools, toks, lock_ok)
        label = "LP lock required" if not lock_ok else "LP lock unknown allowed"
        out = []
        for p in sig:
            key = f"{p['addr']}:{p['ts']}"
            if key not in cache and cl:
                cache[key] = fetch_ohlcv(cl, p["addr"], p["ts"])
            cs = cache.get(key)
            if not cs:
                continue
            s = simulate([tuple(x) for x in cs], p["ts"], sorted(res[p["addr"]]))
            if s:
                out.append((p, s))
        print(f"\n{label}: signals {len(sig)}, simulated {len(out)}")
        if not out:
            continue
        xs = [net(s["x"]) for _, s in out]
        win = sum(x > 1 for x in xs) / len(xs)
        rug = sum(s["rug"] for _, s in out) / len(out)
        byday = defaultdict(list)
        for (p, s), x in zip(out, xs):
            byday[time.strftime("%Y-%m-%d", time.gmtime(p["ts"]))].append(x - 1)
        print(f"  win {win:.0%}  multiple {st.mean(xs):.3f}  profit per $1 {st.mean(xs) - 1:+.3f}  "
              f"rugged {rug:.0%}  days {len(byday)}  median hold {st.median(s['held'] for _, s in out) / 60:.0f} min")
        for d, v in sorted(byday.items()):
            print(f"    {d}: n={len(v)} per $1 {st.mean(v):+.3f}")
    json.dump(cache, open(a.cache, "w"))


if __name__ == "__main__":
    main()
