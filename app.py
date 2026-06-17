
from __future__ import annotations
import os, math, json, requests
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter
import pandas as pd
import streamlit as st

st.set_page_config(page_title="GOAT Shield Live v2", page_icon="🐐", layout="wide", initial_sidebar_state="expanded")
ODDS_API_BASE="https://api.the-odds-api.com/v4"
PICKS_PATH=Path("data/paper_picks.csv")
FALLBACK_SPORTS={"baseball_mlb":"MLB","basketball_nba":"NBA","icehockey_nhl":"NHL","americanfootball_nfl":"NFL","soccer_epl":"EPL","aussierules_afl":"AFL","rugbyleague_nrl":"NRL"}

def secret(name, default=""):
    try: return st.secrets.get(name, default)
    except Exception: return os.environ.get(name, default)

def safe_float(x, default=0.0):
    try:
        v=float(x)
        return v if math.isfinite(v) else default
    except Exception: return default

def fetch_sports(api_key):
    r=requests.get(f"{ODDS_API_BASE}/sports/", params={"apiKey":api_key}, timeout=25)
    r.raise_for_status(); return r.json()

def fetch_odds(api_key, sport_key, regions="us", markets="h2h"):
    params={"apiKey":api_key,"regions":regions,"markets":markets,"oddsFormat":"decimal","dateFormat":"iso"}
    r=requests.get(f"{ODDS_API_BASE}/sports/{sport_key}/odds/", params=params, timeout=35)
    r.raise_for_status()
    return r.json(), {"requests_remaining":r.headers.get("x-requests-remaining"),"requests_used":r.headers.get("x-requests-used"),"last_fetch_utc":datetime.now(timezone.utc).isoformat(timespec="seconds")}

def point_val(x):
    if x is None: return None
    try: return float(x)
    except Exception: return None

def fmt_point(p):
    if p is None: return ""
    return f"+{p:g}" if p>0 else f"{p:g}"

def market_label(m): return {"h2h":"Moneyline","spreads":"Spread","totals":"Total"}.get(m,m)

def pick_label(m, name, point):
    if m=="h2h": return f"{name} ML"
    if m=="spreads": return f"{name} {fmt_point(point)}"
    if m=="totals": return f"{name} {point:g}" if point is not None else name
    return f"{name} {fmt_point(point)}".strip()

def candidate_key(market_key, outcome): return (market_key, str(outcome.get("name","")), point_val(outcome.get("point")))

def home_fav_h2h(best, home, away):
    ho=best.get(("h2h",home,None),(0,""))[0]; ao=best.get(("h2h",away,None),(0,""))[0]
    return bool(ho>1 and ao>1 and ho<ao)

def scan_events(events, sport_key, markets):
    out=[]
    for event in events:
        home=event.get("home_team","") or ""; away=event.get("away_team","") or ""; sport_title=event.get("sport_title", sport_key)
        best={}; probs={}; pinn={}; books=Counter()
        for bm in event.get("bookmakers",[]):
            bm_name=bm.get("title", bm.get("key","unknown")); bmkey=str(bm.get("key","")).lower(); bmtitle=str(bm.get("title","")).lower(); is_pin=("pinnacle" in bmkey or "pinnacle" in bmtitle)
            for mk in bm.get("markets",[]):
                mkey=mk.get("key")
                if mkey not in markets: continue
                outcomes=[o for o in mk.get("outcomes",[]) if safe_float(o.get("price"))>1]
                if len(outcomes)<2: continue
                invs=[1/safe_float(o.get("price")) for o in outcomes]; total=sum(invs)
                if total<=0: continue
                for o,inv in zip(outcomes,invs):
                    key=candidate_key(mkey,o); price=safe_float(o.get("price")); books[key]+=1
                    if price>best.get(key,(0,""))[0]: best[key]=(price,bm_name)
                    probs.setdefault(key,[]).append(inv/total)
                    if is_pin: pinn[key]=price
        hfav=home_fav_h2h(best,home,away)
        for key,plist in probs.items():
            mkey,name,point=key; odds,book=best.get(key,(0,""))
            if odds<=1: continue
            cons=sum(plist)/len(plist); implied=1/odds; edge=cons-implied
            is_team=mkey in ("h2h","spreads") and name in (home,away)
            is_home=is_team and name==home
            if mkey=="h2h": is_home_fav=bool(is_home and hfav)
            elif mkey=="spreads": is_home_fav=bool(is_home and point is not None and point<0)
            else: is_home_fav=False
            pin=pinn.get(key); pin_ok=bool(pin and odds>=pin)
            out.append({"event_id":event.get("id",""),"sport_key":sport_key,"sport_title":sport_title,"commence_time":event.get("commence_time",""),"home":home,"away":away,"pick":pick_label(mkey,name,point),"outcome_name":name,"market":market_label(mkey),"market_key":mkey,"point":point,"odds":round(odds,3),"bookmaker":book,"consensus_prob":round(cons,5),"implied_prob":round(implied,5),"edge":round(edge,5),"is_team_market":is_team,"home_pick":is_home,"home_fav":is_home_fav,"pinnacle":round(pin,3) if pin else None,"pinnacle_ok":pin_ok,"books":int(books[key])})
    return out

def decide(c, rules, manual, approved_today=0, loss_streak=0):
    min_odds=rules["min_odds"]; max_odds=rules["max_odds"]; min_edge=rules["min_edge_pct"]/100; elite_edge=rules["elite_edge_pct"]/100
    if manual.get("late_chase_feeling"): return "LOCKED — EMOTIONAL RISK",0,"Locked/chase","Late/chase feeling marked. Walk away."
    if loss_streak>=rules["lock_losses"]: return "LOCKED — EMOTIONAL RISK",0,"Locked/loss streak",f"Loss-streak lockout active: {loss_streak} losses."
    if approved_today>=rules["max_daily"]: return "LOCKED — DAILY LIMIT",0,"Locked/daily limit",f"Daily paper-pick limit reached: {approved_today}/{rules['max_daily']}."
    red_names=[]
    for label,key in [("Injury/news red flag","injury_red"),("Public-heavy red flag","public_red"),("Schedule/fatigue red flag","fatigue_red"),("Line moved against pick","line_against"),("Key player uncertainty","key_player_red")]:
        if manual.get(key): red_names.append(label)
    if red_names and rules["reject_red_flags"]: return "REJECTED — RED FLAG",0,"Red flag","; ".join(red_names)
    if not (min_odds<=c["odds"]<=max_odds): return "REJECTED — ODDS RANGE",0,"Odds range",f"Odds {c['odds']:.2f} outside {min_odds:.2f}-{max_odds:.2f}."
    if c["edge"]<min_edge: return "REJECTED — EDGE TOO LOW",0,"Edge too low",f"Edge {c['edge']*100:.2f}% below minimum {min_edge*100:.2f}%."
    if rules["apply_home_rules_to_team_markets"] and c["is_team_market"]:
        if rules["require_home_pick"] and not c["home_pick"]: return "REJECTED — NOT HOME PICK",0,"Not home pick","Team-market pick is not the home team."
        if rules["require_home_favourite"] and not c["home_fav"]: return "REJECTED — NOT HOME FAVOURITE",0,"Not home favourite","Team-market pick is not a home favourite."
    if rules["require_pinnacle_value"] and not c["pinnacle_ok"]: return "WATCHLIST — PINNACLE NOT CONFIRMED",45,"Pinnacle missing","Pinnacle value not available/confirmed."
    score=25 + (20 if c["edge"]>=elite_edge else 0) + {"h2h":15,"spreads":12,"totals":10}.get(c["market_key"],5)
    score += (15 if c["home_pick"] else 0) if c["is_team_market"] else 10
    score += (15 if c["home_fav"] else 0) if c["is_team_market"] else 0
    score += 10 if c["pinnacle_ok"] else 0
    score += min(15,c["books"]); score=min(100,score)
    if c["edge"]>=elite_edge and score>=75: return "ELITE PAPER PICK",score,"Approved","High edge and GOAT gates passed."
    return "APPROVED PAPER PICK",score,"Approved","Core live-data GOAT gates passed."

def load_picks():
    if "picks_df" not in st.session_state:
        if PICKS_PATH.exists():
            try: st.session_state.picks_df=pd.read_csv(PICKS_PATH)
            except Exception: st.session_state.picks_df=pd.DataFrame()
        else: st.session_state.picks_df=pd.DataFrame()
    return st.session_state.picks_df

def save_picks(df):
    st.session_state.picks_df=df; PICKS_PATH.parent.mkdir(exist_ok=True)
    try: df.to_csv(PICKS_PATH,index=False)
    except Exception: pass

def approved_today(df):
    if df.empty or "created_at" not in df.columns or "decision" not in df.columns: return 0
    today=datetime.now().date().isoformat()
    return int((df["created_at"].astype(str).str.startswith(today) & df["decision"].astype(str).str.contains("APPROVED|ELITE|SAFE", regex=True)).sum())

def current_loss_streak(df):
    if df.empty or "result" not in df.columns: return 0
    streak=0
    for _,row in df.sort_values("created_at", ascending=False).iterrows():
        res=str(row.get("result","Pending"))
        if res=="Lost": streak+=1
        elif res in ("Won","Push"): break
    return streak

def get_active_sports(api_key):
    if not api_key: return FALLBACK_SPORTS
    if "active_sports" in st.session_state: return st.session_state.active_sports
    try:
        sports=fetch_sports(api_key)
        active={s["key"]:s.get("title",s["key"]) for s in sports if s.get("active",False) and not s.get("has_outrights",False)}
        if active:
            st.session_state.active_sports=active; return active
    except Exception as e: st.sidebar.warning(f"Could not auto-load sports: {e}")
    return FALLBACK_SPORTS

def summary(rows):
    if not rows: return {"plain":"No candidates were generated. Try a different active sport, region, or market.","top":[],"approved":0,"watch":0,"reject":0,"locked":0,"total":0}
    dec=[r["decision"] for r in rows]; buckets=[r["reject_bucket"] for r in rows if r["reject_bucket"]!="Approved"]
    approved=sum(("APPROVED" in d or "ELITE" in d) for d in dec); watch=sum("WATCHLIST" in d for d in dec); reject=sum("REJECTED" in d for d in dec); locked=sum("LOCKED" in d for d in dec)
    top=Counter(buckets).most_common(5)
    plain=f"{approved} approved/elite paper candidate(s). Still paper-log only." if approved else "No approved paper picks. Main blockers: " + (", ".join([f"{k}: {v}" for k,v in top]) if top else "No strong edge found.")
    return {"plain":plain,"top":top,"approved":approved,"watch":watch,"reject":reject,"locked":locked,"total":len(rows)}

def ai_review_text(c, decision, reasons):
    return f"Paper-only discipline review:\n- Pick: {c['pick']} ({c['market']}) @ {c['odds']}\n- Game: {c['away']} @ {c['home']}\n- Edge: {c['edge']*100:.2f}%\n- Decision: {decision}\n- Reasons: {reasons}\nReminder: paper tracking only. No real-money automation."

st.title("🐐 GOAT Shield Live v2")
st.caption("Auto active sports + moneyline/spreads/totals + no-pick explanation. Paper-only. No sportsbook login. No real-money auto-betting.")
api_key_default=secret("ODDS_API_KEY","")
with st.sidebar:
    st.markdown("### 🔌 Connection status")
    st.write("Odds API:", "✅ key found" if api_key_default else "Paste key below / Secrets")
    st.write("AI review:", "Optional/off")
    st.caption("No sportsbook login. No auto-betting.")
    st.markdown("### Data source")
    api_key=st.text_input("The Odds API key", value=api_key_default, type="password")
    if st.button("Reload active sports list"):
        st.session_state.pop("active_sports",None); st.rerun()
    sports_map=get_active_sports(api_key)
    sport_key=st.selectbox("Active sport", list(sports_map.keys()), format_func=lambda k:sports_map.get(k,k))
    markets=st.multiselect("Markets", ["h2h","spreads","totals"], default=["h2h"], format_func=lambda x:{"h2h":"Moneyline / h2h","spreads":"Spreads","totals":"Totals"}[x])
    regions=st.multiselect("Regions", ["us","uk","au","eu"], default=["us"])
    st.caption(f"Estimated current-odds credits for one fetch: about {max(1,len(regions))*max(1,len(markets))}.")
    st.markdown("### GOAT rules")
    rules={}
    rules["min_odds"]=st.number_input("Min decimal odds",1.01,10.0,1.40,0.01)
    rules["max_odds"]=st.number_input("Max decimal odds",1.01,10.0,2.20,0.01)
    rules["min_edge_pct"]=st.number_input("Min edge %",0.0,50.0,2.0,0.1)
    rules["elite_edge_pct"]=st.number_input("Elite edge %",0.0,50.0,5.0,0.1)
    rules["max_daily"]=st.number_input("Max approved paper picks/day",1,20,3,1)
    rules["lock_losses"]=st.number_input("Loss-streak lockout",1,20,3,1)
    rules["apply_home_rules_to_team_markets"]=st.checkbox("Apply home rules to team markets only", True)
    rules["require_home_pick"]=st.checkbox("Require team-market pick to be home team", True)
    rules["require_home_favourite"]=st.checkbox("Require home favourite for team markets", True)
    rules["require_pinnacle_value"]=st.checkbox("Require Pinnacle value if present", False)
    rules["reject_red_flags"]=st.checkbox("Reject any manual red flag", True)
    st.markdown("### Manual proof/red flags")
    manual={"sports_alerts_support":st.checkbox("Sports Alerts proof checked",False),"scp_support":st.checkbox("Sports Chat Place proof checked",False),"picks_parlays_support":st.checkbox("Picks & Parlays proof checked",False),"injury_red":st.checkbox("Injury/news red flag",False),"public_red":st.checkbox("Public-heavy red flag",False),"fatigue_red":st.checkbox("Schedule/fatigue red flag",False),"line_against":st.checkbox("Line moved against pick",False),"key_player_red":st.checkbox("Key player uncertainty",False),"late_chase_feeling":st.checkbox("Late/chase feeling",False)}

df=load_picks()
tabs=st.tabs(["🔴 Live Scanner","📒 Paper Log","✅ Results","📊 Dashboard","🛡️ Backup"])
with tabs[0]:
    st.subheader("🔴 Live odds scanner")
    st.info("v2 scans active sports, moneyline/spreads/totals, then explains why there is no pick.")
    if not api_key: st.warning("Add your The Odds API key in the sidebar or Streamlit Secrets.")
    if st.button("Fetch live odds and scan"):
        if not api_key or not markets or not regions: st.stop()
        try:
            events,meta=fetch_odds(api_key,sport_key,regions=",".join(regions),markets=",".join(markets))
            st.session_state["last_events"]=events; st.session_state["last_meta"]=meta; st.session_state["last_sport_key"]=sport_key; st.session_state["last_markets"]=markets
            st.success(f"Fetched {len(events)} events. Requests remaining: {meta.get('requests_remaining')}")
        except Exception as e:
            st.error(f"Odds fetch failed: {e}"); st.stop()
    events=st.session_state.get("last_events",[]); meta=st.session_state.get("last_meta",{})
    if meta: st.caption(f"Last fetch UTC: {meta.get('last_fetch_utc')} | API used: {meta.get('requests_used')} | remaining: {meta.get('requests_remaining')}")
    if events:
        candidates=scan_events(events, st.session_state.get("last_sport_key",sport_key), st.session_state.get("last_markets",markets))
        rows=[]; loss=current_loss_streak(df); approved=approved_today(df)
        for c in candidates:
            decision,score,bucket,reasons=decide(c,rules,manual,approved,loss)
            row={**c,"decision":decision,"score":score,"reject_bucket":bucket,"reasons":reasons}
            rows.append(row)
        summ=summary(rows)
        c1,c2,c3,c4=st.columns(4); c1.metric("Candidates scanned",summ["total"]); c2.metric("Approved/Elite",summ["approved"]); c3.metric("Watchlist",summ["watch"]); c4.metric("Rejected/Locked",summ["reject"]+summ["locked"])
        if summ["approved"]==0:
            st.warning(summ["plain"])
            if summ["top"]:
                st.write("Main no-pick reasons:")
                for reason,count in summ["top"]: st.write(f"- {reason}: {count}")
        else: st.success(summ["plain"])
        table=pd.DataFrame(rows)
        if table.empty: st.warning("No market candidates found. Try another active sport, region, or market.")
        else:
            table["sort"]=table["decision"].map({"ELITE PAPER PICK":0,"APPROVED PAPER PICK":1,"WATCHLIST — PINNACLE NOT CONFIRMED":2}).fillna(9)
            table=table.sort_values(["sort","edge"],ascending=[True,False]).reset_index(drop=True)
            cols=["decision","score","market","pick","odds","bookmaker","edge","home","away","home_pick","home_fav","pinnacle","pinnacle_ok","books","reasons"]
            st.dataframe(table[cols].style.format({"edge":"{:.2%}"}), use_container_width=True, hide_index=True)
            approved_rows=table[table["decision"].str.contains("APPROVED|ELITE", regex=True, na=False)]
            if not approved_rows.empty:
                labels=approved_rows.apply(lambda r:f"{r.name}: {r['decision']} — {r['pick']} @ {r['odds']} ({r['bookmaker']})",axis=1).tolist()
                selected=st.selectbox("Approved/elite candidate to inspect",labels); idx=int(selected.split(":")[0]); row=table.loc[idx]
                review=ai_review_text(row,row["decision"],row["reasons"]); st.text_area("AI / discipline review",review,height=220)
                if st.button("Auto-log this as PAPER pick"):
                    new={"created_at":datetime.now(timezone.utc).isoformat(timespec="seconds"),"sport":row["sport_key"],"commence_time":row["commence_time"],"home_team":row["home"],"away_team":row["away"],"pick_label":row["pick"],"market":row["market_key"],"odds":row["odds"],"bookmaker":row["bookmaker"],"edge":row["edge"],"decision":row["decision"],"score":row["score"],"result":"Pending","ai_review":review}
                    save_picks(pd.concat([pd.DataFrame([new]),df],ignore_index=True)); st.success("Logged as paper pick only."); st.rerun()
            else: st.info("No approved paper picks in this scan. That is okay. No edge = no paper pick.")
    elif meta: st.warning("Fetched 0 events. Try another active sport, market, or region.")
with tabs[1]:
    st.subheader("📒 Paper Log"); df=load_picks()
    if df.empty: st.info("No paper picks yet.")
    else: st.dataframe(df,use_container_width=True); st.download_button("Download paper log CSV",df.to_csv(index=False).encode("utf-8"),"goat_shield_paper_log.csv","text/csv")
with tabs[2]:
    st.subheader("✅ Update Results"); df=load_picks()
    if df.empty: st.info("No picks yet.")
    else: st.dataframe(df,use_container_width=True)
with tabs[3]:
    st.subheader("📊 Paper Proof Dashboard")
    df=load_picks()
    if df.empty: st.info("No paper picks logged yet.")
    else: st.dataframe(df,use_container_width=True)
with tabs[4]:
    st.subheader("🛡️ Backup / restore")
    df=load_picks()
    if not df.empty: st.download_button("Download backup CSV",df.to_csv(index=False).encode("utf-8"),"goat_shield_backup.csv","text/csv")
    if st.button("Delete all local paper picks"):
        save_picks(pd.DataFrame()); st.success("Deleted local paper log."); st.rerun()
st.divider(); st.caption("GOAT Shield Live v2 is a paper-betting proof system only. It does not place real-money bets, log into sportsbooks, or bypass any betting rules.")
