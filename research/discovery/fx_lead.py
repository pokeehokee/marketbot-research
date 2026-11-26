"""Idea #67: does free public FX spot lead Kalshi's 15-minute FX markets?

KXEURUSD15M / KXUSDJPY15M: "If the value of the EUR/USD exchange rate is at least the value
... published by Pyth Network at 11:45 PM EST ... At 12:00 AM EST ..., then Yes." So each
market is: close of the 15-minute window >= open of the window, both on Pyth. Pyth's own
history now needs an API key (Pyth Core upgrade, 2026-08-26), so this uses Dukascopy's free
public tick archive as a proxy for the same rate -- no account, no key. The control checks
how often the proxy reproduces Kalshi's own result; a proxy that disagrees with the
settlement source is reported, never silently used.

For every print inside a settled window this records, with no lookahead:
  open_mid   proxy mid at the window open (last tick at or before T0)
  spot_mid   proxy mid known at the print (last tick at or before print - LAG_S)
  rv         realised vol of 1-minute log mids over the 30 minutes before the print
  close_mid  proxy mid at the window close (for the control only; never a model input)

Dukascopy hour files: https://datafeed.dukascopy.com/datafeed/<PAIR>/<YYYY>/<MM-1>/<DD>/<HH>h_ticks.bi5,
LZMA, 20-byte big-endian records (ms offset, ask, bid, ask vol, bid vol), prices in points.

Read-only; Kalshi via taker_flow_screen.get (whitelisted, unauthenticated). Outputs, out/:
  fx-<tag>.log, fx-<tag>-trades.csv.gz
"""
import argparse, bisect, csv, gzip, json, lzma, math, os, struct, sys, time, urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taker_flow_screen as T  # noqa: E402
import series_probe as P  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
DUKA = "https://datafeed.dukascopy.com/datafeed"
PAIRS = {"KXEURUSD15M": ("EURUSD", 1e5), "KXUSDJPY15M": ("USDJPY", 1e3)}
LAG_S = 1.0          # a tick is usable one second after its timestamp
WINDOW_S = 900
RV_S = 1800


def duka_url(pair, hour_ts):
    d = datetime.fromtimestamp(hour_ts, timezone.utc)
    return f"{DUKA}/{pair}/{d.year:04d}/{d.month - 1:02d}/{d.day:02d}/{d.hour:02d}h_ticks.bi5"


def parse_bi5(raw, hour_ts, point):
    """[(ts_seconds, mid)] from one decompressed-or-not hour file. Empty file -> []."""
    if not raw:
        return []
    data = lzma.decompress(raw)
    out = []
    for i in range(0, len(data) - len(data) % 20, 20):
        ms, ask, bid, _, _ = struct.unpack(">IIIff", data[i:i + 20])
        out.append((hour_ts + ms / 1000.0, (ask + bid) / 2.0 / point))
    return out


class Ticks:
    def __init__(self, pair, point, log):
        self.pair, self.point, self.log = pair, point, log
        self.hours = {}
        self.fails = 0

    def load_hour(self, h):
        if h in self.hours:
            return
        raw = None
        for nap in (0, 2, 6, 15):
            if nap:
                time.sleep(nap)
            try:
                raw = urllib.request.urlopen(urllib.request.Request(
                    duka_url(self.pair, h), headers={"User-Agent": "research"}), timeout=60).read()
                break
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raw = b""
                    break
            except Exception:
                pass
        if raw is None:
            self.fails += 1
            if self.fails <= 5:
                self.log(f"  dukascopy {self.pair} {h}: unreadable")
            self.hours[h] = []
            return
        self.hours[h] = parse_bi5(raw, h, self.point)

    def ensure(self, start, end):
        h = int(start // 3600 * 3600)
        while h <= end:
            if h not in self.hours:
                self.load_hour(h)
                pts = self.hours[h]
                self.hours[h] = ([q[0] for q in pts], [q[1] for q in pts])
            h += 3600

    def at(self, ts, max_age=300):
        """Mid of the last tick at or before ts, if no older than max_age seconds."""
        h = int(ts // 3600 * 3600)
        for hh in (h, h - 3600):
            v = self.hours.get(hh)
            if not v or not v[0]:
                continue
            i = bisect.bisect_right(v[0], ts) - 1
            if i >= 0:
                return v[1][i] if ts - v[0][i] <= max_age else None
        return None

    def rv(self, ts):
        """Std of 1-minute log changes over the RV_S seconds before ts (known at ts)."""
        mids = [self.at(ts - k * 60, max_age=120) for k in range(RV_S // 60, -1, -1)]
        rets = [math.log(b / a) for a, b in zip(mids, mids[1:]) if a and b]
        if len(rets) < 10:
            return None
        m = sum(rets) / len(rets)
        return math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--series", default="KXEURUSD15M,KXUSDJPY15M")
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--max-markets", type=int, default=3000)
    a = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    logf = open(os.path.join(OUT, f"fx-{a.tag}.log"), "w")

    def log(s):
        print(s); logf.write(s + "\n"); logf.flush()

    p, err = T.get("/historical/cutoff")
    cutoff_ts = T.utc(p.get("trades_created_ts")) if p and isinstance(p.get("trades_created_ts"), str) else None
    since = time.time() - a.days * 86400
    tf = gzip.open(os.path.join(OUT, f"fx-{a.tag}-trades.csv.gz"), "wt", newline="")
    tw = csv.writer(tf)
    tw.writerow(("series", "ticker", "t0", "t1", "result", "open_mid", "close_mid", "trade_ts",
                 "taker_side", "taker_paid", "count", "spot_mid", "rv"))
    for s in [x for x in a.series.split(",") if x]:
        pair, point = PAIRS[s]
        log(f"\n== {s} ({pair})")
        ticks = Ticks(pair, point, log)
        ms = [m for m in P.settled_markets(s, log) if (T.utc(m.get("close_time")) or 0) >= since]
        ms.sort(key=lambda m: T.utc(m.get("close_time")))
        ms = ms[-a.max_markets:]
        log(f"  {s}: {len(ms)} settled markets closing in the last {a.days} days")
        if ms:
            m0 = ms[-1]
            log(f"  sample {m0.get('ticker')}: open {m0.get('open_time')} close {m0.get('close_time')} "
                f"strike_type {m0.get('strike_type')} floor {m0.get('floor_strike')} "
                f"expiration_value {m0.get('expiration_value')}")
        agree = disagree = nodata = big_agree = big_n = ntr = 0
        flat = 0
        for m in ms:
            t1 = T.utc(m.get("close_time"))
            t0 = t1 - WINDOW_S
            ticks.ensure(t0 - RV_S - 600, t1 + 60)
            o, c = ticks.at(t0), ticks.at(t1)
            if o is None or c is None:
                nodata += 1
                continue
            imp = "yes" if round(c, 6) >= round(o, 6) else "no"
            if abs(c - o) < 1e-9:
                flat += 1
            if imp == m.get("result"):
                agree += 1
            else:
                disagree += 1
            pip = 1e-4 if pair == "EURUSD" else 1e-2
            if abs(c - o) >= pip:
                big_n += 1; big_agree += int(imp == m.get("result"))
            for t in P.trades_for(m["ticker"], cutoff_ts, t1, log):
                f = T.trade_fields(t)
                if f is None or not (t0 <= f[1] < t1):
                    continue
                spot = ticks.at(f[1] - LAG_S)
                rv = ticks.rv(f[1] - LAG_S)
                tw.writerow((s, m["ticker"], f"{t0:.0f}", f"{t1:.0f}", m.get("result"),
                             f"{o:.6f}", f"{c:.6f}", f"{f[1]:.0f}", f[2], f"{f[3]:.4f}", f"{f[4]:g}",
                             f"{spot:.6f}" if spot else "", f"{rv:.8f}" if rv else ""))
                ntr += 1
        log(f"  CONTROL {s}: proxy result agrees with Kalshi on {agree}, disagrees on {disagree}, "
            f"no proxy data {nodata}; exactly flat {flat}; when the move is >= 1 pip it agrees on "
            f"{big_agree} of {big_n}")
        log(f"  dukascopy hours loaded {len(ticks.hours)}, unreadable {ticks.fails}, "
            f"ticks {sum(len(v[0]) for v in ticks.hours.values())}")
        log(f"  {s}: {ntr} prints written inside their window")
    tf.close()


if __name__ == "__main__":
    main()
