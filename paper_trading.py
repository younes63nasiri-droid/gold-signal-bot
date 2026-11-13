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
    if not state.get("accounting_version"):
        open_entry_fees = sum(float(p.get("entry_fee", 0.0)) for p in state.get("positions", {}).values())
        old_closed_fees = max(0.0, START_BALANCE + float(state.get("realized_pnl", 0.0)) - float(state.get("balance", START_BALANCE)) - open_entry_fees)
        state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) - old_closed_fees
        state["wins"] = 0
        state["losses"] = 0
        state["accounting_version"] = 2
    return state, obj["sha"]

def save_state(state, sha):
    raw = json.dumps(state, ensure_ascii=False, indent=2).encode()
    body = {"message": "Update paper trading state", "content": base64.b64encode(raw).decode(), "branch": "paper-trading-demo"}
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
    pnl = gross - exit_fee - float(pos.get("entry_fee", 0.0))
    state["balance"] += gross - exit_fee
    state["realized_pnl"] += pnl
    state["closed_trades"] += 1
    if pnl >= 0:
        state["wins"] = int(state.get("wins", 0)) + 1
    else:
        state["losses"] = int(state.get("losses", 0)) + 1
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
    unrealized = 0.0
    for label, pos in state.get("positions", {}).items():
        try:
            kind, symbol = ASSETS[label]
            bars = gold2.get_candles("15m") if kind == "gold" else gold2.get_crypto_candles(symbol, "15m")
            mark = float(bars[-1]["close"])
            sign = 1 if pos["side"] == "LONG" else -1
            gross = (mark - float(pos["entry"])) * float(pos["qty"]) * sign
            unrealized += gross - abs(mark * float(pos["qty"])) * FEE_RATE
        except Exception as e:
            print(f"MARK_TO_MARKET_ERROR {label}: {e}")
    state["last_unrealized_pnl"] = unrealized
    state["last_equity"] = float(state["balance"]) + unrealized
    state["last_total_pnl"] = state["last_equity"] - START_BALANCE
    save_state(state, sha)
    for note in notices:
        telegram(note)
    if notices:
        telegram(f"📊 خلاصه حساب آزمایشی\nموجودی: {state['balance']:.2f} USDT\n"
                 f"سود/زیان تحقق‌یافته خالص: {state['realized_pnl']:+.2f} USDT\n"
                 f"سود/زیان باز تقریبی: {state['last_unrealized_pnl']:+.2f} USDT\n"
                 f"ارزش کل حساب: {state['last_equity']:.2f} USDT\n"
                 f"کل سود/زیان نسبت به شروع: {state['last_total_pnl']:+.2f} USDT\n"
                 f"معاملات بسته‌شده: {state['closed_trades']} (برد جدید: {state.get('wins',0)}، باخت جدید: {state.get('losses',0)})\n"
                 f"معاملات باز: {len(state['positions'])}\n"
                 "این حساب شبیه‌سازی است و هیچ سفارش واقعی به صرافی ارسال نمی‌کند.")

if __name__ == "__main__":
    main()
