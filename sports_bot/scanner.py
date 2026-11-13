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

def send_telegram(message):
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN secret تنظیم نشده است.")
    response = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": message, "disable_web_page_preview": True},
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {response.status_code}: {response.text[:1000]}")
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")

def main():
    events, errors = collect()
    lines = ["🧠 VIVA AI | گزارش اسکن مسابقات", ""]
    if events:
        for item in events[:25]:
            local = item["start"].astimezone().strftime("%m-%d %H:%M")
            lines.append(f"🏟 {item['sport']}\n{item['home']} — {item['away']}\n🕒 {local}")
    else:
        lines.append("در بازه ۴۸ ساعت آینده مسابقه‌ای از منابع فعلی پیدا نشد.")
    lines += [
        "",
        "⚠️ این نسخه فقط مسابقات و زمان شروع را از منبع داده بررسی می‌کند.",
        "هنوز ضریب معتبر DerbyBet و آمار کافی برای محاسبه Value Bet متصل نشده؛ بنابراین هیچ شرطی پیشنهاد نمی‌دهد و هیچ شرطی ثبت نمی‌کند.",
    ]
    if errors:
        lines += ["", "منابع ناموفق: " + "؛ ".join(errors[:5])]
    send_telegram("\n".join(lines))

if __name__ == "__main__":
    main()
