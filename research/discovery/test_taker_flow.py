"""Freezes the taker-flow screen's three load-bearing rules: which side the taker
ACQUIRED and what they paid, the maker's per-contract P&L, and that the
out-of-sample gate can actually reject a cell (a control that cannot fail is
not a control)."""
import gzip, os, sqlite3, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import taker_flow_screen as T  # noqa: E402
import score_taker_flow as S  # noqa: E402


class TradeFields(unittest.TestCase):
    def test_no_taker_pays_one_minus_yes_price(self):
        f = T.trade_fields({"ticker": "X", "created_time": "2026-11-20T01:00:00Z",
                            "yes_price_dollars": "0.9400", "count_fp": "7.00",
                            "taker_outcome_side": "no"})
        self.assertEqual(f[2], "no")
        self.assertAlmostEqual(f[3], 0.06)
        self.assertEqual(f[4], 7.0)

    def test_legacy_cents_are_cents(self):
        f = T.trade_fields({"ticker": "X", "created_time": "2026-11-20T01:00:00.5Z",
                            "yes_price": 4, "count": 25, "taker_side": "yes"})
        self.assertAlmostEqual(f[3], 0.04)

    def test_missing_price_or_side_is_skipped_not_zero(self):
        self.assertIsNone(T.trade_fields({"ticker": "X", "created_time": "2026-11-20T01:00:00Z",
                                          "count": 5, "taker_side": "yes"}))
        self.assertIsNone(T.trade_fields({"ticker": "X", "created_time": "2026-11-20T01:00:00Z",
                                          "yes_price_dollars": "0.5", "count": 5}))


class Guard(unittest.TestCase):
    def test_refuses_portfolio_and_unknown_paths(self):
        for p in ("/portfolio/orders", "/portfolio/balance", "/exchange/status", "/marketsx"):
            with self.assertRaises(RuntimeError):
                T.assert_read_only(p)
        for p in ("/markets", "/markets/trades?limit=1", "/historical/trades", "/series"):
            T.assert_read_only(p)


class ScanHour(unittest.TestCase):
    def test_maker_pnl_and_ttc_from_a_fake_tape(self):
        fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        c = T.db(path)
        close = T.utc("2026-11-20T02:00:00Z")
        markets = {"KXBTCD-26NOV2002-T1": ("KXBTCD-26NOV2002", "KXBTCD", close, "yes")}
        tape = {"trades": [
            # taker buys NO (the cheap side) at 0.06, 20 min out: maker holds YES at 0.94
            {"ticker": "KXBTCD-26NOV2002-T1", "created_time": "2026-11-20T01:40:00Z",
             "yes_price_dollars": "0.9400", "count_fp": "10", "taker_outcome_side": "no"},
            # a trade on a market with no settled result: counted unknown, never graded
            {"ticker": "OTHER", "created_time": "2026-11-20T01:41:00Z",
             "yes_price_dollars": "0.5", "count_fp": "3", "taker_outcome_side": "yes"},
            # after close: dropped
            {"ticker": "KXBTCD-26NOV2002-T1", "created_time": "2026-11-20T02:00:05Z",
             "yes_price_dollars": "0.99", "count_fp": "1", "taker_outcome_side": "yes"},
        ]}
        orig = T.get
        T.get = lambda path, params=None: (tape, None)
        try:
            r = T.scan_hour(c, T.utc("2026-11-20T01:00:00Z"), markets, None, lambda s: None)
        finally:
            T.get = orig
        self.assertEqual(r[0], 3); self.assertEqual(r[1], 1); self.assertEqual(r[2], 1)
        row = c.execute("SELECT ttc, band, taker_side, contracts, paid, taker_won FROM cell").fetchone()
        self.assertEqual(row[:3], ("15-50m", "06-10", "no"))
        self.assertAlmostEqual(row[4], 0.6)   # 10 x 0.06
        self.assertEqual(row[5], 0.0)         # NO lost: the taker won nothing
        os.remove(path)


class Fees(unittest.TestCase):
    def test_quadratic_makers_pay_nothing(self):
        self.assertEqual(S.maker_fee_per_ct("quadratic", "06-10"), 0.0)

    def test_maker_fee_series_rounds_up_per_order(self):
        # ceil(0.0175 x 5 x 0.08 x 0.92) = ceil(0.644c) = 1c per 5-lot
        self.assertAlmostEqual(S.maker_fee_per_ct("quadratic_with_maker_fees", "06-10"), 0.002)


def _rows(days, ev_cents_by_half, half_at):
    """Synthetic aggregate rows: each day 40 events x 100 ct at 0.06."""
    rows = []
    for i, d in enumerate(days):
        target = ev_cents_by_half[0] if d < half_at else ev_cents_by_half[1]
        # maker EV = 0.06 - won/ct  ->  won fraction = 0.06 - target
        frac = max(0.0, 0.06 - target / 100.0)
        losers = round(frac * 40)
        for e in range(40):
            rows.append({"series": "S", "category": "C", "fee_type": "quadratic",
                         "event": f"E{i}-{e}", "day": d, "ttc": "15-50m", "band": "06-10",
                         "taker_side": "no", "contracts": 100.0, "paid": 6.0, "events": 1, "loss_events": 1 if e < losers else 0,
                         "taker_won": 100.0 if e < losers else 0.0, "trades": 3})
    return rows


class OutOfSampleGate(unittest.TestCase):
    days = [f"2026-11-{d:02d}" for d in range(1, 29)]

    def test_a_real_edge_survives(self):
        rows = _rows(self.days, (3.5, 3.5), "2026-11-15")
        surv = S.screen(rows, lambda r: (r["series"], r["ttc"], r["band"]), "t", out=lambda s: None)
        self.assertEqual(len(surv), 1)

    def test_an_edge_that_vanishes_out_of_sample_is_killed(self):
        rows = _rows(self.days, (3.5, -1.0), "2026-11-15")
        surv = S.screen(rows, lambda r: (r["series"], r["ttc"], r["band"]), "t", out=lambda s: None)
        self.assertEqual(surv, [])


if __name__ == "__main__":
    unittest.main()


class Resolver(unittest.TestCase):
    def test_a_ticker_not_asked_for_is_discarded_and_misses_retried_alone(self):
        fd, path = tempfile.mkstemp(suffix=".db"); os.close(fd)
        c = T.db(path)
        calls = []
        def fake(p, params=None):
            calls.append(p)
            if p == "/markets":   # the batch answers with a bogus combined ticker
                return {"markets": [{"ticker": "A,B", "result": "yes"}]}, None
            tk = p.rsplit("/", 1)[1]
            return {"market": {"ticker": tk, "event_ticker": "EV-1", "result": "no",
                               "close_time": "2026-11-20T02:00:00Z"}}, None
        orig = T.get; T.get = fake
        try:
            markets = {}
            n, missed = T.resolve(c, ["A", "B"], markets, lambda s: None)
        finally:
            T.get = orig
        self.assertEqual((n, missed), (2, 2))
        self.assertNotIn("A,B", markets)
        self.assertEqual(markets["A"][3], "no")
        self.assertEqual(calls.count("/markets"), 1)
        os.remove(path)
