"""Isolated market-wide paper trader. It never submits real orders."""
import os
import json
import base64
import requests
from datetime import datetime, timezone

import market_scanner as scanner

REPO = "younes63nasiri-droid/gold-signal-bot"
BRANCH = "market-wide-paper-scanner"
STATE_PATH = "market_wide_paper_state.json"
STATE_URL = f"https://api.github.com/repos/{REPO}/contents/{STATE_PATH}"
START_BALANCE = 100000.0
RISK_FRACTION = 0.0035
MAX_LEVERAGE = 3.0
MAX_OPEN_POSITIONS = 2
FEE_RATE = 0.0006
MIN_SCORE = 55.0
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "601146458")
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
GH_TOKEN = os.environ["GITHUB_TOKEN"]


def headers():
    return {"Authorization": f"Bearer {GH_TOKEN}", "Accept": "application/vnd.github+json"}


def load_state():
    response = requests.get(STATE_URL, params={"ref": BRANCH}, headers=headers(), timeout=20)
    if response.status_code == 404:
        return {
            "version": 1, "balance": START_BALANCE, "realized_pnl": 0.0,
            "positions": {}, "closed_trades": 0, "wins": 0, "losses": 0,
            "created_at": datetime.now(timezone.utc).isoformat()
        }, None
    response.raise_for_status()
    item = response.json()
    state = json.loads(base64.b64decode(item["content"]).decode("utf-8"))
    return state, item["sha"]


def save_state(state, sha):
    content = base64.b64encode(json.dumps(state, ensure_ascii=False, indent=2).encode()).decode()
    body = {"message": "Update market-wide paper trading state", "content": content, "branch": BRANCH}
    if sha:
        body["sha"] = sha
    response = requests.put(STATE_URL, headers=headers(), json=body, timeout=20)
    response.raise_for_status()


def notify(message):
    response = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": message},
        timeout=20
    )
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram sendMessage returned ok=false")


def close_trade(state, symbol, position, exit_price, reason):
    sign = 1 if position["direction"] == "LONG" else -1
    qty = float(position["qty"])
    gross = (exit_price - float(position["entry"])) * qty * sign
    exit_fee = abs(exit_price * qty) * FEE_RATE
    entry_fee = float(position["entry_fee"])
    net = gross - entry_fee - exit_fee
    state["balance"] += gross - exit_fee
    state["realized_pnl"] += net
    state["closed_trades"] += 1
    state["wins" if net >= 0 else "losses"] += 1
    del state["positions"][symbol]
    return (
        f"🔒 بسته‌شدن معامله آزمایشی | {symbol}\n"
        f"جهت: {position['direction']} | دلیل: {reason}\n"
        f"ورود: {position['entry']:.8g} | خروج: {exit_price:.8g}\n"
        f"سود/زیان خالص با کارمزد: {net:+.2f} USDT"
    )


def manage_positions(state):
    notices = []
    unrealized = 0.0
    for symbol, position in list(state["positions"].items()):
        try:
            bars = scanner._closed_candles(symbol, "15m")
            newer = [bar for bar in bars if bar["close_time"] > int(position["opened_candle"])]
            closed = False
            for bar in newer:
                if position["direction"] == "LONG":
                    stop_hit = bar["low"] <= position["stop"]
                    target_hit = bar["high"] >= position["target"]
                else:
                    stop_hit = bar["high"] >= position["stop"]
                    target_hit = bar["low"] <= position["target"]
                # If both levels occur in one candle, assume the stop was hit first.
                if stop_hit:
                    notices.append(close_trade(state, symbol, position, float(position["stop"]), "حد ضرر"))
                    closed = True
                    break
                if target_hit:
                    notices.append(close_trade(state, symbol, position, float(position["target"]), "حد سود"))
                    closed = True
                    break
            if closed:
                continue
            mark = float(bars[-1]["close"])
            sign = 1 if position["direction"] == "LONG" else -1
            gross = (mark - float(position["entry"])) * float(position["qty"]) * sign
            unrealized += gross - abs(mark * float(position["qty"])) * FEE_RATE
            position["last_mark"] = mark
        except Exception as exc:
            notices.append(f"⚠️ بررسی پوزیشن {symbol} ناموفق بود: {type(exc).__name__}: {exc}")
    state["unrealized_pnl"] = unrealized
    state["equity"] = float(state["balance"]) + unrealized
    state["total_pnl"] = state["equity"] - START_BALANCE
    return notices


def open_best(state, best):
    if not best:
        return None
    symbol = best["symbol"]
    if symbol in state["positions"] or len(state["positions"]) >= MAX_OPEN_POSITIONS:
        return None
    entry = float(best["entry"])
    stop = float(best["stop"])
    target = float(best["target"])
    distance = abs(entry - stop)
    if entry <= 0 or distance <= 0 or target <= 0:
        return None

    equity = max(float(state.get("equity", state["balance"])), 0.0)
    risk_cash = equity * RISK_FRACTION
    qty_risk = risk_cash / distance
    qty_leverage = max(float(state["balance"]), 0.0) * MAX_LEVERAGE / entry
    qty = min(qty_risk, qty_leverage)
    if qty <= 0:
        return None
    notional = qty * entry
    leverage = notional / max(float(state["balance"]), 1.0)
    entry_fee = notional * FEE_RATE
    if entry_fee >= float(state["balance"]):
        return None

    state["balance"] -= entry_fee
    state["positions"][symbol] = {
        "direction": best["direction"], "entry": entry, "stop": stop,
        "target": target, "qty": qty, "entry_fee": entry_fee,
        "leverage": round(leverage, 2),
        "opened_candle": int(best["last_candle_time"]),
        "opened_at": datetime.now(timezone.utc).isoformat(),
        "score": best["score"], "spread_pct": best["spread_pct"]
    }
    return (
        f"🟢 بهترین فرصت بازار | معامله آزمایشی\n"
        f"نماد: {symbol}\nجهت: {best['direction']}\n"
        f"امتیاز: {best['score']}/85 | RSI: {best['rsi']}\n"
        f"ورود: {entry:.8g}\nحد ضرر: {stop:.8g}\nحد سود: {target:.8g}\n"
        f"حجم: {qty:.6g} | اهرم محاسباتی: {leverage:.2f}x (سقف 3x)\n"
        f"ریسک هدف: {RISK_FRACTION*100:.2f}% از ارزش حساب\n"
        "این فقط شبیه‌سازی است؛ سفارش واقعی ارسال نشده است."
    )


def main():
    state, sha = load_state()
    notices = manage_positions(state)
    scan = scanner.scan_market()
    best = scan["best"]

    if best and best["score"] >= MIN_SCORE:
        opened = open_best(state, best)
        if opened:
            notices.append(opened)

    # Recalculate equity after any entry fee or closed position.
    unrealized = 0.0
    for symbol, position in state["positions"].items():
        mark = float(position.get("last_mark", position["entry"]))
        sign = 1 if position["direction"] == "LONG" else -1
        gross = (mark - float(position["entry"])) * float(position["qty"]) * sign
        unrealized += gross - abs(mark * float(position["qty"])) * FEE_RATE
    state["unrealized_pnl"] = unrealized
    state["equity"] = float(state["balance"]) + unrealized
    state["total_pnl"] = state["equity"] - START_BALANCE
    state["last_scan"] = datetime.now(timezone.utc).isoformat()
    state["last_scan_counts"] = {
        "contracts": scan["contracts_found"],
        "liquid": scan["liquid_contracts"],
        "analyzed": scan["analyzed_contracts"],
        "qualified": len(scan["candidates"])
    }
    save_state(state, sha)

    summary = (
        "📡 اسکن کامل بازار توبیت | حساب آزمایشی\n"
        f"قراردادهای فعال USDT: {scan['contracts_found']}\n"
        f"بازارهای نقدشونده: {scan['liquid_contracts']}\n"
        f"بررسی تکنیکال: {scan['analyzed_contracts']} نماد\n"
        f"فرصت‌های واجد شرایط: {len(scan['candidates'])}\n"
    )
    if best:
        summary += f"رتبه اول: {best['symbol']} | {best['direction']} | امتیاز {best['score']}\n"
    else:
        summary += "فرصت قوی پیدا نشد؛ معامله جدیدی باز نشد.\n"
    summary += (
        f"موجودی: {state['balance']:.2f} USDT\n"
        f"سود/زیان باز تقریبی: {state['unrealized_pnl']:+.2f} USDT\n"
        f"ارزش حساب: {state['equity']:.2f} USDT\n"
        f"سود/زیان کل خالص: {state['total_pnl']:+.2f} USDT\n"
        f"معاملات باز: {len(state['positions'])} | بسته‌شده: {state['closed_trades']} "
        f"(برد: {state['wins']}، باخت: {state['losses']})\n"
        "⚠️ شبیه‌سازی با کارمزد تخمینی؛ بدون اتصال معاملاتی یا سفارش واقعی."
    )
    notify(summary)
    for notice in notices:
        notify(notice)
    if scan["errors"]:
        notify("⚠️ چند نماد به علت خطای داده بررسی نشدند:\n" + "\n".join(scan["errors"][:5]))


if __name__ == "__main__":
    main()
