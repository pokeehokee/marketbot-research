"""Score the taker-flow screen: which cells pay a MAKER, out of sample?

Input: the aggregate CSV.gz that taker_flow_screen.py --export writes.

Per cell (series x time-to-close x price band of what the TAKER paid), a maker
who sold the taker's side earns `paid - taker_won` over `contracts`. That is the
whole P&L of a maker fill held to settlement; no other number is used.

Discovery protocol, fixed BEFORE looking at results, because a screen over
hundreds of cells will find "edges" in noise by construction (04-05 ET read
-2.27c in-sample and +5.28c out; the base rate here for a cell that looks good
in-sample is poor):

  1. Split settle-DAYS in half by date. The first half is in-sample (IS).
  2. A cell is a candidate if, IS: >= MIN_CT contracts, >= MIN_LOSS_EVENTS
     events where the maker lost, maker EV > 0 net of maker fees, and the
     95% day-clustered bootstrap interval excludes zero.
  3. A candidate SURVIVES only if, on the second half (OOS) alone, maker EV
     > 0 and its day-clustered interval excludes zero too.
  4. Survivors are reported with pooled figures, the loss count, and capacity.

The control: KXBTCD at 5-50 minutes with the taker paying 0.03-0.10 is the live
ladder's own trade. If the screen does not find it, the screen is broken.
"""
import collections, csv, gzip, math, random, sys

MIN_CT = 1500
MIN_LOSS_EVENTS = 8
BOOT = 2000
RNG = random.Random(20261124)

BAND_MID = {"01-02": .015, "03-05": .04, "06-10": .08, "11-20": .155, "21-35": .28,
            "36-50": .43, "51-65": .58, "66-80": .73, "81-90": .855, "91-95": .93,
            "96-99": .975}
MAKER_LOT = 5


def maker_fee_per_ct(fee_type, band):
    """Kalshi maker fee, per contract, at a 5-lot, rounded UP per order.

    `quadratic` (and `flat`) series charge resting fills nothing; the
    `*_maker_fees` types charge ceil(0.0175 x C x P x (1-P)) per order."""
    if not fee_type or "maker" not in fee_type:
        return 0.0
    p = BAND_MID[band]
    return math.ceil(0.0175 * MAKER_LOT * p * (1 - p) * 100 - 1e-9) / 100.0 / MAKER_LOT


def load(path):
    rows = []
    with gzip.open(path, "rt") as f:
        for r in csv.DictReader(f):
            for k in ("contracts", "paid", "taker_won"):
                r[k] = float(r[k])
            r["trades"] = int(r["trades"])
            # Old exports carried one row per event; new ones carry the counts.
            r["events"] = int(r["events"]) if r.get("events") else 1
            r["loss_events"] = (int(r["loss_events"]) if r.get("loss_events")
                                else (1 if r["taker_won"] > 0 else 0))
            r.setdefault("event", "")
            rows.append(r)
    return rows


def stats(day_rows, fee):
    """day_rows: {day: [ct, paid, won, loss_events, events]} -> summary."""
    days = sorted(day_rows)
    ct = sum(day_rows[d][0] for d in days)
    if ct <= 0:
        return None
    paid = sum(day_rows[d][1] for d in days)
    won = sum(day_rows[d][2] for d in days)
    ev = (paid - won) / ct - fee
    boots = []
    for _ in range(BOOT):
        s = [day_rows[RNG.choice(days)] for _ in days]
        c = sum(x[0] for x in s)
        if c > 0:
            boots.append((sum(x[1] for x in s) - sum(x[2] for x in s)) / c - fee)
    boots.sort()
    lo = boots[int(0.025 * len(boots))] if boots else float("nan")
    hi = boots[int(0.975 * len(boots)) - 1] if boots else float("nan")
    return {"ct": ct, "paid": paid, "won": won, "ev": ev, "lo": lo, "hi": hi,
            "ratio": won / paid if paid else float("nan"),
            "loss_events": sum(day_rows[d][3] for d in days),
            "events": sum(day_rows[d][4] for d in days), "days": len(days),
            "ct_per_day": ct / max(1, len(days))}


def group(rows, keyf):
    """{cell: {day: [ct, paid, won, loss_events, events]}}.

    A loss event is an event on which the maker lost something in this cell.
    Two rows of one event in a merged cell (both taker sides) may count it twice;
    the counts are a floor-of-evidence gate, not an estimate."""
    out = collections.defaultdict(dict)
    meta = {}
    for r in rows:
        k = keyf(r)
        if k is None:
            continue
        d = out[k].setdefault(r["day"], [0.0, 0.0, 0.0, 0, 0])
        d[0] += r["contracts"]; d[1] += r["paid"]; d[2] += r["taker_won"]
        d[3] += r["loss_events"]; d[4] += r["events"]
        meta[k] = r.get("fee_type")
    return out, meta


def fmt(s):
    return (f"{s['ct']:>10.0f} ct {s['ct_per_day']:>8.0f}/d  EV {100*s['ev']:+6.2f}c "
            f"[{100*s['lo']:+6.2f},{100*s['hi']:+6.2f}]  ratio {s['ratio']:.2f}  "
            f"maker-loss events {s['loss_events']}/{s['events']}")


def screen(rows, keyf, label, out=print):
    days = sorted({r["day"] for r in rows})
    half = days[len(days) // 2]
    cells, meta = group(rows, keyf)
    out(f"\n=== {label}: {len(cells)} cells, days {days[0]}..{days[-1]}, "
        f"IS < {half} <= OOS ===")
    cands, surv = [], []
    for k, dr in cells.items():
        band = k[-1]
        fee = maker_fee_per_ct(meta.get(k), band)
        is_ = {d: v for d, v in dr.items() if d < half}
        oos = {d: v for d, v in dr.items() if d >= half}
        if not is_ or not oos:
            continue
        s_is = stats(is_, fee)
        if (s_is is None or s_is["ct"] < MIN_CT or s_is["loss_events"] < MIN_LOSS_EVENTS
                or s_is["ev"] <= 0 or s_is["lo"] <= 0):
            continue
        s_oos = stats(oos, fee)
        cands.append((k, s_is, s_oos, fee))
        if s_oos and s_oos["ev"] > 0 and s_oos["lo"] > 0:
            surv.append((k, s_is, s_oos, stats(dr, fee), fee))
    out(f"IS candidates: {len(cands)}; OOS survivors: {len(surv)}")
    for k, s_is, s_oos, fee in sorted(cands, key=lambda x: -x[1]["ev"] * x[1]["ct_per_day"]):
        flag = "SURVIVES" if any(k == s[0] for s in surv) else "dies OOS"
        out(f"  {str(k):<52} fee {100*fee:.2f}c  {flag}")
        out(f"      IS  {fmt(s_is)}")
        if s_oos:
            out(f"      OOS {fmt(s_oos)}")
    return surv


def show(rows, keyf, keys, label, out=print):
    cells, meta = group(rows, keyf)
    out(f"\n=== {label} ===")
    days = sorted({r["day"] for r in rows})
    half = days[len(days) // 2]
    for k in keys:
        if k not in cells:
            out(f"  {k}: no data")
            continue
        dr = cells[k]
        fee = maker_fee_per_ct(meta.get(k), k[-1])
        for nm, sub in (("all", dr), ("H1", {d: v for d, v in dr.items() if d < half}),
                        ("H2", {d: v for d, v in dr.items() if d >= half})):
            s = stats(sub, fee) if sub else None
            out(f"  {str(k):<46} {nm:<3} " + (fmt(s) if s else "none"))


def main(path):
    # RFQ-only combos cannot be rested on, so they are not a maker opportunity.
    rows = [r for r in load(path) if not r["series"].startswith("KXMVE")
            and "combo" not in (r.get("fee_type") or "")]
    out = print
    tot = sum(r["contracts"] for r in rows)
    out(f"{len(rows)} aggregate rows, {tot:,.0f} contracts, "
        f"{len({r['series'] for r in rows})} series")

    # Where the flow is, by category and fee type.
    by = collections.defaultdict(float)
    for r in rows:
        by[(r["category"], r["fee_type"])] += r["contracts"]
    out("\ncontracts by category / fee_type:")
    for k, v in sorted(by.items(), key=lambda x: -x[1])[:25]:
        out(f"  {str(k):<60} {v:>14,.0f}")

    # Whole-exchange maker return by band, the favourite-longshot curve.
    show(rows, lambda r: ("ALL", r["band"]), [("ALL", b) for b in BAND_MID],
         "whole tape, maker EV by taker-paid band")

    # Control: the live ladder's own cell.
    ctrl = lambda r: (r["series"], "ladder", r["band"]) if (
        r["series"] == "KXBTCD" and r["ttc"] in ("5-15m", "15-50m")
        and r["band"] in ("03-05", "06-10")) else None
    show(rows, ctrl, [("KXBTCD", "ladder", "03-05"), ("KXBTCD", "ladder", "06-10")],
         "CONTROL: KXBTCD, 5-50 min, taker paid 0.03-0.10 (the live ladder's trade)")

    surv = screen(rows, lambda r: (r["series"], r["ttc"], r["band"]),
                  "series x ttc x band")
    surv2 = screen(rows, lambda r: (r["category"] or "?", r["ttc"], r["band"]),
                   "category x ttc x band")
    out("\n=== SURVIVORS (pooled) ===")
    for k, s_is, s_oos, s_all, fee in surv + surv2:
        out(f"  {str(k):<52} {fmt(s_all)}")


if __name__ == "__main__":
    main(sys.argv[1])
