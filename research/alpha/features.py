"""Precursor features from a market's public trade tape. Pure functions, no I/O.

Everything here answers one question about a market at a decision time t:
what did the tape show UP TO t, and what happened AFTER t. The two halves are
kept apart by construction, because this repo has shipped four lookaheads and
every one was a feature that quietly read a price from after its timestamp:

  * features(trades, t, ...) reads only trades with ts <= t, and the caller
    hands it nothing else (`before` is cut first, then everything is computed
    from it). test_features freezes this by appending a trade after t and
    checking that no feature moves.
  * targets(trades, t, ...) is the only place a later trade is read.

A trade is (ts, taker_side, yes_price, count). `taker_side` is the outcome the
taker ACQUIRED (CLAUDE.md's `side` trap): a "no" taker pushed the YES price
DOWN, so taker flow is signed +count for yes, -count for no.

Missing is never zero: a feature with no data in its window is None and the
screen drops that row for that feature, rather than reading "no move".
"""
import math

OFFSETS_MIN = (120, 60, 30, 15, 10, 5, 2)   # decision times, minutes before close
FEATURES = ("age", "dp5", "dp15", "dp60", "flow15", "flow60", "vol15", "vol60",
            "burst", "ntr15", "size60", "rng60", "last_yes", "dist", "hour_et")


def group_of(offset_min):
    """Time-to-close group a decision time is screened in."""
    if offset_min >= 60:
        return "long"
    if offset_min >= 15:
        return "mid"
    return "short"


def yes_price(side, taker_price):
    return taker_price if side == "yes" else 1.0 - taker_price


def _last_at_or_before(trades, t):
    """Index of the last trade with ts <= t in a ts-sorted list, or -1."""
    lo, hi = 0, len(trades)
    while lo < hi:
        mid = (lo + hi) // 2
        if trades[mid][0] <= t:
            lo = mid + 1
        else:
            hi = mid
    return lo - 1


def _hour_et(t):
    try:
        from zoneinfo import ZoneInfo
        import datetime as dt
        return dt.datetime.fromtimestamp(t, ZoneInfo("America/New_York")).hour
    except Exception:          # no tz database: fall back to EST, labelled by the caller
        return int(((t - 5 * 3600) % 86400) // 3600)


def features(trades, t):
    """Features at decision time t from trades sorted by ts. None if nothing traded by t.

    Returns (p, p_last, feats) where p is the volume-weighted YES price of the last
    three trades (damps bid-ask bounce) and p_last is the last trade's YES price."""
    i = _last_at_or_before(trades, t)
    if i < 0:
        return None
    before = trades[: i + 1]            # the ONLY data below this line
    last = before[-1]
    tail = before[-3:]
    w = sum(x[3] for x in tail)
    p = sum(x[2] * x[3] for x in tail) / w
    p_last = last[2]

    def price_at(k_min):
        j = _last_at_or_before(before, t - k_min * 60)
        return before[j][2] if j >= 0 else None

    def window(k_min):
        lo = t - k_min * 60
        return [x for x in before if x[0] > lo]

    f = {"age": t - last[0], "last_yes": 1.0 if last[1] == "yes" else 0.0,
         "dist": abs(p - 0.5), "hour_et": float(_hour_et(t))}
    for k in (5, 15, 60):
        q = price_at(k)
        f[f"dp{k}"] = None if q is None else p_last - q
    for k in (15, 60):
        win = window(k)
        tot = sum(x[3] for x in win)
        f[f"flow{k}"] = (sum(x[3] if x[1] == "yes" else -x[3] for x in win) / tot) if tot else None
        f[f"vol{k}"] = math.log1p(tot)
    w15, w60 = window(15), window(60)
    v15, v60 = sum(x[3] for x in w15), sum(x[3] for x in w60)
    f["burst"] = (v15 / (v60 / 4.0)) if v60 > 0 else None
    f["ntr15"] = float(len(w15))
    f["size60"] = math.log(v60 / len(w60)) if w60 else None
    f["rng60"] = (max(x[2] for x in w60) - min(x[2] for x in w60)) if len(w60) >= 2 else None
    return p, p_last, f


def horizon_s(offset_min):
    """How far ahead the move target looks: half the time left, at most 5 minutes."""
    return min(300.0, offset_min * 60 / 2.0)


def targets(trades, t, close_ts, result, offset_min):
    """(y, move). y is 1 if YES settled. move is the YES price change from t to
    t+horizon, None if nothing traded in (t, t+horizon] (missing is not zero)."""
    y = 1.0 if result == "yes" else 0.0
    i = _last_at_or_before(trades, t)
    end = min(t + horizon_s(offset_min), close_ts)
    j = _last_at_or_before(trades, end)
    move = None
    if i >= 0 and j > i:
        move = trades[j][2] - trades[i][2]
    return y, move


def rows_for_market(trades, close_ts, result):
    """One row per decision time at which the market had traded. trades are
    (ts, side, yes_price, count), any order; trades after close are dropped."""
    tr = sorted(x for x in trades if x[0] <= close_ts)
    out = []
    for off in OFFSETS_MIN:
        t = close_ts - off * 60
        r = features(tr, t)
        if r is None:
            continue
        p, p_last, f = r
        y, move = targets(tr, t, close_ts, result, off)
        out.append({"offset_min": off, "p": p, "p_last": p_last, "y": y, "move": move, **f})
    return out
