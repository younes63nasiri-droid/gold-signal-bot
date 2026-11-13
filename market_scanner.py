"""Market-wide Toobit USDT-M perpetual scanner. Paper-trading only.

Discovers all active linear USDT perpetual contracts, ranks liquidity,
then evaluates the most liquid candidates on closed 15m/1h candles.
No order-placement endpoints or API keys are used.
"""
from __future__ import annotations

import math
import time
from typing import Any

import requests

BASE = "https://api.toobit.com"
EXCHANGE_INFO = f"{BASE}/api/v1/exchangeInfo"
TICKER_24H = f"{BASE}/quote/v1/contract/ticker/24hr"
BOOK_TICKER = f"{BASE}/quote/v1/contract/ticker/bookTicker"
KLINES = f"{BASE}/quote/v1/klines"

# Scan the full contract list, then spend candle requests only on liquid markets.
MAX_CANDLE_CANDIDATES = 60
MIN_QUOTE_VOLUME_24H = 500_000.0
MAX_SPREAD_PCT = 0.15
CANDLE_LIMIT = 120
REQUEST_TIMEOUT = 15
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "paper-market-scanner/1.0"})
SERVER_TIME_MS = None


def _get(url: str, params: dict | None = None) -> Any:
    last_error = None
    for attempt in range(3):
        try:
            response = SESSION.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if response.status_code == 429:
                wait = min(2 ** (attempt + 1), 8)
                time.sleep(wait)
                continue
            response.raise_for_status()
            return response.json()
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
    if last_error:
        raise last_error
    raise RuntimeError(f"Toobit request failed: {url}")


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (TypeError, ValueError):
        return default


def discover_contracts() -> list[dict]:
    payload = _get(EXCHANGE_INFO)
    contracts = payload.get("contracts", []) if isinstance(payload, dict) else []
    result = []
    for contract in contracts:
        symbol = str(contract.get("symbol", ""))
        if (
            contract.get("status") == "TRADING"
            and contract.get("quoteAsset") == "USDT"
            and symbol.endswith("-SWAP-USDT")
            and contract.get("inverse") in (False, "false", 0, None)
        ):
            result.append(contract)
    return result


def _ticker_map() -> dict[str, dict]:
    payload = _get(TICKER_24H)
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected Toobit 24h ticker response")
    return {str(row.get("s")): row for row in payload if row.get("s")}


def _book_map() -> dict[str, dict]:
    payload = _get(BOOK_TICKER)
    if not isinstance(payload, list):
        raise RuntimeError("Unexpected Toobit book ticker response")
    return {str(row.get("s")): row for row in payload if row.get("s")}


def _closed_candles(symbol: str, interval: str) -> list[dict]:
    rows = _get(KLINES, {"symbol": symbol, "interval": interval, "limit": CANDLE_LIMIT})
    if not isinstance(rows, list):
        raise RuntimeError(f"Unexpected kline response for {symbol} {interval}")
    now_ms = SERVER_TIME_MS if SERVER_TIME_MS is not None else int(_get(f"{BASE}/api/v1/time")["serverTime"])
    duration_ms = 15 * 60_000 if interval == "15m" else 60 * 60_000
    bars = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 8:
            continue
        open_ms = int(row[0])
        close_ms = int(row[6])
        if close_ms >= now_ms:
            continue
        bars.append({
            "open_time": open_ms,
            "close_time": close_ms,
            "open": _float(row[1]),
            "high": _float(row[2]),
            "low": _float(row[3]),
            "close": _float(row[4]),
            "volume": _float(row[5]),
            "quote_volume": _float(row[7]),
        })
    bars.sort(key=lambda x: x["open_time"])
    if len(bars) < 105:
        raise RuntimeError(f"Not enough closed candles for {symbol} {interval}: {len(bars)}")
    max_age_ms = duration_ms * 2
    if now_ms - bars[-1]["close_time"] > max_age_ms:
        raise RuntimeError(f"Stale candles for {symbol} {interval}")
    return bars


def _ema(values: list[float], period: int) -> float:
    if len(values) < period:
        raise ValueError("Not enough values for EMA")
    alpha = 2.0 / (period + 1)
    value = sum(values[:period]) / period
    for item in values[period:]:
        value = alpha * item + (1 - alpha) * value
    return value


def _rsi(values: list[float], period: int = 14) -> float:
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    sample = changes[-period:]
    gains = sum(max(x, 0.0) for x in sample) / period
    losses = sum(max(-x, 0.0) for x in sample) / period
    if losses == 0:
        return 100.0 if gains else 50.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def _atr_pct(bars: list[dict], period: int = 14) -> float:
    ranges = []
    for i in range(1, len(bars)):
        current = bars[i]
        previous_close = bars[i - 1]["close"]
        ranges.append(max(
            current["high"] - current["low"],
            abs(current["high"] - previous_close),
            abs(current["low"] - previous_close),
        ))
    atr = sum(ranges[-period:]) / period
    close = bars[-1]["close"]
    return (atr / close * 100.0) if close > 0 else 0.0


def _analyze(symbol: str, ticker: dict, book: dict) -> dict | None:
    bid, ask = _float(book.get("b")), _float(book.get("a"))
    if bid <= 0 or ask <= bid:
        return None
    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid * 100.0
    if spread_pct > MAX_SPREAD_PCT:
        return None

    bars15 = _closed_candles(symbol, "15m")
    bars1h = _closed_candles(symbol, "1h")
    c15 = [x["close"] for x in bars15]
    c1h = [x["close"] for x in bars1h]
    e20_15, e50_15 = _ema(c15, 20), _ema(c15, 50)
    e20_1h, e50_1h = _ema(c1h, 20), _ema(c1h, 50)
    rsi = _rsi(c15)
    atr_pct = _atr_pct(bars15)
    if atr_pct < 0.08 or atr_pct > 5.0:
        return None

    direction = None
    if e20_1h > e50_1h and e20_15 > e50_15 and 48 <= rsi <= 68:
        direction = "LONG"
    elif e20_1h < e50_1h and e20_15 < e50_15 and 32 <= rsi <= 52:
        direction = "SHORT"
    if not direction:
        return None

    quote_volume = _float(ticker.get("qv"))
    change_pct = _float(ticker.get("pcp"))
    candle_volume = [x["quote_volume"] for x in bars15[-21:-1]]
    avg_volume = sum(candle_volume) / len(candle_volume) if candle_volume else 0.0
    current_volume_ratio = bars15[-1]["quote_volume"] / avg_volume if avg_volume else 0.0
    # Score rewards liquid, aligned, active markets and penalizes wide spreads.
    trend_strength = abs(e20_15 - e50_15) / max(c15[-1], 1e-12) * 10000
    momentum_fit = max(0.0, 1.0 - abs(rsi - (58 if direction == "LONG" else 42)) / 20.0)
    volume_fit = min(current_volume_ratio, 3.0) / 3.0
    liquidity_fit = min(quote_volume / 20_000_000.0, 1.0)
    spread_penalty = min(spread_pct / MAX_SPREAD_PCT, 1.0)
    score = (
        30.0 * min(trend_strength / 20.0, 1.0)
        + 25.0 * momentum_fit
        + 20.0 * volume_fit
        + 25.0 * liquidity_fit
        - 15.0 * spread_penalty
    )
    entry = c15[-1]
    stop_distance = max(entry * atr_pct / 100.0 * 1.5, entry * 0.002)
    stop = entry - stop_distance if direction == "LONG" else entry + stop_distance
    target = entry + 2.0 * stop_distance if direction == "LONG" else entry - 2.0 * stop_distance
    return {
        "symbol": symbol,
        "direction": direction,
        "score": round(score, 2),
        "entry": entry,
        "stop": stop,
        "target": target,
        "rsi": round(rsi, 1),
        "atr_pct": round(atr_pct, 3),
        "spread_pct": round(spread_pct, 4),
        "quote_volume_24h": quote_volume,
        "volume_ratio": round(current_volume_ratio, 2),
        "change_24h_pct": change_pct,
        "leverage_cap": 3,
        "last_candle_time": bars15[-1]["close_time"],
    }


def scan_market() -> dict:
    """Return ranked candidates and one best eligible setup, or no-trade."""
    global SERVER_TIME_MS
    SERVER_TIME_MS = int(_get(f"{BASE}/api/v1/time")["serverTime"])
    contracts = discover_contracts()
    tickers = _ticker_map()
    books = _book_map()

    liquid = []
    for contract in contracts:
        symbol = contract["symbol"]
        ticker = tickers.get(symbol)
        book = books.get(symbol)
        if not ticker or not book:
            continue
        qv = _float(ticker.get("qv"))
        if qv < MIN_QUOTE_VOLUME_24H:
            continue
        bid, ask = _float(book.get("b")), _float(book.get("a"))
        if bid <= 0 or ask <= bid:
            continue
        spread = (ask - bid) / ((ask + bid) / 2.0) * 100.0
        if spread <= MAX_SPREAD_PCT:
            liquid.append((qv, symbol, ticker, book))

    liquid.sort(key=lambda x: x[0], reverse=True)
    ranked = []
    errors = []
    for _, symbol, ticker, book in liquid[:MAX_CANDLE_CANDIDATES]:
        try:
            candidate = _analyze(symbol, ticker, book)
            if candidate:
                ranked.append(candidate)
        except Exception as exc:
            errors.append(f"{symbol}: {type(exc).__name__}: {exc}")

    ranked.sort(key=lambda x: x["score"], reverse=True)
    # Strict minimum score: abstain rather than force a weak trade.
    best = ranked[0] if ranked and ranked[0]["score"] >= 55.0 else None
    return {
        "contracts_found": len(contracts),
        "liquid_contracts": len(liquid),
        "analyzed_contracts": min(len(liquid), MAX_CANDLE_CANDIDATES) - len(errors),
        "candidates": ranked[:10],
        "best": best,
        "errors": errors[:10],
    }
