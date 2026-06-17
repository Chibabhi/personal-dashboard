# GOAT Shield Live v1

Phone-friendly real-time odds scanner + AI discipline review + paper log.

## What it does
- Pulls live/upcoming moneyline odds from The Odds API.
- Calculates implied probability and consensus no-vig market probability.
- Calculates edge against best available odds.
- Applies GOAT rules: odds range, edge, home pick, home favourite, optional Pinnacle value, manual red flags, daily limit, loss-streak lockout.
- Optionally uses OpenAI for a short discipline review.
- Logs approved picks as PAPER picks only.

## What it does not do
- No real-money betting.
- No auto-login to TAB / Bet365 / Pinnacle.
- No sportsbook scraping.
- No bank/card details.
- No bypassing sportsbook rules.

## Run locally
```bash
pip install -r requirements.txt
streamlit run app.py
```
