import gzip, os, random, sys, tempfile, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import cheap_screen as C   # noqa: E402
import jumps as J          # noqa: E402

TAPE_COLS = ("series,category,ticker,day,close_ts,offset_min,p,p_last,y,move,age,dp5,dp15,dp60,"
             "flow15,flow60,vol15,vol60,burst,ntr15,size60,rng60,last_yes,dist,hour_et")


def tape(**kw):
    r = dict(zip(TAPE_COLS.split(","), [""] * 25))
    r.update({"series": "KXA", "category": "Cat", "ticker": "KXA-1-T1", "day": "2026-11-01",
              "offset_min": "60"})
    r.update({k: str(v) for k, v in kw.items()})
    return r


class TestMoney(unittest.TestCase):
    def test_dollar_stake_return(self):
        # 0.05: 20 contracts, fee ceil(0.07*20*.05*.95*100)=7c on $1
        self.assertAlmostEqual(C.ret_per_dollar(0.05, 1), (20 * 0.95 - 0.07) / 1.0)
        self.assertAlmostEqual(C.ret_per_dollar(0.05, 0), (-1.0 - 0.07) / 1.0)

    def test_breakeven_includes_fee(self):
        q = 0.05
        self.assertAlmostEqual(C.breakeven(q) * C.ret_per_dollar(q, 1)
                               + (1 - C.breakeven(q)) * C.ret_per_dollar(q, 0), 0, places=9)


class TestOrientation(unittest.TestCase):
    def test_cheap_no_side(self):
        r = C.tape_row(tape(p=0.92, y=0, dp15=0.05, flow15=0.4, last_yes=1))
        self.assertAlmostEqual(r["q"], 0.08)
        self.assertEqual(r["won"], 1)
        self.assertAlmostEqual(r["f"]["dq15"], -0.05)     # YES rose, so the NO side fell
        self.assertAlmostEqual(r["f"]["flowc15"], -0.4)
        self.assertEqual(r["f"]["last_cheap"], 0)         # last trade bought YES, not the cheap side

    def test_mid_prices_are_not_cheap(self):
        self.assertIsNone(C.tape_row(tape(p=0.5, y=1)))

    def test_missing_is_none_not_zero(self):
        r = C.tape_row(tape(p=0.05, y=1))
        self.assertIsNone(r["f"]["dq15"])
        self.assertFalse(C.member(("tape", "long", "bot", "dq15", 0.0, None), r))


class TestNewsRows(unittest.TestCase):
    def test_no_lookahead_in_cheap_rows(self):
        H = 3600
        close = 100 * H
        bars = [(0, 0.10, 5.0, 1), (close - 7 * H, 0.08, 5.0, 1)]
        before = J.cheap_rows(bars, close, "no")
        # a bar that ends AFTER the 6h mark must not change the 6h row
        later = bars + [(close - 6 * H - 60, 0.95, 5.0, 1)]
        after = J.cheap_rows(later, close, "no")
        six_b = [r for r in before if r[0] == 6][0]
        six_a = [r for r in after if r[0] == 6][0]
        self.assertEqual(six_b, six_a)

    def test_cheap_no_side_from_bars(self):
        H = 3600
        r = J.cheap_rows([(0, 0.93, 5.0, 1)], 30 * H, "no")
        self.assertTrue(r)
        off, q, cy, won = r[0][:4]
        self.assertAlmostEqual(q, 0.07); self.assertEqual((cy, won), (0, 1))

    def test_crash_into_cheapness_is_minus_one(self):
        H = 3600
        close = 100 * H
        t = close - 6 * H
        bars = [(t - 5 * H, 0.40, 5.0, 1), (t - 4 * H, 0.10, 5.0, 1)]
        r = [x for x in J.cheap_rows(bars, close, "no") if x[0] == 6][0]
        self.assertEqual(r[-1], -1)


def write(path, rows):
    with gzip.open(path, "wt") as f:
        f.write(TAPE_COLS + "\n")
        for r in rows:
            f.write(",".join(r.get(k, "") for k in TAPE_COLS.split(",")) + "\n")


class TestScreen(unittest.TestCase):
    def synth(self, signal):
        rng = random.Random(5)
        rows = []
        for d in range(40):
            day = f"2026-10-{1 + d % 30:02d}" if d < 30 else f"2026-11-{d - 29:02d}"
            for k in range(120):
                q = rng.choice([0.04, 0.08, 0.12])
                f15 = rng.random()
                pw = q * 0.8 + (0.35 if signal and f15 > 0.8 else 0.0)
                y = 1 if rng.random() < pw else 0
                rows.append(tape(ticker=f"KXA-{d}-{k}", day=day, p=q, y=y, flow15=f15,
                                 last_yes=1, dp15=rng.random() - 0.5))
        return rows

    def run_both(self, signal):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.csv.gz")
        write(p, self.synth(signal))
        trusted, sel = C.phase_train([p], "t", d, log=lambda s: None)
        return d, p, trusted, sel

    def test_planted_signal_found_and_survives(self):
        d, p, trusted, sel = self.run_both(True)
        self.assertTrue(trusted)
        self.assertTrue(any(s[3] == "flowc15" and s[2] == "top" for s in sel))
        # a cell that only rides a profitable pool is not a signal and is not selected
        self.assertFalse([s for s in sel if s[3] != "flowc15"])
        found = C.phase_holdout([p], "t", d, log=lambda s: None)
        self.assertTrue(any("flowc15" in f for f in found))

    def test_no_signal_no_survivor(self):
        d, p, trusted, sel = self.run_both(False)
        self.assertTrue(trusted)
        self.assertFalse([s for s in sel if s[3] not in C.CONTROLS])

    def test_holdout_refuses_without_selection(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(SystemExit):
            C.phase_holdout([], "none", d, log=lambda s: None)

    def test_train_never_loads_holdout_days(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.csv.gz")
        write(p, self.synth(False))
        train, hold = C.split_days([p])
        rows = C.load([p], train)
        self.assertFalse({r["day"] for r in rows} & set(hold["tape"]))


if __name__ == "__main__":
    unittest.main()
