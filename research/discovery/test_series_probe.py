"""The series probe follows every page, never treats an error as an empty
listing, and writes trades priced at what the taker paid."""
import csv, gzip, os, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import series_probe as P  # noqa: E402
import taker_flow_screen as T  # noqa: E402


class Probe(unittest.TestCase):
    def test_pages_follow_cursor_and_report_errors(self):
        seq = [({"markets": [1, 2], "cursor": "c"}, None), (None, "HTTP 500")]
        orig = T.get; T.get = lambda p, q=None: seq.pop(0)
        logs = []
        try:
            rows, err = P.pages("/markets", "markets", {}, logs.append)
        finally:
            T.get = orig
        self.assertEqual(rows, [1, 2]); self.assertEqual(err, "HTTP 500")
        self.assertTrue(any("HTTP 500" in s for s in logs))

    def test_end_to_end_on_a_fake_exchange(self):
        def fake(path, q=None):
            q = q or {}
            if path == "/historical/cutoff":
                return {"trades_created_ts": "2026-09-24T00:00:00Z"}, None
            if path == "/series":
                return {"series": [{"ticker": "KXTSAW", "title": "TSA check-ins",
                                    "category": "Economics", "frequency": "weekly"}]}, None
            if path == "/markets":
                if q.get("status") == "settled":
                    return {"markets": [{"ticker": "KXTSAW-1", "result": "no", "rules_primary": "r",
                                         "close_time": "2026-11-20T00:00:00Z"}]}, None
                return {"markets": [{"ticker": "KXBTCD-1", "rules_primary": "avg of 60s"}]}, None
            if path == "/historical/markets":
                return {"markets": []}, None
            if path == "/markets/trades":
                return {"trades": [{"ticker": "KXTSAW-1", "created_time": "2026-11-19T00:00:00Z",
                                    "yes_price_dollars": "0.97", "count_fp": "4",
                                    "taker_outcome_side": "no"}]}, None
            return None, "HTTP 404"
        d = tempfile.mkdtemp(); origo, P.OUT = P.OUT, d
        orig = T.get; T.get = fake
        try:
            P.main(["--tag", "t", "--find", "TSA", "--series", "FOUND", "--rules-only", "KXBTCD"])
        finally:
            T.get = orig; P.OUT = origo
        with gzip.open(os.path.join(d, "probe-t-trades.csv.gz"), "rt") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[1][2:], ["no", "0.0300", "4"])
        log = open(os.path.join(d, "probe-t.log")).read()
        self.assertIn("avg of 60s", log); self.assertIn("KXTSAW", log)


if __name__ == "__main__":
    unittest.main()
