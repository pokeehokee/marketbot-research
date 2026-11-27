"""Freezes the rules the precursor screen rests on. stdlib unittest."""
import gzip, json, os, random, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "discovery"))
import features as F   # noqa: E402
import screen as S     # noqa: E402

CLOSE = 1_800_000_000.0


def tape():
    # (ts, taker_side, yes_price, count), two hours of quiet trading then a burst
    out = []
    for i in range(24):
        out.append((CLOSE - 3 * 3600 + i * 300, "yes" if i % 2 else "no", 0.40 + 0.005 * i, 10))
    out += [(CLOSE - 1800 + k, "yes", 0.60, 50) for k in range(5)]
    return out


class NoLookahead(unittest.TestCase):
    def test_a_later_trade_moves_no_feature(self):
        tr = sorted(tape())
        t = CLOSE - 1800 + 2
        base = F.features(tr, t)
        later = sorted(tr + [(t + 1, "no", 0.05, 9999), (t + 600, "yes", 0.99, 9999)])
        self.assertEqual(base, F.features(later, t))

    def test_a_trade_exactly_at_t_counts(self):
        tr = sorted(tape())
        t = tr[5][0]
        self.assertEqual(F.features(tr, t)[1], tr[5][2])

    def test_targets_are_the_only_forward_read(self):
        tr = sorted(tape())
        t = CLOSE - 1800 - 1
        y, move = F.targets(tr, t, CLOSE, "yes", 30)
        self.assertEqual(y, 1.0)
        self.assertAlmostEqual(move, 0.60 - tr[F._last_at_or_before(tr, t)][2])


class MissingIsNotZero(unittest.TestCase):
    def test_nothing_traded_yet_is_no_row(self):
        self.assertIsNone(F.features([(CLOSE, "yes", 0.5, 1)], CLOSE - 60))

    def test_empty_window_is_none_not_zero(self):
        tr = [(CLOSE - 3 * 3600, "yes", 0.5, 10)]
        _, _, f = F.features(tr, CLOSE - 60)
        self.assertIsNone(f["flow15"])
        self.assertIsNone(f["burst"])
        self.assertIsNone(f["rng60"])
        self.assertIsNone(F.targets(tr, CLOSE - 60, CLOSE, "no", 2)[1])

    def test_no_price_back_then_is_none(self):
        tr = [(CLOSE - 120, "yes", 0.5, 10)]
        _, _, f = F.features(tr, CLOSE - 60)
        self.assertIsNone(f["dp5"])


class Signs(unittest.TestCase):
    def test_no_taker_flow_is_negative(self):
        tr = [(CLOSE - 100, "no", 0.30, 10), (CLOSE - 90, "no", 0.29, 30)]
        _, _, f = F.features(tr, CLOSE - 60)
        self.assertEqual(f["flow15"], -1.0)
        self.assertEqual(f["last_yes"], 0.0)

    def test_burst_compares_last_15_to_the_hour_rate(self):
        tr = [(CLOSE - 3000, "yes", 0.5, 10), (CLOSE - 100, "yes", 0.5, 30)]
        _, _, f = F.features(tr, CLOSE - 60)
        self.assertAlmostEqual(f["burst"], 30 / (40 / 4))

    def test_no_taker_price_becomes_the_yes_price(self):
        self.assertAlmostEqual(F.yes_price("no", 0.96), 0.04)
        self.assertAlmostEqual(F.yes_price("yes", 0.96), 0.96)

    def test_trades_after_close_are_dropped(self):
        rows = F.rows_for_market([(CLOSE - 100, "yes", 0.5, 1), (CLOSE + 5, "yes", 0.99, 1)],
                                 CLOSE, "yes")
        self.assertTrue(all(r["p"] == 0.5 for r in rows))


class Statistics(unittest.TestCase):
    def test_bh(self):
        self.assertEqual(S.bh([0.001, 0.5, 0.02, 0.9]), {0, 2})
        self.assertEqual(S.bh([0.5, 0.6]), set())

    def test_fee_rounds_up_per_order(self):
        self.assertAlmostEqual(S.taker_fee(0.5), 0.02)
        self.assertAlmostEqual(S.taker_fee(0.96), 0.01)

    def _rows(self, planted):
        rng = random.Random(3)
        rows = []
        for d in range(20):
            for i in range(300):
                f = rng.random()
                tgt = (0.10 * (f - 0.5) if planted else 0.0) + rng.gauss(0, 0.3)
                rows.append({"day": f"d{d:02d}", "f": f, "t": tgt})
        return rows

    def test_a_planted_signal_is_found(self):
        r = S.boot(self._rows(True), "f", "t", 0.2, 0.8, random.Random(1), b=200)
        self.assertLess(r[3], 0.01)
        self.assertGreater(r[1], 0)

    def test_noise_is_not(self):
        r = S.boot(self._rows(False), "f", "t", 0.2, 0.8, random.Random(1), b=200)
        self.assertGreater(r[3], 0.01)

    def test_binary_feature_splits_0_against_1(self):
        self.assertEqual(S.cuts([0, 1, 1, 0, 1]), (0, 1))
        self.assertIsNone(S.cuts([1, 1, 1]))


class LockedHoldout(unittest.TestCase):
    def _file(self, d):
        path = os.path.join(d, "rows.csv.gz")
        cols = ("series", "category", "ticker", "day", "close_ts", "offset_min", "p", "p_last",
                "y", "move") + F.FEATURES
        with gzip.open(path, "wt") as f:
            f.write(",".join(cols) + "\n")
            for day in range(10):
                vals = {"series": "X", "category": "c", "ticker": f"T{day}", "day": f"2026-11-{day+10}",
                        "close_ts": "0", "offset_min": "30", "p": "0.5", "p_last": "0.5", "y": "1",
                        "move": ""}
                vals.update({k: "1" for k in F.FEATURES})
                f.write(",".join(vals[c] for c in cols) + "\n")
        return path

    def test_train_never_loads_a_holdout_day(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._file(d)
            train, hold = S.split_days([p])
            self.assertEqual((len(train), len(hold)), (6, 4))
            self.assertLess(max(train), min(hold))
            self.assertTrue(all(r["day"] in train for r in S.load([p], train)))

    def test_holdout_refuses_without_a_committed_selection(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._file(d)
            with self.assertRaises(SystemExit):
                S.phase_holdout([p], "nosuch", d, log=lambda s: None)

    def test_out_of_band_prices_are_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            p = self._file(d)
            S.P_LO, keep = 0.6, S.P_LO
            try:
                self.assertEqual(S.load([p], S.split_days([p])[0]), [])
            finally:
                S.P_LO = keep


if __name__ == "__main__":
    unittest.main()
