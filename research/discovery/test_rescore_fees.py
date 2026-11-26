import unittest
import rescore_fees as R


def row(series, fee_type, contracts, paid, won, band="03-05", day="2026-11-05"):
    return {"series": series, "category": "Sports", "fee_type": fee_type, "day": day,
            "ttc": "3-12h", "band": band, "taker_side": "yes", "contracts": contracts,
            "paid": paid, "taker_won": won, "trades": 1, "events": 1, "loss_events": 0}


class TestFeePerRow(unittest.TestCase):
    def test_mixed_cell_charges_only_maker_fee_rows(self):
        rows = [row("A", "quadratic", 100, 4.0, 0.0),
                row("B", "quadratic_with_maker_fees", 100, 4.0, 0.0)]
        g = R.group_net(rows, lambda r: "cell")
        ct, paid = g["cell"]["2026-11-05"][0], g["cell"]["2026-11-05"][1]
        # 03-05 band at a 5-lot: ceil(0.0175*5*0.04*0.96*100)=1c per order = 0.2c/ct
        self.assertAlmostEqual(paid, 8.0 - 0.002 * 100)
        self.assertEqual(ct, 200)

    def test_order_of_rows_does_not_change_the_fee(self):
        a = [row("A", "quadratic", 100, 4.0, 0.0), row("B", "quadratic_with_maker_fees", 100, 4.0, 0.0)]
        g1 = R.group_net(a, lambda r: "c")["c"]["2026-11-05"][1]
        g2 = R.group_net(list(reversed(a)), lambda r: "c")["c"]["2026-11-05"][1]
        self.assertAlmostEqual(g1, g2)


if __name__ == "__main__":
    unittest.main()
