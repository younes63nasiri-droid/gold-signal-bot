import os
import requests
from datetime import datetime, timezone, timedelta

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "601146458").strip()
BASE = "https://site.api.espn.com/apis/site/v2/sports"
HEADERS = {"User-Agent": "VIVA-AI-Sports/0.1 (+scheduled fixture research)"}
TIMEOUT = 20

# ESPN public scoreboard coverage for the first working version.
# Additional leagues/sports are added only after a stable data source is verified.
FEEDS = [
    ("فوتبال جهان (مسابقات پوشش‌داده‌شده ESPN)", f"{BASE}/soccer/all/scoreboard"),
    ("بسکتبال NBA", f"{BASE}/basketball/nba/scoreboard"),
    ("بسکتبال WNBA", f"{BASE}/basketball/wnba/scoreboard"),
    ("تنیس ATP", f"{BASE}/tennis/atp/scoreboard"),
    ("تنیس WTA", f"{BASE}/tennis/wta/scoreboard"),
    ("والیبال قهرمانی جهان زنان", f"{BASE}/volleyball/fivb.w/scoreboard"),
    ("والیبال قهرمانی جهان مردان", f"{BASE}/volleyball/fivb.m/scoreboard"),
    ("بیسبال MLB", f"{BASE}/baseball/mlb/scoreboard"),
    ("هاکی NHL", f"{BASE}/hockey/nhl/scoreboard"),
]
def american_implied_probability(value):
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return None
    if odds == 0:
        return None
    return (-odds / (-odds + 100)) if odds < 0 else (100 / (odds + 100))

def extract_market_favorite(competition, competitors):
    odds_items = competition.get("odds") or []
    if not odds_items:
        return None
    market = odds_items[0]
    candidates = []
    for side in ("home", "away"):
        team_odds = market.get(f"{side}TeamOdds") or {}
        probability = american_implied_probability(team_odds.get("moneyLine"))
        team = next((x.get("team", {}).get("displayName") for x in competitors
                     if x.get("homeAway") == side), None)
        if probability is not None and team:
            candidates.append((probability, team))
    if not candidates:
        return None
    probability, team = max(candidates)
    if probability < 0.65:
        return None
    return {"team": team, "probability": probability,
            "provider": market.get("provider", {}).get("name", "ESPN odds")}

def fetch_events(label, url, date_str):
    response = requests.get(url, params={"dates": date_str}, headers=HEADERS, timeout=TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    events = []
    for event in payload.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        names = {x.get("homeAway"): x.get("team", {}).get("displayName", "نامشخص") for x in competitors}
        start = event.get("date")
        if not start:
            continue
        try:
            kickoff = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            continue
        now = datetime.now(timezone.utc)
        if kickoff < now or kickoff > now + timedelta(hours=48):
            continue
        events.append({
            "sport": label,
            "home": names.get("home", "نامشخص"),
            "away": names.get("away", "نامشخص"),
            "start": kickoff,
            "name": event.get("name", ""),
            "favorite": extract_market_favorite(comp, competitors),
        })
    return events

def collect():
    now = datetime.now(timezone.utc)
    dates = [(now + timedelta(days=i)).strftime("%Y%m%d") for i in range(3)]
    found, errors = [], []
    for label, url in FEEDS:
        for day in dates:
            try:
                found.extend(fetch_events(label, url, day))
            except Exception as exc:
                errors.append(f"{label}: {type(exc).__name__}")
                break
    unique = {}
    for item in found:
        key = (item["sport"], item["name"], item["start"].isoformat())
        unique[key] = item
    return sorted(unique.values(), key=lambda x: x["start"]), errors

def resolve_chat_id():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN secret تنظیم نشده است.")
    # Confirm which bot the GitHub secret belongs to, then discover the
    # private chat that most recently messaged this exact bot.
    me = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=TIMEOUT)
    me.raise_for_status()
    me_data = me.json()
    if not me_data.get("ok"):
        raise RuntimeError(f"Telegram getMe failed: {me_data}")
    bot_username = me_data["result"].get("username", "unknown")
    print(f"VIVA token belongs to @{bot_username}")

    updates = requests.get(
        f"https://api.telegram.org/bot{TOKEN}/getUpdates",
        params={"limit": 20, "allowed_updates": ["message"]},
        timeout=TIMEOUT,
    )
    updates.raise_for_status()
    update_data = updates.json()
    if not update_data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates failed: {update_data}")
    private_chats = []
    for update in update_data.get("result", []):
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") == "private" and chat.get("id") is not None:
            private_chats.append(str(chat["id"]))
    if private_chats:
        chosen = private_chats[-1]
        print(f"Using private chat id discovered from incoming message: {chosen}")
        return chosen
    print(f"No incoming private chat update found; using configured chat id: {CHAT_ID}")
    return CHAT_ID

def send_telegram(message):
    target_chat_id = resolve_chat_id()
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": target_chat_id, "text": message, "disable_web_page_preview": True},
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {response.status_code}: {response.text[:1000]}")
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")

def main():
    events, errors = collect()
    picks = [event for event in events if event.get("favorite")]
    lines = ["🧠 VIVA AI | خلاصه تحلیل", ""]
    if picks:
        lines.append("⭐ برگزیده‌های بازار (احتمال ضمنی ضرایب حداقل ۶۵٪):")
        for item in sorted(picks, key=lambda x: x["favorite"]["probability"], reverse=True)[:5]:
            local = item["start"].astimezone().strftime("%m-%d %H:%M")
            favorite = item["favorite"]
            lines.append(
                f"• {item['home']} — {item['away']} | انتخاب بازار: {favorite['team']} "
                f"({favorite['probability']*100:.0f}٪) | {local}"
            )
    else:
        lines.append("فعلاً پیش‌بینی قابل‌اتکا ندارم؛ ضرایب معتبر برای هیچ مسابقه‌ای دریافت نشد.")
    lines += [
        "",
        "توجه: درصدها احتمال ضمنی ضرایب بازارند، نه تضمین برد و نه پیش‌بینی مستقل.",
        "اگر داده یا ضریب معتبر نباشد، VIVA عمداً پیشنهاد نمی‌دهد.",
    ]
    if errors:
        lines.append("منابع ناموفق: " + "؛ ".join(errors[:4]))
    send_telegram("\n".join(lines))

if __name__ == "__main__":
    main()
