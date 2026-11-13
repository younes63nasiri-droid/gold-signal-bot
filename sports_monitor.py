#!/usr/bin/env python3
"""VIVA AI sports monitor: fixture discovery only; no betting advice or wagers."""
import json, os, sys
from datetime import datetime, timezone
from urllib.request import Request, urlopen

FEEDS = [
    ("NBA", "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"),
    ("WNBA", "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"),
    ("NHL", "https://site.api.espn.com/apis/site/v2/sports/hockey/nhl/scoreboard"),
    ("MLB", "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"),
    ("NFL", "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"),
    ("EPL", "https://site.api.espn.com/apis/site/v2/sports/soccer/eng.1/scoreboard"),
    ("LaLiga", "https://site.api.espn.com/apis/site/v2/sports/soccer/esp.1/scoreboard"),
    ("SerieA", "https://site.api.espn.com/apis/site/v2/sports/soccer/ita.1/scoreboard"),
    ("Bundesliga", "https://site.api.espn.com/apis/site/v2/sports/soccer/ger.1/scoreboard"),
    ("Ligue1", "https://site.api.espn.com/apis/site/v2/sports/soccer/fra.1/scoreboard"),
    ("UCL", "https://site.api.espn.com/apis/site/v2/sports/soccer/uefa.champions/scoreboard"),
]

def get_json(url):
    req = Request(url, headers={"User-Agent": "VIVA-AI-Sports-Monitor/0.1"})
    with urlopen(req, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))

def collect():
    events, errors = [], []
    for league, url in FEEDS:
        try:
            data = get_json(url)
            for item in data.get("events", []):
                comp = (item.get("competitions") or [{}])[0]
                competitors = comp.get("competitors", [])
                names = [c.get("team", {}).get("displayName", "Unknown") for c in competitors]
                events.append({
                    "league": league, "event": item.get("name"),
                    "date": item.get("date"), "status": (comp.get("status", {}).get("type", {}).get("detail")),
                    "teams": names, "source": "ESPN public scoreboard feed"
                })
        except Exception as exc:
            errors.append({"league": league, "error": str(exc)[:240]})
    return events, errors

def main():
    events, errors = collect()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"VIVA AI | Sports monitor", f"Updated: {now}", f"Events found: {len(events)}"]
    for e in events[:35]:
        lines.append(f"- [{e['league']}] {e['event']} | {e.get('status') or 'Scheduled'}")
    if not events:
        lines.append("No events returned by the configured feeds.")
    if errors:
        lines.append(f"Feed errors: {len(errors)} (see report.json)")
    message = "\n".join(lines)
    print(message)
    with open("report.json", "w", encoding="utf-8") as f:
        json.dump({"generated_at": now, "event_count": len(events), "events": events, "feed_errors": errors}, f, ensure_ascii=False, indent=2)
    token, chat_id = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        import urllib.parse
        body = urllib.parse.urlencode({"chat_id": chat_id, "text": message}).encode()
        req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body)
        with urlopen(req, timeout=20) as response:
            print("Telegram status:", response.status)
    elif token or chat_id:
        print("Telegram not sent: both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required.")

if __name__ == "__main__":
    main()
