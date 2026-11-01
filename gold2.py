import requests
import time
from datetime import datetime, timezone

TOKEN = input("9139830348:AAGpLr9xUzGLGDglNOaCUH36aAmosfcGCYg").strip()
CHAT_ID = "601146458"

BASE = "https://biquote.io/api/XAUUSD/ohlc"

last_signal = None


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
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message
        },
        timeout=20
    )

    response.raise_for_status()


def analyze():
    global last_signal

    candles15 = get_candles("15m")
    candles1h = get_candles("1h")

    close15 = [x["close"] for x in candles15]
    close1h = [x["close"] for x in candles1h]

    fast1h = ema(close1h, 50)
    slow1h = ema(close1h, 100)

    fast15 = ema(close15, 50)
    slow15 = ema(close15, 100)

    current_rsi = rsi(close15)
    current_atr = atr(candles15)

    price = close15[-1]
    candle_id = candles15[-1]["openTime"]

    direction = None

    if (
        fast1h > slow1h
        and fast15 > slow15
        and 50 < current_rsi < 65
    ):
        direction = "BUY"

    elif (
        fast1h < slow1h
        and fast15 < slow15
        and 35 < current_rsi < 50
    ):
        direction = "SELL"

    print("قیمت:", price)
    print("RSI:", round(current_rsi, 2))
    print("ATR:", round(current_atr, 3))
    print("سیگنال:", direction or "NO TRADE")

    if direction is None:
        return

    if candle_id == last_signal:
        print("سیگنال تکراری؛ ارسال نشد.")
        return

    if current_atr <= 0:
        return

    risk = 1.5 * current_atr

    if direction == "BUY":
        stop = price - risk
        target1 = price + 2 * risk
        target2 = price + 3 * risk
    else:
        stop = price + risk
        target1 = price - 2 * risk
        target2 = price - 3 * risk

    message = f"""
🥇 GOLD XAU/USD

📊 Signal: {direction}

💰 Entry: {price:.2f}

🛑 Stop Loss: {stop:.2f}

🎯 Target 1: {target1:.2f}

🎯 Target 2: {target2:.2f}

📈 RSI: {current_rsi:.1f}

⏱ Time: {candle_id}

⚠️ سیگنال آزمایشی؛ سودآوری تأیید نشده است.
"""

    send_telegram(message)
    last_signal = candle_id

    print("پیام سیگنال ارسال شد.")


if __name__ == "__main__":

    if not TOKEN:
        print("توکن وارد نشده است.")

    else:
        print("ربات طلا فعال شد.")

        try:
            send_telegram(
                "✅ ربات طلا فعال شد و آماده بررسی بازار است."
            )
            print("پیام آزمایشی تلگرام ارسال شد.")

        except Exception as error:
            print("خطای ارسال پیام تلگرام:", error)

        while True:
            try:
                print("\nدر حال بررسی بازار...")
                analyze()

            except Exception as error:
                print("ERROR:", error)

            print("بررسی بعدی ۱۵ دقیقه دیگر.")
            time.sleep(900)