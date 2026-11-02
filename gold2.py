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
        timeout=30
    )
    response.raise_for_status()
    bars = response.json()["bars"]
    bars = [b for b in bars if not b.get("isOpen", False)]
    bars.sort(key=lambda x: x["openTime"])

    if len(bars) < 110:
        raise Exception(f"کندل کافی نیست: {interval}")

    latest = datetime.fromisoformat(
        bars[-1]["openTime"].replace("Z", "+00:00")
    )
    age = (
        datetime.now(timezone.utc) - latest
    ).total_seconds() / 60

    print(f"سن آخرین کندل {interval}: {age:.1f} دقیقه")
    max_age = 35 if interval == "15m" else 90
    if age < -5 or age > max_age:
        raise Exception(f"اطلاعات {interval} قدیمی است")

    return bars


def ema(values, period):
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def rsi(values, period=14):
    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period

    if avg_loss == 0:
        return 100
    if avg_gain == 0:
        return 0
    return 100 - 100 / (1 + avg_gain / avg_loss)


def atr(bars, period=14):
    ranges = []
    for i in range(1, len(bars)):
        high = bars[i]["high"]
        low = bars[i]["low"]
        previous = bars[i - 1]["close"]
        ranges.append(max(
            high - low,
            abs(high - previous),
            abs(low - previous)
        ))
    return sum(ranges[-period:]) / period


def send_telegram(message):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN در Secrets تنظیم نشده است.")

    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    response = requests.post(
        url,
        data={"chat_id": CHAT_ID, "text": message},
        timeout=20
    )
    response.raise_for_status()


def analyze():
    candles15 = get_candles("15m")
    candles1h = get_candles("1h")

    close15 = [x["close"] for x in
