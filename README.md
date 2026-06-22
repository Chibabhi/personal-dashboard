# GOAT Shield Live v3.8 — Pinnacle Reference Board

Adds Pinnacle reference odds to the GOAT Shield system.

## v3.8 upgrade

- Pulls your normal board from selected regions, usually `us`.
- Separately pulls Pinnacle reference odds using The Odds API bookmaker key `pinnacle`.
- Matches Pinnacle to the same game + market + pick when available.
- Adds:
  - Pinnacle reference odds
  - Pinnacle gap %
  - Pinnacle status
  - Pinnacle explanation in Mobile Cards
  - Pinnacle fields in paper log
- Keeps no-edge gate removed.
- Keeps NZD decimal odds range.
- Keeps NZ/US time conversion.
- Keeps paper-only safety.

## Important

Pinnacle is used as a reference line only. This app does not log into Pinnacle, scrape Pinnacle, or place bets.

## Suggested use

- Pull Pinnacle reference odds: ON
- Require Pinnacle confirmation: OFF at first
- Sport preset: Single/manual
- Sport: MLB only
- Market: Moneyline / h2h only
- Region: us
- Time filter: NZ Bettor Mode: Next 24 hours

## Safety

Paper-only. No real-money betting. No sportsbook login. No scraping. No bank/card details.
