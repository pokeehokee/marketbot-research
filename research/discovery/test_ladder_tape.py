"""Freezes ladder_tape.py's load-bearing rules: the side the taker acquired and
what they paid, that a failed read is never "no trades", that a market is read
whole or not at all, early-close detection, and that the scorer's pass gate can
reject a cell (a control that cannot fail is not a control)."""
import gzip, os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ladder_tape as L  # noqa: E402
import score_ladder as SL  # noqa: E402
import taker_flow_screen as T  # noqa: E402

CLOSE = T.utc("2026-11-20T02:00:00Z")


def tmpdb():
    fd, p = tempfile.mkstemp(suffix=".db"); os.close(fd)
    return p


class Aggregate(unittest.TestCase):
    def test_no_taker_at_six_cents_is_a_maker_holding_yes_at_94(self):
        p = tmpdb(); c = L.db(p)
        c.execute("INSERT INTO market(ticker, event, series, close_ts, result) VALUES(?,?,?,?,?)",
                  ("KXBTC15M-X", "KXBTC15M-X", "KXBTC15M", CLOSE, "yes"))
        trades = [
            {"trade_id": "a", "ticker": "KXBTC15M-X", "created_time": "2026-11-20T01:57:30Z",
             "yes_price_dollars": "0.9400", "count_fp": "10", "taker_outcome_side": "no"},
            # the same trade again from the other tier: counted once
            {"trade_id": "a", "ticker": "KXBTC15M-X", "created_time": "2026-11-20T01:57:30Z",
             "yes_price_dollars": "0.9400", "count_fp": "10", "taker_outcome_side": "no"},
            # after close: dropped
            {"trade_id": "b", "ticker": "KXBTC15M-X", "created_time": "2026-11-20T02:00:05Z",
             "yes_price_dollars": "0.99", "count_fp": "1", "taker_outcome_side": "yes"},
            # another market's trade in the page: never credited to this one
            {"trade_id": "c", "ticker": "OTHER", "created_time": "2026-11-20T01:58:00Z",
             "yes_price_dollars": "0.5", "count_fp": "3", "taker_outcome_side": "yes"},
        ]
        L.aggregate(c, "KXBTC15M-X", "KXBTC15M", CLOSE, "yes", trades, 1)
        rows = c.execute("SELECT ttc, band, taker_side, contracts, paid, taker_won FROM cell").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][:3], ("2-3m", "06-10", "no"))
        self.assertEqual(rows[0][3], 10.0)
        self.assertAlmostEqual(rows[0][4], 0.6)
        self.assertEqual(rows[0][5], 0.0)
        self.assertEqual(c.execute("SELECT done FROM market").fetchone()[0], 1)
        os.remove(p)


class FailedReadIsNotNoTrades(unittest.TestCase):
    def test_a_failed_page_returns_none_and_the_market_stays_unread(self):
        calls = []
        def fake(path, params=None):
            calls.append(params.get("cursor"))
            if params.get("cursor") == "p2":
                return None, "HTTP 503"
            return {"trades": [{"trade_id": "x"}], "cursor": "p2"}, None
        orig = T.get; T.get = fake
        try:
            tk, trades, pages = L.fetch_market("M", CLOSE, None, lambda s: None)
        finally:
            T.get = orig
        self.assertIsNone(trades)
        self.assertEqual(calls, [None, "p2"])

    def test_listing_failure_refuses_rather_than_lists_partially(self):
        orig = T.get; T.get = lambda path, params=None: (None, "HTTP 500")
        p = tmpdb(); c = L.db(p)
        try:
            with self.assertRaises(RuntimeError):
                L.list_markets(c, "KXBTC15M", CLOSE - 86400, CLOSE, None, lambda s: None)
        finally:
            T.get = orig
        os.remove(p)

    def test_unresolved_markets_are_skipped_and_counted(self):
        page = {"markets": [
            {"ticker": "A", "event_ticker": "A", "close_time": "2026-11-20T02:00:00Z", "result": "yes"},
            {"ticker": "B", "event_ticker": "B", "close_time": "2026-11-20T02:15:00Z", "result": ""}]}
        orig = T.get; T.get = lambda path, params=None: (page, None)
        p = tmpdb(); c = L.db(p)
        try:
            n, u = L.list_markets(c, "KXBTC15M", CLOSE - 86400, CLOSE + 3600, None, lambda s: None)
        finally:
            T.get = orig
        self.assertEqual((n, u), (1, 1))
        os.remove(p)


class TierRouting(unittest.TestCase):
    def test_window_straddling_the_trade_cutoff_reads_both_tiers(self):
        paths = []
        def fake(path, params=None):
            paths.append((path, params["min_ts"]))
            return {"trades": []}, None
        orig = T.get; T.get = fake
        try:
            L.fetch_market("M", CLOSE, CLOSE - 600, lambda s: None)
        finally:
            T.get = orig
        self.assertEqual({p for p, _ in paths}, {"/markets/trades", "/historical/trades"})
        self.assertIn(("/markets/trades", int(CLOSE - 600)), paths)


class EarlyClose(unittest.TestCase):
    def test_a_close_off_the_15_minute_grid_is_counted(self):
        p = tmpdb(); c = L.db(p)
        for tk, ct in (("A", CLOSE), ("B", CLOSE + 900), ("C", CLOSE + 1234)):
            c.execute("INSERT INTO market(ticker, event, series, close_ts, result, done) "
                      "VALUES(?,?,?,?,?,1)", (tk, tk, "KXETH15M", ct, "no"))
        c.commit()
        (s, n, done, tr, off, g, days), = L.coverage(c)
        self.assertEqual((n, off, g), (3, 1, 900))
        os.remove(p)


class HourlyRangeGrid(unittest.TestCase):
    def test_kxbtc_ranges_are_checked_against_the_hourly_grid(self):
        self.assertEqual(L.grid_of("KXBTC"), 3600)
        self.assertEqual(L.grid_of("KXBTCD"), 3600)
        self.assertEqual(L.grid_of("KXBTC15M"), 900)
        self.assertIsNone(L.grid_of("KXBTCMAXY"))
        self.assertEqual(L.grid_of("KXCOFFEEH"), 3600)
        self.assertEqual(L.grid_of("KXCOFFEE15M"), 900)
        self.assertIsNone(L.grid_of("KXCOFFEED"))   # daily: close time not on a fixed grid
        self.assertIn("KXBTC", SL.IDEAS["#40 BTC hourly ranges"])
        self.assertEqual(SL.WINDOWS["#40 BTC hourly ranges"], ["5-15m", "15-50m"])


class GateCanFail(unittest.TestCase):
    def day(self, ct, paid, won, losses):
        return [ct, paid, won, losses, 10]

    def test_too_few_losses_fails_even_when_profitable(self):
        dr = {f"2026-11-{d:02d}": self.day(100, 6.0, 1.0, 1) for d in range(1, 21)}
        ok, _ = SL.verdict(dr, "2026-11-11")
        self.assertFalse(ok)   # 20 losses < 30

    def test_one_bad_half_fails(self):
        dr = {f"2026-11-{d:02d}": self.day(100, 6.0, 1.0 if d < 11 else 9.0, 3) for d in range(1, 21)}
        ok, st = SL.verdict(dr, "2026-11-11")
        self.assertFalse(ok)
        self.assertGreater(st["H1"]["ev"], 0)
        self.assertLess(st["H2"]["ev"], 0)

    def test_a_clean_cell_passes(self):
        dr = {f"2026-11-{d:02d}": self.day(100, 6.0, 2.0 + (d % 3) * 0.5, 3) for d in range(1, 21)}
        ok, _ = SL.verdict(dr, "2026-11-11")
        self.assertTrue(ok)


class ExportShape(unittest.TestCase):
    def test_export_is_readable_by_the_round_1_loader(self):
        p = tmpdb(); c = L.db(p)
        c.execute("INSERT INTO series VALUES('KXBTC15M','Crypto','quadratic')")
        c.execute("INSERT INTO market(ticker, event, series, close_ts, result) VALUES(?,?,?,?,?)",
                  ("M", "M", "KXBTC15M", CLOSE, "no"))
        L.aggregate(c, "M", "KXBTC15M", CLOSE, "no",
                    [{"trade_id": "t", "ticker": "M", "created_time": "2026-11-20T01:55:00Z",
                      "yes_price_dollars": "0.05", "count_fp": "4", "taker_outcome_side": "yes"}], 1)
        out = p + ".csv.gz"
        L.export(p, out)
        import score_taker_flow as S
        rows = S.load(out)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["events"], 1)
        self.assertEqual(rows[0]["loss_events"], 0)
        self.assertEqual(rows[0]["ttc"], "5-10m")
        for f in (p, out, out.replace(".csv.gz", "-coverage.txt")):
            os.remove(f)


if __name__ == "__main__":
    unittest.main()
