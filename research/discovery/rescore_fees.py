"""Re-score the taker-flow screen with the maker fee charged PER ROW.

Why this exists: score_taker_flow.group() stores one fee_type per cell, the
last row's. A category cell mixes `quadratic` series (makers pay nothing) with
`quadratic_with_maker_fees` series (makers pay ceil(0.0175 x C x P x (1-P)) per
order), so a Sports cell was charged either zero or the full fee depending on
which row came last. Here each row pays its own series' fee before grouping.

It also separates the two things a Sports cell mixes that must not be pooled:

  * PRE-GAME (time to close >= 3h). Trades sit hours before the market closes,
    so a few minutes of overtime cannot move a trade across a bucket edge.
  * IN-GAME (< 3h). `ttc` is measured to the market's REALISED close_time, and
    game markets close early when the outcome is decided. A comeback that forces
    overtime pushes the close later, so the trades that preceded a longshot win
    drift OUT of the short buckets: those buckets are selected on the future.
    No in-game cell is credited here, whatever it reads.

Read-only over the committed CSV; makes no network call.
"""
import collections, sys
import score_taker_flow as S

PREGAME = {"3-12h", "12-24h", "1-3d", "3d+"}
INGAME = {"<1m", "1-5m", "5-15m", "15-50m", "50m-3h"}


def group_net(rows, keyf):
    """Like S.group, but `paid` is net of each row's own maker fee."""
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


def report(rows, keyf, label, keys=None, out=print):
    cells = group_net(rows, keyf)
    days = sorted({r["day"] for r in rows})
    half = days[len(days) // 2]
    out(f"\n=== {label} (fee per row; IS < {half} <= OOS) ===")
    for k in (keys or sorted(cells)):
        if k not in cells:
            out(f"  {k}: no data")
            continue
        dr = cells[k]
        for nm, sub in (("all", dr), ("H1", {d: v for d, v in dr.items() if d < half}),
                        ("H2", {d: v for d, v in dr.items() if d >= half})):
            s = S.stats(sub, 0.0) if sub else None
            out(f"  {str(k):<50} {nm:<3} " + (S.fmt(s) if s else "none"))


def main(path):
    rows = [r for r in S.load(path) if not r["series"].startswith("KXMVE")
            and "combo" not in (r.get("fee_type") or "")]
    cheap = ("01-02", "03-05", "06-10", "11-20")

    def phase(r):
        return "pre" if r["ttc"] in PREGAME else "in"

    # #3 / #9: Sports cheap bands, split by phase and by fee type.
    report(rows, lambda r: (("Sports", phase(r), r["fee_type"], r["band"])
                            if r["category"] == "Sports" and r["band"] in cheap else None),
           "Sports, maker selling the cheap side, by phase x fee_type x band")
    report(rows, lambda r: (("Sports", "pre", r["ttc"], r["band"])
                            if r["category"] == "Sports" and r["band"] in cheap
                            and r["ttc"] in PREGAME else None),
           "Sports PRE-GAME cheap bands by ttc")
    # Top pre-game series by volume, so the claim is not one league.
    vol = collections.Counter()
    for r in rows:
        if r["category"] == "Sports" and r["band"] in cheap and r["ttc"] in PREGAME:
            vol[r["series"]] += r["contracts"]
    top = [s for s, _ in vol.most_common(12)]
    report(rows, lambda r: ((r["series"], "pre", "01-20")
                            if r["series"] in top and r["band"] in cheap
                            and r["ttc"] in PREGAME else None),
           "Sports PRE-GAME cheap side, top-12 series by volume",
           keys=[(s, "pre", "01-20") for s in top])
    # Non-sports categories, cheap side, pre-close >= 3h and < 3h, for #3.
    report(rows, lambda r: ((r["category"], phase(r), "01-20")
                            if r["category"] not in ("Sports", None, "")
                            and r["band"] in cheap else None),
           "Other categories, cheap side (01-20), by phase")
    # #19: KXBTCD every ttc, deep-ITM bands (the taker paying the cheap side).
    report(rows, lambda r: (("KXBTCD", r["ttc"], r["band"])
                            if r["series"] == "KXBTCD" and r["band"] in ("01-02", "03-05", "06-10")
                            else None),
           "KXBTCD by ttc x cheap band (#19 settlement window)",
           keys=[("KXBTCD", t, b) for t in ("<1m", "1-5m", "5-15m", "15-50m", "50m-3h")
                 for b in ("01-02", "03-05", "06-10")])
    # #7: Mentions, all bands pooled by phase and by band.
    report(rows, lambda r: (("Mentions", r["band"]) if r["category"] == "Mentions" else None),
           "Mentions by band (#7)")
    # #9 as a TAKER: buying the near-certain side at 96-99 pays the taker fee.
    report(rows, lambda r: (("ALL", "taker 96-99") if r["band"] == "96-99" else None),
           "Whole tape, taker buying at 96-99 (maker EV; the taker gets minus this, less fee)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "out/cells-full-1.csv.gz")
