# GOAT Shield Live v3.2 Hotfix

Best Price Board + active sports + moneyline/spreads/totals + no-pick explanation.

## Fixed in v3.2
- Fixes 422 error from `commenceTimeFrom` / `commenceTimeTo` by using proper `Z` UTC time format.
- Sanitises API errors so your Odds API key is not displayed on the Streamlit page.
- Adds a fallback: if `Today NZ` returns 422, switch time filter to `No time filter`.

## Safety
Paper-only proof system. No sportsbook login. No scraping. No real-money auto-betting.

## Streamlit Secrets
```toml
ODDS_API_KEY = "your_real_key_here"
```
