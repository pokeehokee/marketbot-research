"""Forward recorder for fresh Solana DEX pools, to test the "Grok Bot" overnight meme-coin
prompt (and variants of it) on books that really existed.

Why forward: no free source lists historical new pools with their liquidity, 5-minute volume,
buy/sell counts and holder counts AS THEY STOOD at the time, and every rule in that prompt is a
decision-time filter. A backtest built from today's survivors would be pure survivorship bias
(the rugged coins vanish from listings), so the rules can only be scored on rows written down
at the moment the bot would have decided.

What it records, all from GeckoTerminal's public v2 API, GET only, no key:
  - pool rows from new_pools and trending_pools (5m and 1h), every cycle;
  - for every pool younger than TRACK_H hours with reserve >= MIN_RESERVE, a snapshot every
    REFRESH_S via pools/multi: reserve, price, fdv, volume m5/h1, buys/sells/buyers/sellers m5,
    price change m5/h1, locked liquidity %;
  - token info (holders, top-10 share, mint and freeze authority) for pools that pass the
    prompt's universe, at most every TOKEN_S per token, so holder GROWTH is measurable.
The raw attribute JSON is kept beside the parsed columns, so a field that turns out to be
misnamed can be re-read later instead of being lost as NULL.

Rate: GeckoTerminal's free tier is about 30 calls a minute; this stays under BUDGET a minute.

Run:  python3 -I meme_rec.py --state state.json --out dir --minutes 11
"""
import argparse, gzip, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone

BASE = "https://api.geckoterminal.com/api/v2"
NET = "solana"
ALLOWED = ("/networks/solana/new_pools", "/networks/solana/trending_pools",
           "/networks/solana/pools/multi/", "/networks/solana/tokens/")
TRACK_H = 48          # long enough for the "buy after the rug window" variants
MIN_RESERVE = 10_000  # below the prompt's 40k, so the filters' effect on rug rate is measurable
REFRESH_S = 180
TOKEN_S = 600
BUDGET = 26           # calls per rolling minute
UA = "marketbot-research/1.0 (public research)"


def assert_read_only(path):
    if not any(path.startswith(p) for p in ALLOWED):
        raise RuntimeError(f"path not on the read-only whitelist: {path}")


class Client:
    def __init__(self, log=print):
        self.calls = []
        self.log = log
        self.n = 0
        self.errors = 0

    def get(self, path, params=None):
        assert_read_only(path)
        while True:
            now = time.time()
            self.calls = [t for t in self.calls if now - t < 60]
            if len(self.calls) < BUDGET:
                break
            time.sleep(60 - (now - self.calls[0]) + 0.2)
        q = ("?" + "&".join(f"{k}={v}" for k, v in params.items())) if params else ""
        for attempt in range(4):
            self.calls.append(time.time())
            self.n += 1
            req = urllib.request.Request(BASE + path + q, headers={
                "accept": "application/json;version=20230302", "user-agent": UA})
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    time.sleep(15 * (attempt + 1))
                    continue
                if e.code == 404:
                    return None
                self.errors += 1
                self.log(f"http {e.code} {path}")
                return None
            except Exception as e:  # network: back off and retry
                self.errors += 1
                self.log(f"error {type(e).__name__} {path}")
                time.sleep(5 * (attempt + 1))
        return None


def ts_of(s):
    if not s:
        return None
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def f(x):
    try:
        return None if x is None or x == "" else float(x)
    except (TypeError, ValueError):
        return None


def rel_id(d, name):
    try:
        i = d["relationships"][name]["data"]["id"]
        return i.split("_", 1)[1] if "_" in i else i
    except (KeyError, TypeError):
        return None


def pool_row(d, now):
    """Parse one pool object. Missing fields stay None, never zero."""
    a = d.get("attributes") or {}
    tx = (a.get("transactions") or {}).get("m5") or {}
    vol = a.get("volume_usd") or {}
    chg = a.get("price_change_percentage") or {}
    return {
        "ts": now, "addr": a.get("address"), "name": a.get("name"),
        "token": rel_id(d, "base_token"), "dex": rel_id(d, "dex"),
        "created": ts_of(a.get("pool_created_at")),
        "reserve": f(a.get("reserve_in_usd")), "price": f(a.get("base_token_price_usd")),
        "fdv": f(a.get("fdv_usd")), "mcap": f(a.get("market_cap_usd")),
        "vol_m5": f(vol.get("m5")), "vol_h1": f(vol.get("h1")), "vol_h6": f(vol.get("h6")),
        "buys_m5": tx.get("buys"), "sells_m5": tx.get("sells"),
        "buyers_m5": tx.get("buyers"), "sellers_m5": tx.get("sellers"),
        "chg_m5": f(chg.get("m5")), "chg_h1": f(chg.get("h1")), "chg_h6": f(chg.get("h6")),
        "locked": f(a.get("locked_liquidity_percentage")),
    }


def token_row(d, now, token):
    a = (d or {}).get("data", {}).get("attributes") or {}
    h = a.get("holders") or {}
    dist = h.get("distribution_percentage") or {}
    return {"ts": now, "token": token, "holders": h.get("count"),
            "top10": f(dist.get("top_10")), "mint_auth": a.get("mint_authority"),
            "freeze_auth": a.get("freeze_authority"), "gt_score": f(a.get("gt_score")),
            "raw": {k: a.get(k) for k in a if k not in ("description", "image_url",
                                                         "websites", "image")}}


def universe(p, now):
    """The prompt's universe: launched or migrated in the last 6h, liquidity >= $40k,
    5-minute volume >= $25k. (Holders >= 300 needs token info, so it is checked later.)"""
    return (p["created"] is not None and now - p["created"] <= 6 * 3600
            and (p["reserve"] or 0) >= 40_000 and (p["vol_m5"] or 0) >= 25_000)


def run(a, log=print):
    c = Client(log)
    state = {"pools": {}, "tok": {}}
    if a.state and os.path.exists(a.state):
        with open(a.state) as fo:
            state = json.load(fo)
    os.makedirs(a.out, exist_ok=True)
    pf = gzip.open(os.path.join(a.out, "pools.jsonl.gz"), "at")
    tf = gzip.open(os.path.join(a.out, "tokens.jsonl.gz"), "at")
    end = time.time() + a.minutes * 60
    cyc = 0
    sampled = False
    while time.time() < end:
        now = int(time.time())
        cyc += 1
        seen = []
        for path, params in ([("/networks/solana/new_pools", {"page": p, "include": "base_token,dex"})
                              for p in (1, 2, 3, 4, 5)]
                             + [("/networks/solana/trending_pools",
                                 {"page": p, "duration": dur, "include": "base_token,dex"})
                                for dur in ("5m", "1h") for p in (1, 2)]):
            r = c.get(path, params)
            for d in (r or {}).get("data") or []:
                seen.append(pool_row(d, now))
        if seen and not sampled:
            sampled = True
            s = seen[0]
            log("field check: " + json.dumps({k: s[k] for k in ("addr", "dex", "created", "reserve",
                "vol_m5", "buys_m5", "sells_m5", "locked")}))
        fresh = {}
        for p in seen:
            if p["addr"]:
                fresh[p["addr"]] = p
                if (p["created"] and now - p["created"] <= TRACK_H * 3600
                        and (p["reserve"] or 0) >= MIN_RESERVE):
                    state["pools"].setdefault(p["addr"], {"created": p["created"], "token": p["token"],
                                                          "last": 0})
        # drop pools past the tracking window
        state["pools"] = {k: v for k, v in state["pools"].items()
                          if now - (v.get("created") or now) <= TRACK_H * 3600}
        due = [k for k, v in state["pools"].items()
               if now - v["last"] >= REFRESH_S and k not in fresh]
        for i in range(0, len(due), 30):
            if time.time() > end:
                break
            r = c.get("/networks/solana/pools/multi/" + ",".join(due[i:i + 30]))
            for d in (r or {}).get("data") or []:
                p = pool_row(d, int(time.time()))
                if p["addr"]:
                    fresh[p["addr"]] = p
        for addr, p in fresh.items():
            if addr in state["pools"]:
                state["pools"][addr]["last"] = p["ts"]
                pf.write(json.dumps(p) + "\n")
            elif universe(p, now):
                pf.write(json.dumps(p) + "\n")
        for addr, p in fresh.items():
            tok = p["token"]
            if not tok or not universe(p, now) or time.time() > end:
                continue
            if now - state["tok"].get(tok, 0) < TOKEN_S:
                continue
            r = c.get(f"/networks/solana/tokens/{tok}/info")
            if r is not None:
                tf.write(json.dumps(token_row(r, int(time.time()), tok)) + "\n")
                state["tok"][tok] = now
        state["tok"] = {k: v for k, v in state["tok"].items() if now - v < TRACK_H * 3600}
        log(f"cycle {cyc}: seen {len(seen)} tracked {len(state['pools'])} snap {len(fresh)} "
            f"calls {c.n} errors {c.errors}")
        nxt = now + 60
        if time.time() < nxt and time.time() + 1 < end:
            time.sleep(max(0, min(nxt, end) - time.time()))
    pf.close()
    tf.close()
    if a.state:
        with open(a.state, "w") as fo:
            json.dump(state, fo)
    log(f"done: {cyc} cycles, {c.n} calls, {c.errors} errors, tracked {len(state['pools'])}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--state")
    ap.add_argument("--out", required=True)
    ap.add_argument("--minutes", type=float, default=11)
    run(ap.parse_args())
