"""Score soccer DRAW markets from a series probe (round-2 idea #34).

Protocol fixed 2026-11-24, before any draw-market data was read. Idea: fans back
a team, not a draw, so the draw may be underpriced and a resting BUYER of the
draw earns. Data: series_probe.py with --ticker-re on the tie markets, so every
trade of every settled draw market Kalshi still lists.

Three cells, each pooled over every price band and every series, then per series:

  MAKER BUYS THE DRAW   (primary)  taker bought NO, so the resting side holds YES.
  MAKER SELLS THE DRAW  (control)  taker bought YES, so the resting side holds NO.
  TAKER BUYS THE DRAW              we pay the ask and the taker fee.

The resting side's result per contract is the taker's paid - won (whichever
side the taker took), minus the maker fee where the series charges one
(score_taker_flow.maker_fee_per_ct, 5-lot rounding). The taker's is won - paid
- ceil(0.07 x C x P x (1-P)) per trade. Loss ratio and lost markets are the
holder's own (score_combo.day_cells does the arithmetic for both).

PASS, as everywhere in round 1 and 2: whole window AND each half (match-days
split at the median) EV > 0, interval above zero, ratio < 0.90; >= 30 markets
lost. Interval: 95% bootstrap clustered by match-day (the settle date).

Pooled over the whole match, on purpose: a trade's price and outcome do not
depend on when it was made, so in-game trades cannot be misfiled. It does mean
the figure blends pre-game and in-game makers. Read-only, no network.
Run: python3 -I score_draws.py out/probe-draws-1
"""
import collections, csv, gzip, math, re, sys, os, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import score_combo as C  # noqa: E402
import score_taker_flow as S  # noqa: E402
import taker_flow_screen as T  # noqa: E402

FEE_RX = re.compile(r"\b((?:quadratic|flat)\S*)")


def fee_types(log_path):
    """{series: fee_type} from the probe log's /series listing."""
    out = {}
    if os.path.exists(log_path):
        for line in open(log_path):
            m = re.match(r"^  (KX[A-Z0-9]+)\s", line)
            f = FEE_RX.search(line)
            if m and f:
                out[m.group(1)] = f.group(1)
    return out


def rows_from_probe(prefix):
    fees = fee_types(prefix + ".log")
    mk = {}
    with gzip.open(prefix + "-markets.csv.gz", "rt") as f:
        for r in csv.DictReader(f):
            mk[r["ticker"]] = (r["series"], r["result"], T.utc(r["close_time"]))
    cells, notes = {}, collections.Counter()
    with gzip.open(prefix + "-trades.csv.gz", "rt") as f:
        for r in csv.DictReader(f):
            m = mk.get(r["ticker"])
            if not m or m[1] not in ("yes", "no"):
                notes["no settled market"] += 1; continue
            series, result, close = m
            ts, side, p, ct = float(r["ts"]), r["taker_side"], float(r["taker_paid"]), float(r["count"])
            if close is not None and ts > close:
                notes["after close"] += 1; continue
            band = T.bucket(T.BAND_EDGES, T.BAND_NAMES, p)
            if band is None:
                notes["no band"] += 1; continue
            ft = fees.get(series)
            if ft is None:
                notes[f"fee type unknown for {series}: maker fee assumed"] += 1
                ft = "quadratic_with_maker_fees"
            day = time.strftime("%Y-%m-%d", time.gmtime(close or ts))
            k = (series, ft, day, band, side, r["ticker"])
            a = cells.setdefault(k, [0.0, 0.0, 0.0, 0.0, 0.0, 0])
            won = ct if side == result else 0.0
            a[0] += ct; a[1] += ct * p; a[2] += won
            a[3] += S.maker_fee_per_ct(ft, band) * ct
            a[4] += math.ceil(0.07 * ct * p * (1 - p) * 100 - 1e-9) / 100.0
            a[5] += 1
    # one row per (series, day, band, side): markets and markets the TAKER won
    agg = {}
    for (series, ft, day, band, side, tk), v in cells.items():
        g = agg.setdefault((series, ft, day, band, side), [0.0] * 5 + [0, 0, 0])
        for i in range(5):
            g[i] += v[i]
        g[5] += v[5]; g[6] += 1; g[7] += 1 if v[2] > 0 else 0
    rows = [{"series": s, "fee_type": ft, "day": d, "band": b, "taker_side": sd,
             "contracts": v[0], "paid": v[1], "won": v[2], "mfee": v[3], "tfee": v[4],
             "trades": v[5], "markets": v[6], "loss_markets": v[7]}
            for (s, ft, d, b, sd), v in agg.items()]
    return rows, notes, fees


CELLS = (("MAKER BUYS THE DRAW (primary)", "no", "sell"),
         ("MAKER SELLS THE DRAW (control)", "yes", "sell"),
         ("TAKER BUYS THE DRAW", "yes", "buy"))


def main(prefix, out=print):
    rows, notes, fees = rows_from_probe(prefix)
    out(f"fee types read: {fees}")
    for k, v in sorted(notes.items()):
        out(f"note: {v} trades - {k}")
    days = sorted({r["day"] for r in rows})
    if not days:
        out("no data"); return []
    half = days[len(days) // 2]
    out(f"match-days {days[0]}..{days[-1]} ({len(days)}); H1 < {half} <= H2")
    passes = []
    for label, side, trade in CELLS:
        for series in ["ALL"] + sorted({r["series"] for r in rows}):
            dc = C.day_cells(rows, lambda r, s=series: (
                "k" if r["taker_side"] == side and (s == "ALL" or r["series"] == s) else None), trade)
            if "k" not in dc:
                continue
            ok, st = C.verdict(dc["k"], half)
            out(f"\n{label} - {series}: {'PASS' if ok else 'fail'}")
            for k in ("all", "H1", "H2"):
                out(f"    {k:<3} " + (C.fmt(st[k]) if st[k] else "none"))
            if ok:
                passes.append((label, series))
    out(f"\nPASSING cells: {passes}")
    return passes


if __name__ == "__main__":
    main(sys.argv[1])
