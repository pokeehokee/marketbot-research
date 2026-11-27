#!/usr/bin/env python3
"""Record the order books a fill test needs, for the two edges the 2026-11-24 discovery left alive.

WHY
---
Both survivors were measured on the public trade tape, which shows only the makers who DID get
filled. Whether a small order joining at the back of the queue would also fill, and on which
trades, is the open question -- the same question that killed the tennis longshot edge (real
+2.28c, but the queue at the favourite's bid dwarfed the day's volume). Kalshi serves no
historical order books, so the queue can only be measured by writing it down as it happens.

  * sports: selling the longshot (1-10c) 3-24 h before the game, i.e. resting on the favourite;
  * temp:   selling the cheap side (3-35c) of four city temperature series.

WHAT A SNAPSHOT IS FOR
---------------------
The scorer (not written yet; it needs the data first) joins a simulated 5-lot at the BACK of the
queue at each snapshot and counts it filled only once the public tape shows more contracts trading
at or through that price than were queued ahead of it. Cancellations ahead of us would only help,
so ignoring them makes the fill model conservative. That is why a snapshot every 15 minutes is
enough: each one is a join point, and the tape (read later, at full resolution) does the rest.

WHAT IT IS NOT
--------------
Read-only and unauthenticated. Every request goes through research/commodity/book_collector.get(),
whose whitelist refuses anything but the market listing and the orderbook reads. It places, amends
and cancels nothing, and writes only to its own database and CSV files.

TIME TO CLOSE
-------------
A game market's `close_time` is NOT when the game ends: game markets close early once decided,
which is why the screen measured time to the REALISED close. So the lead stored here is to the
earlier of `close_time` and `expected_expiration_time`, and both raw times are stored too, so the
scorer can re-cut the window once the realised close is known.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "commodity"))
import book_collector as B  # noqa: E402  (get, list_series, fetch_depth, the sentinel rules)

DEFAULT_DB = os.path.join(HERE, "fill_books.db")

# The sports series that carried ~85% of the surviving pre-game cheap-band flow in the
# 2026-11-24 screen (research/discovery/out/cells-full-1.csv.gz on the research branch),
# season-long futures left out. A series with nothing open costs one empty listing call.
SPORTS = ("KXNFLTD KXNCAAMBGAME KXNCAAFGAME KXNFLFIRSTTD KXODIMATCH KXPGATOUR KXNCAAWBGAME "
          "KXPGAR1LEAD KXF1RACE KXNHLFIRSTGOAL KXITFMATCH KXNHLGOAL KXTESTMATCH KXUCLWGAME "
          "KXATPCHALLENGERMATCH KXUFCMOV KXNFLTDPROT KXNBA3D KXINTLFRIENDLYGAME KXDPWORLDTOUR "
          "KXNFLTOTAL KXNFLSPREAD KXUEFANLGAME KXITFWMATCH KXNFLOT KXNASCARRACE KXNFLGAME "
          "KXPGAR2LEAD KXWTACHALLENGERMATCH KXNBAGAME KXBOXING KXLALIGAGAME KXNHLGAME").split()
TEMP = "KXTEMPNYCHS KXTEMPMIAH KXTEMPCHIHS KXTEMPLAXHS".split()

# group -> (series, longest lead recorded in hours, dearest cheap side recorded)
# Each band is wider than the cell it tests, so the scorer can see the edges of the cell.
GROUPS = {
    "sports": (SPORTS, 26.0, 0.12),
    "temp": (TEMP, 26.0, 0.40),
}
DEPTH_MAX = 80   # ladders per cycle, nearest to close first (4 batch calls of 20)

SCHEMA = """
CREATE TABLE IF NOT EXISTS snap (
    ticker TEXT NOT NULL,
    snapshot_ts INTEGER NOT NULL,       -- the cycle's id
    fetched_ts REAL,                    -- when THIS listing response arrived
    grp TEXT, series TEXT, event TEXT,
    close_ts INTEGER, exp_ts INTEGER,   -- raw close_time and expected_expiration_time
    lead_s INTEGER,                     -- min(close_ts, exp_ts) - snapshot_ts
    yes_bid REAL, yes_bid_size REAL,    -- NULL means nothing resting, never 0.00/1.00
    yes_ask REAL, yes_ask_size REAL,
    last_price REAL, volume REAL,
    PRIMARY KEY (ticker, snapshot_ts)
);
CREATE TABLE IF NOT EXISTS depth (
    ticker TEXT NOT NULL, snapshot_ts INTEGER NOT NULL, fetched_ts REAL,
    side TEXT NOT NULL,                 -- 'yes_bid' or 'no_bid', as the exchange reports them
    level INTEGER NOT NULL,             -- 0 = best bid of that side
    price REAL NOT NULL, qty REAL NOT NULL,
    PRIMARY KEY (ticker, snapshot_ts, side, level)
);
CREATE TABLE IF NOT EXISTS cycles (
    cycle_ts INTEGER NOT NULL, grp TEXT NOT NULL,
    series_ok INTEGER, series_failed INTEGER, listed INTEGER,
    with_expected INTEGER, recorded INTEGER, depth_markets INTEGER, depth_rows INTEGER,
    duration_s REAL,
    PRIMARY KEY (cycle_ts, grp)
);
"""


def open_db(path: str) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30)
    db.executescript(SCHEMA)
    return db


def cheap_side(top: dict):
    """Price of the cheaper side, from what is actually quoted, or None.

    YES costs its ask; NO costs 1 - the YES bid. An empty side is None (book_collector maps the
    0.00/1.00 sentinels to None), and None never counts as cheap.
    """
    c = []
    if top["yes_ask"] is not None:
        c.append(top["yes_ask"])
    if top["yes_bid"] is not None:
        c.append(round(1.0 - top["yes_bid"], 4))
    return min(c) if c else None


def lead_of(m: dict, now: int):
    """(close_ts, exp_ts, lead_s): lead to the EARLIER of the two, since a game closes when it ends."""
    close_ts = B.iso_to_ts(m.get("close_time"))
    exp_ts = B.iso_to_ts(m.get("expected_expiration_time"))
    ends = [t for t in (close_ts, exp_ts) if t is not None]
    lead = int(min(ends) - now) if ends else None
    return (int(close_ts) if close_ts else None, int(exp_ts) if exp_ts else None, lead)


def select(m: dict, now: int, max_lead_h: float, cheap_max: float):
    """The row to record for one listed market, or None when it is outside the test."""
    close_ts, exp_ts, lead = lead_of(m, now)
    if lead is None or lead < 0 or lead > max_lead_h * 3600:
        return None
    top = B.top_from_market(m)
    c = cheap_side(top)
    if c is None or c > cheap_max:
        return None
    return dict(close_ts=close_ts, exp_ts=exp_ts, lead=lead, top=top)


def cycle(db: sqlite3.Connection, groups=GROUPS, depth_max: int = DEPTH_MAX,
          lister=B.list_series, depther=B.fetch_depth, log=print) -> dict:
    """One pass over every group. Never raises: a failing series is skipped and counted."""
    now = int(time.time())
    totals = {}
    want = []
    for grp, (series, max_lead_h, cheap_max) in groups.items():
        t0 = time.time()
        ok = failed = listed = with_exp = 0
        rows = []
        for s in series:
            try:
                markets = lister(s)
                at = round(time.time(), 3)
                ok += 1
            except Exception as e:                              # noqa: BLE001
                failed += 1
                log(f"  ! {grp} {s}: {type(e).__name__}: {e}")
                continue
            for m in markets:
                listed += 1
                if m.get("expected_expiration_time"):
                    with_exp += 1
                r = select(m, now, max_lead_h, cheap_max)
                if r is None or not m.get("ticker"):
                    continue
                t = r["top"]
                rows.append((m["ticker"], now, at, grp, s, m.get("event_ticker"), r["close_ts"],
                             r["exp_ts"], r["lead"], t["yes_bid"], t["yes_bid_size"],
                             t["yes_ask"], t["yes_ask_size"], t["last_price"], t["volume"]))
                want.append((r["lead"], m["ticker"]))
        db.executemany("INSERT OR IGNORE INTO snap VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        totals[grp] = [ok, failed, listed, with_exp, len(rows), round(time.time() - t0, 2)]

    want.sort()
    tickers = [t for _, t in want[:depth_max]]
    drows = []
    if tickers:
        try:
            books = depther(tickers)
        except Exception as e:                                  # noqa: BLE001
            books = {}
            log(f"  ! depth: {type(e).__name__}: {e}")
        for t, (b, at_d) in books.items():
            ob = b.get("orderbook_fp") or {}
            for side, tag in (("yes_dollars", "yes_bid"), ("no_dollars", "no_bid")):
                lv = []
                for x in ob.get(side) or []:
                    if x and len(x) >= 2:
                        p, q = B.num(x[0]), B.num(x[1])
                        if p is not None and q is not None and q > 0:
                            lv.append((p, q))
                lv.sort(key=lambda z: -z[0])
                drows += [(t, now, round(at_d, 3), tag, i, p, q) for i, (p, q) in enumerate(lv)]
        db.executemany("INSERT OR IGNORE INTO depth VALUES (?,?,?,?,?,?,?)", drows)

    for grp, (ok, failed, listed, with_exp, rec, dur) in totals.items():
        db.execute("INSERT OR REPLACE INTO cycles VALUES (?,?,?,?,?,?,?,?,?,?)",
                   (now, grp, ok, failed, listed, with_exp, rec, len(tickers), len(drows), dur))
        log(f"  {grp}: series {ok}/{ok + failed}, listed {listed} "
            f"({with_exp} with expected_expiration_time), recorded {rec}, {dur:.1f}s")
    db.commit()
    log(f"  depth: {len(tickers)} markets, {len(drows)} levels")
    return totals


def check(db: sqlite3.Connection, groups=GROUPS, log=print) -> list[str]:
    """Read back the newest cycle and return every broken invariant (empty list = clean).

    Prints the source checks a scorer would otherwise have to trust blind: how many markets the
    earlier expected_expiration_time actually moved, how many sides were empty, and whether the
    ladder's best NO bid gives the same YES ask the listing reported (two separate calls a few
    seconds apart, so a book that moved in between disagrees honestly; only the rate is printed).
    """
    bad = []
    row = db.execute("SELECT MAX(snapshot_ts) FROM snap").fetchone()
    ts = row[0] if row else None
    if ts is None:
        return ["no snapshot recorded"]
    for grp, (_, max_lead_h, cheap_max) in groups.items():
        rs = db.execute("SELECT ticker, lead_s, close_ts, exp_ts, yes_bid, yes_ask FROM snap "
                        "WHERE snapshot_ts=? AND grp=?", (ts, grp)).fetchall()
        moved = sum(1 for r in rs if r[2] and r[3] and r[3] < r[2])
        empty = sum(1 for r in rs if r[4] is None or r[5] is None)
        log(f"  check {grp}: {len(rs)} markets, expected expiry earlier than close on {moved}, "
            f"a side empty on {empty}")
        for t, lead, _, _, b, a in rs:
            for v in (b, a):
                if v is not None and not 0.0 < v < 1.0:
                    bad.append(f"{t}: price {v} outside (0,1), a sentinel stored as a price")
            if lead is None or not 0 <= lead <= max_lead_h * 3600:
                bad.append(f"{t}: lead {lead}s outside the window")
            c = cheap_side({"yes_bid": b, "yes_ask": a})
            if c is None or c > cheap_max + 1e-9:
                bad.append(f"{t}: cheap side {c} outside the band")
    agree = tot = 0
    for t, nb, ya in db.execute(
            "SELECT d.ticker, d.price, s.yes_ask FROM depth d JOIN snap s "
            "ON s.ticker=d.ticker AND s.snapshot_ts=d.snapshot_ts "
            "WHERE d.snapshot_ts=? AND d.side='no_bid' AND d.level=0 AND s.yes_ask IS NOT NULL",
            (ts,)):
        tot += 1
        agree += abs((1.0 - nb) - ya) < 1e-6
    log(f"  check ladder: YES ask = 1 - best NO bid matches the listing on {agree} of {tot}")
    if tot >= 20 and agree < tot / 2:
        bad.append(f"ladder and listing disagree on {tot - agree} of {tot}: a derivation is wrong")
    return bad


def export(db: sqlite3.Connection, out_dir: str) -> None:
    """Every table to gzipped CSV. NULL stays EMPTY, never 0 and never 1.00."""
    import csv, gzip
    os.makedirs(out_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M", time.gmtime())
    for table in ("snap", "depth", "cycles"):
        cur = db.execute(f"SELECT * FROM {table}")
        path = os.path.join(out_dir, f"{table}-{stamp}.csv.gz")
        n = 0
        with gzip.open(path, "wt", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([d[0] for d in cur.description])
            for r in cur:
                w.writerow(["" if v is None else v for v in r])
                n += 1
        print(f"  {path}  {n} rows", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=DEFAULT_DB)
    ap.add_argument("--export", metavar="DIR", help="write every table to gzipped CSV and exit")
    ap.add_argument("--depth-max", type=int, default=DEPTH_MAX)
    ap.add_argument("--check", action="store_true",
                    help="read back the newest cycle, print the source checks, fail on a broken invariant")
    a = ap.parse_args()
    db = open_db(a.db)
    if a.check:
        bad = check(db)
        for b in bad[:20]:
            print(f"  ! {b}", flush=True)
        return 1 if bad else 0
    if a.export:
        export(db, a.export)
        return 0
    print(f"fill_recorder: one cycle -> {a.db}", flush=True)
    cycle(db, depth_max=a.depth_max, log=lambda s: print(s, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
