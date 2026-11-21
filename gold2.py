import os
import hmac
import hashlib
from urllib.parse import urlencode
import json
import base64
import tempfile
from pathlib import Path
import requests
import market_scanner as market_scanner
from risk_manager import calculate_fixed_margin_plan
from paper_ledger import PaperLedger
from datetime import datetime, timezone

TOKEN = (os.environ.get("GOLD_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")).strip()
CHAT_ID = "601146458"
BASE = "https://biquote.io/api/XAUUSD/ohlc"
KRAKEN_OHLC = "https://api.kraken.com/0/public/OHLC"
GOLD_PROXY_PAIR = "PAXGUSD"
TOOBIT_KLINES = "https://api.toobit.com/quote/v1/klines"
ORDER_MARGIN_USDT = float(os.environ.get("ORDER_MARGIN_USDT") or "0.50")


def get_candles(interval):
    candle_minutes = 15 if interval == "15m" else 60
    max_age = 35 if interval == "15m" else 75
    now = datetime.now(timezone.utc).timestamp()

    # Primary source: BiQuote spot XAU/USD
    try:
        response = requests.get(BASE, params={"interval": interval, "limit": 1000}, timeout=20)
        response.raise_for_status()
        bars = response.json()["bars"]
        bars = [bar for bar in bars if not bar.get("isOpen", False)]
        bars.sort(key=lambda bar: bar["openTime"])
        if len(bars) >= 110:
            latest_open = datetime.fromisoformat(bars[-1]["openTime"].replace("Z", "+00:00"))
            age_minutes = (now - latest_open.timestamp() - candle_minutes * 60) / 60
            print(f"BiQuote {interval}: سن آخرین کندل بسته‌شده {age_minutes:.1f} دقیقه")
            if -5 <= age_minutes <= max_age:
                return bars
        print(f"BiQuote {interval} قدیمی یا ناقص است؛ تلاش با منبع جایگزین.")
    except Exception as error:
        print(f"BiQuote {interval} خطا داد: {error}; تلاش با منبع جایگزین.")

    # Fallback: Kraken PAXG/USD OHLC. PAXG is a gold-backed token proxy,
    # not identical to spot XAU/USD. Kraken returns the current candle last; exclude it.
    kraken_interval = 15 if interval == "15m" else 60
    response = requests.get(
        KRAKEN_OHLC,
        params={"pair": GOLD_PROXY_PAIR, "interval": kraken_interval},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    errors = payload.get("error") or []
    if errors:
        raise RuntimeError(f"خطای Kraken برای {GOLD_PROXY_PAIR}: {errors}")
    result = payload.get("result") or {}
    pair_key = next((key for key in result if key != "last"), None)
    if not pair_key:
        raise RuntimeError(f"Kraken برای {GOLD_PROXY_PAIR} داده‌ای برنگرداند.")
    rows = result[pair_key]
    bars = []
    interval_seconds = candle_minutes * 60
    for row in rows:
        try:
            open_ts = int(row[0])
            if open_ts + interval_seconds > now:
                continue
            bars.append({
                "openTime": datetime.fromtimestamp(open_ts, timezone.utc).isoformat(),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
            })
        except (IndexError, TypeError, ValueError):
            continue

    bars.sort(key=lambda bar: bar["openTime"])
    if len(bars) < 110:
        raise RuntimeError(f"کندل کافی از Kraken برای {GOLD_PROXY_PAIR} در {interval} دریافت نشد؛ تعداد: {len(bars)}")
    latest_open = datetime.fromisoformat(bars[-1]["openTime"])
    age_minutes = (now - latest_open.timestamp() - interval_seconds) / 60
    print(f"Kraken {GOLD_PROXY_PAIR} fallback {interval}: {len(bars)} کندل؛ آخرین کندل بسته‌شده {age_minutes:.1f} دقیقه قبل")
    if age_minutes < -5 or age_minutes > max_age:
        raise RuntimeError(f"داده Kraken برای {interval} هم قدیمی است: {age_minutes:.1f} دقیقه")
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




PAPER_STATE_PATH = "paper_trades_state.json"
PAPER_CONTENTS_API = "https://api.github.com/repos/younes63nasiri-droid/gold-signal-bot/contents/" + PAPER_STATE_PATH
PAPER_LOCAL_PATH = Path(tempfile.gettempdir()) / "gold_signal_paper_trades.json"


def _github_headers():
    if not GITHUB_TOKEN:
        raise RuntimeError("توکن GitHub برای ذخیره دفتر معاملات آزمایشی تنظیم نشده است.")
    return {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}


def load_paper_ledger():
    response = requests.get(PAPER_CONTENTS_API, headers=_github_headers(), timeout=20)
    if response.status_code == 404:
        PAPER_LOCAL_PATH.write_text(json.dumps({"balance": 1000.0, "open": {}, "closed": []}), encoding="utf-8")
        return PaperLedger(PAPER_LOCAL_PATH), None
    response.raise_for_status()
    payload = response.json()
    state = json.loads(base64.b64decode(payload["content"]).decode("utf-8"))
    PAPER_LOCAL_PATH.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    return PaperLedger(PAPER_LOCAL_PATH), payload["sha"]


def save_paper_ledger(ledger, sha):
    encoded = base64.b64encode(json.dumps(ledger.export_state(), ensure_ascii=False, indent=2).encode("utf-8")).decode("ascii")
    body = {"message": "Update paper trading ledger state", "content": encoded}
    if sha:
        body["sha"] = sha
    response = requests.put(PAPER_CONTENTS_API, headers=_github_headers(), json=body, timeout=20)
    response.raise_for_status()
    return response.json().get("content", {}).get("sha")

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



# Toobit account connection: READ-ONLY balance check; no order endpoints are used.
TOOBIT_API_KEY = os.environ.get("TOOBIT_API_KEY", "").strip()
TOOBIT_API_SECRET = os.environ.get("TOOBIT_API_SECRET", "").strip()
FUTURES_AVAILABLE_USDT = 0.0

def check_toobit_futures_balance():
    if not TOOBIT_API_KEY or not TOOBIT_API_SECRET:
        print("Toobit account check skipped: API credentials are not configured.")
        return
    params = {"recvWindow": 5000, "timestamp": int(datetime.now(timezone.utc).timestamp() * 1000)}
    query = urlencode(params)
    signature = hmac.new(
        TOOBIT_API_SECRET.encode("utf-8"),
        query.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    url = "https://api.toobit.com/api/v1/futures/balance?" + query + "&signature=" + signature
    response = requests.get(
        url,
        headers={"X-BB-APIKEY": TOOBIT_API_KEY},
        timeout=20,
    )
    if not response.ok:
        # Include Toobit's diagnostic response body (never print API credentials).
        raise RuntimeError(
            f"HTTP {response.status_code} from Toobit: {response.text[:1000]}"
        )
    payload = response.json()
    print("TOOBIT_READ_ONLY_FUTURES_BALANCE:", json.dumps(payload, ensure_ascii=False))
    rows = payload if isinstance(payload, list) else payload.get("balances", [payload]) if isinstance(payload, dict) else []
    usdt_row = next(
        (row for row in rows if isinstance(row, dict) and str(row.get("coin", row.get("asset", ""))).upper() == "USDT"),
        None,
    )
    if usdt_row:
        global FUTURES_AVAILABLE_USDT
        FUTURES_AVAILABLE_USDT = float(usdt_row.get("availableBalance", usdt_row.get("balance", 0)) or 0)
    return FUTURES_AVAILABLE_USDT

def analyze():
    candles_15m = get_candles("15m")
    candles_1h = get_candles("1h")
    expire_previous_gold_signal(candles_15m)
    ledger, paper_sha = load_paper_ledger()
    for trade_id, trade in list(ledger.state["open"].items()):
        if trade.get("symbol") == "XAUUSD":
            for bar in candles_15m:
                ledger.process_candle(trade_id, bar["high"], bar["low"], bar["openTime"])
    closes_15m = [bar["close"] for bar in candles_15m]
    closes_1h = [bar["close"] for bar in candles_1h]
    fast_1h, slow_1h = ema(closes_1h, 50), ema(closes_1h, 100)
    fast_15m, slow_15m = ema(closes_15m, 50), ema(closes_15m, 100)
    current_rsi = rsi(closes_15m)
    current_atr = atr(candles_15m)
    price = closes_15m[-1]
    candle_id = candles_15m[-1]["openTime"]
    previous_signal, _ = load_gold_state()
    if previous_signal and previous_signal.get("candle_id") == candle_id:
        save_paper_ledger(ledger, paper_sha)
        print("Duplicate gold signal suppressed for the same closed candle.")
        return
    direction = None
    if fast_1h > slow_1h and fast_15m > slow_15m and 50 < current_rsi < 65:
        direction = "BUY"
    elif fast_1h < slow_1h and fast_15m < slow_15m and 35 < current_rsi < 50:
        direction = "SELL"
    print("Price:", price, "RSI:", round(current_rsi, 2), "ATR:", round(current_atr, 3))
    print("Signal:", direction or "NO TRADE")
    if direction is None or current_atr <= 0:
        save_paper_ledger(ledger, paper_sha)
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
    paper_plan = calculate_fixed_margin_plan(
        ledger.summary()["balance"], price, stop_loss,
        "LONG" if direction == "BUY" else "SHORT",
        margin_usdt=ORDER_MARGIN_USDT, risk_pct=0.5,
        max_leverage=10, min_notional_usdt=5.0,
    )
    leverage = paper_plan.leverage if paper_plan.allowed else dynamic_gold_leverage(price, stop_loss)
    if paper_plan.allowed and not any(t.get("symbol") == "XAUUSD" for t in ledger.state["open"].values()):
        trade_id = f"XAUUSD:{candle_id}"
        if ledger.open_trade(trade_id, "XAUUSD", "LONG" if direction == "BUY" else "SHORT",
                             price, stop_loss, target_1, paper_plan.quantity):
            ledger.state["open"][trade_id]["entry_candle"] = str(candle_id)
    save_paper_ledger(ledger, paper_sha)
    message = (
        "🥇 سیگنال طلا | XAU/USD\n\n"
        f"📍 جهت معامله: {'خرید' if direction == 'BUY' else 'فروش'}\n"
        f"💰 قیمت ورود: {format_price(price)}\n"
        f"🎯 حد سود اول: {format_price(target_1)}\n"
        f"🛑 حد ضرر: {format_price(stop_loss)}\n"
        f"⚖️ اهرم پیشنهادی متغیر: {leverage}x (حداکثر ۱۰x)\n"
        + (f"🧪 مارجین ثابت آزمایشی: {paper_plan.margin_usdt:.2f} USDT | حجم اسمی: {paper_plan.notional_usdt:.2f} USDT | ریسک: {paper_plan.max_loss_usdt:.4f} USDT\n" if paper_plan.allowed else "🧪 ورود آزمایشی رد شد: " + paper_plan.reason + "\n")
        + "⏳ اعتبار ورود: ۱۵ دقیقه؛ اگر قیمت ورود نرسید، سیگنال لغو می‌شود.\n\n"
        f"⏱ زمان سیگنال: {candle_id}\n"
        "⚠️ سیگنال آزمایشی است؛ قبل از معامله بررسی شود."
    )
    send_telegram(message)
    save_pending_gold_signal(price, direction, candle_id)
    print("Signal message sent.")


def analyze_best_crypto_market():
    """اسکن بازار قراردادهای پرپچوال USDT توبیت و ثبت فقط بهترین فرصت در دفتر آزمایشی."""
    ledger, paper_sha = load_paper_ledger()
    # Process every newly closed 15m candle for already-open crypto paper positions.
    for trade_id, trade in list(ledger.state["open"].items()):
        symbol = trade.get("symbol", "")
        if symbol.endswith("-SWAP-USDT"):
            try:
                bars = get_crypto_candles(symbol, "15m")
                for bar in bars:
                    ledger.process_candle(trade_id, bar["high"], bar["low"], bar["openTime"])
            except Exception as error:
                print(f"Paper position candle update failed for {symbol}: {error}")
    scan = market_scanner.scan_market()
    best = scan.get("best")
    header = (
        "🪙 بهترین فرصت ارز دیجیتال | اسکن بازار توبیت\n\n"
        f"🔎 قراردادهای فعال بررسی‌شده: {scan.get('contracts_found', 0)}\n"
        f"💧 بازارهای نقدشونده: {scan.get('liquid_contracts', 0)}\n"
        f"📊 بررسی تکنیکال: {scan.get('analyzed_contracts', 0)} نماد\n"
    )
    if not best:
        save_paper_ledger(ledger, paper_sha)
        summary = ledger.summary()
        send_telegram(
            header
            + "\n🟡 فعلاً هیچ موقعیت قدرتمندی پیدا نشد.\n"
            + "برای جلوگیری از سیگنال ضعیف، ورود پیشنهاد نمی‌شود.\n"
            + "ربات در نوبت بعدی دوباره بازار را اسکن می‌کند.\n"
            + f"\n🧪 دفتر آزمایشی: موجودی {summary['balance']:.2f} | باز {summary['open_count']} | بسته {summary['closed_count']} | سود/زیان خالص {summary['net_pnl']:.2f}"
        )
        print("No qualifying crypto opportunity.")
        return

    symbol = best["symbol"]
    direction = best["direction"]
    meta = ledger.state.setdefault("meta", {})
    crypto_signal_key = f"{symbol}:{best.get('last_candle_time')}"
    if meta.get("last_crypto_signal") == crypto_signal_key:
        save_paper_ledger(ledger, paper_sha)
        print("Duplicate crypto signal suppressed for the same closed candle.")
        return
    entry = float(best["entry"])
    stop = float(best["stop"])
    target = float(best["target"])
    sizing = calculate_fixed_margin_plan(
        ledger.summary()["balance"], entry, stop, direction,
        margin_usdt=ORDER_MARGIN_USDT, risk_pct=0.5,
        max_leverage=int(best.get("leverage_cap", 10)), min_notional_usdt=5.0,
    )
    leverage = sizing.leverage if sizing.allowed else 1
    sizing_message = (
        "\n📐 برنامه حجم (محاسبه آزمایشی)\n"
        + f"مارجین ثابت: {sizing.margin_usdt:.2f} USDT | حجم اسمی: {sizing.notional_usdt:.2f} USDT\n"
        + f"مقدار: {sizing.quantity:.8f} | اهرم: {sizing.leverage}x\n"
        + f"ریسک تا حد ضرر: {sizing.max_loss_usdt:.4f} USDT"
        if sizing.allowed
        else "\n⛔ معامله از نظر مدیریت ریسک مجاز نیست: " + sizing.reason
    )
    # Open only one simulated position per unique closed candle.
    try:
        crypto_bars = get_crypto_candles(symbol, "15m")
        latest_bar = crypto_bars[-1]
        trade_id = f"{symbol}:{latest_bar['openTime']}"
        if sizing.allowed and not any(t.get("symbol") == symbol for t in ledger.state["open"].values()):
            if ledger.open_trade(trade_id, symbol, direction, entry, stop, target, sizing.quantity):
                ledger.state["open"][trade_id]["entry_candle"] = str(latest_bar["openTime"])
        for bar in crypto_bars:
            if trade_id in ledger.state["open"]:
                ledger.process_candle(trade_id, bar["high"], bar["low"], bar["openTime"])
    except Exception as error:
        print(f"Paper crypto ledger update failed for {symbol}: {error}")
    meta["last_crypto_signal"] = crypto_signal_key
    save_paper_ledger(ledger, paper_sha)
    paper_summary = ledger.summary()
    message = (
        header
        + "\n🏆 بهترین موقعیت فعلی بازار\n"
        + f"🪙 نماد: {symbol}\n"
        + f"📍 جهت: {'خرید' if direction == 'LONG' else 'فروش'}\n"
        + f"⭐ امتیاز کیفیت: {best['score']}\n"
        + f"💰 ورود: {format_price(entry)} USDT\n"
        + f"🎯 حد سود: {format_price(target)} USDT\n"
        + f"🛑 حد ضرر: {format_price(stop)} USDT\n"
        + f"⚖️ اهرم پیشنهادی متغیر: {leverage}x (سقف 10x در شبیه‌سازی)\n"
        + f"📈 RSI: {best['rsi']} | ATR: {best['atr_pct']}%\n"
        + f"💧 حجم معاملات ۲۴ساعته: {best['quote_volume_24h']:.0f} USDT\n"
        + sizing_message
        + f"\n🧪 دفتر آزمایشی: موجودی {paper_summary['balance']:.2f} | باز {paper_summary['open_count']} | بسته {paper_summary['closed_count']} | سود/زیان خالص {paper_summary['net_pnl']:.2f}"
        + "\n⚠️ فقط شبیه‌سازی است؛ هیچ سفارش واقعی ثبت نمی‌شود."
    )
    send_telegram(message)
    print(f"Best crypto opportunity sent: {symbol} {direction} score={best['score']}")


if __name__ == "__main__":
    print("بررسی اتصال حساب توبیت (فقط‌خواندنی)...")
    try:
        check_toobit_futures_balance()
    except Exception as error:
        print(f"Toobit read-only account check failed: {type(error).__name__}: {error}")
    print("شروع بررسی بازار طلا...")
    try:
        analyze()
    except Exception as error:
        print(f"خطا در بررسی طلا: {error}")
        try:
            send_telegram(
                f"⚠️ خطا در بررسی طلا | XAU/USD\\n"
                f"{type(error).__name__}: {error}\\n"
                "بررسی ارزهای دیجیتال بدون توقف ادامه پیدا می‌کند."
            )
        except Exception as telegram_error:
            print(f"ارسال گزارش خطای طلا ناموفق بود: {telegram_error}")

    print("شروع اسکن کل بازار ارز دیجیتال...")
    try:
        analyze_best_crypto_market()
    except Exception as error:
        print(f"خطا در اسکن بازار ارز دیجیتال: {error}")
        try:
            send_telegram(
                f"⚠️ خطا در اسکن بازار ارز دیجیتال\\n"
                f"{type(error).__name__}: {error}\\n"
                "در اجرای بعدی دوباره تلاش می‌کنم."
            )
        except Exception as telegram_error:
            print(f"ارسال گزارش خطای اسکن ناموفق بود: {telegram_error}")
