import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import pm_sharp as p


class TestPmSharp(unittest.TestCase):
    def test_winner(self):
        self.assertEqual(p.winner({"outcomes": '["Yes","No"]', "outcomePrices": '["0","1"]'}), 1)
        self.assertIsNone(p.winner({"outcomes": '["Yes","No"]', "outcomePrices": '["0.5","0.5"]'}))
        self.assertIsNone(p.winner({"outcomes": '["A","B","C"]', "outcomePrices": '["1","0","0"]'}))

    def test_value(self):
        self.assertAlmostEqual(p.value("BUY", 0.3, 10, 1.0), 7.0)
        self.assertAlmostEqual(p.value("SELL", 0.3, 10, 1.0), -7.0)
        self.assertAlmostEqual(p.value("SELL", 0.3, 10, 0.0), 3.0)

    def test_follow_never_uses_an_earlier_print(self):
        rows = {"M": [("M", "x", "BUY", 0, .40, 10, 100), ("M", "y", "BUY", 0, .45, 5, 150),
                      ("M", "z", "BUY", 0, .55, 5, 200)]}
        out = p.follow(rows, {"x"}, {"M": 0}, {"M": 86400 * 3}, delay=60)
        self.assertEqual(out[3], [(1 - .55, .55, 1.0)])   # the 150 print is inside the delay

    def test_sell_is_followed_by_buying_the_other_side(self):
        rows = {"M": [("M", "x", "SELL", 0, .60, 10, 100), ("M", "y", "BUY", 1, .42, 5, 200)]}
        out = p.follow(rows, {"x"}, {"M": 1}, {"M": 0}, delay=60)
        self.assertEqual(out[0], [(1 - .42, .42, 1.0)])

    def test_v2_page(self):
        rows, nxt = p.page_of({"data": [{"a": 1}], "pagination": {"next_cursor": "abc", "has_more": True}})
        self.assertEqual((rows, nxt), ([{"a": 1}], "abc"))
        self.assertEqual(p.page_of({"data": [], "pagination": {"next_cursor": None, "has_more": False}}), ([], None))
        self.assertEqual(p.page_of([{"a": 1}]), ([{"a": 1}], None))

    def test_parse_trade(self):
        x = {"proxy_wallet": "0xAB", "side": "SELL", "outcome_index": 1, "price": "0.4", "size": "5",
             "timestamp": 100}
        self.assertEqual(p.parse_trade(x), ("0xab", 0, 1, 0.4, 5.0, 100))
        self.assertIsNone(p.parse_trade(dict(x, outcome_index=999)))
        self.assertIsNone(p.parse_trade(dict(x, side="MERGE")))

    def test_score_end_to_end_on_the_compact_schema(self):
        """A wallet right on 20 train markets is found sharp and followed in holdout; a capped
        holdout market is excluded from the follow."""
        import tempfile, sqlite3
        d = tempfile.mkdtemp()
        c = p.db(os.path.join(d, "pm_t_0.db"))
        day = 86400
        for i in range(30):
            cid, end = f"c{i}", (i + 1) * day
            c.execute("INSERT INTO mkt(cid, q, end_ts, win, vol, done, capped) VALUES (?,?,?,?,?,1,?)",
                      (cid, "", end, 0, 1e5, 1 if i == 29 else 0))
            m = c.execute("SELECT rowid FROM mkt WHERE cid=?", (cid,)).fetchone()[0]
            t0 = end - day // 2
            base = 0.40 + (i % 5) * 0.01
            rows = [(m, p.wid(c, "0xsharp"), 1, 0, base, 100, t0),
                    (m, p.wid(c, "0xdumb"), 1, 1, 1 - base, 100, t0 + 1),
                    (m, p.wid(c, "0xtape"), 1, 0, base + 0.05, 10, t0 + 120)][: 3 if i >= 18 else 2]
            c.executemany("INSERT INTO tr VALUES (?,?,?,?,?,?,?)", rows)
        c.commit()
        out = []
        p._WID.clear()
        p.score(type("A", (), {"score": os.path.join(d, "pm_t_*.db")}), log=out.append)
        text = "\n".join(out)
        self.assertIn("sharp 1", text)
        self.assertIn("1 capped", text)
        line = next(l for l in out if l.strip().startswith("sharp:"))
        self.assertIn("win 100.0%", line)
        self.assertIn("days=11", line)   # 12 holdout markets, the capped one excluded

    def test_read_only(self):
        with self.assertRaises(RuntimeError):
            p.get("https://clob.polymarket.com/order")
        with self.assertRaises(RuntimeError):
            p.get("https://data-api.polymarket.com.evil.example/v2/trades")


if __name__ == "__main__":
    unittest.main()
