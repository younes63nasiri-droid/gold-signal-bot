import unittest

from risk_manager import calculate_position_plan, calculate_fixed_margin_plan


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


import tempfile
from pathlib import Path
from paper_ledger import PaperLedger


class PaperLedgerTests(unittest.TestCase):
    def test_open_close_persists_and_updates_balance(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger = PaperLedger(path, starting_balance=1000, fee_rate=0)
            self.assertTrue(ledger.open_trade("id1", "BTCUSDT", "LONG", 100, 95, 110, 2))
            self.assertFalse(ledger.open_trade("id1", "BTCUSDT", "LONG", 100, 95, 110, 2))
            closed = ledger.process_candle("id1", high=111, low=99)
            self.assertEqual(closed["net_pnl"], 20)
            reloaded = PaperLedger(path, starting_balance=1000, fee_rate=0)
            self.assertEqual(reloaded.summary()["balance"], 1020)
            self.assertEqual(reloaded.summary()["closed_count"], 1)

    def test_open_trade_stays_open_before_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = PaperLedger(Path(directory) / "ledger.json")
            ledger.open_trade("id2", "ETHUSDT", "SHORT", 100, 105, 90, 1)
            self.assertEqual(ledger.process_candle("id2", 104, 91)["reason"], "OPEN")
            self.assertEqual(ledger.summary()["open_count"], 1)

    def test_fee_deducted_from_result(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = PaperLedger(Path(directory) / "ledger.json", 1000, fee_rate=0.001)
            ledger.open_trade("id3", "X", "LONG", 100, 95, 110, 1)
            closed = ledger.process_candle("id3", 111, 99)
            self.assertAlmostEqual(closed["net_pnl"], 9.79)


class PaperLedgerCandleIdempotencyTests(unittest.TestCase):
    def test_entry_candle_is_not_used_to_exit_new_trade(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = PaperLedger(Path(directory) / "ledger.json", fee_rate=0)
            ledger.open_trade("id4", "BTCUSDT", "LONG", 100, 95, 110, 1)
            ledger.state["open"]["id4"]["entry_candle"] = "2026-11-21T10:00:00+00:00"
            result = ledger.process_candle("id4", 111, 94, "2026-11-21T10:00:00+00:00")
            self.assertEqual(result["reason"], "ENTRY_CANDLE")
            self.assertEqual(ledger.summary()["open_count"], 1)

    def test_same_candle_is_processed_only_once(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = PaperLedger(Path(directory) / "ledger.json", fee_rate=0)
            ledger.open_trade("id5", "BTCUSDT", "LONG", 100, 95, 110, 1)
            candle = "2026-11-21T10:15:00+00:00"
            first = ledger.process_candle("id5", 105, 99, candle)
            second = ledger.process_candle("id5", 111, 94, candle)
            self.assertEqual(first["reason"], "OPEN")
            self.assertEqual(second["reason"], "DUPLICATE_CANDLE")
            self.assertEqual(ledger.summary()["open_count"], 1)


class FixedMarginSizingTests(unittest.TestCase):
    def test_uses_exact_half_dollar_margin_and_dynamic_minimum_leverage(self):
        plan = calculate_fixed_margin_plan(1000, 100, 98, "LONG")
        self.assertTrue(plan.allowed)
        self.assertAlmostEqual(plan.margin_usdt, 0.50)
        self.assertGreaterEqual(plan.notional_usdt, 5.0)
        self.assertEqual(plan.leverage, 10)

    def test_rejects_contract_minimum_that_cannot_fit_margin_and_leverage_cap(self):
        plan = calculate_fixed_margin_plan(
            1000, 100, 98, "LONG", min_notional_usdt=10.0, max_leverage=10
        )
        self.assertFalse(plan.allowed)
        self.assertIn("needs at least 20x", plan.reason)

    def test_rejects_margin_above_balance(self):
        plan = calculate_fixed_margin_plan(0.25, 100, 98, "LONG")
        self.assertFalse(plan.allowed)
