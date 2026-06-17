# GOAT Shield Live v2 Hotfix

This is a single-file hotfix. It removes the import error by placing the v2 engine inside `app.py`.

## v2 features
- Auto-load active sports from The Odds API
- Markets: h2h/moneyline, spreads, totals
- No-pick explanation with main blockers
- Paper-only safety

## Streamlit Secrets
```toml
ODDS_API_KEY = "your_real_key_here"
OPENAI_API_KEY = ""
OPENAI_MODEL = "gpt-4.1-mini"
```
