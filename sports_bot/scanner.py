import os
import time
import requests
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "601146458").strip()
TIMEOUT = 18
BASE = "https://oddslink.app/v1/public/board"
HEADERS = {"User-Agent": "VIVA-AI-Sports/0.4"}
TEHRAN = ZoneInfo("Asia/Tehran")
MIN_ODDS = 1.48

# Only requested competitions. Matching is deliberately fail-closed:
# if the feed does not identify a competition, that fixture is not selected.
LEAGUES = {
    "england": ("premier league", "english premier league"),
    "spain": ("la liga", "laliga", "primera division"),
    "germany": ("bundesliga",),
    "italy": ("serie a", "serie a"),
    "france": ("ligue 1", "league 1"),
    "champions": ("uefa champions league", "champions league"),
    "europa": ("uefa europa league", "europa league"),
    "conference": ("uefa conference league", "conference league"),
    "iran": ("persian gulf pro league", "iran pro league", "iranian pro league", "league 1 iran"),
    "uae": ("uae pro league", "arabian gulf league", "united arab emirates"),
    "saudi": ("saudi pro league", "roshn saudi league"),
    "women_spain": ("liga f", "liga f femenina", "spanish women's", "primera division femenina"),
    "women_germany": ("frauen-bundesliga", "women bundesliga", "google pixel frauen-bundesliga"),
    "japan2": ("j2 league", "japan j2", "j league 2"),
}
WOMEN_HINTS = ("women", "woman", "femen", "frauen", "womens")
NATIONAL_HINTS = ("uefa nations league", "fifa world cup", "world cup qualifier",
                  "international friendly", "friendly international", "afc asian cup",
                  "africa cup of nations", "copa america", "euro", "world cup",
                  "national team", "nations league")

SPORTS = [("فوتبال", "soccer"), ("بسکتبال", "nba")]

def parse_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

def american_to_decimal(value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    if n == 0:
        return None
    return 1 + (100 / abs(n) if n < 0 else n / 100)

def competition_text(fixture):
    parts = []
    for key in ("league", "competition", "tournament", "sport_title", "league_name", "competition_name"):
        value = fixture.get(key)
        if isinstance(value, dict):
            parts.extend(str(value.get(k, "")) for k in ("title", "name", "key"))
        elif value:
            parts.append(str(value))
    return " ".join(parts).lower()

def allowed_competition(fixture):
    label = competition_text(fixture)
    if not label:
        return False
    # Women competitions are restricted to Spain and Germany only.
    if any(h in label for h in WOMEN_HINTS):
        return any(h in label for h in LEAGUES["women_spain"] + LEAGUES["women_germany"])
    if any(h in label for h in NATIONAL_HINTS):
        return True
    return any(h in label for aliases in LEAGUES.values() for h in aliases)

def get_market_candidates(fixture):
    candidates = []
    for book in fixture.get("bookmakers") or []:
        if not isinstance(book, dict) or book.get("kind") not in (None, "sportsbook"):
            continue
        markets = book.get("markets") or {}
        if isinstance(markets, list):
            market_items = [(m.get("key"), m) for m in markets if isinstance(m, dict)]
        elif isinstance(markets, dict):
            market_items = list(markets.items())
        else:
            continue
        updated_book = parse_time(book.get("last_update"))
        for market_key, market in market_items:
            if market_key not in ("h2h", "spreads", "totals"):
                continue
            if isinstance(market, dict):
                outcomes = market.get("outcomes") or []
                updated = parse_time(market.get("last_update")) or updated_book
            elif isinstance(market, list):
                outcomes, updated = market, updated_book
            else:
                continue
            if updated is None:
                continue
            age = (datetime.now(timezone.utc) - updated.astimezone(timezone.utc)).total_seconds()
            if age < -60 or age > 900:
                continue
            for outcome in outcomes:
                if not isinstance(outcome, dict):
                    continue
                odds = american_to_decimal(outcome.get("price"))
                if odds is None or odds < MIN_ODDS:
                    continue
                candidates.append({
                    "market": market_key,
                    "selection": str(outcome.get("name", "نامشخص")),
                    "odds": odds,
                    "point": outcome.get("point"),
                    "book": book.get("title") or book.get("key") or "Sportsbook",
                    "updated": updated,
                })
    return candidates

def board_sports():
    try:
        r = requests.get(f"{BASE}/sports", headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json().get("sports", [])
    except Exception as exc:
        print("Board sport discovery failed:", type(exc).__name__, str(exc)[:160])
        return []

def collect():
    now = datetime.now(timezone.utc)
    tomorrow = (datetime.now(TEHRAN).date() + timedelta(days=1))
    found, errors, stats = [], [], []
    available = board_sports()
    available_keys = {x.get("key"): x.get("title", x.get("key", "")) for x in available if isinstance(x, dict)}
    sports_to_scan = []
    if "soccer" in available_keys:
        sports_to_scan.append(("فوتبال", "soccer"))
    # Do NOT scan NBA: the requested basketball competition is Japan's B.League only.
    # Accept provider keys/titles that explicitly identify B.League or Japanese basketball.
    for key, title in available_keys.items():
        label = (str(key) + " " + str(title)).lower().replace("-", " ")
        is_japan_basketball = (
            "b.league" in label or "b league" in label or
            ("japan" in label and ("basket" in label or "b1" in label or "b2" in label or "b3" in label))
        )
        if is_japan_basketball:
            sports_to_scan.append(("بسکتبال ژاپن", key))
    for index, (label, sport) in enumerate(sports_to_scan):
        if index:
            time.sleep(1.1)
        try:
            response = requests.get(f"{BASE}/{sport}", headers=HEADERS, timeout=TIMEOUT)
            response.raise_for_status()
            fixtures = response.json().get("fixtures") or []
            count = 0
            for fixture in fixtures:
                if not isinstance(fixture, dict):
                    continue
                start = parse_time(fixture.get("commence_time"))
                if start is None or start.astimezone(TEHRAN).date() != tomorrow:
                    continue
                if sport == "soccer" and not allowed_competition(fixture):
                    continue
                count += 1
                markets = get_market_candidates(fixture)
                for market in markets:
                    found.append({
                        "sport": label,
                        "home": fixture.get("home_team", "نامشخص"),
                        "away": fixture.get("away_team", "نامشخص"),
                        "start": start,
                        **market,
                    })
            stats.append(f"{label}: {count} مسابقه فردا")
        except Exception as exc:
            errors.append(f"{label}: {type(exc).__name__} {str(exc)[:100]}")
            stats.append(f"{label}: خطا")
    if "بسکتبال ژاپن" not in [x[0] for x in sports_to_scan]:
        stats.append("بسکتبال ژاپن: در فهرست سرویس فعلی موجود نیست")
    return found, errors, stats, tomorrow

def deduplicate_picks(picks):
    # Different bookmakers quoting the same event/market/selection are ONE pick.
    # Keep the best (highest) decimal price and its bookmaker.
    unique = {}
    for pick in picks:
        key = (
            pick["sport"],
            pick["home"],
            pick["away"],
            pick["start"].isoformat(),
            pick["market"],
            pick["selection"],
            str(pick.get("point")),
        )
        current = unique.get(key)
        if current is None or pick["odds"] > current["odds"]:
            unique[key] = pick
    return list(unique.values())


def resolve_chat_id():
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN secret تنظیم نشده است.")
    me = requests.get(f"https://api.telegram.org/bot{TOKEN}/getMe", timeout=TIMEOUT)
    me.raise_for_status()
    if not me.json().get("ok"):
        raise RuntimeError("Telegram getMe failed")
    updates = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates",
                           params={"limit": 20, "allowed_updates": ["message"]}, timeout=TIMEOUT)
    updates.raise_for_status()
    for update in reversed(updates.json().get("result", [])):
        msg = update.get("message") or update.get("edited_message") or {}
        chat = msg.get("chat") or {}
        if chat.get("type") == "private" and chat.get("id") is not None:
            return str(chat["id"])
    return CHAT_ID

def send_telegram(message):
    target = resolve_chat_id()
    response = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                             data={"chat_id": target, "text": message, "disable_web_page_preview": True},
                             timeout=TIMEOUT)
    response.raise_for_status()
    if not response.json().get("ok"):
        raise RuntimeError("Telegram sendMessage failed")

def main():
    raw_picks, errors, stats, tomorrow = collect()
    picks = deduplicate_picks(raw_picks)
    # Higher available price is better for the same selection.
    picks.sort(key=lambda x: x["odds"], reverse=True)
    lines = [
        "🧠 VIVA AI | پیش‌بینی مسابقات فردا",
        f"📅 {tomorrow.isoformat()} | ارسال ۲۲:۰۰ تهران",
        f"💎 حداقل ضریب: {MIN_ODDS:.2f}",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if picks:
        for i, p in enumerate(picks[:12], 1):
            local_time = p["start"].astimezone(TEHRAN).strftime("%H:%M")
            market_names = {"h2h": "نتیجه مسابقه", "spreads": "هندیکپ", "totals": "مجموع امتیاز/گل"}
            point = f" ({p['point']})" if p.get("point") is not None else ""
            lines += [
                f"#{i} {p['sport']} | {p['home']} 🆚 {p['away']}",
                f"🕒 {local_time} | 🎯 {market_names.get(p['market'], p['market'])}: {p['selection']}{point}",
                f"💎 ضریب: {p['odds']:.2f} | منبع: {p['book']}",
                "────────────",
            ]
    else:
        lines.append("امشب گزینه‌ای با ضریب ۱٫۴۸ به بالا در رقابت‌های قابل‌تأیید پیدا نشد؛ پیش‌بینی حدسی ارسال نمی‌شود.")
    lines += [
        f"گزینه‌های یکتا پس از حذف تکرار ضرایب سایت‌ها: {len(picks)}",
        f"نقل‌قول‌های خام بازار از همه سایت‌ها: {len(raw_picks)}",
        "بازارهای فعلی این منبع: نتیجه، هندیکپ و مجموع گل/امتیاز؛ کرنر، خطا، کارت، VAR و گل‌زن بازیکن در فید عمومی فعلی ارائه نمی‌شوند.",
        "لیگ زنان اسپانیا/آلمان، لیگ‌های ایران/امارات/عربستان و J2 فقط وقتی مسابقه می‌گیرند که فید نام رقابت را مشخص کند.",
        "بسکتبال NBA اسکن نمی‌شود؛ فقط لیگ بسکتبال ژاپن (B.League) در صورت ارائه کلید معتبر از سوی منبع بررسی می‌شود.",
        "این خروجی تحلیل قطعی یا تضمین نتیجه نیست.",
        "وضعیت منابع: " + "؛ ".join(stats),
    ]
    if errors:
        lines.append("خطاها: " + "؛ ".join(errors[:4]))
    send_telegram("\n".join(lines))

if __name__ == "__main__":
    main()
