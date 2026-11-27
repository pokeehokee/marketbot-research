"""Tests for fill_recorder.py. Stdlib unittest, no network: listings and ladders are faked."""
import csv
import datetime as dt
import gzip
import os
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fill_recorder as R  # noqa: E402

NOW = int(time.time())


def iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def market(ticker, yes_bid="0.9300", yes_ask="0.9500", close_in=6 * 3600, exp_in=None, **kw):
    m = {"ticker": ticker, "event_ticker": ticker.rsplit("-", 1)[0],
         "close_time": iso(NOW + close_in),
         "yes_bid_dollars": yes_bid, "yes_bid_size_fp": "120.00",
         "yes_ask_dollars": yes_ask, "yes_ask_size_fp": "40.00",
         "last_price_dollars": "0.9400", "volume_fp": "5000.00"}
    if exp_in is not None:
        m["expected_expiration_time"] = iso(NOW + exp_in)
    m.update(kw)
    return m


class TestCheapSide(unittest.TestCase):
    def test_no_is_one_minus_the_yes_bid(self):
        # Favourite on YES at 0.93: the longshot NO costs 0.07.
        self.assertAlmostEqual(R.cheap_side({"yes_bid": 0.93, "yes_ask": 0.95}), 0.07)
        self.assertAlmostEqual(R.cheap_side({"yes_bid": 0.80, "yes_ask": 0.99}), 0.20)

    def test_empty_sides_are_never_cheap(self):
        # book_collector nulls the 0.00 bid / 1.00 ask sentinels; a NULL must not read as 0c.
        self.assertIsNone(R.cheap_side({"yes_bid": None, "yes_ask": None}))
        self.assertAlmostEqual(R.cheap_side({"yes_bid": None, "yes_ask": 0.04}), 0.04)

    def test_sentinel_market_is_not_selected(self):
        m = market("KXNFLGAME-26NOV30X-A", yes_bid="0.0000", yes_ask="1.0000")
        self.assertIsNone(R.select(m, NOW, 26, 0.12))


class TestWindow(unittest.TestCase):
    def test_lead_uses_the_earlier_of_close_and_expected_expiration(self):
        # A game market's close_time can sit days after the game; the game ends first.
        m = market("KXNBAGAME-X-A", close_in=14 * 86400, exp_in=5 * 3600)
        r = R.select(m, NOW, 26, 0.12)
        self.assertIsNotNone(r)
        self.assertEqual(r["lead"], 5 * 3600)
        self.assertEqual(r["close_ts"], NOW + 14 * 86400)

    def test_far_and_past_markets_are_skipped(self):
        self.assertIsNone(R.select(market("A-1", close_in=40 * 3600), NOW, 26, 0.12))
        self.assertIsNone(R.select(market("A-2", close_in=-60), NOW, 26, 0.12))

    def test_no_close_time_at_all_is_skipped_not_guessed(self):
        m = market("A-3")
        m.pop("close_time")
        self.assertIsNone(R.select(m, NOW, 26, 0.12))

    def test_band_edge(self):
        self.assertIsNotNone(R.select(market("A-4", yes_bid="0.8800", yes_ask="0.9900"), NOW, 26, 0.12))
        self.assertIsNone(R.select(market("A-5", yes_bid="0.8700", yes_ask="0.9900"), NOW, 26, 0.12))


class TestCycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = R.open_db(os.path.join(self.tmp.name, "t.db"))
        self.groups = {"sports": (["KXNFLGAME", "KXBROKEN"], 26.0, 0.12),
                       "temp": (["KXTEMPNYCHS"], 26.0, 0.40)}
        self.listings = {
            "KXNFLGAME": [market("KXNFLGAME-1-A"),                                   # in
                          market("KXNFLGAME-1-B", yes_bid="0.5000", yes_ask="0.5200"),  # dear: out
                          market("KXNFLGAME-2-A", yes_bid="0.0000", yes_ask="0.0300")],  # NO side empty
            "KXTEMPNYCHS": [market("KXTEMPNYCHS-1-T50", yes_bid="0.7000", yes_ask="0.7500",
                                   close_in=1800)],
        }

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def lister(self, s):
        if s == "KXBROKEN":
            raise RuntimeError("HTTP 500")
        return self.listings.get(s, [])

    def depther(self, tickers):
        self.asked = list(tickers)
        return {t: ({"ticker": t, "orderbook_fp": {
            "yes_dollars": [["0.0300", "10.00"], ["0.0500", "7.00"], ["0.0400", "0.00"]],
            "no_dollars": []}}, time.time()) for t in tickers}

    def run_cycle(self, **kw):
        return R.cycle(self.db, self.groups, lister=self.lister, depther=self.depther,
                       log=lambda s: None, **kw)

    def test_records_only_the_band_and_counts_a_failed_series(self):
        tot = self.run_cycle()
        rows = {r[0] for r in self.db.execute("SELECT ticker FROM snap")}
        self.assertEqual(rows, {"KXNFLGAME-1-A", "KXNFLGAME-2-A", "KXTEMPNYCHS-1-T50"})
        self.assertEqual(tot["sports"][:2], [1, 1])        # one series ok, one failed, no raise
        c = self.db.execute("SELECT series_failed FROM cycles WHERE grp='sports'").fetchone()
        self.assertEqual(c[0], 1)

    def test_empty_side_is_stored_as_null(self):
        self.run_cycle()
        r = self.db.execute("SELECT yes_bid, yes_ask FROM snap WHERE ticker='KXNFLGAME-2-A'").fetchone()
        self.assertIsNone(r[0])
        self.assertAlmostEqual(r[1], 0.03)

    def test_depth_nearest_close_first_best_level_zero_zero_qty_dropped(self):
        self.run_cycle(depth_max=1)
        self.assertEqual(self.asked, ["KXTEMPNYCHS-1-T50"])   # 30 min beats 6 h
        lv = self.db.execute("SELECT level, price, qty FROM depth ORDER BY level").fetchall()
        self.assertEqual(lv, [(0, 0.05, 7.0), (1, 0.03, 10.0)])

    def test_a_failing_depth_call_does_not_lose_the_snapshot(self):
        def boom(_):
            raise RuntimeError("HTTP 429")
        R.cycle(self.db, self.groups, lister=self.lister, depther=boom, log=lambda s: None)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM snap").fetchone()[0], 3)

    def test_rerun_of_same_cycle_is_idempotent(self):
        self.run_cycle()
        n = self.db.execute("SELECT COUNT(*) FROM snap").fetchone()[0]
        self.run_cycle()   # same second or a new one: never a duplicate key
        self.assertGreaterEqual(self.db.execute("SELECT COUNT(*) FROM snap").fetchone()[0], n)

    def test_check_is_clean_on_a_good_cycle_and_catches_a_stored_sentinel(self):
        self.run_cycle()
        self.assertEqual(R.check(self.db, self.groups, log=lambda s: None), [])
        # Plant the bug the check exists for: an empty bid written as the 0.00 sentinel.
        self.db.execute("UPDATE snap SET yes_bid=0.0 WHERE ticker='KXNFLGAME-2-A'")
        bad = R.check(self.db, self.groups, log=lambda s: None)
        self.assertTrue(any("sentinel" in b for b in bad), bad)

    def test_check_refuses_an_empty_database(self):
        self.assertEqual(R.check(self.db, self.groups, log=lambda s: None), ["no snapshot recorded"])

    def test_export_keeps_null_empty(self):
        self.run_cycle()
        out = os.path.join(self.tmp.name, "out")
        R.export(self.db, out)
        snap = [f for f in os.listdir(out) if f.startswith("snap-")][0]
        with gzip.open(os.path.join(out, snap), "rt") as fh:
            rows = list(csv.DictReader(fh))
        r = [x for x in rows if x["ticker"] == "KXNFLGAME-2-A"][0]
        self.assertEqual(r["yes_bid"], "")      # not "0", not "0.0", not "1.0"


class TestReadOnly(unittest.TestCase):
    def test_writes_are_refused_before_any_network(self):
        for path in ("/portfolio/orders", "/portfolio/orders/abc/amend", "/portfolio/balance"):
            with self.assertRaises(Exception):
                R.B.assert_read_only(path)

    def test_the_reads_it_needs_are_allowed(self):
        R.B.assert_read_only("/markets?series_ticker=KXNFLGAME&status=open&limit=200")
        R.B.assert_read_only("/markets/orderbooks?tickers=A&tickers=B")

    def test_no_signer_or_key_is_imported(self):
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fill_recorder.py")) as fh:
            src = fh.read()
        for word in ("KALSHI-ACCESS", "private_key", "kalshi_auth", "api_key"):
            self.assertNotIn(word, src)


if __name__ == "__main__":
    unittest.main()
