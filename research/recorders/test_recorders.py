import csv, gzip, os, sys, tempfile, unittest
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
