"""Conservative position sizing helpers. This module never places orders."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PositionPlan:
    allowed: bool
    reason: str
    notional_usdt: float = 0.0
    margin_usdt: float = 0.0
    quantity: float = 0.0
    leverage: int = 1
    max_loss_usdt: float = 0.0


def calculate_position_plan(
    balance_usdt: float,
    entry: float,
    stop: float,
    direction: str,
    *,
    risk_pct: float = 0.5,
    max_leverage: int = 3,
    min_notional_usdt: float = 5.0,
) -> PositionPlan:
    """Size a hypothetical USDT-M position under a fixed account-risk cap.

    LONG requires stop < entry; SHORT requires stop > entry. Notional is
    limited by both the risk budget and max leverage. This is sizing only:
    exchange filters, fees, slippage and contract multipliers still need
    validation before any live order can be considered.
    """
    values = (balance_usdt, entry, stop, risk_pct, min_notional_usdt)
    if any(not isinstance(v, (int, float)) for v in values):
        return PositionPlan(False, "invalid numeric input")
    if balance_usdt <= 0 or entry <= 0 or stop <= 0:
        return PositionPlan(False, "balance, entry and stop must be positive")
    if not 0 < risk_pct <= 1:
        return PositionPlan(False, "risk_pct must be greater than 0 and at most 1")
    if not isinstance(max_leverage, int) or max_leverage < 1:
        return PositionPlan(False, "max_leverage must be a positive integer")
    direction = direction.upper()
    if direction == "LONG" and stop >= entry:
        return PositionPlan(False, "LONG stop must be below entry")
    if direction == "SHORT" and stop <= entry:
        return PositionPlan(False, "SHORT stop must be above entry")
    if direction not in ("LONG", "SHORT"):
        return PositionPlan(False, "direction must be LONG or SHORT")

    stop_fraction = abs(entry - stop) / entry
    risk_budget = balance_usdt * (risk_pct / 100.0)
    risk_limited_notional = risk_budget / stop_fraction
    leverage_limited_notional = balance_usdt * max_leverage
    notional = min(risk_limited_notional, leverage_limited_notional)

    if notional < min_notional_usdt:
        return PositionPlan(
            False,
            f"notional {notional:.8f} USDT is below minimum {min_notional_usdt:.2f} USDT",
        )

    leverage = max(1, min(max_leverage, int((notional / balance_usdt) + 0.999999)))
    margin = notional / leverage
    quantity = notional / entry
    max_loss = notional * stop_fraction
    if max_loss > risk_budget * (1 + 1e-9):
        return PositionPlan(False, "risk budget exceeded")
    return PositionPlan(
        True,
        "paper sizing only; no order submitted",
        notional_usdt=notional,
        margin_usdt=margin,
        quantity=quantity,
        leverage=leverage,
        max_loss_usdt=max_loss,
    )
