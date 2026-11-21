"""Local paper-trade ledger for simulation only; never calls exchange APIs."""
from __future__ import annotations
import json
import os
import tempfile
from pathlib import Path
from paper_trader import evaluate_exit


class PaperLedger:
    def __init__(self, path="paper_trades.json", starting_balance=1000.0, fee_rate=0.0006):
        if starting_balance <= 0 or fee_rate < 0:
            raise ValueError("invalid starting balance or fee rate")
        self.path = Path(path)
        self.starting_balance = float(starting_balance)
        self.fee_rate = float(fee_rate)
        self.state = self._load()

    def _load(self):
        if not self.path.exists():
            return {"balance": self.starting_balance, "open": {}, "closed": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("open"), dict) or not isinstance(data.get("closed"), list):
            raise ValueError("invalid paper ledger file")
        return data

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(self.state, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def open_trade(self, trade_id, symbol, direction, entry, stop, target, quantity):
        if not trade_id or not symbol or trade_id in self.state["open"]:
            return False
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        # Validate directional price levels without assuming a live exchange instrument.
        evaluate_exit(direction, entry, stop, target, entry, entry)
        self.state["open"][trade_id] = {
            "symbol": symbol, "direction": direction.upper(), "entry": float(entry),
            "stop": float(stop), "target": float(target), "quantity": float(quantity),
            "entry_candle": None, "last_processed_candle": None
        }
        self._save()
        return True

    def process_candle(self, trade_id, high, low, candle_id=None):
        trade = self.state["open"].get(trade_id)
        if not trade:
            return None
        if candle_id is not None:
            if trade.get("entry_candle") is not None and str(candle_id) <= str(trade["entry_candle"]):
                return {"closed": False, "reason": "ENTRY_CANDLE", "exit_price": None, "pnl_per_unit": 0.0}
            if trade.get("last_processed_candle") == str(candle_id):
                return {"closed": False, "reason": "DUPLICATE_CANDLE", "exit_price": None, "pnl_per_unit": 0.0}
            trade["last_processed_candle"] = str(candle_id)
        result = evaluate_exit(trade["direction"], trade["entry"], trade["stop"],
                               trade["target"], float(high), float(low))
        if not result["closed"]:
            if candle_id is not None:
                self._save()
            return result
        gross = result["pnl_per_unit"] * trade["quantity"]
        notional_in = trade["entry"] * trade["quantity"]
        notional_out = result["exit_price"] * trade["quantity"]
        fees = (notional_in + notional_out) * self.fee_rate
        net = gross - fees
        self.state["balance"] = float(self.state["balance"]) + net
        record = {**trade, "trade_id": trade_id, "exit": result["exit_price"],
                  "exit_reason": result["reason"], "gross_pnl": gross,
                  "fees": fees, "net_pnl": net}
        self.state["closed"].append(record)
        del self.state["open"][trade_id]
        self._save()
        return record

    def export_state(self):
        return self.state

    def summary(self):
        closed = self.state["closed"]
        return {"balance": float(self.state["balance"]), "open_count": len(self.state["open"]),
                "closed_count": len(closed),
                "wins": sum(1 for t in closed if t["net_pnl"] > 0),
                "losses": sum(1 for t in closed if t["net_pnl"] <= 0),
                "net_pnl": sum(t["net_pnl"] for t in closed)}
