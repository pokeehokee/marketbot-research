import csv, gzip, os, sys, tempfile, unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import score_draws as D  # noqa: E402

CLOSE = "2026-11-08T17:00:00Z"
TS = 1_794_153_600  # an hour before CLOSE


def write_probe(d, markets, trades, log=""):
    p = os.path.join(d, "probe-x")
    with gzip.open(p + "-markets.csv.gz", "wt", newline="") as f:
        w = csv.writer(f); w.writerow(("series", "ticker", "result", "close_time"))
        w.writerows(markets)
    with gzip.open(p + "-trades.csv.gz", "wt", newline="") as f:
        w = csv.writer(f); w.writerow(("ticker", "ts", "taker_side", "taker_paid", "count"))
        w.writerows(trades)
    open(p + ".log", "w").write(log)
    return p


class DrawsTest(unittest.TestCase):
    def test_fee_type_is_read_from_the_probe_listing(self):
        d = tempfile.mkdtemp()
        p = write_probe(d, [], [], "  KXEPLGAME                Sports                 custom     "
                        "quadratic_with_maker_fees  English Premier League Game\n"
                        "  KXMLSGAME                Sports                 custom     quadratic                  MLS\n")
        self.assertEqual(D.fee_types(p + ".log"),
                         {"KXEPLGAME": "quadratic_with_maker_fees", "KXMLSGAME": "quadratic"})

    def test_resting_draw_buyer_is_scored_from_the_NO_takers_trades(self):
        # Taker bought NO at 0.70 on 10 contracts; the draw happened, so the
        # resting YES holder paid 3.00 and collected 10: +7.00 gross, fee-free series.
        d = tempfile.mkdtemp()
        p = write_probe(d, [("KXMLSGAME", "KXMLSGAME-A-TIE", "yes", CLOSE)],
                        [("KXMLSGAME-A-TIE", TS, "no", "0.7000", "10")],
                        "  KXMLSGAME                Sports   custom  quadratic  MLS\n")
        rows, notes, _ = D.rows_from_probe(p)
        dc = D.C.day_cells(rows, lambda r: "k" if r["taker_side"] == "no" else None, "sell")["k"]
        (_, v), = dc.items()
        self.assertAlmostEqual(v[1], 7.0); self.assertAlmostEqual(v[2], 7.0)
        self.assertEqual((v[5], v[6]), (0, 1))  # the holder lost no market

    def test_unknown_fee_type_assumes_the_maker_fee_and_says_so(self):
        d = tempfile.mkdtemp()
        p = write_probe(d, [("KXEPLGAME", "KXEPLGAME-A-TIE", "no", CLOSE)],
                        [("KXEPLGAME-A-TIE", TS, "no", "0.7000", "5")])
        rows, notes, _ = D.rows_from_probe(p)
        self.assertGreater(rows[0]["mfee"], 0)
        self.assertTrue(any("maker fee assumed" in k for k in notes))

    def test_trades_after_close_and_unsettled_markets_are_dropped(self):
        d = tempfile.mkdtemp()
        p = write_probe(d, [("KXMLSGAME", "KXMLSGAME-A-TIE", "yes", CLOSE),
                            ("KXMLSGAME", "KXMLSGAME-B-TIE", "", CLOSE)],
                        [("KXMLSGAME-A-TIE", TS + 10 ** 6, "no", "0.7000", "10"),
                         ("KXMLSGAME-B-TIE", TS, "no", "0.7000", "10")])
        rows, notes, _ = D.rows_from_probe(p)
        self.assertEqual(rows, [])
        self.assertEqual((notes["after close"], notes["no settled market"]), (1, 1))


if __name__ == "__main__":
    unittest.main()
