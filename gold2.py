import os
import requests
from datetime import datetime, timezone

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = "601146458"
BASE = "https://biquote.io/api/XAUUSD/ohlc"


def get_candles(interval):
    response = requests.get(
        BASE,
        params={"interval": interval, "limit": 1000},
        timeout=30,
    )
    response.raise_for_status()
    bars = response.json()["bars"]
    bars = [bar for bar in bars if not bar.get("isOpen", False)]
    bars.sort(key=lambda bar: bar["openTime"])

    if len(bars) < 110:
        raise RuntimeError(f"کندل کافی نیست: {interval}")

    latest = datetime.fromisoformat(
        bars[-1]["openTime"].replace("Z", "+00:00")
    )
    age_minutes = (
        datetime.now(timezone.utc) - latest
    ).total_seconds() / 60

    print(f"سن آخرین کندل {interval}: {age_minutes:.1f} دقیقه")
    max_age = 35 if interval == "15m" else 90
    if age_minutes < -5 or age_minutes > max_age:
        raise RuntimeError(f"اطلاعات {interval} قدیمی است")

    return bars


def ema(values, period):
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def rsi(values, period=14):
    changes = [
        values[index] - values[index - 1]
        for index in range(1, len(values))
    ]
    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100.0
    if avg_gain == 0:
        return 0.0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(bars, period=14):
    true_ranges = []
    for index in range(1, len(bars)):
        high = bars[index]["high"]
        low = bars[index]["low"]
        previous_close = bars[index - 1]["close"]
        true_ranges.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        )
    return sum(true_ranges[-period:]) / period


def send_telegram(message):
    if not TOKEN:
        raise RuntimeError(
            "Secret با نام TELEGRAM_BOT_TOKEN تنظیم نشده است."
        )

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    response = requests.post(
        url,
        data={"chat_id": CHAT_ID, "text": message},
        timeout=20,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def analyze():
    candles_15m = get_candles("15m")
    candles_1h = get_candles("1h")

    closes_15m = [bar["close"] for bar in candles_15m]
    closes_1h = [bar["close"] for bar in candles_1h]

    fast_1h = ema(closes_1h, 50)
    slow_1h = ema(closes_1h, 100)
    fast_15m = ema(closes_15m, 50)
    slow_15m = ema(closes_15m, 100)

    current_rsi = rsi(closes_15m)
    current_atr = atr(candles_15m)
    price = closes_15m[-1]
    candle_id = candles_15m[-1]["openTime"]

    direction = None
    if (
        fast_1h > slow_1h
        and fast_15m > slow_15m
        and 50 < current_rsi < 65
    ):
        direction = "BUY"
    elif (
        fast_1h < slow_1h
        and fast_15
