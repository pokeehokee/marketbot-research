import csv, gzip, os, sys, tempfile, unittest
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import record as R, compact as C


class TestPrices(unittest.TestCase):
    def test_missing_is_empty_never_zero(self):
        self.assertEqual([R.px(x) for x in (None, "", "0", "0.00", "abc", "1.2")], [""] * 6)
        self.assertEqual(R.px("0.94"), 0.94)
        self.assertEqual(R.px("1.00"), 1.0)

    def test_gemini_flatten(self):
        ev = [{"ticker": "BTC2611272200", "category": "Crypto", "type": "categorical",
               "expiryDate": "2026-11-27T22:00:00.000Z", "volume": "10",
               "sourceDetails": {"agency": "lukka", "index": "LS-LRRR-XBTUSD"},
               "contracts": [{"instrumentSymbol": "GEMI-X-HI59000", "strike": {"type": "above", "value": "59000"},
                              "prices": {"buy": {"yes": "0.99", "no": "0.02"}, "sell": {"yes": "0.98"},
                                         "bestBid": "0.98", "bestAsk": "0.99"}}]}]
        r = R.gemini_rows(ev, "t")[0]
        self.assertEqual((r["bid"], r["ask"], r["strike"], r["sell_no"], r["last"]), (0.98, 0.99, 59000.0, "", ""))

    def test_predictit_flatten(self):
        doc = {"markets": [{"id": 1, "shortName": "m", "timeStamp": "x",
                            "contracts": [{"id": 2, "shortName": "c", "bestBuyYesCost": 0.4,
                                           "bestBuyNoCost": None, "lastTradePrice": 0.41}]}]}
        r = R.pi_rows(doc, "t")[0]
        self.assertEqual((r["buy_yes"], r["buy_no"], r["last"]), (0.4, "", 0.41))


class TestPaging(unittest.TestCase):
    def test_ignored_offset_stops_instead_of_looping(self):
        calls = []
        def f(off):
            calls.append(off)
            return [{"id": 1}, {"id": 2}]            # same page whatever the offset
        rows = R.paged(f, lambda m: m["id"], 2, 50)
        self.assertEqual((len(rows), len(calls)), (2, 2))

    def test_short_page_ends(self):
        pages = {0: [{"id": 1}, {"id": 2}], 2: [{"id": 3}]}
        self.assertEqual(len(R.paged(lambda o: pages.get(o, []), lambda m: m["id"], 2, 50)), 3)


    def test_an_error_past_page_one_keeps_the_rows(self):
        def f(off):
            if off >= 4:
                raise RuntimeError("HTTP 422")
            return [{"id": off}, {"id": off + 1}]
        self.assertEqual(len(R.paged(f, lambda m: m["id"], 2, 50)), 4)

    def test_an_error_on_page_one_is_a_failure(self):
        def f(off):
            raise RuntimeError("HTTP 500")
        with self.assertRaises(RuntimeError):
            R.paged(f, lambda m: m["id"], 2, 50)


class TestReadOnly(unittest.TestCase):
    def test_writes_are_refused(self):
        with self.assertRaises(RuntimeError):
            R.P.get("https://api.gemini.com/v1/prediction-markets/order")
        with self.assertRaises(RuntimeError):
            R.P.get("https://clob.polymarket.com/order")


class TestGeminiTrades(unittest.TestCase):
    def ev(self, ticker, cat, exp, states=("open",)):
        return {"ticker": ticker, "category": cat, "expiryDate": exp,
                "contracts": [{"instrumentSymbol": f"GEMI-{ticker}-{i}", "marketState": s}
                              for i, s in enumerate(states)]}

    def test_targets_soonest_crypto_open_only(self):
        now = datetime(2026, 11, 26, 20, 0, tzinfo=timezone.utc).timestamp()
        evs = [self.ev("LATE", "Crypto", "2026-11-26T22:30:00Z"),
               self.ev("SOON", "Crypto", "2026-11-26T20:30:00Z", ("open", "closed")),
               self.ev("FAR", "Crypto", "2026-11-27T22:00:00Z"),          # beyond 3h
               self.ev("PAST", "Crypto", "2026-11-26T19:00:00Z"),         # already expired
               self.ev("FED", "Economics", "2026-11-26T20:10:00Z")]       # not crypto
        t = R.trade_targets(evs, now)
        self.assertEqual([x[1] for x in t], ["GEMI-SOON-0", "GEMI-LATE-0"])
        self.assertEqual(len(R.trade_targets(evs, now, cap=1)), 1)

    def test_trade_rows_keep_missing_price_empty(self):
        rows = R.gemini_trade_rows([("S", "E", "X", {"tid": 1, "timestampms": 5, "price": "0",
                                                      "amount": "3", "type": "buy"})], "ts")
        self.assertEqual(rows[0]["price"], "")
        self.assertEqual(rows[0]["taker"], "buy")

    def test_trades_path_is_whitelisted_and_writes_are_not(self):
        import urllib.parse
        with self.assertRaises(RuntimeError):
            R.P.get("https://api.gemini.com/v1/order/new")
        u = urllib.parse.urlsplit("https://api.gemini.com/v1/trades/GEMI-X")
        self.assertTrue(any(u.path.startswith(p + "/") for p in R.P.ALLOWED["api.gemini.com"]))


class TestBooks(unittest.TestCase):
    def test_kalshi_depth_best_first_and_empty_side(self):
        b = {"orderbook_fp": {"yes_dollars": [["0.9400", "10.00"], ["0.9600", "5.00"], ["0.9500", "0"]],
                              "no_dollars": []}}
        rows = R.kalshi_depth_rows([("T", 1.0, b)], "ts")
        self.assertEqual([(r["level"], r["price"], r["qty"]) for r in rows],
                         [(0, 0.96, 5.0), (1, 0.94, 10.0)])        # zero-size level dropped, no NO rows

    def test_kalshi_deep_band(self):
        self.assertTrue(R.kx_deep({"yes_bid_dollars": "0.9300", "yes_ask_dollars": "0.9500"}))
        self.assertTrue(R.kx_deep({"yes_bid_dollars": "0.0300", "yes_ask_dollars": "0.0500"}))
        self.assertFalse(R.kx_deep({"yes_bid_dollars": "0.5000", "yes_ask_dollars": "0.5200"}))
        self.assertFalse(R.kx_deep({"yes_bid_dollars": "0.0000", "yes_ask_dollars": "1.0000"}))  # empty

    def test_kalshi_batch_pairs_by_named_ticker(self):
        calls = []
        def fake(url, params=None, raw=False):
            calls.append(params)
            return {"orderbooks": [{"ticker": "A,B", "orderbook_fp": {}}, {"ticker": "B"}]}
        old, R.P.get = R.P.get, fake
        try:
            got = R.kx_books(["A", "B"])
        finally:
            R.P.get = old
        self.assertEqual(set(got), {"B"})                    # "A,B" is never a market
        self.assertEqual(calls[0]["tickers"], ["A", "B"])    # a list: urlencode repeats it

    def test_kalshi_orders_refused(self):
        with self.assertRaises(RuntimeError):
            R.P.get("https://api.elections.kalshi.com/trade-api/v2/portfolio/orders")

    def test_gemini_book_levels(self):
        b = {"bids": [{"price": "0.62", "amount": "5"}, {"price": "0.63", "amount": "1"}],
             "asks": [{"price": "0.66", "amount": "2"}, {"price": "0.65", "amount": "3"}]}
        rows = R.gemini_book_rows([("S", "E", "X", 1.0, b)], "ts")
        self.assertEqual([(r["side"], r["level"], r["price"]) for r in rows],
                         [("bid", 0, 0.63), ("bid", 1, 0.62), ("ask", 0, 0.65), ("ask", 1, 0.66)])

    def test_pmus_targets_window(self):
        now = datetime(2026, 11, 26, 0, 0, tzinfo=timezone.utc).timestamp()
        ms = [{"slug": "a", "endDate": "2026-11-26T05:00:00Z"}, {"slug": "b", "endDate": "2026-11-28T00:00:00Z"},
              {"slug": "c", "endDate": "2026-11-26T01:00:00Z", "closed": True}, {"slug": "d", "endDate": "x"}]
        self.assertEqual(R.pmus_targets(ms, now), ["a"])


class TestCompact(unittest.TestCase):
    def snap(self, root, name, rows, header=("ts", "a")):
        d = os.path.join(root, name); os.makedirs(d)
        with gzip.open(os.path.join(d, "gemini.csv.gz"), "wt", newline="") as fh:
            w = csv.writer(fh); w.writerow(header); w.writerows(rows)

    def test_merge_one_header_in_order_and_day_only(self):
        with tempfile.TemporaryDirectory() as t:
            src, out = os.path.join(t, "s"), os.path.join(t, "o")
            self.snap(src, "snap-20261126-0010-2", [["t2", "2"]])
            self.snap(src, "snap-20261126-0000-1", [["t1", "1"]])
            self.snap(src, "snap-20261127-0000-3", [["t3", "3"]])          # another day
            os.makedirs(os.path.join(src, "snap-20261126-0020-4"))         # venue failed
            snaps, rep = C.merge(src, "20261126", out)
            with gzip.open(os.path.join(out, "gemini-20261126.csv.gz"), "rt") as fh:
                self.assertEqual(fh.read().split(), ["ts,a", "t1,1", "t2,2"])
            self.assertEqual(rep["gemini"], (2, 2, 1))

    def test_header_change_fails(self):
        with tempfile.TemporaryDirectory() as t:
            src = os.path.join(t, "s")
            self.snap(src, "snap-20261126-0000-1", [["t1", "1"]])
            self.snap(src, "snap-20261126-0010-2", [["t2", "2"]], header=("ts", "b"))
            with self.assertRaises(ValueError):
                C.merge(src, "20261126", os.path.join(t, "o"))


if __name__ == "__main__":
    unittest.main()
