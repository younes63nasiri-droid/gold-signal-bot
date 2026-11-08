import os, json, base64, requests
from datetime import datetime, timezone
import gold2

OWNER_REPO = "younes63nasiri-droid/gold-signal-bot"
STATE_PATH = "paper_trading_state.json"
STATE_URL = f"https://api.github.com/repos/{OWNER_REPO}/contents/{STATE_PATH}?ref=paper-trading-demo"
GH_TOKEN = os.environ["GITHUB_TOKEN"]
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "601146458"
START_BALANCE = 100000.0
RISK_FRACTION = 0.005
MAX_LEVERAGE = 10.0
FEE_RATE = 0.0006

ASSETS = {
    "XAU/USD (PAXG proxy)": ("gold", None),
    "ریپل (XRP-SWAP-USDT)": ("crypto", "XRP-SWAP-USDT"),
    "دوج‌کوین (DOGE-SWAP-USDT)": ("crypto", "DOGE-SWAP-USDT"),
    "ترون (TRX-SWAP-USDT)": ("crypto", "TRX-SWAP-USDT"),
}

def gh_headers():
    return {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}

def load_state():
    r = requests.get(STATE_URL, headers=gh_headers(), timeout=20)
    if r.status_code == 404:
        return {"balance": START_BALANCE, "positions": {}, "closed_trades": 0, "realized_pnl": 0.0}, None
    r.raise_for_status()
    obj = r.json()
    state = json.loads(base64.b64decode(obj["content"]).decode("utf-8"))
    return state, obj["sha"]

def save_state(state, sha):
    raw = json.dumps(state, ensure_ascii=False, indent=2).encode()
    body = {"message": "Update paper trading state", "content": base64.b64encode(raw).decode()}
    if sha:
        body["sha"] = sha
    r = requests.put(STATE_URL, headers=gh_headers(), json=body, timeout=20)
    r.raise_for_status()

def telegram(text):
    r = requests.post(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                      data={"chat_id": CHAT_ID, "text": text}, timeout=20)
    r.raise_for_status()
    if not r.json().get("ok"):
        raise RuntimeError("Telegram send failed")

def market_data(label, kind, symbol):
    if kind == "gold":
        a, b = gold2.get_candles("15m"), gold2.get_candles("1h")
    else:
        a, b = gold2.get_crypto_candles(symbol, "15m"), gold2.get_crypto_candles(symbol, "1h")
    closes_a = [x["close"] for x in a]
    closes_b = [x["close"] for x in b]
    rsi = gold2.rsi(closes_a)
    atr = gold2.atr(a)
    fast_a, slow_a = gold2.ema(closes_a, 50), gold2.ema(closes_a, 100)
    fast_b, slow_b = gold2.ema(closes_b, 50), gold2.ema(closes_b, 100)
    direction = None
    if fast_b > slow_b and fast_a > slow_a and 50 < rsi < 65:
        direction = "LONG"
    elif fast_b < slow_b and fast_a < slow_a and 35 < rsi < 50:
        direction = "SHORT"
    last = a[-1]
    return a, last, direction, atr

def close_position(state, label, pos, price, reason):
    side_sign = 1 if pos["side"] == "LONG" else -1
    gross = (price - pos["entry"]) * pos["qty"] * side_sign
    exit_fee = abs(price * pos["qty"]) * FEE_RATE
    pnl = gross - exit_fee
    state["balance"] += pnl
    state["realized_pnl"] += pnl
    state["closed_trades"] += 1
    del state["positions"][label]
    return (f"🔴 بسته‌شدن معامله آزمایشی | {label}\n"
            f"علت: {reason}\nورود: {pos['entry']:.6g}\nخروج: {price:.6g}\n"
            f"سود/زیان خالص: {pnl:+.2f} USDT\nموجودی مجازی: {state['balance']:.2f} USDT")

def main():
    state, sha = load_state()
    notices = []
    for label, (kind, symbol) in ASSETS.items():
        try:
            bars, last, signal, atr = market_data(label, kind, symbol)
            high, low, close = last["high"], last["low"], last["close"]
            pos = state["positions"].get(label)
            if pos:
                if pos["side"] == "LONG":
                    stop_hit, target_hit = low <= pos["stop"], high >= pos["target"]
                else:
                    stop_hit, target_hit = high >= pos["stop"], low <= pos["target"]
                # Conservative candle rule: if both levels touched, count stop first.
                if stop_hit:
                    notices.append(close_position(state, label, pos, pos["stop"], "حد ضرر"))
                elif target_hit:
                    notices.append(close_position(state, label, pos, pos["target"], "حد سود"))
                continue
            if not signal or atr <= 0:
                continue
            entry = close
            distance = 1.5 * atr
            stop = entry - distance if signal == "LONG" else entry + distance
            target = entry + 3 * atr if signal == "LONG" else entry - 3 * atr
            risk_cash = max(0.0, state["balance"]) * RISK_FRACTION
            qty_by_risk = risk_cash / distance
            max_notional = max(0.0, state["balance"]) * MAX_LEVERAGE
            qty_by_leverage = max_notional / entry
            qty = min(qty_by_risk, qty_by_leverage)
            if qty <= 0:
                continue
            leverage = (qty * entry) / max(state["balance"], 1.0)
            leverage = min(MAX_LEVERAGE, max(1.0, leverage))
            entry_fee = abs(entry * qty) * FEE_RATE
            state["balance"] -= entry_fee
            state["positions"][label] = {
                "side": signal, "entry": entry, "stop": stop, "target": target,
                "qty": qty, "entry_fee": entry_fee, "opened_at": last["openTime"],
                "leverage": leverage
            }
            notices.append(
                f"🟢 بازشدن معامله آزمایشی | {label}\nجهت: {'خرید' if signal == 'LONG' else 'فروش'}\n"
                f"ورود: {entry:.6g}\nحد ضرر: {stop:.6g}\nحد سود: {target:.6g}\n"
                f"اهرم شبیه‌سازی: {leverage:.1f}x\nریسک هدف: ۰٫۵٪ موجودی"
            )
        except Exception as e:
            notices.append(f"⚠️ خطای بررسی آزمایشی {label}: {type(e).__name__}: {e}")
    save_state(state, sha)
    for note in notices:
        telegram(note)
    if notices:
        telegram(f"📊 خلاصه حساب آزمایشی\nموجودی: {state['balance']:.2f} USDT\n"
                 f"سود/زیان تحقق‌یافته: {state['realized_pnl']:+.2f} USDT\n"
                 f"معاملات بسته‌شده: {state['closed_trades']}\n"
                 f"معاملات باز: {len(state['positions'])}\n"
                 "این حساب شبیه‌سازی است و هیچ سفارش واقعی به صرافی ارسال نمی‌کند.")

if __name__ == "__main__":
    main()
