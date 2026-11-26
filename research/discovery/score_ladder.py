"""Score ladder_tape.py's output: does the ladder's trade pay on other series?

Protocol, fixed BEFORE any result was seen (2026-11-24):

  The trade: a maker holds the 90-99c side, i.e. sold the side a taker bought
  at 1-10c (bands 01-02, 03-05, 06-10). Fee: each row's own maker fee
  (score_taker_flow.maker_fee_per_ct; `quadratic` series charge makers nothing).

  Headline cells, one per idea x time-to-close window x band:
    #21 crypto 15-minute:  BTC, ETH, SOL, XRP, DOGE           ttc 1-5m, 5-15m
    #22 equity index:      KXINXU, KXNASDAQ100U, KXINX15M      ttc 1-5m, 5-15m, 15-50m
    #23 commodity 15-min:  gold, WTI, nat gas, Brent, silver, copper  ttc 1-5m, 5-15m
    #24 FX and rates 15m:  EUR/USD, USD/JPY, GBP/USD, USD/CAD, AUD/USD,
                           2/5/10/30-year yields                ttc 1-5m, 5-15m
  "1-5m" pools the fine buckets 1-2m, 2-3m, 3-5m; "5-15m" pools 5-10m, 10-15m;
  "15-50m" pools 15-30m, 30-50m. The last minute is reported, never credited:
  round 1 found it flips sign between halves on KXBTCD.

  A headline cell PASSES only if, on the whole window AND on each half
  (settle-days split by date at the median day):
    * maker EV > 0 and the day-clustered 95% interval excludes zero,
    * loss ratio (taker wins / implied) < 0.90,
  and the whole window holds >= 30 markets on which the maker lost.

  CONTROL: KXBTCD, ttc 5-50 min, bands 03-10 is the live ladder's own trade
  (+1.49c/contract on 13,020 real contracts). If the control does not read
  positive with its interval clear of zero, a FAIL elsewhere is "not resolved",
  never "killed".

  Added 2026-11-25, BEFORE any KXBTC tape was read (round 1's sampled hours read
  +2.48c with H2's interval through zero, which is why it gets the full tape):
    #40 BTC hourly ranges: KXBTC                              ttc 5-15m, 15-50m
  i.e. the live ladder's own 5-50 minute window, on the range series.

  A series with any close off its fixed grid is refused: an early close moves
  trades between time-to-close buckets on the future (round 1's in-game trap).

Read-only over the exported CSVs; no network.
"""
import collections, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import score_taker_flow as S  # noqa: E402

IDEAS = {
    "#21 crypto 15m": ["KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXDOGE15M"],
    "#22 equity index": ["KXINXU", "KXNASDAQ100U", "KXINX15M"],
    "#23 commodity 15m": ["KXGOLD15M", "KXWTI15M", "KXNATGAS15M", "KXBRENT15M",
                          "KXSILVER15M", "KXCOPPER15M"],
    "#24 FX/rates 15m": ["KXEURUSD15M", "KXUSDJPY15M", "KXGBPUSD15M", "KXUSDCAD15M",
                         "KXAUDUSD15M", "KX2YRRATE15M", "KX5YRRATE15M", "KX10YRRATE15M",
                         "KX30YRRATE15M"],
    "#40 BTC hourly ranges": ["KXBTC"],
}
WINDOWS = {"#21 crypto 15m": ["1-5m", "5-15m"], "#22 equity index": ["1-5m", "5-15m", "15-50m"],
           "#23 commodity 15m": ["1-5m", "5-15m"], "#24 FX/rates 15m": ["1-5m", "5-15m"],
           "#40 BTC hourly ranges": ["5-15m", "15-50m"]}
POOL = {"<30s": "<1m", "30-60s": "<1m", "1-2m": "1-5m", "2-3m": "1-5m", "3-5m": "1-5m",
        "5-10m": "5-15m", "10-15m": "5-15m", "15-30m": "15-50m", "30-50m": "15-50m",
        "50m-3h": "50m-3h"}
BANDS = ["01-02", "03-05", "06-10"]
MIN_LOSSES = 30
MAX_RATIO = 0.90


def cells(rows, keyf):
    """{key: {day: [ct, paid_net_of_fee, won, loss_markets, markets]}}"""
    out = collections.defaultdict(dict)
    for r in rows:
        k = keyf(r)
        if k is None:
            continue
        fee = S.maker_fee_per_ct(r.get("fee_type"), r["band"])
        d = out[k].setdefault(r["day"], [0.0, 0.0, 0.0, 0, 0])
        d[0] += r["contracts"]; d[1] += r["paid"] - fee * r["contracts"]
        d[2] += r["taker_won"]; d[3] += r["loss_events"]; d[4] += r["events"]
    return out


def verdict(dr, half):
    parts = {"all": dr, "H1": {d: v for d, v in dr.items() if d < half},
             "H2": {d: v for d, v in dr.items() if d >= half}}
    st = {k: (S.stats(v, 0.0) if v else None) for k, v in parts.items()}
    ok = all(s and s["ev"] > 0 and s["lo"] > 0 and s["ratio"] < MAX_RATIO for s in st.values())
    ok = ok and st["all"]["loss_events"] >= MIN_LOSSES
    return ok, st


def main(paths, out=print):
    rows, cov = [], {}
    for p in paths:
        rows += S.load(p)
        cp = p.replace(".csv.gz", "-coverage.txt")
        if os.path.exists(cp):
            for line in open(cp).read().splitlines()[1:]:
                s, n, done, tr, off, g, days = line.split()
                cov[s] = (int(n), int(done), int(tr), int(off), g, int(days))
    out("Coverage (markets listed / read / trades / closes off grid / days read):")
    refused = set()
    for s in sorted(cov):
        n, done, tr, off, g, days = cov[s]
        flag = ""
        if off:
            refused.add(s); flag = "  REFUSED: closes off the grid"
        if done < n:
            flag += f"  INCOMPLETE: {n - done} unread"
        out(f"  {s:<16} {n:>6} {done:>6} {tr:>10} {off:>4} grid {g:>5} {days:>3}d{flag}")
    rows = [r for r in rows if r["series"] not in refused]
    days = sorted({r["day"] for r in rows})
    if not days:
        out("no data"); return
    half = days[len(days) // 2]
    out(f"\nWindow {days[0]}..{days[-1]} ({len(days)} settle-days); H1 < {half} <= H2.")
    out("EV is per contract to the maker holding the 90-99c side, after maker fee; "
        "interval = 95% bootstrap clustered by settle-day; losses = markets the maker lost.")

    # control
    ctl = cells(rows, lambda r: "ctl" if (r["series"] == "KXBTCD" and r["band"] in ("03-05", "06-10")
                                          and r["ttc"] in ("5-10m", "10-15m", "15-30m", "30-50m"))
                else None)
    out("\n=== CONTROL: KXBTCD, 5-50 min, taker paying 3-10c (the live ladder's trade) ===")
    ctl_ok = False
    if "ctl" in ctl:
        ok, st = verdict(ctl["ctl"], half)
        for k in ("all", "H1", "H2"):
            out(f"  {k:<3} " + (S.fmt(st[k]) if st[k] else "none"))
        ctl_ok = bool(st["all"] and st["all"]["ev"] > 0 and st["all"]["lo"] > 0)
        out(f"  control {'RESOLVES the ladder edge' if ctl_ok else 'does NOT resolve it: FAILs below are not kills'}"
            f" (account: +1.49c/contract)")
    else:
        out("  no control data")

    inv = {s: i for i, ss in IDEAS.items() for s in ss}
    head = cells(rows, lambda r: (inv[r["series"]], POOL.get(r["ttc"]), r["band"])
                 if r["series"] in inv and r["band"] in BANDS else None)
    out("\n=== HEADLINE (pre-registered) ===")
    passes = []
    for idea in IDEAS:
        out(f"\n{idea}")
        for w in WINDOWS[idea] + ["<1m"]:
            for b in BANDS:
                k = (idea, w, b)
                if k not in head:
                    continue
                ok, st = verdict(head[k], half)
                tag = "info only (last minute)" if w == "<1m" else ("PASS" if ok else "fail")
                if ok and w != "<1m":
                    passes.append(k)
                out(f"  {w:<6} {b}  {tag}")
                for kk in ("all", "H1", "H2"):
                    out(f"      {kk:<3} " + (S.fmt(st[kk]) if st[kk] else "none"))

    out("\n=== Per series, headline windows pooled, bands 01-10 (detail, not a test) ===")
    per = cells(rows, lambda r: r["series"] if (r["series"] in inv and r["band"] in BANDS
                                                and POOL.get(r["ttc"]) in WINDOWS[inv[r["series"]]])
                else None)
    for s in sorted(per):
        ok, st = verdict(per[s], half)
        out(f"  {s:<16} all " + S.fmt(st["all"]))
        for kk in ("H1", "H2"):
            if st[kk]:
                out(f"  {'':<16} {kk}  " + S.fmt(st[kk]))

    out(f"\nPASSING headline cells: {len(passes)}")
    for k in passes:
        out(f"  {k}")
    if not ctl_ok:
        out("Control did not resolve; read every 'fail' as unresolved.")


if __name__ == "__main__":
    main(sys.argv[1:])
