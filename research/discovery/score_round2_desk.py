"""Round-2 ideas #27-#65 that the round-1 whole-exchange sample can already answer.

Protocol fixed 2026-11-24 BEFORE these slices were computed. Each idea is one
pre-registered slice of `out/cells-full-1.csv.gz` (80 sampled hours over 28 days,
every trade on the exchange in those hours), scored as round 1 scored a cell:

  the maker sells what the taker bought; EV/contract = (paid - won)/ct - maker fee
  (score_taker_flow.maker_fee_per_ct: 0 on `quadratic`, a 5-lot's rounded fee
  on `*_maker_fees`). PASS needs, on the whole window and on each half
  (settle-days split at the median): EV > 0, settle-day-clustered 95% interval
  above zero, loss ratio < 0.90; and >= 30 markets on which the maker lost.
  Fewer than 30 losses reads UNDERPOWERED, never "killed".

Pooling over every time-to-close bucket is deliberate: round 1's in-game trap
moves trades BETWEEN buckets, it does not change a trade's paid or won, so a
slice that pools all buckets is immune to it. #40 is the one slice that keeps a
bucket (the ladder's own 5-50 min) and it is a crypto series with fixed closes.

Read-only, no network. Run: python3 -I score_round2_desk.py out/cells-full-1.csv.gz
"""
import collections, re, sys, os

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import score_taker_flow as S  # noqa: E402

LONG = ("01-02", "03-05", "06-10")
ALLB = tuple(S.BAND_MID)
LADDER_TTC = ("5-15m", "15-50m")

# (id, label, series regex, taker side or None for both, bands, ttc buckets or None)
IDEAS = [
    ("#27", "esports longshots", r"^KX(CS2|LOL|VALORANT|VAL|DOTA2?|R6|COD|OW|RL)[A-Z0-9]*(GAME|MAP|MATCH)$", None, LONG, None),
    ("#30", "player props: sell the over", r"^KX(NBA(PTS|REB|AST|3PT|PRA|BLK|STL)|NFL(RECYDS|RSHYDS|PASSYDS|REC|TD|PASSTD|RUSHTD)|NHL(PTS|GOAL|GOALS|AST|SOG)|MLB(HR|HITS|KS|TB|RBI))$", "yes", ALLB, None),
    ("#30b", "player props: sell the under (control for side)", r"^KX(NBA(PTS|REB|AST|3PT|PRA|BLK|STL)|NFL(RECYDS|RSHYDS|PASSYDS|REC|TD|PASSTD|RUSHTD)|NHL(PTS|GOAL|GOALS|AST|SOG)|MLB(HR|HITS|KS|TB|RBI))$", "no", ALLB, None),
    ("#33", "in-game quarter/half longshots, all buckets pooled", r"^KX(NBA|NFL|NCAAF|NCAAB|NCAAMB|NHL|WNBA)(1Q|2Q|3Q|4Q|1H|2H|1P|2P|3P)[A-Z]*$", None, LONG, None),
    ("#29", "Rotten Tomatoes longshots", r"^KXRT", None, LONG, None),
    ("#31", "Trump approval longshots", r"APPROV", None, LONG, None),
    ("#32", "mortgage-rate longshots", r"^KX30YMORT|MORTGAGE", None, LONG, None),
    ("#35", "YouTube view longshots", r"^KXYT", None, LONG, None),
    ("#36", "Netflix top-10 longshots", r"^KXNETFLIX", None, LONG, None),
    ("#37", "golf round-1 leader longshots", r"^KXPGAR1", None, LONG, None),
    ("#38", "rain longshots", r"^KXRAIN", None, LONG, None),
    ("#38b", "rain, every band (calibration)", r"^KXRAIN", None, ALLB, None),
    ("#40", "BTC hourly ranges, ladder window", r"^KXBTC$", None, LONG, LADDER_TTC),
    ("#43", "House races being counted", r"^KXHOUSERACE", None, LONG, None),
    ("#48", "monthly min/max longshots", r"(MINMON|MAXMON)$", None, LONG, None),
    ("#57", "award longshots", r"GRAMMY|OSCAR|EMMY|GOLDENGLOBE|MVP|CYYOUNG|ROTY|HEISMAN", None, LONG, None),
    ("#58", "reality-TV longshots", r"VOICE|DWTS|DANCING|BACHELOR|SURVIVOR|BIGBROTHER|IDOL", None, LONG, None),
    ("#59", "snow longshots", r"SNOW|SKI", None, LONG, None),
    ("#60", "hurricane longshots", r"HURR|TROPICAL|NAMEDSTORM", None, LONG, None),
    ("#62", "app / ranking longshots", r"APPSTORE|TOPAPP|STEAM|APPRANK|AIUSAGE|CHATGPT", None, LONG, None),
]
MIN_LOSSES, MAX_RATIO = 30, 0.90


def slice_cells(rows, rx, side, bands, ttcs):
    rx = re.compile(rx)
    out, series = {}, collections.Counter()
    for r in rows:
        if not rx.search(r["series"]) or r["band"] not in bands:
            continue
        if side and r["taker_side"] != side:
            continue
        if ttcs and r["ttc"] not in ttcs:
            continue
        fee = S.maker_fee_per_ct(r.get("fee_type"), r["band"])
        d = out.setdefault(r["day"], [0.0, 0.0, 0.0, 0, 0])
        d[0] += r["contracts"]; d[1] += r["paid"] - fee * r["contracts"]
        d[2] += r["taker_won"]; d[3] += r["loss_events"]; d[4] += r["events"]
        series[r["series"]] += r["contracts"]
    return out, series


def verdict(dr, half):
    parts = {"all": dr, "H1": {d: v for d, v in dr.items() if d < half},
             "H2": {d: v for d, v in dr.items() if d >= half}}
    st = {k: (S.stats(v, 0.0) if v else None) for k, v in parts.items()}
    if not st["all"]:
        return "NO DATA", st
    if st["all"]["loss_events"] < MIN_LOSSES:
        return "UNDERPOWERED", st
    ok = all(s and s["ev"] > 0 and s["lo"] > 0 and s["ratio"] < MAX_RATIO for s in st.values())
    return ("PASS" if ok else "fail"), st


def main(paths, out=print):
    rows = []
    for p in paths:
        rows += S.load(p)
    days = sorted({r["day"] for r in rows})
    half = days[len(days) // 2]
    out(f"{len(rows)} rows, settle-days {days[0]}..{days[-1]}; H1 < {half} <= H2")
    for iid, label, rx, side, bands, ttcs in IDEAS:
        dr, series = slice_cells(rows, rx, side, bands, ttcs)
        v, st = verdict(dr, half)
        top = ", ".join(f"{s} {c/1e6:.2f}M" for s, c in series.most_common(4)) or "none"
        out(f"\n{iid} {label}: {v}\n    series: {top}" + (f" (+{len(series)-4} more)" if len(series) > 4 else ""))
        for k in ("all", "H1", "H2"):
            if st.get(k):
                out(f"    {k:<3} " + S.fmt(st[k]))


if __name__ == "__main__":
    main(sys.argv[1:])
