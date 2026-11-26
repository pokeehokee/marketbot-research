import os, sys, unittest
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import probe as P


class TestReadOnly(unittest.TestCase):
    def test_refuses_other_hosts_and_paths(self):
        for u in ("https://api.gemini.com/v1/prediction-markets/order",
                  "https://api.gemini.com/v1/order/new",
                  "https://clob.polymarket.com/order",
                  "http://api.gemini.com/v2/klines",
                  "https://evil.example/markets",
                  "https://gamma-api.polymarket.com.evil.example/markets",
                  "https://api.crypto.com/exchange/v1/private/create-order"):
            with self.assertRaises(RuntimeError, msg=u):
                P.get(u)

    def test_prefix_is_a_path_segment(self):
        with self.assertRaises(RuntimeError):
            P.get("https://gamma-api.polymarket.com/markets-admin")

    def test_a_failed_section_is_logged_not_raised(self):
        P.LOG.clear()
        P.section("x", lambda: P.get("https://evil.example/"))
        self.assertTrue(any("FAILED" in s for s in P.LOG))


if __name__ == "__main__":
    unittest.main()
