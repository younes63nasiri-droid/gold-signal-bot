import os
import requests
from datetime import datetime, timezone, timedelta

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "601146458").strip()
BASE = "https://site.api.espn.com/apis/site/v2/sports"
HEADERS = {"User-Agent": "VIVA-AI-Sports/0.2 (+scheduled fixture research)"}
TIMEOUT = 20

# ESPN public scoreboard endpoints that are currently supported.
# Unsupported volleyball endpoints were removed rather than repeatedly failing.
FEEDS = [
    ("فوتبال جهان (مسابقات پوشش‌داده‌شده ESPN)", f"{BASE}/soccer/all/scoreboard"),
    ("بسکتبال NBA", f"{BASE}/basketball/nba/scoreboard"),
    ("بسکتبال WNBA", f"{BASE}/basketball/wnba/scoreboard"),
    ("تنیس ATP", f"{BASE}/tennis/atp/scoreboard"),
    ("تنیس WTA", f"{BASE}/tennis/wta/scoreboard"),
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
    if not isinstance(competition, dict) or not isinstance(competitors, list):
        return None
    odds_items = competition.get("odds") or []
    if isinstance(odds_items, dict):
        odds_items = [odds_items]
    if not isinstance(odds_items, list) or not odds_items:
        return None

    for market in odds_items:
        if not isinstance(market, dict):
            continue
        candidates = []
        for side in ("home", "away"):
            team_odds = market.get(f"{side}TeamOdds") or {}
            if not isinstance(team_odds, dict):
                continue
            probability = american_implied_probability(team_odds.get("moneyLine"))
            team = next(
                (
                    (x.get("team") or {}).get("displayName")
                    for x in competitors
                    if isinstance(x, dict) and x.get("homeAway") == side
                    and isinstance(x.get("team") or {}, dict)
                ),
                None,
            )
            if probability is not None and team:
                candidates.append((probability, team))
        if candidates:
            probability, team = max(candidates)
            if probability >= 0.65:
                provider = market.get("provider") or {}
                if not isinstance(provider, dict):
                    provider = {}
                return {
                    "team": team,
                    "probability": probability,
                    "provider": provider.get("name", "ESPN odds"),
                }
    return None


def fetch_events(label, url, date_str):
    response = requests.get(
        url, params={"dates": date_str}, headers=HEADERS, timeout=TIMEOUT
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("پاسخ ESPN ساختار JSON مورد انتظار را ندارد")

    raw_events = payload.get("events") or []
    if not isinstance(raw_events, list):
        raise ValueError("فیلد events در پاسخ ESPN فهرست نیست")

    events = []
    now = datetime.now(timezone.utc)
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        competitions = event.get("competitions") or []
        comp = competitions[0] if isinstance(competitions, list) and competitions else {}
        if not isinstance(comp, dict):
            comp = {}
        competitors = comp.get("competitors") or []
        if not isinstance(competitors, list):
            competitors = []

        names = {}
        for item in competitors:
            if not isinstance(item, dict):
                continue
            team = item.get("team") or {}
            if isinstance(team, dict):
                names[item.get("homeAway")] = team.get("displayName", "نامشخص")

        start = event.get("date")
        if not start:
            continue
        try:
            kickoff = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            continue

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
    found, errors, feed_stats = [], [], []
    for label, url in FEEDS:
        before = len(found)
        try:
            for day in dates:
                try:
                    found.extend(fetch_events(label, url, day))
                except Exception as exc:
                    errors.append(f"{label}: {type(exc).__name__}: {str(exc)[:100]}")
                    break
            feed_stats.append(f"{label}: {len(found) - before}")
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__}: {str(exc)[:100]}")

    unique = {}
    for item in found:
        key = (item["sport"], item["name"], item["start"].isoformat())
        unique[key] = item
    return sorted(unique.values(), key=lambda x: x["start"]), errors, feed_stats


def resolve_chat_id():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN secret تنظیم نشده است.")
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
        data={
            "chat_id": target_chat_id,
            "text": message,
            "disable_web_page_preview": True,
        },
        timeout=TIMEOUT,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Telegram HTTP {response.status_code}: {response.text[:1000]}")
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")


def main():
    events, errors, feed_stats = collect()
    picks = [event for event in events if event.get("favorite")]
    events_with_odds = sum(1 for event in events if event.get("favorite"))
    lines = ["🧠 VIVA AI | خلاصه تحلیل", ""]

    if picks:
        lines.append("⭐ برگزیده‌های بازار (احتمال ضمنی ضرایب حداقل ۶۵٪):")
        for item in sorted(
            picks, key=lambda x: x["favorite"]["probability"], reverse=True
        )[:5]:
            local = item["start"].astimezone().strftime("%m-%d %H:%M")
            favorite = item["favorite"]
            lines.append(
                f"• {item['home']} — {item['away']} | انتخاب بازار: {favorite['team']} "
                f"({favorite['probability']*100:.0f}٪) | {local}"
            )
    else:
        lines.append(
            "فعلاً پیش‌بینی قابل‌اتکا ندارم؛ در مسابقات دریافت‌شده ضریب معتبرِ "
            "قابل‌استفاده پیدا نشد."
        )

    lines += [
        "",
        f"مسابقات دریافت‌شده: {len(events)} | انتخاب واجدشرایط: {events_with_odds}",
        "توجه: درصدها احتمال ضمنی ضرایب بازارند، نه تضمین برد و نه پیش‌بینی مستقل.",
        "اگر داده یا ضریب معتبر نباشد، VIVA عمداً پیشنهاد نمی‌دهد.",
    ]
    if feed_stats:
        lines.append("وضعیت منابع: " + "؛ ".join(feed_stats[:7]))
    if errors:
        lines.append("خطاها: " + "؛ ".join(errors[:4]))
    send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()
