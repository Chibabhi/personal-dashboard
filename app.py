
import os, json
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd
import streamlit as st
from goat_engine_live import DEFAULT_SPORTS, fetch_odds, scan_all_moneyline, decide_candidate, ai_review_text

st.set_page_config(page_title="GOAT Shield Live v1", page_icon="🐐", layout="wide")
PICKS_PATH = Path("data/paper_picks.csv")
DEFAULT_RULES = {"min_odds":1.40,"max_odds":2.20,"min_edge_pct":2.0,"elite_edge_pct":5.0,"max_daily":3,"lock_losses":3,"require_home_pick":True,"require_home_favourite":True,"require_pinnacle_value":False,"reject_red_flags":True}

def secret(name, default=""):
    try: return st.secrets.get(name, default)
    except Exception: return os.environ.get(name, default)

def load_picks():
    if "picks_df" not in st.session_state:
        st.session_state.picks_df = pd.read_csv(PICKS_PATH) if PICKS_PATH.exists() else pd.DataFrame()
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

def ai_review(candidate, decision):
    api_key = secret("OPENAI_API_KEY", "")
    model = secret("OPENAI_MODEL", "gpt-4.1-mini")
    if not api_key: return ai_review_text(candidate, decision)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        prompt = f"""You are GOAT Shield, a paper-betting discipline reviewer. Do NOT encourage real-money betting. Review this candidate and return a short practical warning/summary.\nCandidate:\n{json.dumps(candidate.__dict__, indent=2)}\nDecision:\n{json.dumps(decision.__dict__, indent=2)}\nWrite: 1 line decision, 2 bullet reasons, 1 discipline warning."""
        resp = client.responses.create(model=model, input=prompt, max_output_tokens=250)
        return resp.output_text
    except Exception as e:
        return ai_review_text(candidate, decision) + f"\n\nAI fallback used because AI call failed: {e}"

def candidate_to_row(c, d, review):
    return {"created_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),"event_id":c.event_id,"sport":c.sport_key,"commence_time":c.commence_time,"home_team":c.home_team,"away_team":c.away_team,"pick_team":c.pick_team,"market":c.market,"odds":c.best_odds,"bookmaker":c.best_bookmaker,"consensus_prob":c.consensus_prob,"implied_prob":c.implied_prob,"edge":c.edge,"decision":d.decision,"score":d.score,"risk":d.risk,"result":"Pending","closing_odds":"","profit_units":"","clv":"","ai_review":review}

def render_dashboard(df):
    st.subheader("📊 Paper Proof Dashboard")
    if df.empty: st.info("No paper picks logged yet."); return
    settled = df[df["result"].isin(["Won","Lost","Push"])] if "result" in df.columns else pd.DataFrame()
    pending = df[df["result"].astype(str).eq("Pending")] if "result" in df.columns else pd.DataFrame()
    wins = int((settled["result"]=="Won").sum()) if not settled.empty else 0
    losses = int((settled["result"]=="Lost").sum()) if not settled.empty else 0
    pushes = int((settled["result"]=="Push").sum()) if not settled.empty else 0
    profit = pd.to_numeric(settled.get("profit_units", pd.Series(dtype=float)), errors="coerce").fillna(0).sum() if not settled.empty else 0
    stake_count = max(len(settled),1); roi = profit/stake_count
    clv = pd.to_numeric(settled.get("clv", pd.Series(dtype=float)), errors="coerce").dropna() if not settled.empty else pd.Series(dtype=float)
    clv_pos = (clv > 0).mean() if len(clv) else 0
    c1,c2,c3,c4 = st.columns(4)
    c1.metric("Settled paper picks", len(settled)); c2.metric("Pending", len(pending)); c3.metric("Profit units", f"{profit:+.2f}u"); c4.metric("ROI", f"{roi*100:.1f}%")
    c5,c6,c7,c8 = st.columns(4)
    c5.metric("Wins", wins); c6.metric("Losses", losses); c7.metric("Pushes", pushes); c8.metric("Positive CLV", f"{clv_pos*100:.1f}%")
    proof_target = 300; progress = min(1, len(settled)/proof_target)
    st.progress(progress, text=f"Proof progress: {len(settled)}/{proof_target} settled paper picks")
    if len(settled) < proof_target: st.warning("System verdict: NOT PROVEN — keep paper testing.")
    elif profit > 0 and clv_pos > .55: st.success("System verdict: PROVEN ON PAPER — still no auto-betting.")
    else: st.error("System verdict: FAILED PROOF — do not use real money.")

def render_result_update(df):
    st.subheader("✅ Update Results")
    if df.empty: st.info("No picks yet."); return
    pending = df[df["result"].astype(str).eq("Pending")] if "result" in df.columns else pd.DataFrame()
    if pending.empty: st.info("No pending paper picks."); return
    labels = pending.apply(lambda r: f"{r.name}: {r['pick_team']} @ {r['odds']} ({r['home_team']} vs {r['away_team']})", axis=1).tolist()
    idx = int(st.selectbox("Select pending pick", labels).split(":")[0])
    result = st.selectbox("Result", ["Won","Lost","Push"])
    closing_odds = st.number_input("Closing odds if known", min_value=0.0, value=0.0, step=0.01)
    if st.button("Save result"):
        odds = float(df.loc[idx,"odds"])
        profit = odds-1 if result=="Won" else (-1 if result=="Lost" else 0)
        df.loc[idx,"result"] = result; df.loc[idx,"profit_units"] = profit
        if closing_odds > 0:
            df.loc[idx,"closing_odds"] = closing_odds; df.loc[idx,"clv"] = (odds-closing_odds)/closing_odds
        save_picks(df); st.success("Result updated."); st.rerun()

def main():
    st.title("🐐 GOAT Shield Live v1")
    st.caption("Real-time odds scanner + AI discipline review + paper log. No sportsbook login. No real-money auto-betting.")
    api_key_default = secret("ODDS_API_KEY", "")
    openai_key = secret("OPENAI_API_KEY", "")
    st.sidebar.markdown("### 🔌 Connection status")
    st.sidebar.write("Odds API:", "✅ key found" if api_key_default else "❌ no key")
    st.sidebar.write("AI review:", "✅ OpenAI key found" if openai_key else "Optional/off")
    st.sidebar.caption("No sportsbook login. No auto-betting.")
    with st.sidebar:
        st.markdown("### Data source")
        api_key = st.text_input("The Odds API key", value=api_key_default, type="password")
        sport_key = st.selectbox("Sport", list(DEFAULT_SPORTS.keys()), format_func=lambda k: DEFAULT_SPORTS[k])
        regions = st.multiselect("Regions", ["us","uk","au","eu"], default=["us","uk","au"])
        st.markdown("### GOAT rules")
        rules = DEFAULT_RULES.copy()
        rules["min_odds"] = st.number_input("Min decimal odds", 1.01, 10.0, 1.40, .01)
        rules["max_odds"] = st.number_input("Max decimal odds", 1.01, 10.0, 2.20, .01)
        rules["min_edge_pct"] = st.number_input("Min edge %", 0.0, 50.0, 2.0, .1)
        rules["elite_edge_pct"] = st.number_input("Elite edge %", 0.0, 50.0, 5.0, .1)
        rules["max_daily"] = st.number_input("Max approved paper picks/day", 1, 20, 3, 1)
        rules["lock_losses"] = st.number_input("Loss-streak lockout", 1, 20, 3, 1)
        rules["require_home_pick"] = st.checkbox("Require pick to be home team", True)
        rules["require_home_favourite"] = st.checkbox("Require home favourite", True)
        rules["require_pinnacle_value"] = st.checkbox("Require Pinnacle value if present", False)
        rules["reject_red_flags"] = st.checkbox("Reject any manual red flag", True)
        st.markdown("### Manual proof/red flags")
        manual_signals = {"sports_alerts_support":st.checkbox("Sports Alerts proof checked", False),"scp_support":st.checkbox("Sports Chat Place proof checked", False),"picks_parlays_support":st.checkbox("Picks & Parlays proof checked", False),"injury_red":st.checkbox("Injury/news red flag", False),"public_red":st.checkbox("Public-heavy red flag", False),"fatigue_red":st.checkbox("Schedule/fatigue red flag", False),"line_against":st.checkbox("Line moved against pick", False),"key_player_red":st.checkbox("Key player uncertainty", False),"late_chase_feeling":st.checkbox("Late/chase feeling", False)}
    df = load_picks()
    tab1,tab2,tab3,tab4,tab5 = st.tabs(["🔴 Live Scanner","📒 Paper Log","✅ Results","📊 Dashboard","🛡️ Backup"])
    with tab1:
        st.subheader("🔴 Live odds scanner")
        st.info("This scans live odds from your odds API key and classifies PAPER picks only.")
        if not api_key: st.warning("Add your The Odds API key in the sidebar or Streamlit Secrets.")
        if st.button("Fetch live odds and scan"):
            if not api_key: st.stop()
            with st.spinner("Fetching odds..."):
                try:
                    events, meta = fetch_odds(api_key, sport_key, regions=','.join(regions), markets="h2h")
                    st.session_state["last_events"] = events; st.session_state["last_meta"] = meta
                    st.success(f"Fetched {len(events)} events. Requests remaining: {meta.get('requests_remaining')}")
                except Exception as e:
                    st.error(f"Odds fetch failed: {e}"); st.stop()
        events = st.session_state.get("last_events", []); meta = st.session_state.get("last_meta", {})
        if meta: st.caption(f"Last fetch UTC: {meta.get('last_fetch_utc')} | API used: {meta.get('requests_used')} | remaining: {meta.get('requests_remaining')}")
        if events:
            candidates = scan_all_moneyline(events, sport_key)
            rows=[]; loss_streak=current_loss_streak(df); approved=approved_today(df)
            for c in candidates:
                d=decide_candidate(c, rules, manual_signals, approved_today=approved, loss_streak=loss_streak)
                rows.append({"decision":d.decision,"score":d.score,"risk":d.risk,"commence_time":c.commence_time,"home":c.home_team,"away":c.away_team,"pick":c.pick_team,"odds":c.best_odds,"bookmaker":c.best_bookmaker,"consensus_prob":c.consensus_prob,"implied_prob":c.implied_prob,"edge":c.edge,"home_pick":c.is_home_pick,"home_fav":c.is_home_favourite,"pinnacle":c.pinnacle_price,"pinnacle_ok":c.pinnacle_value_ok,"reasons":"; ".join(d.reasons),"_candidate":c,"_decision_obj":d})
            table_df=pd.DataFrame(rows)
            if table_df.empty: st.warning("No moneyline candidates found.")
            else:
                table_df["sort"] = table_df["decision"].map({"ELITE PAPER PICK":0,"APPROVED PAPER PICK":1}).fillna(9)
                table_df=table_df.sort_values(["sort","edge"], ascending=[True,False]).reset_index(drop=True)
                st.dataframe(table_df[["decision","score","home","away","pick","odds","bookmaker","edge","home_pick","home_fav","pinnacle","pinnacle_ok","reasons"]].style.format({"edge":"{:.2%}"}), use_container_width=True, hide_index=True)
                appr=table_df[table_df["decision"].str.contains("APPROVED|ELITE", regex=True, na=False)]
                if not appr.empty:
                    choice=st.selectbox("Approved/elite candidate to inspect", appr.apply(lambda r:f"{r.name}: {r['decision']} — {r['pick']} @ {r['odds']} ({r['bookmaker']})", axis=1).tolist())
                    idx=int(choice.split(":")[0]); row=table_df.loc[idx]; c=row["_candidate"]; d=row["_decision_obj"]
                    st.markdown("### 🧠 Review")
                    review=ai_review(c,d); st.text_area("AI / discipline review", review, height=220)
                    if st.button("Auto-log this as PAPER pick"):
                        save_picks(pd.concat([pd.DataFrame([candidate_to_row(c,d,review)]), df], ignore_index=True))
                        st.success("Logged as paper pick only."); st.rerun()
                else: st.warning("No approved paper picks in this scan. Good — the system should reject most action.")
    with tab2:
        st.subheader("📒 Paper Log"); df=load_picks()
        if df.empty: st.info("No paper picks yet.")
        else:
            st.dataframe(df, use_container_width=True)
            st.download_button("Download paper log CSV", df.to_csv(index=False).encode('utf-8'), "goat_shield_paper_log.csv", "text/csv")
    with tab3: render_result_update(load_picks())
    with tab4: render_dashboard(load_picks())
    with tab5:
        st.subheader("🛡️ Backup / restore"); df=load_picks(); st.write("Download backups regularly. Cloud storage can reset.")
        if not df.empty: st.download_button("Download backup CSV", df.to_csv(index=False).encode('utf-8'), "goat_shield_backup.csv", "text/csv")
        up=st.file_uploader("Restore CSV backup", type=["csv"])
        if up is not None:
            new_df=pd.read_csv(up)
            if st.button("Restore this CSV"): save_picks(new_df); st.success("Restored."); st.rerun()
        if st.button("Delete all local paper picks"): save_picks(pd.DataFrame()); st.success("Deleted local paper log."); st.rerun()
    st.divider(); st.caption("Paper-only proof system. No sportsbook login. No real-money auto-betting.")
if __name__ == "__main__": main()
