"""Deterministic paper-trade exit simulation. Never submits exchange orders."""
from __future__ import annotations


def evaluate_exit(direction: str, entry: float, stop: float, target: float,
                  candle_high: float, candle_low: float) -> dict:
    """Resolve one candle conservatively; if stop and target both touched, stop wins."""
    direction = direction.upper()
    values = (entry, stop, target, candle_high, candle_low)
    if direction not in ("LONG", "SHORT"):
        raise ValueError("direction must be LONG or SHORT")
    if any(not isinstance(v, (int, float)) for v in values):
        raise ValueError("prices must be numeric")
    if min(values) <= 0 or candle_low > candle_high:
        raise ValueError("invalid prices or candle range")
    if direction == "LONG":
        if not stop < entry < target:
            raise ValueError("LONG requires stop < entry < target")
        stop_hit = candle_low <= stop
        target_hit = candle_high >= target
    else:
        if not target < entry < stop:
            raise ValueError("SHORT requires target < entry < stop")
        stop_hit = candle_high >= stop
        target_hit = candle_low <= target

    if stop_hit:
        exit_price, reason = stop, "STOP_LOSS"
    elif target_hit:
        exit_price, reason = target, "TAKE_PROFIT"
    else:
        return {"closed": False, "reason": "OPEN", "exit_price": None, "pnl_per_unit": 0.0}

    pnl = exit_price - entry if direction == "LONG" else entry - exit_price
    return {"closed": True, "reason": reason, "exit_price": exit_price, "pnl_per_unit": pnl}
