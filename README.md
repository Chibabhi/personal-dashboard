# GOAT Shield Live v4.4.2 — Sharp Bookmaker Priority

This version adds a sharp/core bookmaker support layer.

## v4.4.2 upgrade

- Keeps minimum bookmakers compared default: 10
- Adds sharp/core support detection
- Counts Pinnacle reference as sharp support when available
- Detects returned books/exchanges such as:
  - Pinnacle
  - Circa
  - BookMaker / CRIS
  - Betfair / Betfair Exchange
  - Matchbook
  - Smarkets
- Adds fields:
  - sharp_support
  - sharp_core_count
  - sharp_books
  - sharp_status
  - retail_books_count
- Auto Verify penalises retail-only support
- Decision engine moves retail-only picks to WATCHLIST by default
- Picks tab shows sharp/core status
- Health Check shows sharp-supported vs retail-only candidates

## Important

This does not guarantee the “best books on Earth” are available.
It detects and uses sharp/core books only when The Odds API returns them or when the separate Pinnacle reference pull matches.

Paper-only. No guarantee of winning.
