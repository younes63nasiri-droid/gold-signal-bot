import os
import requests
from datetime import datetime, timezone

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = "601146458"
BASE = "https://biquote.io/api/XAUUSD/ohlc"


def get_candles(interval):
    response = requests.get(BASE, params={"interval": interval, "limit": 1000}, timeout=30)
    response.raise_for_status()
    bars = response.json()["bars"]
    bars = [bar for bar in bars if not bar.get("isOpen", False)]
    bars.sort(key=lambda bar: bar["openTime"])
    if len(bars) < 110:
        raise RuntimeError(f"کندل کافی نیست: {interval}")
    latest_open = datetime.fromisoformat(bars[-1]["openTime"].replace("Z", "+00:00"))
    candle_minutes = 15 if interval == "15m" else 60
    latest_close = latest_open.timestamp() + candle_minutes * 60
    age_minutes = (datetime.now(timezone.utc).timestamp() - latest_close) / 60
    print(f"سن آخرین کندل بسته‌شده {interval}: {age_minutes:.1f} دقیقه")
    max_age = 35 if interval == "15m" else 75
    if age_minutes < -5 or age_minutes > max_age:
        raise RuntimeError(f"اطلاعات {interval} واقعاً قدیمی است")
    return bars


def ema(values, period):
    alpha = 2 / (period + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (1 - alpha) * result
    return result


def rsi(values, period=14):
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
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
    for i in range(1, len(bars)):
        high, low = bars[i]["high"], bars[i]["low"]
        previous_close = bars[i - 1]["close"]
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    return sum(true_ranges[-period:]) / period


def send_telegram(message):
    if not TOKEN:
        raise RuntimeError("Secret با نام TELEGRAM_BOT_TOKEN تنظیم نشده است.")
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    response = requests.post(url, data={"chat_id": CHAT_ID, "text": message}, timeout=20)
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def analyze():
    candles_15m = get_candles("15m")
    candles_1h = get_candles("1h")
    closes_15m = [bar["close"] for bar in candles_15m]
    closes_1h = [bar["close"] for bar in candles_1h]
    fast_1h, slow_1h = ema(closes_1h, 50), ema(closes_1h, 100)
    fast_15m, slow_15m = ema(closes_15m, 50), ema(closes_15m, 100)
    current_rsi = rsi(closes_15m)
    current_atr = atr(candles_15m)
    price = closes_15m[-1]
    candle_id = candles_15m[-1]["openTime"]
    direction = None
    if fast_1h > slow_1h and fast_15m > slow_15m and 50 < current_rsi < 65:
        direction = "BUY"
    elif fast_1h < slow_1h and fast_15m < slow_15m and 35 < current_rsi < 50:
        direction = "SELL"
    print("Price:", price, "RSI:", round(current_rsi, 2), "ATR:", round(current_atr, 3))
    print("Signal:", direction or "NO TRADE")
    if direction is None or current_atr <= 0:
        return
    risk = 1.5 * current_atr
    if direction == "BUY":
        stop_loss, target_1, target_2 = price - risk, price + 2 * risk, price + 3 * risk
    else:
        stop_loss, target_1, target_2 = price + risk, price - 2 * risk, price - 3 * risk
    message = (
        "🥇 GOLD XAU/USD\n\n"
        f"📊 Signal: {direction}\n"
        f"💰 Entry: {price:.2f}\n"
        f"🛑 Stop Loss: {stop_loss:.2f}\n"
        f"🎯 Target 1: {target_1:.2f}\n"
        f"🎯 Target 2: {target_2:.2f}\n"
        f"📈 RSI: {current_rsi:.1f}\n"
        f"⏱ Time: {candle_id}\n\n"
        "⚠️ سیگنال آزمایشی؛ سودآوری تأیید نشده است."
    )
    send_telegram(message)
    print("Signal message sent.")


if __name__ == "__main__":
    print("شروع بررسی بازار طلا...")
    analyze()
