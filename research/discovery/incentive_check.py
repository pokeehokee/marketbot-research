"""Round-2 idea #44: is there a Kalshi incentive pool on any series we trade?

CLAUDE.md (2026-11-18) read both public listings and found only `liquidity`
pools, 1,000 contracts resting per side, and none on our series. This re-reads
them. Read-only and unauthenticated; its whitelist is these two GETs and nothing
else, so it cannot be widened into a write by accident.

Output: out/incentives-<tag>.txt with the raw shape of one record (so the
fields are read, not assumed), counts by type, and every record whose text
names a series on OUR list. Run: python3 -I incentive_check.py --tag inc-1
"""
import argparse, collections, gzip, json, os, re, time, urllib.parse, urllib.request, urllib.error

BASE = "https://api.elections.kalshi.com/trade-api/v2"
ALLOWED = ("/incentive_programs", "/incentive_schedules")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
# Series the account trades or that round 1/2 found worth a look.
# Exact series tickers (not prefixes: KXLOW must not match KXLOWFT).
OURS = {"KXBTCD", "KXBTC", "KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M", "KXETHD", "KXINXU", "KXNASDAQ100U",
        "KXBRENTD", "KXBRENTW", "KXWTI", "KXWTIW", "KXNATGASD", "KXNATGASW", "KXGOLDD", "KXGOLDW",
        "KXSILVERD", "KXSILVERW", "KXCOPPERD", "KXCOPPERW", "KXEPLGAME", "KXUCLGAME", "KXLALIGAGAME",
        "KXSERIEAGAME", "KXBUNDESLIGAGAME", "KXLIGUE1GAME", "KXMLSGAME", "KXNBAGAME", "KXNFLGAME",
        "KXNHLGAME", "KXNCAAFGAME", "KXNCAAMBGAME", "KXAAAGASD", "KXAAAGASW", "KXAAAGASM"}
PAGE_CAP = 400


def get(path, params):
    assert path in ALLOWED, path
    url = BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    for nap in (0, 2, 5, 15):
        time.sleep(nap)
        try:
            r = urllib.request.urlopen(urllib.request.Request(
                url, headers={"User-Agent": "research", "Accept": "application/json"}), timeout=60)
            return json.load(r), None
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                return None, f"HTTP {e.code}"
            err = f"HTTP {e.code}"
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
    return None, err


def listing(path, log):
    rows, cursor, key = [], None, None
    for _ in range(PAGE_CAP):
        q = {"limit": 1000}
        if cursor:
            q["cursor"] = cursor
        p, err = get(path, q)
        if err:
            log(f"{path}: {err} after {len(rows)} rows"); return rows, key
        if key is None:
            log(f"{path} top-level keys: {sorted(p)}")
            key = next((k for k, v in p.items() if isinstance(v, list)), None)
        rows += p.get(key) or []
        cursor = p.get("cursor") or p.get("next_cursor")
        if not cursor:
            return rows, key
    log(f"{path}: STOPPED AT THE {PAGE_CAP}-PAGE CAP with {len(rows)} rows; the listing is incomplete")
    return rows, key


def main(argv=None):
    ap = argparse.ArgumentParser(); ap.add_argument("--tag", required=True)
    a = ap.parse_args(argv)
    os.makedirs(OUT, exist_ok=True)
    f = open(os.path.join(OUT, f"incentives-{a.tag}.txt"), "w")

    def log(s):
        print(s); f.write(s + "\n"); f.flush()

    log(f"read {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}")
    for path in ALLOWED:
        rows, key = listing(path, log)
        log(f"\n{path}: {len(rows)} records under '{key}'")
        if rows:
            log("  one record verbatim: " + json.dumps(rows[0])[:1500])
        types = collections.Counter(str(r.get("incentive_type")) for r in rows)
        log(f"  incentive_type: {dict(types)}")
        for fld in ("target_size_fp", "target_size"):
            vals = collections.Counter(str(r.get(fld)) for r in rows if fld in r)
            if vals:
                log(f"  {fld}: {dict(vals.most_common(8))}")
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        live = [r for r in rows if str(r.get("end_date") or r.get("ends") or "") > now]
        log(f"  not yet ended at read time: {len(live)}")
        for r in rows:
            if r.get("incentive_type") not in ("liquidity", None):
                log("  NON-LIQUIDITY record verbatim: " + json.dumps(r)[:1500])
        sizes = collections.Counter((str(r.get("target_size_fp")), r.get("series_ticker")) for r in live)
        small = collections.Counter()
        for (t, s_), n in sizes.items():
            if t not in ("1000.00", "None"):
                small[f"{t} {s_}"] += n
        log(f"  live records with a target other than 1000, by series: {dict(small.most_common(30))}")
        bys = collections.Counter(r.get("series_ticker") for r in live)
        log(f"  live records by series (top 30): {dict(bys.most_common(30))}")
        hits = [r for r in rows if r.get("series_ticker") in OURS]
        hl = [r for r in live if r.get("series_ticker") in OURS]
        log(f"  records on one of our exact series: {len(hits)} ever, {len(hl)} live; by series "
            f"{dict(collections.Counter(r.get('series_ticker') for r in hits))}")
        for r in hl[:40]:
            log("    " + json.dumps(r)[:600])
        with gzip.open(os.path.join(OUT, f"incentives-{a.tag}{path.replace('/', '-')}.json.gz"), "wt") as g:
            json.dump(rows, g)

if __name__ == "__main__":
    main()
