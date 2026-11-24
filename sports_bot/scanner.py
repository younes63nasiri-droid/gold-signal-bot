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
    keys = ("league", "competition", "tournament", "sport_title", "league_name",
            "competition_name", "displayName")
    for key in keys:
        value = fixture.get(key)
        if isinstance(value, dict):
            parts.extend(str(value.get(k, "")) for k in ("title", "name", "key", "displayName"))
        elif value:
            parts.append(str(value))
    for parent_key in ("event", "fixture", "competition_info"):
        parent = fixture.get(parent_key)
        if isinstance(parent, dict):
            for key in keys:
                value = parent.get(key)
                if isinstance(value, dict):
                    parts.extend(str(value.get(k, "")) for k in ("title", "name", "key", "displayName"))
                elif value:
                    parts.append(str(value))
    return " ".join(parts).lower()

def competition_name(fixture):
    # Providers use different field names and sometimes nest the competition.
    for key in ("league", "competition", "tournament", "league_name",
                "competition_name", "sport_title"):
        value = fixture.get(key)
        if isinstance(value, dict):
            value = (value.get("name") or value.get("title") or
                     value.get("displayName") or value.get("key"))
        if isinstance(value, str) and value.strip():
            return value.strip()
    # Some feeds put the competition label inside a nested event object.
    for parent_key in ("event", "fixture", "competition_info"):
        parent = fixture.get(parent_key)
        if isinstance(parent, dict):
            for key in ("league", "competition", "tournament", "name", "title"):
                value = parent.get(key)
                if isinstance(value, dict):
                    value = value.get("name") or value.get("title") or value.get("key")
                if isinstance(value, str) and value.strip():
                    return value.strip()
    return "لیگ نامشخص"

def allowed_competition(fixture):
    # Keep the user's requested competitions only. Never treat an unknown
    # competition label as eligible just because a provider returned a fixture.
    label = competition_text(fixture)
    if not label.strip():
        return False

    # International national-team competitions and friendlies.
    if any(term in label for term in NATIONAL_HINTS):
        return True

    # Spain's women's top division only; do not accidentally admit other
    # women's competitions or the men's Spanish league through broad aliases.
    is_women = any(term in label for term in WOMEN_HINTS)
    if is_women:
        return any(term in label for term in (
            "liga f", "primera division femenina", "spanish women's",
            "primera division women", "liga femenina"
        ))

    # Exclude women's fixtures from men's domestic competitions.
    if any(term in label for term in WOMEN_HINTS):
        return False

    return any(
        alias in label
        for aliases in LEAGUES.values()
        if aliases
        for alias in aliases
    )

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
            if age < -60 or age > 3600:
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
                start = parse_time(
                    fixture.get("commence_time") or fixture.get("start_time")
                    or fixture.get("startTime") or fixture.get("date")
                )
                if start is None or start.astimezone(TEHRAN).date() != tomorrow:
                    continue
                if is_soccer and not allowed_competition(fixture):
                    continue
                count += 1
                for book in fixture.get("bookmakers") or []:
                    if not isinstance(book, dict):
                        continue
                    raw_markets = book.get("markets") or []
                    if isinstance(raw_markets, dict):
                        market_rows = []
                        for market_key, market_value in raw_markets.items():
                            if isinstance(market_value, dict):
                                market_rows.append({"key": market_key, **market_value})
                            elif isinstance(market_value, list):
                                market_rows.append({"key": market_key, "outcomes": market_value})
                    elif isinstance(raw_markets, list):
                        market_rows = raw_markets
                    else:
                        market_rows = []
                    for market in market_rows:
                        if not isinstance(market, dict) or market.get("key") not in ("h2h", "spreads", "totals"):
                            continue
                        updated = parse_time(market.get("last_update")) or parse_time(book.get("last_update"))
                        if updated is None:
                            continue
                        age = (datetime.now(timezone.utc)-updated.astimezone(timezone.utc)).total_seconds()
                        if age < -60 or age > 3600:
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
                                "home": fixture.get("home_team") or fixture.get("home") or "نامشخص",
                                "away": fixture.get("away_team") or fixture.get("away") or "نامشخص",
                                "start": start,
                                "league": competition_name(fixture) or "لیگ نامشخص",
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


UK_ODDS_API_KEY = os.getenv("UK_ODDS_API_KEY", "").strip()
UK_ODDS_API_BASE = "https://api.ukoddsapi.com"

def ukodds_get(path, params=None):
    response = requests.get(
        f"{UK_ODDS_API_BASE}/{path.lstrip('/')}",
        headers={**HEADERS, "X-Api-Key": UK_ODDS_API_KEY},
        params=params or {}, timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("detail") and not data.get("events"):
        raise RuntimeError(str(data["detail"])[:160])
    return data

def collect_ukodds(tomorrow):
    """Read the documented UK Odds API markets/selections payload."""
    if not UK_ODDS_API_KEY:
        return [], [], ["UK Odds API: کلید UK_ODDS_API_KEY تنظیم نشده"]
    picks, errors, stats = [], [], []
    try:
        payload = ukodds_get("v1/football/events", {
            "schedule_date": tomorrow.isoformat(), "has_odds": "true",
            "per_page": 200, "page": 1,
        })
        events = payload.get("events", []) if isinstance(payload, dict) else []
        if not isinstance(events, list):
            raise RuntimeError("فرمت فهرست مسابقات نامعتبر است")
    except Exception as exc:
        return [], [f"UK Odds API events: {type(exc).__name__} {str(exc)[:120]}"], ["UK Odds API: خطا در دریافت مسابقات"]

    eligible = []
    for event in events:
        if not isinstance(event, dict):
            continue
        start = parse_time(event.get("kickoff_utc"))
        fixture = {"league": event.get("league_name", "")}
        try:
            has_odds = int(event.get("markets_with_odds") or 0) > 0
        except (TypeError, ValueError):
            has_odds = False
        if (start is None or start.astimezone(TEHRAN).date() != tomorrow
                or start <= datetime.now(timezone.utc)
                or not allowed_competition(fixture)
                or not event.get("event_id") or not has_odds):
            continue
        eligible.append((event, start))

    # Free plan: 300 metered odds requests/month; keep a small daily shortlist.
    eligible = eligible[:6]
    for event, start in eligible:
        try:
            odds_data = ukodds_get(
                f"v1/football/events/{event['event_id']}/odds",
                {"package": "core", "odds_format": "decimal"},
            )
            if not isinstance(odds_data, dict):
                raise RuntimeError("پاسخ ضرایب ساختار معتبر ندارد")
            captured = parse_time(odds_data.get("captured_at"))
            if captured is None:
                raise RuntimeError("زمان ثبت قیمت در پاسخ موجود نیست")
            age = (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds()
            if age < -60 or age > 3600:
                stats.append(f"UK Odds API {event.get('home_team','?')}–{event.get('away_team','?')}: قیمت قدیمی است")
                continue

            # Documented response: bookmakers=[{code,name}], markets=[{market_name,selections:[{selection_name,odds,bookmaker_code}]}]
            bookmaker_names = {
                str(row.get("code")): str(row.get("name") or row.get("code"))
                for row in (odds_data.get("bookmakers") or [])
                if isinstance(row, dict) and row.get("code")
            }
            markets = odds_data.get("markets") or []
            if not isinstance(markets, list):
                raise RuntimeError("ساختار markets نامعتبر است")
            for market in markets:
                if not isinstance(market, dict):
                    continue
                name = str(market.get("market_name") or "").lower()
                if any(x in name for x in ("corner", "card", "goalscorer", "scorer", "player")):
                    continue
                if any(x in name for x in ("1x2", "match winner", "match result", "winner", "match odds")):
                    market_key = "h2h"
                elif "handicap" in name or "asian" in name or "spread" in name:
                    market_key = "spreads"
                elif "total" in name or "over/under" in name or "over under" in name:
                    market_key = "totals"
                else:
                    continue
                for selection in market.get("selections") or []:
                    if not isinstance(selection, dict):
                        continue
                    price = selection.get("odds")
                    code = str(selection.get("bookmaker_code") or "")
                    book = bookmaker_names.get(code, code)
                    if not valid_decimal_odds(price) or not allowed_bookmaker(book):
                        continue
                    picks.append({
                        "sport": "فوتبال",
                        "home": event.get("home_team") or "نامشخص",
                        "away": event.get("away_team") or "نامشخص",
                        "start": start,
                        "league": event.get("league_name") or "لیگ نامشخص",
                        "market": market_key,
                        "selection": str(selection.get("selection_name") or "نامشخص"),
                        "odds": float(price),
                        "point": selection.get("handicap", selection.get("point")),
                        "book": book or "UK Odds API",
                        "updated": captured,
                    })
        except Exception as exc:
            errors.append(f"UK Odds API {event.get('home_team','?')}–{event.get('away_team','?')}: {type(exc).__name__} {str(exc)[:90]}")
    stats.append(f"UK Odds API: {len(eligible)} مسابقه بررسی شد؛ {len(picks)} قیمت تازه و معتبر")
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

def collect_espn_public_odds(tomorrow):
    """Use ESPN's publicly accessible scoreboard/Core API odds; no API key."""
    picks, stats, errors = [], [], []
    leagues = [
        ("eng.1", "Premier League"), ("esp.1", "La Liga"),
        ("ger.1", "Bundesliga"), ("ita.1", "Serie A"),
        ("fra.1", "Ligue 1"), ("uefa.champions", "Champions League"),
        ("uefa.europa", "Europa League"), ("uefa.europa.conf", "Conference League"),
        ("irn.1", "Iran Pro League"), ("ksa.1", "Saudi Pro League"),
        ("uae.1", "UAE Pro League"),
    ]
    checked = 0
    for league_key, league_label in leagues:
        try:
            response = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_key}/scoreboard",
                params={"dates": tomorrow.strftime("%Y%m%d")},
                headers=HEADERS, timeout=TIMEOUT,
            )
            if response.status_code in (404, 400):
                continue
            response.raise_for_status()
            events = response.json().get("events", [])
        except Exception as exc:
            errors.append(f"ESPN {league_label}: {type(exc).__name__} {str(exc)[:90]}")
            continue
        for event in events:
            if not isinstance(event, dict):
                continue
            start = parse_time(event.get("date"))
            competitions = event.get("competitions") or []
            if start is None or start.astimezone(TEHRAN).date() != tomorrow or not competitions:
                continue
            comp = competitions[0]
            comp_id = str(comp.get("id") or event.get("id") or "")
            event_id = str(event.get("id") or "")
            if not event_id or not comp_id:
                continue
            checked += 1
            try:
                odds_url = (
                    f"https://sports.core.api.espn.com/v2/sports/soccer/leagues/{league_key}"
                    f"/events/{event_id}/competitions/{comp_id}/odds"
                )
                odds_response = requests.get(odds_url, params={"limit": 10},
                                             headers=HEADERS, timeout=TIMEOUT)
                odds_response.raise_for_status()
                items = odds_response.json().get("items", [])
                for item in items:
                    if not isinstance(item, dict) or item.get("$ref"):
                        continue
                    provider = (item.get("provider") or {}).get("name") or "ESPN bookmaker"
                    home_ml = (item.get("homeTeamOdds") or {}).get("moneyLine")
                    away_ml = (item.get("awayTeamOdds") or {}).get("moneyLine")
                    draw_ml = (item.get("drawOdds") or {}).get("moneyLine")
                    for raw, selection in ((home_ml, "Home"), (draw_ml, "Draw"), (away_ml, "Away")):
                        decimal = american_to_decimal(raw) if raw is not None else None
                        if decimal is not None and MIN_ODDS <= decimal <= MAX_ODDS:
                            competitors = comp.get("competitors") or []
                            home_name = away_name = ""
                            for team in competitors:
                                if team.get("homeAway") == "home":
                                    home_name = (team.get("team") or {}).get("displayName", "")
                                elif team.get("homeAway") == "away":
                                    away_name = (team.get("team") or {}).get("displayName", "")
                            if not home_name or not away_name:
                                continue
                            picks.append({
                                "sport": "فوتبال", "league": league_label,
                                "home": home_name, "away": away_name, "start": start,
                                "market": "h2h", "selection": selection,
                                "odds": decimal, "point": None, "book": provider,
                                "fixture_sources": ["ESPN"],
                                "book_count": 1, "source": "ESPN public odds",
                            })
            except Exception as exc:
                errors.append(f"ESPN odds {event_id}: {type(exc).__name__} {str(exc)[:90]}")
    stats.append(f"ESPN عمومی: {checked} مسابقه بررسی شد؛ {len(picks)} انتخاب ضریب‌دار")
    return picks, errors, stats

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
    headers = {
        **HEADERS,
        'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Referer': 'https://www.sofascore.com/',
        'Origin': 'https://www.sofascore.com',
        'Accept-Language': 'en-US,en;q=0.9',
    }
    # SofaScore is currently returning HTTP 403 from GitHub Actions; skip it rather than
    # repeatedly failing. FotMob, ESPN and TheSportsDB remain independent schedule checks.
    sources = [
        ('FotMob', 'https://www.fotmob.com/api/data/matches?date=' + tomorrow.strftime('%Y%m%d')),
    ]
    for source, url in sources:
        try:
            response = requests.get(url, headers=headers, timeout=TIMEOUT)
            response.raise_for_status()
            data = response.json()
            if source == 'SofaScore':
                rows = data.get('events', [])
            else:
                # Current FotMob daily feed groups fixtures under leagues.
                rows = data.get('matches', [])
                if not rows and isinstance(data.get('leagues'), list):
                    rows = [
                        match
                        for league in data.get('leagues', [])
                        if isinstance(league, dict)
                        for match in (league.get('matches') or [])
                        if isinstance(match, dict)
                    ]
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
    # ESPN's public scoreboard provides a broad soccer schedule feed.
    # It is unofficial/undocumented, so it supplements rather than replaces
    # the other providers and is used only to verify fixture identity.
    try:
        response = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard",
            params={"dates": tomorrow.strftime("%Y%m%d")},
            headers=headers,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        for event in data.get("events") or []:
            if not isinstance(event, dict):
                continue
            start = parse_time(event.get("date"))
            if start is None or start.astimezone(TEHRAN).date() != tomorrow:
                continue
            competitions = event.get("competitions") or []
            if not competitions:
                continue
            competitors = competitions[0].get("competitors") or []
            home = away = None
            for competitor in competitors:
                team = competitor.get("team") or {}
                name = team.get("displayName") or team.get("name") or competitor.get("displayName")
                if competitor.get("homeAway") == "home":
                    home = name
                elif competitor.get("homeAway") == "away":
                    away = name
            if home and away:
                found.setdefault((norm_team(home), norm_team(away)), set()).add("ESPN")
    except Exception as exc:
        errors.append(f'ESPN: {type(exc).__name__} {str(exc)[:90]}')
    # Third independent schedule provider. The free TheSportsDB key has limited
    # event coverage/rate limits, so it supplements (never replaces) SofaScore/FotMob.
    try:
        api_key = os.getenv("THESPORTSDB_API_KEY", "123").strip() or "123"
        response = requests.get(
            f"https://www.thesportsdb.com/api/v1/json/{api_key}/eventsday.php",
            params={"d": tomorrow.isoformat(), "s": "Soccer"},
            headers=headers,
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        rows = data.get("events") or []
        for row in rows:
            if not isinstance(row, dict):
                continue
            home = row.get("strHomeTeam")
            away = row.get("strAwayTeam")
            if not home or not away:
                continue
            event_date = str(row.get("dateEvent") or "")
            if event_date != tomorrow.isoformat():
                continue
            found.setdefault((norm_team(home), norm_team(away)), set()).add("TheSportsDB")
    except Exception as exc:
        errors.append(f'TheSportsDB: {type(exc).__name__} {str(exc)[:90]}')
    return found, errors

def collect_match_previews(target_date, limit=5):
    """Build non-betting previews from public football fixtures and recent results."""
    headers = {
        **HEADERS,
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    fixtures = []
    # Primary schedule: FotMob.
    try:
        response = requests.get(
            "https://www.fotmob.com/api/data/matches",
            params={"date": target_date.strftime("%Y%m%d")},
            headers=headers, timeout=TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        rows = data.get("matches") or []
        if not rows and isinstance(data.get("leagues"), list):
            # FotMob's standard response nests fixtures under leagues[].matches.
            # Preserve the parent competition metadata while flattening.
            rows = [
                {**m,
                 "_league_name": league.get("name") or league.get("shortName") or "",
                 "_league_gender": league.get("gender") or ""}
                for league in data["leagues"] if isinstance(league, dict)
                for m in (league.get("matches") or []) if isinstance(m, dict)
            ]
        for row in rows:
            home_obj, away_obj = row.get("home") or {}, row.get("away") or {}
            home = home_obj.get("name") if isinstance(home_obj, dict) else str(home_obj)
            away = away_obj.get("name") if isinstance(away_obj, dict) else str(away_obj)
            status = row.get("status") or {}
            start = parse_time(status.get("utcTime") or row.get("utcTime"))
            # FotMob exposes the actual competition under tournament/leagueName.
            # Never use a stringified event label (often "Away at Home") as league.
            league_obj = row.get("tournament") or row.get("league") or {}
            league = row.get("_league_name") or ""
            if isinstance(league_obj, dict):
                league = (league or league_obj.get("name") or league_obj.get("leagueName") or
                          league_obj.get("parentLeagueName") or "")
            league = (league or row.get("leagueName") or row.get("parentLeagueName") or
                      row.get("tournamentName") or "")
            if not league or " at " in str(league).lower() or " vs " in str(league).lower():
                league = "نام لیگ در منبع مشخص نیست"
            gender = str(row.get("_league_gender") or "").lower()
            if gender in ("female", "women", "w"):
                league = f"{league} (زنان)"
            if not home or not away or start is None:
                continue
            if start.astimezone(TEHRAN).date() != target_date:
                continue
            if not allowed_competition({"league": league}):
                continue
            fixtures.append({
                "sport": "فوتبال", "home": home, "away": away,
                "start": start, "league": league or "لیگ نامشخص",
            })
    except Exception as exc:
        print("PREVIEW_FOTMOB_ERROR:", type(exc).__name__, str(exc)[:120])

    # Fallback schedule: ESPN soccer scoreboard. This prevents an empty preview
    # merely because FotMob changes its response shape or league labels.
    if not fixtures:
        try:
            response = requests.get(
                "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard",
                params={"dates": target_date.strftime("%Y%m%d")},
                headers=headers, timeout=TIMEOUT,
            )
            response.raise_for_status()
            for event in response.json().get("events") or []:
                start = parse_time(event.get("date"))
                if start is None or start.astimezone(TEHRAN).date() != target_date:
                    continue
                competitions = event.get("competitions") or []
                if not competitions:
                    continue
                competitors = competitions[0].get("competitors") or []
                sides = {}
                for item in competitors:
                    team = item.get("team") or {}
                    name = team.get("displayName") or team.get("name") or item.get("displayName")
                    if name and item.get("homeAway") in ("home", "away"):
                        sides[item["homeAway"]] = name
                if "home" not in sides or "away" not in sides:
                    continue
                season = event.get("season") or {}
                comp = competitions[0]
                notes = comp.get("notes") or []
                note_league = next((n.get("headline") for n in notes
                                    if isinstance(n, dict) and n.get("headline")), "")
                league_obj = event.get("league") or comp.get("league") or {}
                league = (note_league or
                          (league_obj.get("name") if isinstance(league_obj, dict) else "") or
                          comp.get("name") or season.get("name") or "")
                # event.name is commonly just "Away at Home", not the league.
                if not league or " at " in str(league).lower() or " vs " in str(league).lower():
                    league = "نام لیگ در منبع مشخص نیست"
                fixtures.append({
                    "sport": "فوتبال", "home": sides["home"], "away": sides["away"],
                    "start": start, "league": league,
                })
            print("PREVIEW_ESPN_FIXTURES:", len(fixtures))
        except Exception as exc:
            print("PREVIEW_ESPN_ERROR:", type(exc).__name__, str(exc)[:120])

    if not fixtures:
        print("PREVIEW_NO_FIXTURES")
        return []

    # ESPN scoreboard accepts one calendar date at a time.
    form = {}
    from concurrent.futures import ThreadPoolExecutor, as_completed
    dates = [target_date - timedelta(days=offset) for offset in range(1, 36)]
    def fetch_scoreboard(day):
        response = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/soccer/all/scoreboard",
            params={"dates": day.strftime("%Y%m%d")},
            headers=headers, timeout=10,
        )
        response.raise_for_status()
        return response.json().get("events") or []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fetch_scoreboard, day): day for day in dates}
        for future in as_completed(futures):
            try:
                events = future.result()
            except Exception as exc:
                print("PREVIEW_FORM_DAY_ERROR:", futures[future].isoformat(), type(exc).__name__)
                continue
            for event in events:
                event_date = parse_time(event.get("date"))
                if event_date is None or event_date.astimezone(TEHRAN).date() >= target_date:
                    continue
                competitions = event.get("competitions") or []
                if not competitions:
                    continue
                sides = {}
                for item in competitions[0].get("competitors") or []:
                    team = item.get("team") or {}
                    name = team.get("displayName") or team.get("name")
                    try:
                        score = float(item.get("score"))
                    except (TypeError, ValueError):
                        continue
                    if name and item.get("homeAway") in ("home", "away"):
                        sides[item["homeAway"]] = (name, score)
                if "home" not in sides or "away" not in sides:
                    continue
                (hn, hs), (an, away_score) = sides["home"], sides["away"]
                for name, scored, conceded in ((hn, hs, away_score), (an, away_score, hs)):
                    form.setdefault(norm_team(name), []).append({
                        "date": event_date, "scored": scored, "conceded": conceded,
                        "result": "W" if scored > conceded else "D" if scored == conceded else "L",
                    })
    print("PREVIEW_FORM_TEAMS:", len(form))

    previews = []
    for fixture in fixtures:
        home_form = sorted(form.get(norm_team(fixture["home"]), []), key=lambda x: x["date"])[-5:]
        away_form = sorted(form.get(norm_team(fixture["away"]), []), key=lambda x: x["date"])[-5:]
        # Include the fixture even if a free historical feed lacks one team's form.
        fixture["home_form"] = "".join(x["result"] for x in home_form) or "ناموجود"
        fixture["away_form"] = "".join(x["result"] for x in away_form) or "ناموجود"
        if home_form:
            fixture["home_avg_scored"] = sum(x["scored"] for x in home_form) / len(home_form)
            fixture["home_avg_conceded"] = sum(x["conceded"] for x in home_form) / len(home_form)
        if away_form:
            fixture["away_avg_scored"] = sum(x["scored"] for x in away_form) / len(away_form)
            fixture["away_avg_conceded"] = sum(x["conceded"] for x in away_form) / len(away_form)
        fixture["form_matches"] = min(len(home_form), len(away_form))
        previews.append(fixture)
    previews.sort(key=lambda x: (
        -(x.get("home_avg_scored", 0) + x.get("away_avg_scored", 0)),
        x["start"],
    ))
    return previews[:limit]

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
                        "league": competition_name(fixture) or "لیگ نامشخص",
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
    uk_picks, uk_errors, uk_stats = collect_ukodds(tomorrow)
    found.extend(uk_picks)
    errors.extend(uk_errors)
    stats.extend(uk_stats)
    espn_picks, espn_errors, espn_stats = collect_espn_public_odds(tomorrow)
    found.extend(espn_picks)
    errors.extend(espn_errors)
    stats.extend(espn_stats)
    extra_picks, extra_errors, extra_stats = collect_odds_api(tomorrow)
    found.extend(extra_picks)
    errors.extend(extra_errors)
    stats.extend(extra_stats)
    schedules, schedule_errors = fixture_crosschecks(tomorrow)
    checked = 0
    for pick in found:
        home_key = norm_team(pick.get('home'))
        away_key = norm_team(pick.get('away'))
        key = (home_key, away_key)
        # Some independent scoreboards reverse home/away for neutral venues.
        # Accept the reversed pairing as the same fixture, but never a one-team match.
        matched = set(schedules.get(key, set()))
        matched.update(schedules.get((away_key, home_key), set()))
        pick['fixture_sources'] = sorted(matched)
        if matched: checked += 1
    stats.append(f'برنامه مسابقات: {len(schedules)} بازی از SofaScore/FotMob/TheSportsDB/ESPN؛ {checked} انتخاب با تأیید مستقل')
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
    # Use the explicitly configured destination. getUpdates can fail with
    # Telegram 409 when a webhook/another poller is active, blocking delivery.
    if not TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN secret تنظیم نشده است.")
    if not CHAT_ID:
        raise RuntimeError("TELEGRAM_CHAT_ID secret تنظیم نشده است.")
    return CHAT_ID

def send_telegram(message):
    target = resolve_chat_id()
    response = requests.post(f"https://api.telegram.org/bot{TOKEN}/sendMessage",
                             data={"chat_id": target, "text": message, "parse_mode": "HTML",
                                   "disable_web_page_preview": True},
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

    def fixture_id(p):
        # History is fixture-level, not selection-level: a match can never be resent
        # merely because the market/selection/bookmaker changed on the next scan.
        raw = "|".join(str(p.get(k, "")) for k in ("sport", "home", "away"))
        raw += "|" + p["start"].astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    # Strictly reject matches that have started or start within the next 20 minutes.
    now_utc = datetime.now(timezone.utc)
    cutoff = now_utc + timedelta(minutes=20)
    upcoming = [p for p in picks
                if p.get("start")
                and p["start"].astimezone(timezone.utc) > cutoff
                # Accept an independent schedule confirmation OR matching odds
                # from at least two distinct bookmakers for the same fixture.
                and (bool(p.get("fixture_sources")) or p.get("book_count", 1) >= 2)]
    # Emit source/filter diagnostics in the Actions log so a zero-pick run
    # can be traced to provider failures, league filtering, timing, or history.
    upcoming_fixture_ids = {fixture_id(p) for p in upcoming}
    already_sent_fixture_ids = upcoming_fixture_ids & sent_ids
    independently_verified = sum(1 for p in picks if p.get("fixture_sources"))
    print(f"DIAGNOSTIC raw={len(raw_picks)} unique={len(picks)} independently_verified={independently_verified} upcoming_verified={len(upcoming)} upcoming_fixtures={len(upcoming_fixture_ids)} already_sent_fixtures={len(already_sent_fixture_ids)} sent_history={len(sent_ids)}")
    # Log exact fixture names/times behind the duplicate filter for diagnosis.
    if already_sent_fixture_ids:
        reported = set()
        for p in upcoming:
            fid = fixture_id(p)
            if fid in already_sent_fixture_ids and fid not in reported:
                reported.add(fid)
                print("HISTORY_MATCH:", p.get("sport"), "|", p.get("home"), "vs", p.get("away"),
                      "|", p["start"].astimezone(TEHRAN).isoformat(), "|", fid[:12])
    if upcoming_fixture_ids and len(already_sent_fixture_ids) == len(upcoming_fixture_ids):
        stats.append(f"توضیح صفر پیشنهاد: هر {len(already_sent_fixture_ids)} مسابقه واجدشرایط قبلاً ارسال شده است؛ برای جلوگیری از تکرار، دوباره ارسال نشد")
    for item in stats:
        print("SOURCE:", item)
    for item in errors:
        print("SOURCE_ERROR:", item)

    # One recommendation per fixture and never resend that fixture on later scans.
    fresh = []
    seen_fixtures = set()
    for p in upcoming:
        fid = fixture_id(p)
        if fid in seen_fixtures or fid in sent_ids:
            continue
        seen_fixtures.add(fid)
        fresh.append(p)
        if len(fresh) >= 10:
            break
    import html
    esc = lambda value: html.escape(str(value or ""), quote=False)
    displayed_preview_count = 0
    # Do not resend old previews or an empty "0 picks" report on every scan.
    # A Telegram message is sent only when there is a new qualified pick or preview.
    lines = [
        "✨ <b>VIVA AI</b>  <i>پیشنهادهای منتخب مسابقات</i>",
        f"📅 <b>{tomorrow.strftime('%d.%m.%Y')}</b>  |  به وقت تهران",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    # Always include fresh statistical previews, even when odds-qualified picks exist.
    # Preview data comes from fixture/results feeds, not bookmaker price feeds.
    analysis_fresh = []
    previews = collect_match_previews(tomorrow, limit=5)
    pick_fixture_keys = {
        (norm_team(p.get("home")), norm_team(p.get("away")))
        for p in fresh
    }
    for p in previews:
        analysis_id = "ANALYSIS|" + fixture_id(p)
        same_as_pick = (norm_team(p.get("home")), norm_team(p.get("away"))) in pick_fixture_keys
        if analysis_id not in sent_ids and not same_as_pick:
            analysis_fresh.append((p, analysis_id))

    if not fresh and not analysis_fresh:
        print("NO_NEW_CONTENT: no new odds picks or unseen previews; Telegram message suppressed.")
        return

    if analysis_fresh:
        lines += [
            "📊 <b>آنالیز آماری بازی‌ها</b>",
            "<i>فرم و آمار ۵ بازی اخیر؛ جدا از سیگنال‌های ضریب‌دار</i>",
            "━━━━━━━━━━━━━━━━━━━━",
        ]
        for i, (p, analysis_id) in enumerate(analysis_fresh, 1):
            local_time = p["start"].astimezone(TEHRAN).strftime("%H:%M")
            hf, af = p.get("home_form", ""), p.get("away_form", "")
            hp = sum(3 if x == "W" else 1 if x == "D" else 0 for x in hf if x in "WDL")
            ap = sum(3 if x == "W" else 1 if x == "D" else 0 for x in af if x in "WDL")
            if hf == "ناموجود" or af == "ناموجود":
                lean = "داده فرم کافی نیست"
            elif hp > ap + 2:
                lean = "گرایش آماری به میزبان"
            elif ap > hp + 2:
                lean = "گرایش آماری به میهمان"
            else:
                lean = "از نظر فرم، نزدیک و متعادل"
            goals = (
                f"⚽ گل زده: {p['home_avg_scored']:.1f} - {p['away_avg_scored']:.1f}\n"
                f"🛡 گل خورده: {p['home_avg_conceded']:.1f} - {p['away_avg_conceded']:.1f}"
                if all(k in p for k in ("home_avg_scored","away_avg_scored","home_avg_conceded","away_avg_conceded"))
                else "⚽ آمار گل: در منبع رایگان کامل نبود"
            )
            lines += [
                f"<b>⚽ {i:02d}. {esc(p['home'])} × {esc(p['away'])}</b>",
                f"🏆 {esc(p['league'])}  |  🕒 {local_time}",
                f"📈 فرم ۵ بازی: <b>{hf}</b>  مقابل  <b>{af}</b>",
                goals,
                f"🧠 جمع‌بندی فرم: <b>{lean}</b>",
                "💠 ضریب: <i>برای این مسابقه از منبع فعلی قیمت معتبر دریافت نشد</i>",
                "┄┄┄┄┄┄┄┄┄┄",
            ]
    if not fresh and not analysis_fresh:
        # Never silently exit: send a useful run report and, when available,
        # show up to five fixture previews even if those previews were sent before.
        print("NO_FRESH_PICKS: sending status report instead of exiting.")
        fallback_previews = previews[:5]
        displayed_preview_count = len(fallback_previews)
        if fallback_previews:
            lines += [
                "📊 <b>گزارش آنالیز آماری (بدون سیگنال ضریب‌دار تازه)</b>",
                "━━━━━━━━━━━━━━━━━━━━",
            ]
            for i, p in enumerate(fallback_previews, 1):
                local_time = p["start"].astimezone(TEHRAN).strftime("%H:%M")
                hf, af = p.get("home_form", "ناموجود"), p.get("away_form", "ناموجود")
                lines += [
                    f"⚽ <b>{esc(p['home'])}</b>  <i>vs</i>  <b>{esc(p['away'])}</b>",
                    f"🏆 <b>{esc(p.get('league') or 'نام لیگ در منبع مشخص نیست')}</b>",
                    f"🕒 {local_time}  |  📊 فرم ۵ بازی اخیر",
                    f"🏠 میزبان: <b>{esc(hf)}</b>   ✈️ میهمان: <b>{esc(af)}</b>",
                    "💠 ضریب معتبر: <i>در دسترس نیست</i>",
                    "━━━━━━━━━━━━━━━━━━━━",
                ]
        else:
            lines += [
                "⚠️ <b>فعلاً مسابقه تازه‌ای برای آنالیز پیدا نشد.</b>",
                "منابع برنامه مسابقات یا فهرست ضرایب پاسخ کامل نداده‌اند.",
                "━━━━━━━━━━━━━━━━━━━━",
            ]
        lines += [
            f"📊 مسابقات نمایش‌داده‌شده: {displayed_preview_count}",
            "💎 سیگنال ضریب‌دار تازه: 0",
            "<i>این گزارش به معنی پیشنهاد شرط‌بندی نیست.</i>",
        ]
    if fresh:
        for i, p in enumerate(fresh[:10], 1):
            local_time = p["start"].astimezone(TEHRAN).strftime("%H:%M")
            local_date = p["start"].astimezone(TEHRAN).strftime("%d.%m.%Y")
            market_names = {"h2h": "نتیجه مسابقه", "spreads": "هندیکپ", "totals": "مجموع گل / امتیاز"}
            point = f" ({esc(p['point'])})" if p.get("point") is not None else ""
            league = esc(p.get("league") or "لیگ نامشخص")
            home, away = esc(p["home"]), esc(p["away"])
            status = ("🟢 <b>برنامه تأییدشده:</b> " + esc("، ".join(p["fixture_sources"]))
                      if p.get("fixture_sources") else "⚪ <i>تأیید مستقل برنامه موجود نیست</i>")
            lines += [
                f"<b>⚽ مسابقه {i:02d}</b>",
                f"🏆 <b>{league}</b>",
                "",
                f"🔹 <b>{home}</b>",
                "<b>          VS</b>",
                f"🔸 <b>{away}</b>",
                "",
                f"🗓 {local_date}　　🕒 <b>{local_time}</b>",
                "┄┄┄┄┄┄┄┄┄┄┄┄┄┄┄",
                f"🎯 <b>{esc(market_names.get(p['market'], p['market']))}</b>",
                f"📌 انتخاب: <b>{esc(p['selection'])}{point}</b>",
                f"💎 ضریب: <b>{p['odds']:.2f}</b>",
                f"📡 منبع ضریب: {esc(p['book'])}",
                status,
                "━━━━━━━━━━━━━━━━━━━━",
            ]
    else:
        # If no betting picks exist, send the verified form-based previews above.
        pass
    lines += [
        f"💎 <b>سیگنال ضریب‌دار: {len(fresh[:10])}</b>",
        f"📊 <b>آنالیز آماری: {len(analysis_fresh) if analysis_fresh else displayed_preview_count}</b>",
        "<i>این خروجی تحلیل آماری است و تضمینی برای نتیجه ندارد.</i>",
    ]
    if errors and fresh:
        lines.append("⚠️ <i>برخی منابع داده خطا داشتند؛ جزئیات در گزارش اجرای GitHub ثبت شده است.</i>")

    # Save fixture history only after Telegram confirms successful delivery.
    send_telegram("\n".join(lines))
    sent_ids.update(fixture_id(p) for p in fresh[:10])
    sent_ids.update(analysis_id for _, analysis_id in analysis_fresh)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(sorted(sent_ids), ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Telegram sent: odds_picks={min(len(fresh), 10)} previews={len(analysis_fresh)}.")

if __name__ == "__main__":
    main()
