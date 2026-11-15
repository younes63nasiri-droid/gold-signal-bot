import os
import time
import statistics
import hashlib
import json
from pathlib import Path
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
MAX_ODDS = 15.00  # Reject malformed/prediction-market prices masquerading as decimal odds.
EXCLUDED_BOOKS = ("polymarket", "kalshi", "matchbook")

def valid_decimal_odds(value):
    try:
        odds = float(value)
    except (TypeError, ValueError):
        return False
    return MIN_ODDS <= odds <= MAX_ODDS

def allowed_bookmaker(name):
    label = str(name or "").strip().lower()
    return bool(label) and not any(blocked in label for blocked in EXCLUDED_BOOKS)

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
    "japan2": ("j2 league", "japan j2", "j league 2"),
}
WOMEN_HINTS = ("women", "woman", "femen", "frauen", "womens")
NATIONAL_HINTS = ("uefa nations league", "fifa world cup", "world cup qualifier",
                  "international friendly", "friendly international", "afc asian cup",
                  "africa cup of nations", "copa america", "uefa european championship", "world cup",
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
    # Only Spain women's league is in the requested competition list.
    if any(h in label for h in WOMEN_HINTS):
        return any(h in label for h in LEAGUES["women_spain"])
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



ODDSLINK_API_KEY = os.getenv("ODDSLINK_API_KEY", "").strip()
ODDSLINK_API_BASE = "https://oddslink.app/v1"

def oddslink_authenticated_get(path, params=None):
    response = requests.get(
        f"{ODDSLINK_API_BASE}/{path.lstrip('/')}",
        headers={**HEADERS, "X-API-Key": ODDSLINK_API_KEY},
        params=params or {},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data["error"])[:160])
    return data

def collect_oddslink_authenticated(tomorrow):
    if not ODDSLINK_API_KEY:
        return [], [], ["OddsLink: کلید API تنظیم نشده"]
    picks, stats, errors = [], [], []
    try:
        sports = oddslink_authenticated_get("sports")
        if isinstance(sports, dict):
            sports = sports.get("sports") or sports.get("data") or []
        if not isinstance(sports, list):
            raise RuntimeError("فرمت فهرست ورزش‌ها نامعتبر است")
    except Exception as exc:
        return [], [], [f"OddsLink: {type(exc).__name__} {str(exc)[:120]}"]

    # Aggregate soccer returns all competitions carried by OddsLink.
    sport_jobs = [("فوتبال", "soccer", True)]
    for item in sports:
        if not isinstance(item, dict):
            continue
        label = " ".join(str(item.get(k, "")) for k in ("key", "title", "group", "description")).lower()
        if ("b.league" in label or "b league" in label or
            ("japan" in label and ("basketball" in label or "basket" in label))):
            key = str(item.get("key", "")).strip()
            if key:
                sport_jobs.append(("بسکتبال ژاپن", key, False))

    for sport_label, sport_key, is_soccer in sport_jobs:
        try:
            data = oddslink_authenticated_get(
                f"sports/{sport_key}/odds",
                {"oddsFormat": "decimal", "markets": "h2h,spreads,totals"},
            )
            if isinstance(data, dict):
                fixtures = data.get("fixtures") or data.get("data") or []
            else:
                fixtures = data
            count = 0
            for fixture in fixtures or []:
                if not isinstance(fixture, dict):
                    continue
                start = parse_time(fixture.get("commence_time"))
                if start is None or start.astimezone(TEHRAN).date() != tomorrow:
                    continue
                if is_soccer and not allowed_competition(fixture):
                    continue
                count += 1
                for book in fixture.get("bookmakers") or []:
                    if not isinstance(book, dict):
                        continue
                    for market in book.get("markets") or []:
                        if not isinstance(market, dict) or market.get("key") not in ("h2h", "spreads", "totals"):
                            continue
                        updated = parse_time(market.get("last_update")) or parse_time(book.get("last_update"))
                        if updated is None:
                            continue
                        age = (datetime.now(timezone.utc)-updated.astimezone(timezone.utc)).total_seconds()
                        if age < -60 or age > 900:
                            continue
                        for outcome in market.get("outcomes") or []:
                            if not isinstance(outcome, dict):
                                continue
                            try:
                                price = float(outcome.get("price"))
                            except (TypeError, ValueError):
                                continue
                            if not valid_decimal_odds(price) or not allowed_bookmaker(book.get("title") or book.get("key")):
                                continue
                            picks.append({
                                "sport": sport_label,
                                "home": fixture.get("home_team", "نامشخص"),
                                "away": fixture.get("away_team", "نامشخص"),
                                "start": start,
                                "market": market["key"],
                                "selection": str(outcome.get("name", "نامشخص")),
                                "odds": price,
                                "point": outcome.get("point"),
                                "book": book.get("title") or book.get("key") or "OddsLink",
                                "updated": updated,
                            })
            stats.append(f"OddsLink API {sport_label}: {count} مسابقه فردا")
        except Exception as exc:
            errors.append(f"OddsLink API {sport_label}: {type(exc).__name__} {str(exc)[:100]}")
            stats.append(f"OddsLink API {sport_label}: خطا")
    if len(sport_jobs) == 1:
        stats.append("بسکتبال ژاپن در فهرست ورزش‌های OddsLink API موجود نیست")
    return picks, errors, stats

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()
ODDS_API_BASE = "https://api.odds-api.io/v3"

def odds_api_get(path, params):
    response = requests.get(f"{ODDS_API_BASE}/{path}", params=params, timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(str(data.get("error"))[:160])
    return data

def odds_api_market_candidates(event_odds):
    results = []
    books = event_odds.get("bookmakers") or {}
    for book_name, markets in books.items():
        for market in markets or []:
            name = str(market.get("name", "")).strip().lower()
            rows = market.get("odds") or []
            for row in rows:
                selections = []
                if name in ("ml", "moneyline", "match result"):
                    selections = [("home", row.get("home")), ("draw", row.get("draw")), ("away", row.get("away"))]
                    market_key = "h2h"
                elif name in ("spread", "handicap", "asian handicap"):
                    selections = [("home", row.get("home")), ("away", row.get("away"))]
                    market_key = "spreads"
                elif name in ("totals", "total", "goals over/under", "points over/under"):
                    selections = [("over", row.get("over")), ("under", row.get("under"))]
                    market_key = "totals"
                else:
                    continue
                point = row.get("hdp", row.get("max", row.get("line")))
                for side, raw_price in selections:
                    try:
                        price = float(raw_price)
                    except (TypeError, ValueError):
                        continue
                    if not valid_decimal_odds(price) or not allowed_bookmaker(book_name):
                        continue
                    if side == "home":
                        selection = str(event_odds.get("home", "میزبان"))
                    elif side == "away":
                        selection = str(event_odds.get("away", "میهمان"))
                    else:
                        selection = "مساوی" if side == "draw" else side
                    results.append({
                        "market": market_key,
                        "selection": selection,
                        "odds": price,
                        "point": point,
                        "book": book_name,
                        "updated": parse_time(market.get("updatedAt")) or datetime.now(timezone.utc),
                    })
    return results

def collect_odds_api(tomorrow):
    if not ODDS_API_KEY:
        return [], [], ["Odds-API.io: کلید API تنظیم نشده"]
    picks, stats, errors = [], [], []
    # The official API requires an explicit bookmaker list for /odds/multi.
    try:
        selected = odds_api_get("bookmakers/selected", {"apiKey": ODDS_API_KEY})
        if isinstance(selected, dict):
            selected = selected.get("bookmakers") or selected.get("data") or []
        bookmaker_names = [
            str(x.get("name", "")).strip() for x in (selected or [])
            if isinstance(x, dict) and x.get("name")
        ]
        if not bookmaker_names:
            raise RuntimeError("هیچ سایت شرط‌بندی در پنل API انتخاب نشده؛ حداقل یک Bookmaker انتخاب کن")
        bookmaker_names = bookmaker_names[:30]
    except Exception as exc:
        return [], [], [f"Odds-API.io bookmakers: {type(exc).__name__} {str(exc)[:120]}"]
    wanted_sports = (("فوتبال", "football"), ("بسکتبال ژاپن", "basketball"))
    for label, sport in wanted_sports:
        try:
            events = odds_api_get("events", {
                "apiKey": ODDS_API_KEY, "sport": sport, "status": "pending",
                "limit": 1000
            })
            if not isinstance(events, list):
                raise RuntimeError("فرمت فهرست مسابقات نامعتبر است")
            selected = []
            for event in events:
                league = event.get("league") or {}
                league_name = league.get("name", "") if isinstance(league, dict) else str(league)
                start = parse_time(event.get("date"))
                if start is None or start.astimezone(TEHRAN).date() != tomorrow:
                    continue
                fixture = {
                    "league": league_name,
                    "home_team": event.get("home", ""),
                    "away_team": event.get("away", ""),
                }
                if sport == "football":
                    if not allowed_competition(fixture):
                        continue
                else:
                    league_text = str(league_name).lower()
                    if not ("b.league" in league_text or "b league" in league_text or
                            "japan" in league_text or "japan" in str(league.get("slug", "") if isinstance(league, dict) else "").lower()):
                        continue
                selected.append((event, start, league_name))
            for offset in range(0, len(selected), 10):
                batch = selected[offset:offset + 10]
                payload = odds_api_get("odds/multi", {
                    "apiKey": ODDS_API_KEY,
                    "eventIds": ",".join(str(item[0].get("id")) for item in batch),
                    "bookmakers": ",".join(bookmaker_names),
                })
                if isinstance(payload, dict):
                    payload = payload.get("events") or payload.get("data") or [payload]
                by_id = {str(item.get("id")): item for item in payload if isinstance(item, dict)}
                for event, start, league_name in batch:
                    odds_event = by_id.get(str(event.get("id")), {})
                    for market in odds_api_market_candidates(odds_event):
                        picks.append({
                            "sport": label,
                            "home": event.get("home", "نامشخص"),
                            "away": event.get("away", "نامشخص"),
                            "start": start,
                            "league": league_name,
                            **market,
                        })
            stats.append(f"Odds-API.io {label}: {len(selected)} مسابقه واجدشرایط")
        except Exception as exc:
            errors.append(f"Odds-API.io {label}: {type(exc).__name__} {str(exc)[:110]}")
            stats.append(f"Odds-API.io {label}: خطا")
    return picks, errors, stats


def norm_team(value):
    import unicodedata
    value = unicodedata.normalize('NFKD', str(value or '').lower())
    value = ''.join(ch for ch in value if not unicodedata.combining(ch))
    return ''.join(ch for ch in value if ch.isalnum())

def fixture_crosschecks(tomorrow):
    """Independent fixture schedule checks; these feeds do not provide odds."""
    found, errors = {}, []
    headers = {**HEADERS, 'User-Agent': 'Mozilla/5.0 (compatible; VIVA-AI-Sports/1.0)'}
    sources = [
        ('SofaScore', 'https://www.sofascore.com/api/v1/sport/football/scheduled-events/' + tomorrow.isoformat()),
        ('FotMob', 'https://www.fotmob.com/api/matches?date=' + tomorrow.strftime('%Y%m%d')),
    ]
    for source, url in sources:
        try:
            response = requests.get(url, headers=headers, timeout=TIMEOUT)
            response.raise_for_status()
            data = response.json()
            rows = data.get('events', []) if source == 'SofaScore' else data.get('matches', [])
            for row in rows:
                if source == 'SofaScore':
                    home = (row.get('homeTeam') or {}).get('name')
                    away = (row.get('awayTeam') or {}).get('name')
                    stamp = row.get('startTimestamp')
                    if not home or not away or not stamp: continue
                    start = datetime.fromtimestamp(int(stamp), timezone.utc)
                else:
                    h, a = row.get('home') or {}, row.get('away') or {}
                    home = h.get('name') if isinstance(h, dict) else str(h)
                    away = a.get('name') if isinstance(a, dict) else str(a)
                    status = row.get('status') or {}
                    start = parse_time(status.get('utcTime') or row.get('utcTime'))
                    if not home or not away or start is None: continue
                if start.astimezone(TEHRAN).date() != tomorrow: continue
                found.setdefault((norm_team(home), norm_team(away)), set()).add(source)
        except Exception as exc:
            errors.append(f'{source}: {type(exc).__name__} {str(exc)[:90]}')
    return found, errors
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
        stats.append("بسکتبال ژاپن در OddsLink: در فهرست سرویس فعلی موجود نیست")
    # Prefer the authenticated OddsLink odds feed when its key is configured.
    # Keep the public board and Odds-API.io as independent fallbacks/supplementary sources.
    link_picks, link_errors, link_stats = collect_oddslink_authenticated(tomorrow)
    found.extend(link_picks)
    errors.extend(link_errors)
    stats.extend(link_stats)
    extra_picks, extra_errors, extra_stats = collect_odds_api(tomorrow)
    found.extend(extra_picks)
    errors.extend(extra_errors)
    stats.extend(extra_stats)
    schedules, schedule_errors = fixture_crosschecks(tomorrow)
    checked = 0
    for pick in found:
        key = (norm_team(pick.get('home')), norm_team(pick.get('away')))
        matched = schedules.get(key, set())
        pick['fixture_sources'] = sorted(matched)
        if matched: checked += 1
    stats.append(f'برنامه مسابقات: {len(schedules)} بازی از SofaScore/FotMob؛ {checked} انتخاب با تأیید مستقل')
    errors.extend(schedule_errors)
    return found, errors, stats, tomorrow

def deduplicate_picks(picks):
    # Merge quotes for the same selection across bookmakers. Keep the best
    # executable price, plus market consensus and quote coverage.
    grouped = {}
    for pick in picks:
        key = (
            pick["sport"], pick["home"], pick["away"],
            pick["start"].isoformat(), pick["market"],
            pick["selection"], str(pick.get("point")),
        )
        grouped.setdefault(key, []).append(pick)
    unique = []
    for quotes in grouped.values():
        best = max(quotes, key=lambda item: item["odds"]).copy()
        prices = [float(item["odds"]) for item in quotes]
        best["book_count"] = len({str(item.get("book", "")).lower() for item in quotes})
        best["median_odds"] = statistics.median(prices)
        best["quote_count"] = len(quotes)
        unique.append(best)
    return unique


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
    # Prioritize independent fixture confirmation and bookmaker quote coverage.
    # Median odds represent market consensus; best odds remain the price to compare.
    picks.sort(key=lambda x: (
        -len(x.get("fixture_sources", [])),
        -x.get("book_count", 1),
        x.get("median_odds", x["odds"]),
    ))
    state_path = Path("sports_bot/viva_sent.json")
    try:
        sent_ids = set(json.loads(state_path.read_text(encoding="utf-8")))
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        sent_ids = set()

    def pick_id(p):
        raw = "|".join(str(p.get(k, "")) for k in ("sport", "home", "away", "market", "selection", "point"))
        raw += "|" + p["start"].isoformat()
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # Keep only genuinely upcoming fixtures; discard stale/in-play/past start times.
    now_utc = datetime.now(timezone.utc)
    upcoming = [p for p in picks
                if p.get("start") and p["start"].astimezone(timezone.utc) > now_utc + timedelta(minutes=5)]
    # One recommendation per match, regardless of how many 1X2 outcomes/books exist.
    fresh = []
    seen_fixtures = set()
    for p in upcoming:
        fixture_key = (
            p["sport"], norm_team(p["home"]), norm_team(p["away"]),
            p["start"].astimezone(timezone.utc).isoformat(),
        )
        if fixture_key in seen_fixtures:
            continue
        seen_fixtures.add(fixture_key)
        if pick_id(p) in sent_ids:
            continue
        fresh.append(p)
        if len(fresh) >= 5:
            break
    lines = [
        "🧠 VIVA AI | فرصت‌های جدید مسابقات",
        f"📅 مسابقات تاریخ {tomorrow.isoformat()}",
        f"💎 بازه ضرایب مجاز: {MIN_ODDS:.2f} تا {MAX_ODDS:.2f}",
        "━━━━━━━━━━━━━━━━━━",
    ]
    if fresh:
        for i, p in enumerate(fresh[:5], 1):
            local_time = p["start"].astimezone(TEHRAN).strftime("%H:%M")
            market_names = {"h2h": "نتیجه مسابقه", "spreads": "هندیکپ", "totals": "مجموع امتیاز/گل"}
            point = f" ({p['point']})" if p.get("point") is not None else ""
            lines += [
                f"#{i} {p['sport']} | {p['home']} 🆚 {p['away']}",
                f"🕒 {local_time} | 🎯 {market_names.get(p['market'], p['market'])}: {p['selection']}{point}",
                f"💎 ضریب: {p['odds']:.2f} | منبع: {p['book']}",
                (f"📊 تأیید برنامه: {', '.join(p['fixture_sources'])}" if p.get('fixture_sources') else "📊 تأیید برنامه مستقل: موجود نیست"),
                "────────────",
            ]
    else:
        lines.append("فعلاً فرصت جدید و تکرارنشده‌ای با ضریب ۱٫۴۸ به بالا پیدا نشد.")
    lines += [
        f"فرصت‌های جدید و یکتا برای ارسال: {len(fresh[:5])}",
        f"نقل‌قول‌های خام بازار از همه سایت‌ها: {len(raw_picks)}",
        "بازارهای فعلی این منبع: نتیجه، هندیکپ و مجموع گل/امتیاز؛ کرنر، خطا، کارت، VAR و گل‌زن بازیکن در فید عمومی فعلی ارائه نمی‌شوند.",
        "لیگ زنان اسپانیا/آلمان، لیگ‌های ایران/امارات/عربستان و J2 فقط وقتی مسابقه می‌گیرند که فید نام رقابت را مشخص کند.",
        "NBA اسکن نمی‌شود؛ بسکتبال ژاپن از منابعی بررسی می‌شود که لیگ B.League را در فهرست مسابقات ارائه کنند.",
        "ضرایب خارج از بازه ۱٫۴۸ تا ۱۵ و بازارهای پیش‌بینی/صرافی حذف می‌شوند.",
        "این خروجی تحلیل قطعی یا تضمین نتیجه نیست.",
        "وضعیت منابع: " + "؛ ".join(stats),
    ]
    if errors:
        lines.append("خطاها: " + "؛ ".join(errors[:4]))
    if fresh:
        send_telegram("\n".join(lines))
        sent_ids.update(pick_id(p) for p in fresh[:5])
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(sorted(sent_ids), ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Telegram sent {min(len(fresh), 5)} new unique picks.")
    else:
        print("No new unique picks; Telegram message skipped.")

if __name__ == "__main__":
    main()
