# VIVA AI Sports Monitor (MVP)

This branch is an isolated sports-monitoring MVP. It does **not** place bets, access betting accounts, or claim profitable picks.

## Current scope
- Public ESPN scoreboard feeds: NBA, WNBA, NHL, MLB, NFL, EPL, LaLiga, Serie A, Bundesliga, Ligue 1, UEFA Champions League.
- Scheduled scan every two hours and manual workflow dispatch.
- Optional Telegram delivery.
- JSON artifact with event data and feed errors.

## Telegram setup
In GitHub repository Settings → Secrets and variables → Actions, add:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

Both are required for Telegram delivery. Never commit secrets.

## Run
GitHub → Actions → VIVA AI Sports Monitor → Run workflow.

## Important limitations / next work
- This version discovers fixtures and statuses only; it does not yet calculate probabilities or value bets.
- Odds from DerbyBet are not connected. Do not assume an odds feed/API exists until verified and permitted.
- SofaScore/FotMob integrations are not included; use authorized APIs or permitted data access only.
- Volleyball and tennis feeds still need a reliable, authorized provider.
- Before any recommendation engine, add timestamped odds snapshots, market-specific models, calibration, closing-line comparison, and a long paper-trading record including all losses and fees.
- No betting outcome is guaranteed.
