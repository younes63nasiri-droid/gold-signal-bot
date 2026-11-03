import os
import requests
from datetime import datetime, timezone

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = "601146458"
BASE = "https://biquote.io/api/XAUUSD/ohlc"
BYBIT_KLINES = "https://api.bybit.com/v5/market/kline"
CRYPTO_ASSETS = {
    "XRPUSDT": "ریپل",
    "DOGEUSDT": "دوج‌کوین",
    "TRXUSDT": "ترون",
}


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


dedef get_crypto_candles(symbol, interval):
    bybit_interval = {"15m": "15", "1h": "60"}.get(interval)
    if bybit_interval is None:
        raise ValueError(f"بازه زمانی پشتیبانی نمی‌شود: {interval}")

    response = requests.get(
        BYBIT_KLINES,
        params={
            "category": "linear",
            "symbol": symbol,
            "interval": bybit_interval,
            "limit": 300,
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"خطای Bybit برای {symbol}: {payload.get('retMsg', 'unknown error')}")

    rows = payload.get("result", {}).get("list", [])
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    candle_ms = int(bybit_interval) * 60 * 1000
    # Bybit returns candles newest-first: start, open, high, low, close, volume, turnover.
    closed = [row for row in rows if int(row[0]) + candle_ms <= now_ms]
    closed.sort(key=lambda row: int(row[0]))
    if len(closed) < 110:
        raise RuntimeError(f"کندل کافی برای {symbol} در بازه {interval} نیست")

    latest_close_ms = int(closed[-1][0]) + candle_ms
    latest_age = (now_ms - latest_close_ms) / 60000
    print(f"{symbol} {interval}: آخرین کندل بسته‌شده {latest_age:.1f} دقیقه قبل")
    max_age = 35 if interval == "15m" else 75
    if latest_age < 0 or latest_age > max_age:
        raise RuntimeError(f"داده {symbol} در بازه {interval} قدیمی است")

    return [
        {
            "openTime": datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc).isoformat(),
            "high": float(row[2]),
            "low": float(row[3]),
            "close": float(row[4]),
        }
        for row in closed
    ]

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


def format_price(value):
    """نمایش قیمت با تعداد رقم اعشار متناسب با ارزش نماد."""
    if abs(value) >= 100:
        return f"{value:.2f}"
    if abs(value) >= 1:
        return f"{value:.4f}"
    return f"{value:.6f}"


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
        status_message = (
            "🥇 گزارش ربات طلا | XAU/USD\n\n"
            "🟡 فعلاً موقعیت معاملاتی معتبری وجود ندارد.\n"
            f"💰 قیمت بررسی‌شده: {price:.2f}\n"
            f"📈 RSI: {current_rsi:.1f}\n"
            f"📊 ATR: {current_atr:.3f}\n"
            f"⏱ زمان کندل: {candle_id}\n\n"
            "ربات فعال است و در بررسی بعدی دوباره بازار را تحلیل می‌کند.\n"
            "⚠️ این گزارش توصیه مالی نیست."
        )
        send_telegram(status_message)
        print("No-trade status message sent.")
        return
    risk = 1.5 * current_atr
    if direction == "BUY":
        stop_loss, target_1, target_2 = price - risk, price + 2 * risk, price + 3 * risk
    else:
        stop_loss, target_1, target_2 = price + risk, price - 2 * risk, price - 3 * risk
    message = (
        "🥇 سیگنال طلا | XAU/USD\n\n"
        f"📍 جهت معامله: {'خرید' if direction == 'BUY' else 'فروش'}\n"
        f"💰 قیمت ورود: {format_price(price)}\n"
        f"🎯 حد سود اول: {format_price(target_1)}\n"
        f"🛑 حد ضرر: {format_price(stop_loss)}\n\n"
        f"⏱ زمان سیگنال: {candle_id}\n"
        "⚠️ سیگنال آزمایشی است؛ قبل از معامله بررسی شود."
    )
    send_telegram(message)
    print("Signal message sent.")


def analyze_crypto(symbol, persian_name):
    candles_15m = get_crypto_candles(symbol, "15m")
    candles_1h = get_crypto_candles(symbol, "1h")
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

    print(f"{symbol}: price={price}, RSI={current_rsi:.2f}, ATR={current_atr:.8f}, signal={direction or 'NO TRADE'}")
    if direction is None or current_atr <= 0:
        message = (
            f"🪙 گزارش ارز دیجیتال | {persian_name} ({symbol})\n\n"
            "🟡 فعلاً موقعیت معاملاتی معتبری وجود ندارد.\n"
            f"💰 قیمت بررسی‌شده: {price:.8f} USDT\n"
            f"📈 RSI: {current_rsi:.1f}\n"
            f"📊 ATR: {current_atr:.8f}\n"
            f"⏱ زمان کندل: {candle_id}\n\n"
            "ربات در بررسی بعدی دوباره بازار را تحلیل می‌کند.\n"
            "⚠️ این گزارش توصیه مالی نیست."
        )
    else:
        risk = 1.5 * current_atr
        if direction == "BUY":
            stop_loss, target_1, target_2 = price - risk, price + 2 * risk, price + 3 * risk
        else:
            stop_loss, target_1, target_2 = price + risk, price - 2 * risk, price - 3 * risk
        message = (
            f"🪙 سیگنال {persian_name} | {symbol}\n\n"
            f"📍 جهت معامله: {'خرید' if direction == 'BUY' else 'فروش'}\n"
            f"💰 قیمت ورود: {format_price(price)} USDT\n"
            f"🎯 حد سود اول: {format_price(target_1)} USDT\n"
            f"🛑 حد ضرر: {format_price(stop_loss)} USDT\n\n"
            f"⏱ زمان سیگنال: {candle_id}\n"
            "⚠️ سیگنال آزمایشی است؛ قبل از معامله بررسی شود."
        )
    send_telegram(message)
    print(f"Telegram crypto report sent for {symbol}.")


if __name__ == "__main__":
    print("شروع بررسی بازار طلا...")
    analyze()
    print("شروع بررسی ارزهای دیجیتال...")
    for crypto_symbol, crypto_name in CRYPTO_ASSETS.items():
        try:
            analyze_crypto(crypto_symbol, crypto_name)
        except Exception as error:
            print(f"خطا در بررسی {crypto_symbol}: {error}")
            send_telegram(
                f"⚠️ خطا در بررسی {crypto_name} ({crypto_symbol})\n"
                f"{type(error).__name__}: {error}\n"
                "در بررسی بعدی دوباره تلاش می‌کنم."
            )
