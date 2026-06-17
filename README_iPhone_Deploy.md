# iPhone upgrade guide — GOAT Shield Live v2

You already have v1 deployed.

## Upgrade steps

1. Download this ZIP.
2. Unzip it in Files.
3. Go to your GitHub repo: personal-dashboard.
4. Upload and replace these files:
   - app.py
   - goat_engine_live.py
   - requirements.txt
   - README.md
   - README_iPhone_Deploy.md
5. Commit changes.
6. Streamlit should rebuild automatically.
7. If not, open Streamlit Manage app > Reboot.

## First v2 test settings

- Active sport: MLB or any active sport shown
- Markets: h2h only first
- Region: us only first
- Min odds: 1.40
- Max odds: 2.20
- Min edge: 2.00
- Elite edge: 5.00
- Max paper picks/day: 3
- Loss-streak lockout: 3
- Apply home rules to team markets only: ON
- Require home pick: ON
- Require home favourite: ON
- Require Pinnacle value: OFF for now
- Reject any manual red flag: ON
- All red flags: OFF unless genuinely true

Then try:
- h2h + spreads
- h2h + spreads + totals

More markets/regions = more API credits.
