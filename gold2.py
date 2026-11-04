import os
import json
import base64
import requests
from datetime import datetime, timezone

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = "601146458"
BASE = "https://biquote.io/api/XAUUSD/ohlc"
TOOBIT_KLINES = "https://api.toobit.com/quote/v1/klines"
CRYPTO_ASSETS = {
    "XRP-SWAP-USDT": "ریپل",
    "DOGE-SWAP-USDT": "دوج‌کوین",
    "TRX-SWAP-USDT": "ترون",
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


def get_crypto_candles(symbol, interval):
    if interval not in ("15m", "1h"):
        raise ValueError(f"بازه زمانی پشتیبانی نمی‌شود: {interval}")

    interval_minutes = 15 if interval == "15m" else 60
    # Use Toobit's own clock so candle freshness checks are not affected by
    # clock skew on the GitHub Actions runner.
    time_response = requests.get("https://api.toobit.com/api/v1/time", timeout=15)
    time_response.raise_for_status()
    server_time = time_response.json()
    if not isinstance(server_time, dict) or "serverTime" not in server_time:
        raise RuntimeError(f"زمان سرور Toobit نامعتبر است: {server_time}")
    now_ms = int(server_time["serverTime"])
    start_ms = now_ms - 300 * interval_minutes * 60 * 1000

    response = requests.get(
        TOOBIT_KLINES,
        params={
            "symbol": symbol,
            "interval": interval,
            "startTime": start_ms,
            "endTime": now_ms,
            "limit": 300,
        },
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list):
        raise RuntimeError(f"پاسخ نامعتبر Toobit برای {symbol}: {rows}")

    # Toobit identifies each candle by its open timestamp. Derive its
    # scheduled close from openTime + interval instead of trusting closeTime,
    # which can be inconsistent across contract market responses.
    interval_ms = interval_minutes * 60 * 1000
    valid_rows = [row for row in rows if isinstance(row, (list, tuple)) and len(row) >= 5]
    valid_rows.sort(key=lambda row: int(row[0]))
    closed = [
        row for row in valid_rows
        if int(row[0]) + interval_ms <= now_ms
    ]
    if len(closed) < 110:
        raise RuntimeError(f"کندل کافی از Toobit برای {symbol} در بازه {interval} دریافت نشد؛ تعداد: {len(closed)}")

    latest_open_raw = closed[-1][0]
    latest_open_ms = int(latest_open_raw)
    latest_close_ms = latest_open_ms + interval_ms
    latest_age = (now_ms - latest_close_ms) / 60000
    print(
        f"TOOBIT_DIAG symbol={symbol} interval={interval} rows={len(rows)} "
        f"closed_rows={len(closed)} now_ms={now_ms} "
        f"now_utc={datetime.fromtimestamp(now_ms / 1000, timezone.utc).isoformat()} "
        f"last_open_raw={latest_open_raw} "
        f"last_open_utc={datetime.fromtimestamp(latest_open_ms / 1000, timezone.utc).isoformat()} "
        f"derived_close_utc={datetime.fromtimestamp(latest_close_ms / 1000, timezone.utc).isoformat()} "
        f"age_minutes={latest_age:.2f}"
    )
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


GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
STATE_PATH = "gold_signal_state.json"
GITHUB_CONTENTS_API = "https://api.github.com/repos/younes63nasiri-droid/gold-signal-bot/contents/" + STATE_PATH


def load_gold_state():
    if not GITHUB_TOKEN:
        raise RuntimeError("توکن دسترسی ذخیره وضعیت گیت‌هاب تنظیم نشده است.")
    response = requests.get(
        GITHUB_CONTENTS_API,
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
        timeout=20,
    )
    if response.status_code == 404:
        return None, None
    response.raise_for_status()
    payload = response.json()
    state = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
    if state.get("status") != "pending":
        return None, payload["sha"]
    return state, payload["sha"]


def save_gold_state(state, sha=None):
    if not GITHUB_TOKEN:
        raise RuntimeError("توکن دسترسی ذخیره وضعیت گیت‌هاب تنظیم نشده است.")
    content = base64.b64encode(json.dumps(state, ensure_ascii=False).encode("utf-8")).decode("ascii")
    body = {"message": "Update gold signal tracking state", "content": content}
    if sha:
        body["sha"] = sha
    response = requests.put(
        GITHUB_CONTENTS_API,
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"},
        json=body,
        timeout=20,
    )
    response.raise_for_status()


def dynamic_gold_leverage(entry, stop_loss):
    stop_pct = abs(entry - stop_loss) / entry
    if stop_pct <= 0:
        return 1
    # Aim to keep the stop-distance loss near 1% of margin; cap leverage at 10x.
    return max(1, min(10, round(0.01 / stop_pct, 1)))


def expire_previous_gold_signal(candles):
    state, sha = load_gold_state()
    if not state:
        return
    previous_candle = state.get("candle_id")
    later_bars = [bar for bar in candles if bar["openTime"] > previous_candle]
    if not later_bars:
        return
    first = later_bars[0]
    entry = float(state["entry"])
    touched = first["low"] <= entry <= first["high"]
    if not touched:
        send_telegram(
            "⛔ لغو سیگنال طلا | XAU/USD\n\n"
            f"قیمت ورود {format_price(entry)} در مهلت ۱۵ دقیقه‌ای لمس نشد.\n"
            "سیگنال منقضی شد؛ وارد معامله نشوید."
        )
    else:
        print("Gold entry price was reached during the validity candle.")
    # Clear state after the first completed candle following the signal.
    save_gold_state({"status": "clear"}, sha)


def save_pending_gold_signal(entry, direction, candle_id):
    state, sha = load_gold_state()
    save_gold_state(
        {"status": "pending", "entry": entry, "direction": direction, "candle_id": candle_id},
        sha,
    )


def analyze():
    candles_15m = get_candles("15m")
    candles_1h = get_candles("1h")
    expire_previous_gold_signal(candles_15m)
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
    leverage = dynamic_gold_leverage(price, stop_loss)
    message = (
        "🥇 سیگنال طلا | XAU/USD\n\n"
        f"📍 جهت معامله: {'خرید' if direction == 'BUY' else 'فروش'}\n"
        f"💰 قیمت ورود: {format_price(price)}\n"
        f"🎯 حد سود اول: {format_price(target_1)}\n"
        f"🛑 حد ضرر: {format_price(stop_loss)}\n"
        f"⚖️ اهرم پیشنهادی متغیر: {leverage}x (حداکثر ۱۰x)\n"
        "⏳ اعتبار ورود: ۱۵ دقیقه؛ اگر قیمت ورود نرسید، سیگنال لغو می‌شود.\n\n"
        f"⏱ زمان سیگنال: {candle_id}\n"
        "⚠️ سیگنال آزمایشی است؛ قبل از معامله بررسی شود."
    )
    send_telegram(message)
    save_pending_gold_signal(price, direction, candle_id)
    print("Signal message sent.")



CRYPTO_STATE_PATH = "crypto_signal_state.json"
CRYPTO_STATE_API = "https://api.github.com/repos/younes63nasiri-droid/gold-signal-bot/contents/" + CRYPTO_STATE_PATH


def load_crypto_states():
    if not GITHUB_TOKEN:
        raise RuntimeError("توکن دسترسی ذخیره وضعیت گیت‌هاب تنظیم نشده است.")
    response = requests.get(CRYPTO_STATE_API, headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}, timeout=20)
    if response.status_code == 404:
        return {}, None
    response.raise_for_status()
    payload = response.json()
    return json.loads(base64.b64decode(payload["content"]).decode("utf-8")), payload["sha"]


def save_crypto_states(states, sha=None):
    content = base64.b64encode(json.dumps(states, ensure_ascii=False).encode("utf-8")).decode("ascii")
    body = {"message": "Update crypto signal tracking state", "content": content}
    if sha:
        body["sha"] = sha
    response = requests.put(CRYPTO_STATE_API, headers={"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}, json=body, timeout=20)
    response.raise_for_status()


def expire_previous_crypto_signal(symbol, persian_name, candles):
    states, sha = load_crypto_states()
    state = states.get(symbol)
    if not state or state.get("status") != "pending":
        return
    later_bars = [bar for bar in candles if bar["openTime"] > state["candle_id"]]
    if not later_bars:
        return
    first = later_bars[0]
    entry = float(state["entry"])
    if not (first["low"] <= entry <= first["high"]):
        send_telegram(f"⛔ لغو سیگنال {persian_name} | {symbol}\\n\\nقیمت ورود {format_price(entry)} در مهلت ۱۵ دقیقه‌ای لمس نشد.\\nسیگنال منقضی شد؛ وارد معامله نشوید.")
    states[symbol] = {"status": "clear"}
    save_crypto_states(states, sha)


def save_pending_crypto_signal(symbol, entry, direction, candle_id):
    states, sha = load_crypto_states()
    states[symbol] = {"status": "pending", "entry": entry, "direction": direction, "candle_id": candle_id}
    save_crypto_states(states, sha)

def analyze_crypto(symbol, persian_name):
    candles_15m = get_crypto_candles(symbol, "15m")
    candles_1h = get_crypto_candles(symbol, "1h")
    expire_previous_crypto_signal(symbol, persian_name, candles_15m)
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
    else:
        risk = 1.5 * current_atr
        if direction == "BUY":
            stop_loss, target_1, target_2 = price - risk, price + 2 * risk, price + 3 * risk
        else:
            stop_loss, target_1, target_2 = price + risk, price - 2 * risk, price - 3 * risk
        leverage = dynamic_gold_leverage(price, stop_loss)
        message = (
            f"🪙 سیگنال {persian_name} | {symbol}\\n\\n"
            f"📍 جهت معامله: {'خرید' if direction == 'BUY' else 'فروش'}\\n"
            f"💰 قیمت ورود: {format_price(price)} USDT\\n"
            f"🎯 حد سود اول: {format_price(target_1)} USDT\\n"
            f"🛑 حد ضرر: {format_price(stop_loss)} USDT\\n"
            f"⚖️ اهرم پیشنهادی متغیر: {leverage}x (حداکثر ۱۰x)\\n"
            "⏳ اعتبار ورود: ۱۵ دقیقه؛ اگر قیمت ورود نرسید، سیگنال لغو می‌شود.\\n\\n"
            f"⏱ زمان سیگنال: {candle_id}\\n"
            "⚠️ سیگنال آزمایشی است؛ قبل از معامله بررسی شود."
        )
    send_telegram(message)
    if direction is not None and current_atr > 0:
        save_pending_crypto_signal(symbol, price, direction, candle_id)
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
