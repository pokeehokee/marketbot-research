"""Score ideas #20 (new listings) and #13 (fading overreactions) from the CSVs
age_move_screen.py exports. Same protocol as score_taker_flow.py, fixed before
looking: split settle-days in half; a cell is a candidate if IS it has >= 1500
contracts, >= 8 losing events, EV > 0 net of fees with a day-clustered 95%
interval above zero; it SURVIVES only if the OOS half alone clears the same bar.

Both sides are screened. MAKER: sold what the taker bought, pays the maker fee
only on `*_maker_fees` series. TAKER: bought it at that price, pays the taker
fee ceil(0.07 x C x P x (1-P)) per order at a 10-lot.
"""
import collections, math, os, sys

# The workflow runs this with `python3 -I`, which drops the script's own folder
# from sys.path; without this the import fails and the score file comes out empty.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score_taker_flow as S  # noqa: E402

TAKER_LOT = 10


def taker_fee_per_ct(band):
    p = S.BAND_MID[band]
    return math.ceil(0.07 * TAKER_LOT * p * (1 - p) * 100 - 1e-9) / 100.0 / TAKER_LOT


def group(rows, keyf, side):
    out = collections.defaultdict(dict)
    for r in rows:
        k = keyf(r)
        if k is None:
            continue
        d = out[k].setdefault(r["day"], [0.0, 0.0, 0.0, 0, 0])
        ct = r["contracts"]
        if side == "maker":
            fee = S.maker_fee_per_ct(r.get("fee_type"), r["band"])
            d[1] += r["paid"] - fee * ct; d[2] += r["taker_won"]
            d[3] += r["loss_events"]
        else:
            d[1] += r["taker_won"]; d[2] += r["paid"] + taker_fee_per_ct(r["band"]) * ct
            d[3] += r["events"] - r["loss_events"]
        d[0] += ct; d[4] += r["events"]
    return out


def screen(rows, keyf, label, out=print):
    days = sorted({r["day"] for r in rows})
    half = days[len(days) // 2]
    out(f"\n=== {label}: IS < {half} <= OOS ===")
    surv = []
    for side in ("maker", "taker"):
        cells = group(rows, keyf, side)
        n_c = 0
        for k, dr in sorted(cells.items()):
            is_ = {d: v for d, v in dr.items() if d < half}
            oos = {d: v for d, v in dr.items() if d >= half}
            if not is_ or not oos:
                continue
            a = S.stats(is_, 0.0)
            if (a is None or a["ct"] < S.MIN_CT or a["loss_events"] < S.MIN_LOSS_EVENTS
                    or a["ev"] <= 0 or a["lo"] <= 0):
                continue
            n_c += 1
            b = S.stats(oos, 0.0)
            ok = b is not None and b["ev"] > 0 and b["lo"] > 0
            flag = "SURVIVES" if ok else "dies OOS"
            out(f"  {side:<5} {str(k):<46} {flag}")
            out(f"      IS  {S.fmt(a)}")
            if b:
                out(f"      OOS {S.fmt(b)}")
            if ok:
                surv.append((side, k))
        out(f"  [{side}] IS candidates {n_c}")
    out(f"  survivors: {len(surv)}")
    return surv


def table(rows, keyf, keys, label, out=print):
    days = sorted({r["day"] for r in rows})
    half = days[len(days) // 2]
    cells = group(rows, keyf, "maker")
    out(f"\n=== {label} (maker EV; a taker gets minus this, less the taker fee) ===")
    for k in keys:
        dr = cells.get(k)
        if not dr:
            out(f"  {k}: no data"); continue
        for nm, sub in (("all", dr), ("H1", {d: v for d, v in dr.items() if d < half}),
                        ("H2", {d: v for d, v in dr.items() if d >= half})):
            s = S.stats(sub, 0.0) if sub else None
            out(f"  {str(k):<40} {nm:<3} " + (S.fmt(s) if s else "none"))


def main(prefix):
    import age_move_screen as A
    def load(p):
        return [r for r in S.load(p) if not r["series"].startswith("KXMVE")
                and "combo" not in (r.get("fee_type") or "")]
    age = load(prefix + "-age.csv.gz")
    move = load(prefix + "-move.csv.gz")
    print(f"age rows {len(age)}, {sum(r['contracts'] for r in age):,.0f} ct; "
          f"move rows {len(move)}, {sum(r['contracts'] for r in move):,.0f} ct")
    bands = list(S.BAND_MID)
    # #20 new listings
    table(age, lambda r: (r["ttc"],), [(a,) for a in A.AGE_NAMES], "#20 by listing age, all bands")
    screen(age, lambda r: ("ALL", r["ttc"], r["band"]), "#20 listing age x band, whole tape")
    screen(age, lambda r: (r["category"] or "?", r["ttc"], r["band"]),
           "#20 listing age x band, by category")
    # #13 overreactions
    table(move, lambda r: (r["ttc"],), [(m,) for m in A.CHASE_NAMES], "#13 by chase bucket, all bands")
    screen(move, lambda r: ("ALL", r["ttc"], r["band"]), "#13 chase x band, whole tape")
    screen(move, lambda r: (r["category"] or "?", r["ttc"], r["band"]),
           "#13 chase x band, by category")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "out/agemove-1")
