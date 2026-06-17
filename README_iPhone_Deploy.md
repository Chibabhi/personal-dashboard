# iPhone deployment guide — GOAT Shield Live v1

This app needs cloud hosting because API keys must be hidden on a backend.

## Best simple path: Streamlit Community Cloud

1. Create/sign into GitHub and Streamlit Community Cloud.
2. Create a GitHub repo. Private is best.
3. Upload: `app.py`, `goat_engine_live.py`, `requirements.txt`, `README.md`.
4. In Streamlit Cloud, create New app and choose `app.py` as the main file.
5. In App Settings > Secrets, add:

```toml
ODDS_API_KEY = "your_odds_api_key"
OPENAI_API_KEY = ""
OPENAI_MODEL = "gpt-4.1-mini"
```

6. Open the app link in Safari. Share button > Add to Home Screen.

## Daily use
1. Choose sport.
2. Fetch live odds and scan.
3. Log only approved PAPER picks.
4. After games, update Won/Lost/Push.
5. Judge the system only after 300+ settled paper picks.

No real-money automation.
