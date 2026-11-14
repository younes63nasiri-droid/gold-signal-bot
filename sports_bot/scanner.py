import os
import time
import json
import random
import subprocess
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
MAX_ODDS = 5.00
STATE_FILE = "sports_bot/viva_state.json"
MIN_SEND_MINUTES = 45
MAX_SEND_MINUTES = 150  # Reject malformed/prediction-market prices masquerading as decimal odds.
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

def valid_market_line(market, point):
    if point is None:
        return True
    try:
        line = abs(float(point))
    except (TypeError, ValueError):
        return False
    if market == "totals":
        return line <= 4.5
    if market == "spreads":
        return line <= 2.5
    return True

# Only requested competitions. Matching is deliberately fail-closed:
# if the feed does not identify a competition, that fixture is not selected.
LEAGUES = {
    "england": ("premier league", "english premier league"),
    "spain": ("la liga", "laliga"),
    "germany": ("bundesliga",),
    "italy": ("serie a",),
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
                point = outcome.get("point")
                odds = american_to_decimal(outcome.get("price"))
                if odds is None or odds < MIN_ODDS:
                    continue
                candidates.append({
                    "market": market_key,
                    "selection": str(outcome.get("name", "نامشخص")),
                    "odds": odds,
                    "point": point,
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
                            point = outcome.get("point")
                            if not valid_market_line(market["key"], point):
                                continue
                            picks.append({
                                "sport": sport_label,
                                "sport_key": sport_key,
                                "event_id": str(fixture.get("id", "")),
                                "league": competition_text(fixture),
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
                    if not valid_market_line(market_key, point):
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
                            "sport_key": sport,
                            "event_id": str(event.get("id", "")),
                            "league": league_name,
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
                        "sport_key": sport,
                        "event_id": str(fixture.get("id", "")),
                        "league": competition_text(fixture),
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

def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            state = json.load(handle)
            if isinstance(state, dict):
                return state
    except (OSError, json.JSONDecodeError):
        pass
    return {"sent": {}, "pending": {}, "next_send_at": None, "settled": {}}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2)
    # Persist deduplication and settlement state between GitHub Actions runs.
    try:
        subprocess.run(["git", "config", "user.name", "VIVA Bot"], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "viva-bot@users.noreply.github.com"], check=True, capture_output=True)
        subprocess.run(["git", "add", STATE_FILE], check=True, capture_output=True)
        changed = subprocess.run(["git", "diff", "--cached", "--quiet"])
        if changed.returncode == 1:
            subprocess.run(["git", "commit", "-m", "Update VIVA sent picks and results"], check=True, capture_output=True)
            subprocess.run(["git", "push", "origin", "HEAD:main"], check=True, capture_output=True)
    except Exception as exc:
        print("STATE_PERSISTENCE_ERROR:", type(exc).__name__, str(exc)[:180])
        raise


def pick_identity(pick):
    return "|".join(str(pick.get(k, "")) for k in (
        "event_id", "sport_key", "home", "away", "market", "selection", "point"
    ))


def grade_pick(pick, score):
    try:
        home_score, away_score = int(score["home"]), int(score["away"])
    except (KeyError, TypeError, ValueError):
        return None
    market = pick.get("market")
    selection = str(pick.get("selection", "")).strip().lower()
    point = float(pick["point"]) if pick.get("point") is not None else 0.0
    home = str(pick.get("home", "")).strip().lower()
    away = str(pick.get("away", "")).strip().lower()
    if market in ("h2h", "h2h_3_way"):
        if home_score == away_score:
            actual = "draw"
        elif home_score > away_score:
            actual = home
        else:
            actual = away
        if selection in ("draw", "tie", "مساوی"):
            return "برد" if actual == "draw" else "باخت"
        if selection == home or selection in home:
            return "برد" if actual == home else "باخت"
        if selection == away or selection in away:
            return "برد" if actual == away else "باخت"
        return None
    if market == "totals":
        total = home_score + away_score
        if selection in ("over", "بالای"):
            signed = total - point
        elif selection in ("under", "زیر"):
            signed = point - total
        else:
            return None
        # Asian quarter totals split the stake over the two adjacent half-lines.
        fraction = round(point % 0.5, 2)
        if fraction in (0.25, 0.75):
            lower = point - 0.25 if fraction == 0.25 else point - 0.25
            upper = point + 0.25
            a = total - lower if selection in ("over", "بالای") else lower - total
            b = total - upper if selection in ("over", "بالای") else upper - total
            outcomes = ["برد" if x > 0 else ("برگشت" if x == 0 else "باخت") for x in (a, b)]
            if outcomes.count("برد") == 2:
                return "برد کامل"
            if outcomes.count("باخت") == 2:
                return "باخت"
            if "برد" in outcomes and "برگشت" in outcomes:
                return "نیم‌برد"
            if "باخت" in outcomes and "برگشت" in outcomes:
                return "نیم‌باخت"
            return "برگشت"
        return "برد" if signed > 0 else ("باخت" if signed < 0 else "برگشت")
    if market == "spreads":
        if selection == home or selection in home:
            margin = home_score + point - away_score
        elif selection == away or selection in away:
            margin = away_score + point - home_score
        else:
            return None
        return "برد" if margin > 0 else ("باخت" if margin < 0 else "برگشت")
    return None


def check_pending_results(state):
    pending = state.setdefault("pending", {})
    settled = state.setdefault("settled", {})
    by_sport = {}
    for key, item in list(pending.items()):
        sport_key = item.get("sport_key") or "soccer"
        event_id = item.get("event_id")
        if event_id:
            by_sport.setdefault(sport_key, []).append((key, item))
    for sport_key, rows in by_sport.items():
        try:
            data = oddslink_authenticated_get(
                f"sports/{sport_key}/events",
                {"eventIds": ",".join(item["event_id"] for _, item in rows)},
            )
            events = data.get("events", data.get("data", [])) if isinstance(data, dict) else data
            event_map = {str(e.get("id")): e for e in events or [] if isinstance(e, dict)}
            for key, item in rows:
                event = event_map.get(str(item["event_id"]), {})
                if str(event.get("status", "")).lower() != "finished":
                    continue
                score = event.get("score")
                if not isinstance(score, dict):
                    continue
                verdict = grade_pick(item, score)
                if verdict is None:
                    continue
                settled[key] = {**item, "score": score, "verdict": verdict,
                                "settled_at": datetime.now(timezone.utc).isoformat()}
                pending.pop(key, None)
                results = list(settled.values())
                wins = sum(1 for x in results if x.get("verdict") in ("برد", "برد کامل", "نیم‌برد"))
                losses = sum(1 for x in results if x.get("verdict") in ("باخت", "نیم‌باخت"))
                pushes = sum(1 for x in results if x.get("verdict") == "برگشت")
                graded = wins + losses + pushes
                rate = (wins / graded * 100) if graded else 0.0
                send_telegram(
                    "📊 VIVA | نتیجه پیش‌بینی\\n"
                    f"{item.get('home')} 🆚 {item.get('away')}\\n"
                    f"پیش‌بینی: {item.get('selection')} {item.get('point') or ''}\\n"
                    f"نتیجه نهایی: {score.get('home')} - {score.get('away')}\\n"
                    f"ارزیابی: {verdict}\\n"
                    "━━━━━━━━━━━━━━━━━━\\n"
                    f"آمار ثبت‌شده: {graded} | برد: {wins} | باخت: {losses} | برگشت: {pushes}\\n"
                    f"درصد برد: {rate:.1f}%"
                )
        except Exception as exc:
            print("RESULT_CHECK_ERROR:", sport_key, type(exc).__name__, str(exc)[:140])


def main():
    state = load_state()
    check_pending_results(state)

    now = datetime.now(timezone.utc)
    next_send = parse_time(state.get("next_send_at"))
    raw_picks, errors, stats, target_date = collect()
    picks = deduplicate_picks(raw_picks)
    # Keep only upcoming fixtures and picks never sent before.
    sent = state.setdefault("sent", {})
    available = []
    for pick in picks:
        if not pick.get("event_id"):
            continue
        if pick["start"].astimezone(timezone.utc) <= now:
            continue
        identity = pick_identity(pick)
        if identity not in sent:
            available.append(pick)

    should_send = next_send is None or now >= next_send.astimezone(timezone.utc)
    if should_send and available:
        # Send up to three distinct eligible picks in one batch.
        chosen_picks = random.sample(available, k=min(3, len(available)))
        market_names = {"h2h": "نتیجه مسابقه", "h2h_3_way": "نتیجه مسابقه", "spreads": "هندیکپ", "totals": "مجموع گل/امتیاز"}
        sent_lines = []
        for index, chosen in enumerate(chosen_picks, 1):
            identity = pick_identity(chosen)
            local_time = chosen["start"].astimezone(TEHRAN).strftime("%H:%M")
            point = f" ({chosen['point']})" if chosen.get("point") is not None else ""
            sent_lines.append(
                f"#{index} {chosen['home']} 🆚 {chosen['away']}\n"
                f"🏆 {chosen.get('league') or 'رقابت تأییدشده'} | 🕒 {local_time} تهران\n"
                f"🎯 {market_names.get(chosen['market'], chosen['market'])}: {chosen['selection']}{point}\n"
                f"💎 ضریب: {chosen['odds']:.2f} | منبع: {chosen['book']}"
            )
            sent[identity] = now.isoformat()
            state.setdefault("pending", {})[identity] = {**chosen, "sent_at": now.isoformat()}
        message = (
            "🧠 VIVA AI | سه پیش‌بینی تصادفی\n"
            + "\n━━━━━━━━━━━━━━━━━━\n".join(sent_lines)
            + "\n\nهر انتخاب فقط یک‌بار ارسال می‌شود.\n"
            "این پیش‌بینی‌ها تضمین نتیجه نیستند."
        )
        send_telegram(message)
        delay = random.randint(MIN_SEND_MINUTES, MAX_SEND_MINUTES)
        state["next_send_at"] = (now + timedelta(minutes=delay)).isoformat()
        print("SENT_PICKS:", len(chosen_picks), "NEXT_RANDOM_MINUTES:", delay)
    elif should_send:
        # Retry the search soon when there are no qualifying fixtures.
        state["next_send_at"] = (now + timedelta(minutes=30)).isoformat()
        print("NO_NEW_ELIGIBLE_PICKS")
    else:
        print("WAITING_FOR_RANDOM_SEND_WINDOW:", state.get("next_send_at"))

    # Keep state bounded without losing pending picks or recent duplicate protection.
    if len(sent) > 3000:
        keep = dict(list(sent.items())[-2500:])
        for key in state.get("pending", {}):
            if key in sent:
                keep[key] = sent[key]
        state["sent"] = keep
    if len(state.get("settled", {})) > 2000:
        state["settled"] = dict(list(state["settled"].items())[-1500:])

    save_state(state)



if __name__ == "__main__":
    main()
