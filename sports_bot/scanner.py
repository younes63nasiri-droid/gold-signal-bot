import os
import time
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "601146458").strip()
TIMEOUT = 18
ODDSLINK = "https://oddslink.app/v1/public/board"
HEADERS = {"User-Agent": "VIVA-AI-Sports/0.3"}
SPORTS = [
    ("فوتبال", "soccer"),
    ("NBA", "nba"),
    ("WNBA", "wnba"),
    ("تنیس", "tennis"),
    ("بیسبال", "mlb"),
    ("هاکی", "nhl"),
]


def american_probability(value):
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return None
    if odds == 0:
        return None
    return (-odds / (-odds + 100)) if odds < 0 else (100 / (odds + 100))


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def market_favorite(fixture):
    best = None
    books = fixture.get("bookmakers") or []
    for book in books:
        if not isinstance(book, dict):
            continue
        # Use actual sportsbooks; exclude prediction markets and exchanges.
        if book.get("kind") not in (None, "sportsbook"):
            continue
        markets = book.get("markets") or {}
        if isinstance(markets, list):
            h2h = next((m for m in markets if isinstance(m, dict) and m.get("key") == "h2h"), None)
        elif isinstance(markets, dict):
            h2h = markets.get("h2h")
        else:
            h2h = None
        if isinstance(h2h, list):
            outcomes = h2h
            market_updated = None
        elif isinstance(h2h, dict):
            outcomes = h2h.get("outcomes") or []
            market_updated = h2h.get("last_update")
        else:
            continue

        updated = parse_time(market_updated or book.get("last_update"))
        if updated is None:
            continue
        age = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
        if age < -60 or age > 900:
            continue

        raw = []
        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue
            prob = american_probability(outcome.get("price"))
            name = outcome.get("name")
            if prob is not None and name:
                raw.append((name, prob))
        total = sum(p for _, p in raw)
        if total <= 0:
            continue
        for name, prob in raw:
            fair_prob = prob / total
            if best is None or fair_prob > best["probability"]:
                best = {
                    "team": name,
                    "probability": fair_prob,
                    "book": book.get("title") or book.get("key") or "Sportsbook",
                    "decimal_odds": (1 + 100 / abs(float(next(o["price"] for o in outcomes if isinstance(o, dict) and o.get("name") == name)))) if float(next(o["price"] for o in outcomes if isinstance(o, dict) and o.get("name") == name)) < 0 else (1 + float(next(o["price"] for o in outcomes if isinstance(o, dict) and o.get("name") == name)) / 100),
                    "updated": updated,
                }
    if best and best["probability"] >= 0.65:
        return best
    return None


def collect():
    now = datetime.now(timezone.utc)
    found, errors, stats = [], [], []
    for index, (label, sport) in enumerate(SPORTS):
        if index:
            time.sleep(1.05)  # respect the public board's per-address rate limit
        try:
            response = requests.get(
                f"{ODDSLINK}/{sport}",
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
            fixtures = payload.get("fixtures") or []
            count = 0
            for fixture in fixtures:
                if not isinstance(fixture, dict):
                    continue
                start = parse_time(fixture.get("commence_time"))
                if start is None or start < now or start > now + timedelta(hours=36):
                    continue
                count += 1
                favorite = market_favorite(fixture)
                found.append({
                    "sport": label,
                    "home": fixture.get("home_team", "نامشخص"),
                    "away": fixture.get("away_team", "نامشخص"),
                    "start": start,
                    "favorite": favorite,
                })
            stats.append(f"{label}: {count} مسابقه")
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__} {str(exc)[:90]}")
            stats.append(f"{label}: خطا")
    return found, errors, stats


def resolve_chat_id():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN secret تنظیم نشده است.")
    me = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=TIMEOUT)
    me.raise_for_status()
    data = me.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getMe failed: {data}")
    print("VIVA Telegram bot verified: @" + data["result"].get("username", "unknown"))

    updates = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/getUpdates",
        params={"limit": 20, "allowed_updates": ["message"]},
        timeout=TIMEOUT,
    )
    updates.raise_for_status()
    update_data = updates.json()
    for update in reversed(update_data.get("result", [])):
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") == "private" and chat.get("id") is not None:
            return str(chat["id"])
    return CHAT_ID


def send_telegram(message):
    target = resolve_chat_id()
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": target, "text": message, "disable_web_page_preview": True},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def main():
    events, errors, stats = collect()
    picks = [e for e in events if e.get("favorite")]
    picks.sort(key=lambda e: e["favorite"]["probability"], reverse=True)

    lines = ["🧠 VIVA AI | پیش‌بینی مسابقات", ""]
    if picks:
        lines.append("⭐ انتخاب‌های برتر بر اساس ضرایب تازه بازار")
        lines.append("━━━━━━━━━━━━━━━━━━")
        for index, item in enumerate(picks[:8], 1):
            fav = item["favorite"]
            probability = fav["probability"] * 100
            level = "🟢 احتمال بازار بالاتر" if probability >= 72 else "🟡 احتمال بازار متوسط"
            local_time = item["start"].astimezone(ZoneInfo("Asia/Tehran")).strftime("%H:%M")
            lines += [
                f"{level} • #{index}",
                f"🏟 {item['home']} 🆚 {item['away']}",
                f"🕒 ساعت شروع: {local_time}",
                f"🎯 انتخاب بازار: {fav['team']} برنده",
                f"💎 ضریب اعشاری: {fav['decimal_odds']:.2f}",
                f"📊 احتمال ضمنی تعدیل‌شده بازار: {probability:.0f}٪",
                f"📝 دلیل انتخاب: در بازار ثبت‌شده، این گزینه بالاترین احتمال تعدیل‌شده را دارد. منبع ضریب: {fav['book']}.",
                "┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
            ]
    else:
        lines.append("فعلاً گزینه واجدشرایط پیدا نشد؛ هیچ پیش‌بینی حدسی ارسال نمی‌شود.")

    lines += [
        f"مسابقات آینده بررسی‌شده: {len(events)} | گزینه‌های واجدشرایط: {len(picks)}",
        "",
        "⚠️ این‌ها انتخاب‌های مبتنی بر بازارند، نه پیش‌بینی مستقل یا تضمین برد.",
        "آمارهایی مثل xG، فرم اخیر و مصدومیت فقط در صورت اتصال منبع معتبر جداگانه گزارش می‌شوند؛ چیزی حدس زده نمی‌شود.",
        "ضرایب قدیمی‌تر از ۱۵ دقیقه حذف می‌شوند.",
        "وضعیت منابع: " + "؛ ".join(stats),
    ]
    if errors:
        lines.append("جزئیات خطا: " + "؛ ".join(errors[:4]))
    send_telegram("\n".join(lines))

if __name__ == "__main__":
    main()
