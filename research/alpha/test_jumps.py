import gzip, io, os, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jumps as J          # noqa: E402
import score_jumps as S    # noqa: E402

H = 3600


def bars(points):
    """points: [(bar_start, price)] -> bar tuples."""
    return [(b, p, 10.0, 1) for b, p in points]


class TestBars(unittest.TestCase):
    def test_vwap_per_bucket(self):
        b = J.bars_from([(0, 0.40, 10), (100, 0.60, 30), (310, 0.5, 1)])
        self.assertEqual(b[0][0], 0)
        self.assertAlmostEqual(b[0][1], 0.55)
        self.assertEqual(b[1][0], 300)


class TestJumps(unittest.TestCase):
    def test_finds_a_jump_and_its_direction(self):
        bb = bars([(0, 0.30), (1800, 0.32), (2100, 0.50)])
        j = J.find_jumps(bb, close_ts=10 * H)
        self.assertEqual(len(j), 1)
        i, d, pref, p0 = j[0]
        self.assertEqual((i, d), (2, 1))
        self.assertAlmostEqual(pref, 0.30)   # last bar at or before 2100-1800=300 is bar 0

    def test_down_jump(self):
        j = J.find_jumps(bars([(0, 0.80), (1800, 0.60)]), close_ts=10 * H)
        self.assertEqual(j[0][1], -1)

    def test_small_move_is_not_a_jump(self):
        self.assertEqual(J.find_jumps(bars([(0, 0.30), (1800, 0.44)]), close_ts=10 * H), [])

    def test_stale_reference_is_not_used(self):
        # the only earlier bar is 3h old: no reference, so no jump
        self.assertEqual(J.find_jumps(bars([(0, 0.30), (3 * H, 0.70)]), close_ts=20 * H), [])

    def test_no_jumps_in_the_last_hour(self):
        self.assertEqual(J.find_jumps(bars([(0, 0.30), (1800, 0.90)]), close_ts=1800 + 300 + 1800), [])

    def test_one_jump_per_six_hours(self):
        bb = bars([(0, 0.30), (1800, 0.50), (3600, 0.70), (7 * H, 0.70), (7 * H + 1800, 0.40)])
        j = J.find_jumps(bb, close_ts=20 * H)
        self.assertEqual([x[0] for x in j], [1, 4])


class TestOutcomes(unittest.TestCase):
    def test_reads_only_after_the_jump(self):
        bb = bars([(0, 0.30), (1800, 0.50), (1800 + 3600, 0.60)])
        later, entry = J.outcomes(bb, 1, close_ts=48 * H)
        self.assertAlmostEqual(later[3600], 0.60)
        # 15 minutes after the jump bar's end there is no newer bar: None, not the jump price
        self.assertIsNone(entry[900])

    def test_next_bar_entry_is_the_bar_after_the_jump_bar(self):
        """entry[0] is the bar that starts when the jump bar ends; the jump bar is never it."""
        bb = bars([(0, 0.30), (1800, 0.50), (2100, 0.44), (2400, 0.41)])
        later, entry = J.outcomes(bb, 1, close_ts=48 * H)
        self.assertAlmostEqual(entry[0], 0.44)
        gap = bars([(0, 0.30), (1800, 0.50), (3000, 0.44)])
        later, entry = J.outcomes(gap, 1, close_ts=48 * H)
        self.assertIsNone(entry[0])   # nothing traded in the next 5 minutes: None, not p0

    def test_lookahead_planted_fails(self):
        """The jump bar itself must never be read as a later price."""
        bb = bars([(0, 0.30), (1800, 0.50)])
        later, entry = J.outcomes(bb, 1, close_ts=48 * H)
        self.assertTrue(all(v is None for v in later.values()))
        self.assertTrue(all(v is None for v in entry.values()))

    def test_capped_at_close(self):
        bb = bars([(0, 0.30), (1800, 0.50), (5000, 0.55)])
        later, _ = J.outcomes(bb, 1, close_ts=5400)
        self.assertAlmostEqual(later[86400], 0.55)


class TestScore(unittest.TestCase):
    def test_fee(self):
        self.assertAlmostEqual(S.fee10(0.5), 0.018)     # ceil(17.5c)/10
        self.assertAlmostEqual(S.fee10(0.05), 0.004)

    def row(self, d, p0, y, **kw):
        r = {"dir": d, "p0": p0, "y": y, "entry_15m": None, "entry_60m": None}
        r.update(kw); return r

    def test_follow_up_jump_that_settles_yes(self):
        q, won, taker, maker = S.bet(self.row(1, 0.70, 1), "p0", "follow")
        self.assertAlmostEqual(q, 0.70); self.assertEqual(won, 1)
        self.assertAlmostEqual(maker, 0.30)
        self.assertAlmostEqual(taker, 0.30 - S.fee10(0.70))

    def test_fade_buys_the_cheap_side(self):
        q, won, _, maker = S.bet(self.row(1, 0.85, 0), "p0", "fade")
        self.assertAlmostEqual(q, 0.15); self.assertEqual(won, 1)
        self.assertAlmostEqual(maker, 0.85)

    def test_down_jump_follow_buys_no(self):
        q, won, _, maker = S.bet(self.row(-1, 0.20, 1), "p0", "follow")
        self.assertAlmostEqual(q, 0.80); self.assertEqual(won, 0)
        self.assertAlmostEqual(maker, -0.80)

    def test_missing_entry_is_no_bet(self):
        self.assertIsNone(S.bet(self.row(1, 0.5, 1), "entry_15m", "follow"))

    def test_clustered_by_event(self):
        bets = [("E1", "d", 0.5, 1, 0.5, 0.5)] * 10 + [("E2", "d", 0.5, 0, -0.5, -0.5)]
        c = S.cell(bets, 1)
        self.assertEqual(c["events"], 2)
        self.assertLess(c["lo"], 0)    # two events cannot give a CI clear of zero

    def test_end_to_end_runs(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "j.csv.gz")
        with gzip.open(p, "wt") as f:
            f.write(",".join(J.COLS) + "\n")
            for k in range(40):
                f.write(f"Politics,S,E{k},T{k},0,2026-11-{1+k%20:02d},1,0.3,0.5,10,24,{k%2},"
                        "0.55,0.6,,,\n")
        out = io.StringIO()
        S.main([p], out)
        self.assertIn("cells passing every test", out.getvalue())


if __name__ == "__main__":
    unittest.main()
