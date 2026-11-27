import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import meme_rec as m

POOL = {"id": "solana_P1", "attributes": {
    "address": "P1", "name": "DOG / SOL", "pool_created_at": "2026-11-27T10:00:00Z",
    "reserve_in_usd": "52000.5", "base_token_price_usd": "0.0012", "fdv_usd": "1200000",
    "volume_usd": {"m5": "30000", "h1": "90000"}, "price_change_percentage": {"m5": "12.5", "h1": "80"},
    "transactions": {"m5": {"buys": 120, "sells": 30, "buyers": 90, "sellers": 25}},
    "locked_liquidity_percentage": None},
    "relationships": {"base_token": {"data": {"id": "solana_TOK"}}, "dex": {"data": {"id": "pumpswap"}}}}


class TestMemeRec(unittest.TestCase):
    def test_parse(self):
        p = m.pool_row(POOL, 1790000000)
        self.assertEqual((p["addr"], p["token"], p["dex"]), ("P1", "TOK", "pumpswap"))
        self.assertEqual(p["created"], 1795773600)
        self.assertAlmostEqual(p["reserve"], 52000.5)
        self.assertEqual((p["buys_m5"], p["sells_m5"]), (120, 30))

    def test_missing_is_none_not_zero(self):
        p = m.pool_row({"attributes": {"address": "X"}}, 0)
        self.assertIsNone(p["reserve"])
        self.assertIsNone(p["vol_m5"])
        self.assertIsNone(p["locked"])
        self.assertIsNone(p["created"])

    def test_universe(self):
        p = m.pool_row(POOL, 0)
        now = p["created"] + 3600
        self.assertTrue(m.universe(p, now))
        self.assertFalse(m.universe(p, p["created"] + 7 * 3600))   # older than 6h
        q = dict(p, reserve=39_999.0)
        self.assertFalse(m.universe(q, now))
        q = dict(p, vol_m5=None)
        self.assertFalse(m.universe(q, now))

    def test_token(self):
        r = m.token_row({"data": {"attributes": {"holders": {"count": 412,
            "distribution_percentage": {"top_10": "28.4"}}, "mint_authority": "no",
            "freeze_authority": None}}}, 5, "TOK")
        self.assertEqual((r["holders"], r["top10"], r["mint_auth"]), (412, 28.4, "no"))

    def test_read_only(self):
        with self.assertRaises(RuntimeError):
            m.assert_read_only("/networks/solana/swap")
        m.assert_read_only("/networks/solana/pools/multi/A,B")


if __name__ == "__main__":
    unittest.main()
