
import os, json
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import streamlit as st

from goat_engine_live import (
    FALLBACK_SPORTS, fetch_sports, fetch_odds, scan_events, decide_candidate,
    summarize_decisions, ai_review_text
)

st.set_page_config(page_title="GOAT Shield Live v2", page_icon="🐐", layout="wide", initial_sidebar_state="expanded")
PICKS_PATH = Path("data/paper_picks.csv")

DEFAULT_RULES = {
    "min_odds": 1.40, "max_odds": 2.20, "min_edge_pct": 2.0, "elite_edge_pct": 5.0,
    "max_daily": 3, "lock_losses": 3, "require_home_pick": True, "require_home_favourite": True,
    "require_pinnacle_value": False, "reject_red_flags": True, "apply_home_rules_to_team_markets": True,
}

def secret(name, default=""):
    try:
        return st.secrets.get(name, default)
    except Exception:
        return os.environ.get(name, default)

def load_picks():
    if "picks_df" not in st.session_state:
        if PICKS_PATH.exists():
            try: st.session_state.picks_df = pd.read_csv(PICKS_PATH)
            except Exception: st.session_state.picks_df = pd.DataFrame()
        else:
            st.session_state.picks_df = pd.DataFrame()
    return st.session_state.picks_df

def save_picks(df):
    st.session_state.picks_df = df
    PICKS_PATH.parent.mkdir(exist_ok=True)
    try: df.to_csv(PICKS_PATH, index=False)
    except Exception: pass

def approved_today(df):
    if df.empty or "created_at" not in df.columns or "decision" not in df.columns: return 0
    today = datetime.now().date().isoformat()
    return int((df["created_at"].astype(str).str.startswith(today) & df["decision"].astype(str).str.contains("APPROVED|ELITE|SAFE", regex=True)).sum())

def current_loss_streak(df):
    if df.empty or "result" not in df.columns: return 0
    streak = 0
    for _, row in df.sort_values("created_at", ascending=False).iterrows():
        result = str(row.get("result", "Pending"))
        if result == "Lost": streak += 1
        elif result in ("Won", "Push"): break
    return streak

def get_active_sports(api_key):
    if not api_key: return FALLBACK_SPORTS
    if "active_sports" in st.session_state: return st.session_state.active_sports
    try:
        sports = fetch_sports(api_key)
        active = {s["key"]: s.get("title", s["key"]) for s in sports if s.get("active", False) and not s.get("has_outrights", False)}
        if active:
            st.session_state.active_sports = active
            return active
    except Exception as e:
        st.sidebar.warning(f"Could not auto-load sports: {e}")
    return FALLBACK_SPORTS

def ai_review(candidate, decision):
    api_key = secret("OPENAI_API_KEY", "")
    model = secret("OPENAI_MODEL", "gpt-4.1-mini")
    if not api_key: return ai_review_text(candidate, decision)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        prompt = f"""You are GOAT Shield, a paper-betting discipline reviewer.
Do NOT encourage real-money betting.
Candidate: {json.dumps(candidate.__dict__, indent=2)}
Decision: {json.dumps(decision.__dict__, indent=2)}
Write 1 line decision, 2 bullet reasons, and 1 discipline warning."""
        resp = client.responses.create(model=model, input=prompt, max_output_tokens=250)
        return resp.output_text
    except Exception as e:
        return ai_review_text(candidate, decision) + f"\n\nAI fallback used: {e}"

def candidate_to_row(c, d, ai_text):
    return {
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event_id": c.event_id,
        "sport": c.sport_key, "sport_title": c.sport_title, "commence_time": c.commence_time,
        "home_team": c.home_team, "away_team": c.away_team, "pick_label": c.pick_label,
        "outcome_name": c.outcome_name, "market": c.market_key, "market_label": c.market_label,
        "point": c.point, "odds": c.best_odds, "bookmaker": c.best_bookmaker,
        "consensus_prob": c.consensus_prob, "implied_prob": c.implied_prob, "edge": c.edge,
        "decision": d.decision, "score": d.score, "risk": d.risk, "result": "Pending",
        "closing_odds": "", "profit_units": "", "clv": "", "ai_review": ai_text,
    }

def render_dashboard(df):
    st.subheader("📊 Paper Proof Dashboard")
    if df.empty:
        st.info("No paper picks logged yet.")
        return
    settled = df[df["result"].isin(["Won", "Lost", "Push"])] if "result" in df.columns else pd.DataFrame()
    pending = df[df["result"].astype(str).eq("Pending")] if "result" in df.columns else pd.DataFrame()
    wins = int((settled["result"] == "Won").sum()) if not settled.empty else 0
    losses = int((settled["result"] == "Lost").sum()) if not settled.empty else 0
    pushes = int((settled["result"] == "Push").sum()) if not settled.empty else 0
    profit = pd.to_numeric(settled.get("profit_units", pd.Series(dtype=float)), errors="coerce").fillna(0).sum() if not settled.empty else 0
    stake_count = max(len(settled), 1)
    roi = profit / stake_count if stake_count else 0
    clv = pd.to_numeric(settled.get("clv", pd.Series(dtype=float)), errors="coerce").dropna() if not settled.empty else pd.Series(dtype=float)
    clv_pos = (clv > 0).mean() if len(clv) else 0
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Settled", len(settled)); c2.metric("Pending", len(pending)); c3.metric("Profit units", f"{profit:+.2f}u"); c4.metric("ROI", f"{roi*100:.1f}%")
    c5, c6, c7, c8 = st.columns(4)
    c5.metric("Wins", wins); c6.metric("Losses", losses); c7.metric("Pushes", pushes); c8.metric("Positive CLV", f"{clv_pos*100:.1f}%")
    proof_target = 300
    st.progress(min(1, len(settled)/proof_target), text=f"Proof progress: {len(settled)}/{proof_target} settled paper picks")
    if len(settled) < proof_target: st.warning("System verdict: NOT PROVEN — keep paper testing.")
    elif profit > 0 and clv_pos > 0.55: st.success("System verdict: PROVEN ON PAPER — still no auto-betting.")
    else: st.error("System verdict: FAILED PROOF — do not use real money.")

def render_result_update(df):
    st.subheader("✅ Update Results")
    if df.empty:
        st.info("No picks yet."); return
    pending = df[df["result"].astype(str).eq("Pending")] if "result" in df.columns else pd.DataFrame()
    if pending.empty:
        st.info("No pending paper picks."); return
    labels = pending.apply(lambda r: f"{r.name}: {r.get('pick_label', 'Pick')} @ {r['odds']} ({r['home_team']} vs {r['away_team']})", axis=1).tolist()
    choice = st.selectbox("Select pending pick", labels)
    idx = int(choice.split(":")[0])
    result = st.selectbox("Result", ["Won", "Lost", "Push"])
    closing_odds = st.number_input("Closing odds if known", min_value=0.0, value=0.0, step=0.01)
    if st.button("Save result"):
        odds = float(df.loc[idx, "odds"])
        profit = odds - 1 if result == "Won" else (-1 if result == "Lost" else 0)
        df.loc[idx, "result"] = result; df.loc[idx, "profit_units"] = profit
        if closing_odds > 0:
            df.loc[idx, "closing_odds"] = closing_odds
            df.loc[idx, "clv"] = (odds - closing_odds) / closing_odds
        save_picks(df); st.success("Result updated."); st.rerun()

def main():
    st.title("🐐 GOAT Shield Live v2")
    st.caption("Auto active sports + moneyline/spreads/totals + no-pick explanation. Paper-only. No sportsbook login. No real-money auto-betting.")
    api_key_default = secret("ODDS_API_KEY", "")
    openai_key = secret("OPENAI_API_KEY", "")

    with st.sidebar:
        st.markdown("### 🔌 Connection status")
        st.write("Odds API:", "✅ key found" if api_key_default else "Paste key below / Secrets")
        st.write("AI review:", "✅ OpenAI key found" if openai_key else "Optional/off")
        st.caption("No sportsbook login. No auto-betting.")

        st.markdown("### Data source")
        api_key = st.text_input("The Odds API key", value=api_key_default, type="password")
        if st.button("Reload active sports list"):
            st.session_state.pop("active_sports", None); st.rerun()

        sports_map = get_active_sports(api_key)
        sport_key = st.selectbox("Active sport", list(sports_map.keys()), format_func=lambda k: sports_map.get(k, k))
        markets = st.multiselect("Markets", ["h2h", "spreads", "totals"], default=["h2h"], format_func=lambda x: {"h2h":"Moneyline / h2h","spreads":"Spreads","totals":"Totals"}[x])
        regions = st.multiselect("Regions", ["us", "uk", "au", "eu"], default=["us"])
        credit_estimate = max(1, len(regions))*max(1, len(markets))
        st.caption(f"Estimated current-odds credits for one fetch: about {credit_estimate}. Use one region when testing.")

        st.markdown("### GOAT rules")
        rules = DEFAULT_RULES.copy()
        rules["min_odds"] = st.number_input("Min decimal odds", 1.01, 10.0, 1.40, 0.01)
        rules["max_odds"] = st.number_input("Max decimal odds", 1.01, 10.0, 2.20, 0.01)
        rules["min_edge_pct"] = st.number_input("Min edge %", 0.0, 50.0, 2.0, 0.1)
        rules["elite_edge_pct"] = st.number_input("Elite edge %", 0.0, 50.0, 5.0, 0.1)
        rules["max_daily"] = st.number_input("Max approved paper picks/day", 1, 20, 3, 1)
        rules["lock_losses"] = st.number_input("Loss-streak lockout", 1, 20, 3, 1)
        rules["apply_home_rules_to_team_markets"] = st.checkbox("Apply home rules to team markets only", True)
        rules["require_home_pick"] = st.checkbox("Require team-market pick to be home team", True)
        rules["require_home_favourite"] = st.checkbox("Require home favourite for team markets", True)
        rules["require_pinnacle_value"] = st.checkbox("Require Pinnacle value if present", False)
        rules["reject_red_flags"] = st.checkbox("Reject any manual red flag", True)

        st.markdown("### Manual proof/red flags")
        manual_signals = {
            "sports_alerts_support": st.checkbox("Sports Alerts proof checked", False),
            "scp_support": st.checkbox("Sports Chat Place proof checked", False),
            "picks_parlays_support": st.checkbox("Picks & Parlays proof checked", False),
            "injury_red": st.checkbox("Injury/news red flag", False),
            "public_red": st.checkbox("Public-heavy red flag", False),
            "fatigue_red": st.checkbox("Schedule/fatigue red flag", False),
            "line_against": st.checkbox("Line moved against pick", False),
            "key_player_red": st.checkbox("Key player uncertainty", False),
            "late_chase_feeling": st.checkbox("Late/chase feeling", False),
        }

    df = load_picks()
    tabs = st.tabs(["🔴 Live Scanner", "📒 Paper Log", "✅ Results", "📊 Dashboard", "🛡️ Backup"])

    with tabs[0]:
        st.subheader("🔴 Live odds scanner")
        st.info("v2 scans active sports, moneyline/spreads/totals, then explains why there is no pick.")
        if not api_key: st.warning("Add your The Odds API key in the sidebar or Streamlit Secrets.")

        if st.button("Fetch live odds and scan"):
            if not api_key or not markets or not regions:
                st.error("Add API key and choose at least one market and region."); st.stop()
            with st.spinner("Fetching odds..."):
                try:
                    events, meta = fetch_odds(api_key, sport_key, regions=",".join(regions), markets=",".join(markets))
                    st.session_state["last_events"] = events
                    st.session_state["last_meta"] = meta
                    st.session_state["last_sport_key"] = sport_key
                    st.session_state["last_markets"] = markets
                    st.success(f"Fetched {len(events)} events. Requests remaining: {meta.get('requests_remaining')}")
                except Exception as e:
                    st.error(f"Odds fetch failed: {e}"); st.stop()

        events = st.session_state.get("last_events", [])
        meta = st.session_state.get("last_meta", {})
        last_sport = st.session_state.get("last_sport_key", sport_key)
        last_markets = st.session_state.get("last_markets", markets)

        if meta:
            st.caption(f"Last fetch UTC: {meta.get('last_fetch_utc')} | API used: {meta.get('requests_used')} | remaining: {meta.get('requests_remaining')}")

        if events:
            candidates = scan_events(events, last_sport, last_markets)
            dec_rows = []
            for c in candidates:
                d = decide_candidate(c, rules, manual_signals, approved_today=approved_today(df), loss_streak=current_loss_streak(df))
                dec_rows.append({
                    "decision": d.decision, "score": d.score, "risk": d.risk, "market": c.market_label, "pick": c.pick_label,
                    "point": c.point, "odds": c.best_odds, "bookmaker": c.best_bookmaker, "edge": c.edge,
                    "consensus_prob": c.consensus_prob, "implied_prob": c.implied_prob, "home": c.home_team, "away": c.away_team,
                    "commence_time": c.commence_time, "home_pick": c.is_home_pick, "home_fav": c.is_home_favourite,
                    "pinnacle": c.pinnacle_price, "pinnacle_ok": c.pinnacle_value_ok, "books": c.bookmaker_count,
                    "reasons": "; ".join(d.reasons), "reject_bucket": d.reject_bucket, "_candidate": c, "_decision_obj": d
                })

            summary = summarize_decisions(dec_rows)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Candidates scanned", summary["total"]); c2.metric("Approved/Elite", summary["approved"])
            c3.metric("Watchlist", summary["watchlist"]); c4.metric("Rejected/Locked", summary["rejected"] + summary["locked"])
            if summary["approved"] == 0:
                st.warning(summary["plain"])
                if summary["top_reasons"]:
                    st.write("Main no-pick reasons:")
                    for reason, count in summary["top_reasons"]:
                        st.write(f"- {reason}: {count}")
            else:
                st.success(summary["plain"])

            table_df = pd.DataFrame(dec_rows)
            if table_df.empty:
                st.warning("No market candidates found. Try another active sport, region, or market.")
            else:
                rank = {"ELITE PAPER PICK":0, "APPROVED PAPER PICK":1, "WATCHLIST — PINNACLE NOT CONFIRMED":2}
                table_df["sort"] = table_df["decision"].map(rank).fillna(9)
                table_df = table_df.sort_values(["sort","edge"], ascending=[True, False]).reset_index(drop=True)
                cols = ["decision","score","market","pick","odds","bookmaker","edge","home","away","home_pick","home_fav","pinnacle","pinnacle_ok","books","reasons"]
                st.dataframe(table_df[cols].style.format({"edge":"{:.2%}"}), use_container_width=True, hide_index=True)

                approved_rows = table_df[table_df["decision"].str.contains("APPROVED|ELITE", regex=True, na=False)]
                if not approved_rows.empty:
                    labels = approved_rows.apply(lambda r: f"{r.name}: {r['decision']} — {r['pick']} @ {r['odds']} ({r['bookmaker']})", axis=1).tolist()
                    selected = st.selectbox("Approved/elite candidate to inspect", labels)
                    idx = int(selected.split(":")[0])
                    row = table_df.loc[idx]; c = row["_candidate"]; d = row["_decision_obj"]
                    review = ai_review(c, d)
                    st.text_area("AI / discipline review", review, height=220)
                    if st.button("Auto-log this as PAPER pick"):
                        save_picks(pd.concat([pd.DataFrame([candidate_to_row(c, d, review)]), df], ignore_index=True))
                        st.success("Logged as paper pick only."); st.rerun()
                else:
                    st.info("No approved paper picks in this scan. That is okay. No edge = no paper pick.")
        elif meta:
            st.warning("Fetched 0 events. Try another active sport, market, or region.")

    with tabs[1]:
        st.subheader("📒 Paper Log")
        df = load_picks()
        if df.empty: st.info("No paper picks yet.")
        else:
            st.dataframe(df, use_container_width=True)
            st.download_button("Download paper log CSV", df.to_csv(index=False).encode("utf-8"), "goat_shield_paper_log.csv", "text/csv")

    with tabs[2]: render_result_update(load_picks())
    with tabs[3]: render_dashboard(load_picks())
    with tabs[4]:
        st.subheader("🛡️ Backup / restore")
        df = load_picks()
        if not df.empty: st.download_button("Download backup CSV", df.to_csv(index=False).encode("utf-8"), "goat_shield_backup.csv", "text/csv")
        up = st.file_uploader("Restore CSV backup", type=["csv"])
        if up is not None:
            new_df = pd.read_csv(up)
            if st.button("Restore this CSV"):
                save_picks(new_df); st.success("Restored."); st.rerun()
        if st.button("Delete all local paper picks"):
            save_picks(pd.DataFrame()); st.success("Deleted local paper log."); st.rerun()

    st.divider()
    st.caption("GOAT Shield Live v2 is a paper-betting proof system only. It does not place real-money bets, log into sportsbooks, or bypass any betting rules.")

if __name__ == "__main__":
    main()
