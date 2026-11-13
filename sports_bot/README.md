# VIVA AI Sports

An isolated sports-fixture scanner. This project is separate from the gold bot and does not place bets.

## Current scope
- ESPN public scoreboard feed: English, Spanish, Italian, German and French top-flight football, UEFA Champions League, and NBA.
- Upcoming fixtures in the next 48 hours.
- Telegram report every two hours and manual GitHub Actions runs.

## Important limitations
- This first version is a fixture-discovery MVP, not a betting tipster.
- DerbyBet odds are not connected. No value-bet claim is made without a verified odds feed and timestamped prices.
- SofaScore/FotMob integrations, volleyball/tennis coverage, team statistics, injury data, model calibration and historical backtesting remain to be implemented after stable lawful data sources are verified.
- No deposits, account credentials, automated bet placement, or live wagering are used.

## GitHub Actions secrets
- TELEGRAM_BOT_TOKEN (reuse the existing bot token if desired)
- TELEGRAM_CHAT_ID (optional; defaults to the existing configured chat ID)
