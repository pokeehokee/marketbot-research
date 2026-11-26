"""Round-2 idea #44b: what would Kalshi's liquidity pools pay a small resting order?

The 2026-11-18 note in CLAUDE.md read every pool as needing "1,000 contracts resting per
side, the retail-excluded wall". Kalshi's own help article says otherwise (read 2026-11-24,
help.kalshi.com "Liquidity Incentive Program"): Target Size is the depth the WHOLE book must
reach on a side for a snapshot to count, not a minimum per participant. Each snapshot (one a
second) scores every resting order as size x a distance multiplier: full credit at or better
than the Reference Price (walking down from the best bid, the first level where cumulative
size reaches Target/5), Discount Factor ^ ticks below it. Scores are normalised per side, a
snapshot is worth up to 2.0 (yes share + no share), and the period's reward is split by
each account's share of all snapshot scores. period_reward is in centi-cents (API schema).

So a small order's take is roughly  daily_reward x q / (S + q) / n,  where S is the side's
weighted qualifying size and n the number of sides that qualify (1 or 2). This scan
measures S on every market with an ACTIVE liquidity pool, several times, and reports what
q contracts resting at the reference price would earn per day and per dollar of collateral.

It measures the REWARD only. The cost (being filled by someone who knows more) is measured
separately from the tape, so nothing here is a verdict by itself.

Read-only, unauthenticated, its own two-path whitelist. Run:
  python3 -I lip_scan.py --tag lip-1 --snapshots 8 --gap-min 20
"""
import argparse, calendar, collections, gzip, json, math, os, statistics, time, urllib.error, urllib.parse, urllib.request

BASE = "https://api.elections.kalshi.com/trade-api/v2"
ALLOWED = ("/incentive_programs", "/markets/orderbooks")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
CHUNK = 20
Q = 10  # contracts we would rest per side


def get(path, params):
    assert path in ALLOWED, path
    # orderbooks needs the ticker parameter REPEATED; a comma list fabricates one empty book.
    qs = urllib.parse.urlencode(params, doseq=True)
    err = None
    for nap in (0, 2, 5, 15, 30):
        time.sleep(nap)
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                BASE + path + "?" + qs, headers={"User-Agent": "research", "Accept": "application/json"}),
                timeout=60)
            return json.load(r), None
        except urllib.error.HTTPError as e:
            err = f"HTTP {e.code}"
            if e.code not in (429, 500, 502, 503, 504):
                return None, err
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    return None, err


def ts(s):
    return calendar.timegm(time.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")) if s else None


def active_programs(log):
    rows, cursor = [], None
    for page in range(2000):
        q = {"status": "active", "type": "liquidity", "limit": 10000}
        if cursor:
            q["cursor"] = cursor
        p, err = get("/incentive_programs", q)
        if err:
            raise RuntimeError(f"/incentive_programs: {err} after {len(rows)} rows")
        rows += p.get("incentive_programs") or []
        cursor = p.get("next_cursor")
        if not cursor:
            break
    else:
        log(f"STOPPED AT THE PAGE CAP with {len(rows)} rows: the status filter may be ignored")
    log(f"/incentive_programs?status=active: {len(rows)} rows in {page + 1} pages")
    now = time.time()
    keep = [r for r in rows if r.get("incentive_type") == "liquidity"
            and (ts(r.get("start_date")) or 0) <= now < (ts(r.get("end_date")) or 0)]
    log(f"active liquidity programmes: {len(rows)} listed, {len(keep)} running now")
    return keep


def daily_reward(r):
    """Dollars a day for one programme: period_reward is centi-cents ($1 = 10,000)."""
    days = (ts(r["end_date"]) - ts(r["start_date"])) / 86400
    return r["period_reward"] / 10000.0 / days if days > 0 else 0.0


SUBCENT = [0, 0]  # [levels seen, levels off the 1c grid]


def levels(side):
    out = []
    for lv in side or []:
        try:
            p, c = float(lv[0]), float(lv[1])
        except (TypeError, ValueError, IndexError):
            continue
        if 0 < p < 1 and c > 0:
            out.append((p, c))
            SUBCENT[0] += 1
            SUBCENT[1] += abs(p * 100 - round(p * 100)) > 1e-6
    return sorted(out, reverse=True)


def side_score(lv, target, disc, tick=0.01):
    """(reference price, weighted size S) for one side, or None when the side never reaches
    Target Size (then no order on it qualifies in that snapshot)."""
    if not lv or sum(c for _, c in lv) < target:
        return None
    cum, ref = 0.0, None
    for p, c in lv:
        cum += c
        if cum >= target / 5.0:
            ref = p
            break
    s = 0.0
    for p, c in lv:
        k = max(0, round((ref - p) / tick))
        s += c * (disc ** k)
    return ref, s


def market_take(book, target, disc, q=Q):
    """Fraction of the snapshot's pool that q contracts at each qualifying side's reference
    price would earn, and the collateral they lock. (share, collateral, n_sides)."""
    sides = []
    for key in ("yes_dollars", "no_dollars"):
        sc = side_score(levels(book.get(key)), target, disc)
        if sc:
            sides.append(sc)
    if not sides:
        return 0.0, 0.0, 0
    n = len(sides)
    share = sum(q / (s + q) for _, s in sides) / n
    coll = sum(q * ref for ref, _ in sides)
    return share, coll, n


def scan(snapshots, gap_min, log):
    progs = active_programs(log)
    log("one programme verbatim: " + (json.dumps(progs[0]) if progs else "none"))
    by_m = collections.defaultdict(list)
    for r in progs:
        by_m[r["market_ticker"]].append(r)
    res = collections.defaultdict(list)  # ticker -> [(share, coll, n)]
    for i in range(snapshots):
        tickers, t0, got = sorted(by_m), time.time(), 0
        for j in range(0, len(tickers), CHUNK):
            chunk = tickers[j:j + CHUNK]
            p, err = get("/markets/orderbooks", {"tickers": chunk})
            if err:
                log(f"  orderbooks chunk {j}: {err}"); continue
            for b in p.get("orderbooks") or []:
                t = b.get("ticker")
                if t not in by_m or t not in chunk:
                    continue  # never pair by position, and refuse a label we did not ask for
                r0 = by_m[t][0]
                tgt = float(r0.get("target_size_fp") or 0)
                disc = (r0.get("discount_factor_bps") or 0) / 10000.0
                res[t].append(market_take(b.get("orderbook_fp") or {}, tgt, disc))
                got += 1
        log(f"snapshot {i + 1}/{snapshots}: {got} of {len(tickers)} books in {time.time() - t0:.0f}s")
        if i + 1 < snapshots:
            time.sleep(gap_min * 60)
    return by_m, res


def report(by_m, res, log):
    rows = []
    for t, obs in res.items():
        d = sum(daily_reward(r) for r in by_m[t])
        if not obs:
            continue
        share = statistics.mean(o[0] for o in obs)            # 0 on snapshots that don't qualify
        coll = statistics.mean(o[1] for o in obs if o[2]) if any(o[2] for o in obs) else 0.0
        qual = sum(1 for o in obs if o[2]) / len(obs)
        rows.append((t, by_m[t][0].get("series_ticker"), d, share * d, coll, qual,
                     by_m[t][0].get("target_size_fp"), max((r.get("max_reward_per_account") or 0)
                                                           for r in by_m[t]) / 10000.0))
    rows.sort(key=lambda r: -(r[3] / r[4] if r[4] else 0))
    tot_d = sum(r[2] for r in rows)
    log(f"\n{len(rows)} markets with a running pool, ${tot_d:,.0f}/day of rewards in total")
    log(f"what {Q} contracts resting at each qualifying side's reference price would earn:")
    log(f"{'market':<44}{'pool $/d':>9}{'our $/d':>9}{'coll $':>8}{'%/day':>7}{'qual':>6}{'target':>8}{'cap $':>7}")
    for t, s, d, ours, coll, qual, tgt, cap in rows[:60]:
        log(f"{t:<44}{d:>9.2f}{ours:>9.3f}{coll:>8.2f}{(100 * ours / coll if coll else 0):>7.2f}"
            f"{qual:>6.2f}{tgt or '':>8}{cap:>7.0f}")
    by_s = collections.defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for t, s, d, ours, coll, qual, tgt, cap in rows:
        a = by_s[s]; a[0] += 1; a[1] += d; a[2] += ours; a[3] += coll
    log("\nby series (top 30 by our $/day):")
    for s, (n, d, ours, coll) in sorted(by_s.items(), key=lambda x: -x[1][2])[:30]:
        log(f"  {s:<28} {n:>4} mkts  pool ${d:>8.2f}/d  ours ${ours:>6.3f}/d on ${coll:>7.2f}"
            f"  {(100 * ours / coll if coll else 0):.2f}%/day")
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--snapshots", type=int, default=8)
    ap.add_argument("--gap-min", type=float, default=20)
    a = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    f = open(os.path.join(OUT, f"lip-{a.tag}.txt"), "w")

    def log(s):
        print(s, flush=True); f.write(s + "\n"); f.flush()

    log(f"read from {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    by_m, res = scan(a.snapshots, a.gap_min, log)
    log(f"price levels read {SUBCENT[0]}, off the 1c grid {SUBCENT[1]} (the discount assumes a 1c tick;"
        " a finer tick would make S too small and OUR share too large)")
    rows = report(by_m, res, log)
    with gzip.open(os.path.join(OUT, f"lip-{a.tag}.json.gz"), "wt") as g:
        json.dump({"programs": by_m, "obs": res, "rows": rows}, g)


if __name__ == "__main__":
    main()
