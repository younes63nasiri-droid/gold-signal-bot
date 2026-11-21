import unittest

from risk_manager import calculate_position_plan


class PositionSizingTests(unittest.TestCase):
    def test_sizes_long_within_risk_budget(self):
        plan = calculate_position_plan(1000, 100, 98, "LONG")
        self.assertTrue(plan.allowed)
        self.assertLessEqual(plan.max_loss_usdt, 5.0 + 1e-8)
        self.assertLessEqual(plan.leverage, 3)

    def test_sizes_short_within_risk_budget(self):
        plan = calculate_position_plan(1000, 100, 102, "SHORT")
        self.assertTrue(plan.allowed)
        self.assertLessEqual(plan.max_loss_usdt, 5.0 + 1e-8)

    def test_rejects_tiny_balance_below_minimum(self):
        plan = calculate_position_plan(0.00019179, 100, 98, "LONG")
        self.assertFalse(plan.allowed)
        self.assertIn("below minimum", plan.reason)

    def test_rejects_wrong_stop_side(self):
        self.assertFalse(calculate_position_plan(1000, 100, 101, "LONG").allowed)
        self.assertFalse(calculate_position_plan(1000, 100, 99, "SHORT").allowed)

    def test_rejects_zero_balance_and_invalid_direction(self):
        self.assertFalse(calculate_position_plan(0, 100, 98, "LONG").allowed)
        self.assertFalse(calculate_position_plan(1000, 100, 98, "SIDEWAYS").allowed)


if __name__ == "__main__":
    unittest.main()


from paper_trader import evaluate_exit


class PaperTradeExitTests(unittest.TestCase):
    def test_long_take_profit(self):
        result = evaluate_exit("LONG", 100, 95, 110, 111, 99)
        self.assertEqual(result["reason"], "TAKE_PROFIT")
        self.assertEqual(result["pnl_per_unit"], 10)

    def test_long_stop_loss(self):
        result = evaluate_exit("LONG", 100, 95, 110, 105, 94)
        self.assertEqual(result["reason"], "STOP_LOSS")
        self.assertEqual(result["pnl_per_unit"], -5)

    def test_short_take_profit(self):
        result = evaluate_exit("SHORT", 100, 105, 90, 101, 89)
        self.assertEqual(result["reason"], "TAKE_PROFIT")
        self.assertEqual(result["pnl_per_unit"], 10)

    def test_stop_wins_when_both_levels_touch(self):
        result = evaluate_exit("LONG", 100, 95, 110, 111, 94)
        self.assertEqual(result["reason"], "STOP_LOSS")

    def test_position_remains_open(self):
        result = evaluate_exit("SHORT", 100, 105, 90, 104, 91)
        self.assertFalse(result["closed"])
        self.assertEqual(result["reason"], "OPEN")
