
from __future__ import annotations

import os, math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
PICKS_PATH = Path("data/paper_picks.csv")
NZ_TZ = "Pacific/Auckland"
FALLBACK_SPORTS = {
    "baseball_mlb": "MLB",
    "basketball_nba": "NBA",
    "icehockey_nhl": "NHL",
    "americanfootball_nfl": "NFL",
    "soccer_epl": "EPL",
    "aussierules_afl": "AFL",
    "rugbyleague_nrl": "NRL",
}
NZ_BOOK_HINTS = ("tab", "tab nz", "betcha", "entain")
PINNACLE_HINTS = ("pinnacle",)
BET365_HINTS = ("bet365",)


def secret(name: str, default: str = "") -> str:
    try:
        return st.secrets.get(name, default)
    except Exception:
        return os.environ.get(name, default)


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default


def point_val(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def has(text: str, hints: Tuple[str, ...]) -> bool:
    t = str(text or "").lower()
    return any(h in t for h in hints)


def fmt_point(p: Optional[float]) -> str:
    if p is None:
        return ""
    return f"+{p:g}" if p > 0 else f"{p:g}"


def mlabel(market: str) -> str:
    return {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}.get(market, market)


def plabel(market: str, outcome: str, point: Optional[float]) -> str:
    if market == "h2h":
        return f"{outcome} ML"
    if market == "spreads":
        return f"{outcome} {fmt_point(point)}"
    if market == "totals":
        return f"{outcome} {point:g}" if point is not None else outcome
    return f"{outcome} {fmt_point(point)}".strip()


def time_window(mode: str) -> Tuple[Optional[str], Optional[str]]:
    now_utc = datetime.now(timezone.utc)
    if mode == "No time filter":
        return None, None
    if mode == "Next 24 hours":
        return now_utc.isoformat(timespec="seconds"), (now_utc + timedelta(hours=24)).isoformat(timespec="seconds")
    if mode == "Today NZ" and ZoneInfo is not None:
        nz = ZoneInfo(NZ_TZ)
        now_nz = datetime.now(nz)
        start_nz = now_nz.replace(hour=0, minute=0, second=0, microsecond=0)
        end_nz = start_nz + timedelta(days=1)
        return start_nz.astimezone(timezone.utc).isoformat(timespec="seconds"), end_nz.astimezone(timezone.utc).isoformat(timespec="seconds")
    start = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
    return start.isoformat(timespec="seconds"), (start + timedelta(days=1)).isoformat(timespec="seconds")


def fetch_sports(api_key: str) -> Dict[str, str]:
    if not api_key:
        return FALLBACK_SPORTS
    if "active_sports_v3" in st.session_state:
        return st.session_state.active_sports_v3
    try:
        r = requests.get(f"{ODDS_API_BASE}/sports/", params={"apiKey": api_key}, timeout=25)
        r.raise_for_status()
        sports = r.json()
        active = {s["key"]: s.get("title", s["key"]) for s in sports if s.get("active", False) and not s.get("has_outrights", False)}
        if active:
            st.session_state.active_sports_v3 = active
            return active
    except Exception as e:
        st.sidebar.warning(f"Could not auto-load sports yet: {e}")
    return FALLBACK_SPORTS


def fetch_odds(api_key: str, sport: str, regions: str, markets: str, bookmakers: str, start: Optional[str], end: Optional[str]):
    params = {"apiKey": api_key, "markets": markets, "oddsFormat": "decimal", "dateFormat": "iso"}
    if bookmakers.strip():
        params["bookmakers"] = bookmakers.strip()
    else:
        params["regions"] = regions
    if start:
        params["commenceTimeFrom"] = start
    if end:
        params["commenceTimeTo"] = end
    r = requests.get(f"{ODDS_API_BASE}/sports/{sport}/odds/", params=params, timeout=35)
    r.raise_for_status()
    return r.json(), {"sport": sport, "used": r.headers.get("x-requests-used"), "remaining": r.headers.get("x-requests-remaining"), "fetched": datetime.now(timezone.utc).isoformat(timespec="seconds")}


def explode_prices(events: List[Dict[str, Any]], markets: List[str]) -> List[Dict[str, Any]]:
    rows = []
    for ev in events:
        home, away = ev.get("home_team", ""), ev.get("away_team", "")
        for bm in ev.get("bookmakers", []):
            bk = str(bm.get("key", ""))
            bt = str(bm.get("title", bk or "unknown"))
            for m in bm.get("markets", []):
                mk = str(m.get("key", ""))
                if mk not in markets:
                    continue
                for o in m.get("outcomes", []):
                    price = fnum(o.get("price"), 0)
                    if price <= 1:
                        continue
                    pt = point_val(o.get("point"))
                    name = str(o.get("name", ""))
                    rows.append({
                        "sport_key": ev.get("sport_key", ""), "sport_title": ev.get("sport_title", ev.get("sport_key", "")),
                        "event_id": ev.get("id", ""), "start": ev.get("commence_time", ""),
                        "home": home, "away": away, "market": mk, "market_label": mlabel(mk),
                        "outcome": name, "point": pt, "pick": plabel(mk, name, pt),
                        "bookmaker_key": bk, "bookmaker": bt, "price": round(price, 3)
                    })
    return rows


def key_for(r: Dict[str, Any]) -> Tuple[str, str, Optional[float]]:
    return (r["market"], r["outcome"], r["point"])


def consensus_probs(event_rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str, Optional[float]], float]:
    # No-vig probability per bookmaker/market/line, then averaged.
    probs = defaultdict(list)
    groups = defaultdict(list)
    for r in event_rows:
        line = r["point"] if r["market"] in ("spreads", "totals") else None
        groups[(r["bookmaker_key"], r["market"], line)].append(r)
    for _, g in groups.items():
        if len(g) < 2:
            continue
        invs = [1 / x["price"] for x in g if x["price"] > 1]
        s = sum(invs)
        if s <= 0:
            continue
        for row, inv in zip(g, invs):
            probs[key_for(row)].append(inv / s)
    return {k: sum(v) / len(v) for k, v in probs.items() if v}


def build_board(events: List[Dict[str, Any]], markets: List[str]) -> pd.DataFrame:
    rows = explode_prices(events, markets)
    if not rows:
        return pd.DataFrame()
    board_rows = []
    by_event = defaultdict(list)
    for r in rows:
        by_event[r["event_id"]].append(r)

    for _, ev_rows in by_event.items():
        cprobs = consensus_probs(ev_rows)
        groups = defaultdict(list)
        for r in ev_rows:
            groups[key_for(r)].append(r)

        home = ev_rows[0]["home"]
        away = ev_rows[0]["away"]
        h2h_best = {}
        for k, g in groups.items():
            if k[0] == "h2h":
                h2h_best[k[1]] = max(x["price"] for x in g)
        home_fav_h2h = bool(h2h_best.get(home, 0) > 1 and h2h_best.get(away, 0) > 1 and h2h_best[home] < h2h_best[away])

        for k, g in groups.items():
            best = max(g, key=lambda x: x["price"])
            avg = sum(x["price"] for x in g) / len(g)
            pin = [x for x in g if has(x["bookmaker"], PINNACLE_HINTS) or has(x["bookmaker_key"], PINNACLE_HINTS)]
            nzb = [x for x in g if has(x["bookmaker"], NZ_BOOK_HINTS) or has(x["bookmaker_key"], NZ_BOOK_HINTS)]
            b365 = [x for x in g if has(x["bookmaker"], BET365_HINTS) or has(x["bookmaker_key"], BET365_HINTS)]
            pin_best = max(pin, key=lambda x: x["price"]) if pin else None
            nz_best = max(nzb, key=lambda x: x["price"]) if nzb else None
            b365_best = max(b365, key=lambda x: x["price"]) if b365 else None
            cp = cprobs.get(k, 1 / avg if avg > 1 else 0)
            implied = 1 / best["price"]
            edge = cp - implied
            market, outcome, point = k
            is_team = market in ("h2h", "spreads") and outcome in (home, away)
            is_home = is_team and outcome == home
            if market == "h2h":
                is_home_fav = bool(is_home and home_fav_h2h)
            elif market == "spreads":
                is_home_fav = bool(is_home and point is not None and point < 0)
            else:
                is_home_fav = False
            prices_text = " | ".join(f"{x['bookmaker']}: {x['price']:.2f}" for x in sorted(g, key=lambda x: x["price"], reverse=True)[:10])
            board_rows.append({
                "sport": best["sport_title"], "event_id": best["event_id"], "start": best["start"],
                "game": f"{away} @ {home}", "home": home, "away": away,
                "market": mlabel(market), "market_key": market, "pick": best["pick"], "outcome": outcome, "point": point,
                "best_odds": round(best["price"], 3), "best_bookmaker": best["bookmaker"],
                "avg_odds": round(avg, 3), "pinnacle": round(pin_best["price"], 3) if pin_best else None,
                "pinnacle_book": pin_best["bookmaker"] if pin_best else "",
                "tab_betcha": round(nz_best["price"], 3) if nz_best else None, "tab_betcha_book": nz_best["bookmaker"] if nz_best else "",
                "bet365": round(b365_best["price"], 3) if b365_best else None, "bet365_book": b365_best["bookmaker"] if b365_best else "",
                "consensus_prob": round(cp, 5), "implied_prob": round(implied, 5), "edge": round(edge, 5),
                "books": len(g), "home_pick": is_home, "home_fav": is_home_fav, "is_team_market": is_team,
                "pinnacle_ok": bool(pin_best and best["price"] >= pin_best["price"]), "all_prices": prices_text
            })
    return pd.DataFrame(board_rows)


def decide(row: pd.Series, rules: Dict[str, Any], flags: Dict[str, bool], approved_count: int, loss_streak: int) -> Dict[str, Any]:
    min_odds, max_odds = rules["min_odds"], rules["max_odds"]
    min_edge = rules["min_edge_pct"] / 100
    elite_edge = rules["elite_edge_pct"] / 100
    if flags.get("late_chase_feeling"):
        return {"decision": "LOCKED — EMOTIONAL RISK", "score": 0, "risk": "High", "reject_bucket": "Locked/chase", "reasons": "Late/chase feeling marked."}
    if loss_streak >= rules["lock_losses"]:
        return {"decision": "LOCKED — LOSS STREAK", "score": 0, "risk": "High", "reject_bucket": "Locked/loss streak", "reasons": f"Loss streak lockout: {loss_streak}."}
    if approved_count >= rules["max_daily"]:
        return {"decision": "LOCKED — DAILY LIMIT", "score": 0, "risk": "High", "reject_bucket": "Locked/daily limit", "reasons": f"Daily limit reached: {approved_count}/{rules['max_daily']}."}
    red_flags = [name for name, val in [
        ("Injury/news red flag", flags.get("injury_red")), ("Public-heavy red flag", flags.get("public_red")),
        ("Schedule/fatigue red flag", flags.get("fatigue_red")), ("Line moved against pick", flags.get("line_against")),
        ("Key player uncertainty", flags.get("key_player_red"))] if val]
    if red_flags and rules["reject_red_flags"]:
        return {"decision": "REJECTED — RED FLAG", "score": 0, "risk": "High", "reject_bucket": "Red flag", "reasons": "; ".join(red_flags)}
    if not (min_odds <= row.best_odds <= max_odds):
        return {"decision": "REJECTED — ODDS RANGE", "score": 0, "risk": "Medium", "reject_bucket": "Odds range", "reasons": f"Best odds {row.best_odds:.2f} outside {min_odds:.2f}-{max_odds:.2f}."}
    if row.edge < min_edge:
        return {"decision": "REJECTED — EDGE TOO LOW", "score": 0, "risk": "Medium", "reject_bucket": "Edge too low", "reasons": f"Edge {row.edge*100:.2f}% below {min_edge*100:.2f}%."}
    if rules["apply_home_rules_to_team_markets"] and bool(row.is_team_market):
        if rules["require_home_pick"] and not bool(row.home_pick):
            return {"decision": "REJECTED — NOT HOME PICK", "score": 0, "risk": "Medium", "reject_bucket": "Not home pick", "reasons": "Team-market pick is not home team."}
        if rules["require_home_favourite"] and not bool(row.home_fav):
            return {"decision": "REJECTED — NOT HOME FAVOURITE", "score": 0, "risk": "Medium", "reject_bucket": "Not home favourite", "reasons": "Home favourite rule failed."}
    if rules["require_pinnacle_value"] and not bool(row.pinnacle_ok):
        return {"decision": "WATCHLIST — PINNACLE NOT CONFIRMED", "score": 45, "risk": "Medium", "reject_bucket": "Pinnacle missing", "reasons": "Pinnacle value not present/confirmed."}
    score = 25 + (20 if row.edge >= elite_edge else 0) + min(15, int(row.books)) + (10 if row.best_odds >= row.avg_odds else 0) + (10 if bool(row.pinnacle_ok) else 0)
    if bool(row.is_team_market):
        score += 10 if bool(row.home_pick) else 0
        score += 10 if bool(row.home_fav) else 0
    else:
        score += 10
    score = min(100, score)
    if row.edge >= elite_edge and score >= 75:
        return {"decision": "ELITE PAPER PICK", "score": score, "risk": "Low/Medium", "reject_bucket": "Approved", "reasons": "Best-price edge and GOAT gates passed."}
    return {"decision": "APPROVED PAPER PICK", "score": score, "risk": "Medium", "reject_bucket": "Approved", "reasons": "Best-price board and GOAT gates passed."}


def load_log() -> pd.DataFrame:
    if "paper_log_v3" not in st.session_state:
        if PICKS_PATH.exists():
            try: st.session_state.paper_log_v3 = pd.read_csv(PICKS_PATH)
            except Exception: st.session_state.paper_log_v3 = pd.DataFrame()
        else:
            st.session_state.paper_log_v3 = pd.DataFrame()
    return st.session_state.paper_log_v3


def save_log(df: pd.DataFrame) -> None:
    st.session_state.paper_log_v3 = df
    PICKS_PATH.parent.mkdir(exist_ok=True)
    try: df.to_csv(PICKS_PATH, index=False)
    except Exception: pass


def approved_today(df: pd.DataFrame) -> int:
    if df.empty or "created_at" not in df.columns or "decision" not in df.columns: return 0
    today = datetime.now().date().isoformat()
    return int((df.created_at.astype(str).str.startswith(today) & df.decision.astype(str).str.contains("APPROVED|ELITE", regex=True)).sum())


def loss_streak(df: pd.DataFrame) -> int:
    if df.empty or "result" not in df.columns: return 0
    s = 0
    for _, row in df.sort_values("created_at", ascending=False).iterrows():
        if str(row.get("result")) == "Lost": s += 1
        elif str(row.get("result")) in ("Won", "Push"): break
    return s


def summarize(scan: pd.DataFrame) -> str:
    if scan.empty: return "No candidates generated."
    approved = scan.decision.astype(str).str.contains("APPROVED|ELITE", regex=True).sum()
    if approved: return f"{approved} approved/elite paper candidate(s). Paper-log only."
    top = Counter(scan.reject_bucket[scan.reject_bucket != "Approved"]).most_common(5)
    return "No approved paper picks. Main blockers: " + ", ".join(f"{k}: {v}" for k, v in top)


# ==========================================================
# APP UI
# ==========================================================

st.set_page_config(page_title="GOAT Shield Live v3", page_icon="🐐", layout="wide", initial_sidebar_state="expanded")
st.title("🐐 GOAT Shield Live v3")
st.caption("Best Price Board + active sports + moneyline/spreads/totals + no-pick explanation. Paper-only. No sportsbook login. No real-money auto-betting.")

api_secret = secret("ODDS_API_KEY", "")
with st.sidebar:
    st.markdown("### 🔌 Connection status")
    st.write("Odds API:", "✅ key found" if api_secret else "Paste key below / Secrets")
    st.caption("No sportsbook login. No auto-betting.")
    api_key = st.text_input("The Odds API key", value=api_secret, type="password")
    if st.button("Reload active sports list"):
        st.session_state.pop("active_sports_v3", None); st.rerun()
    sports_map = fetch_sports(api_key)
    sports = list(sports_map.keys())
    default = "baseball_mlb" if "baseball_mlb" in sports else sports[0]
    selected_sports = st.multiselect("Active sports to scan", sports, default=[default], format_func=lambda k: sports_map.get(k, k))
    markets = st.multiselect("Markets", ["h2h", "spreads", "totals"], default=["h2h"], format_func=lambda x: {"h2h":"Moneyline / h2h","spreads":"Spreads","totals":"Totals"}[x])
    regions = st.multiselect("Regions", ["us", "uk", "au", "eu"], default=["us"])
    bookmakers = st.text_input("Optional bookmaker filter", value="", help="Example: pinnacle,draftkings. Leave blank to use region books.")
    time_mode = st.selectbox("Time filter", ["Today NZ", "Next 24 hours", "No time filter"])
    credits = max(1, len(selected_sports)) * max(1, len(markets)) * (1 if bookmakers.strip() else max(1, len(regions)))
    st.caption(f"Estimated credits/fetch: about {credits}. Start small.")

    st.markdown("### GOAT rules")
    rules = dict(DEFAULT_RULES)
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
    flags = {
        "injury_red": st.checkbox("Injury/news red flag", False),
        "public_red": st.checkbox("Public-heavy red flag", False),
        "fatigue_red": st.checkbox("Schedule/fatigue red flag", False),
        "line_against": st.checkbox("Line moved against pick", False),
        "key_player_red": st.checkbox("Key player uncertainty", False),
        "late_chase_feeling": st.checkbox("Late/chase feeling", False),
    }

tabs = st.tabs(["🟢 Best Price Board", "📒 Paper Log", "✅ Results", "📊 Dashboard", "🛡️ Backup"])
logdf = load_log()

with tabs[0]:
    st.subheader("🟢 Best Price Board")
    st.info("Compares the same pick/market/line across bookmakers in the live odds feed. Research and paper-log only.")
    if st.button("Fetch best prices and GOAT scan"):
        if not api_key: st.error("Add your Odds API key first."); st.stop()
        if not selected_sports: st.error("Choose at least one sport."); st.stop()
        if not markets: st.error("Choose at least one market."); st.stop()
        start, end = time_window(time_mode)
        all_events, metas, errors = [], [], []
        with st.spinner("Fetching odds..."):
            for s in selected_sports:
                try:
                    ev, meta = fetch_odds(api_key, s, ",".join(regions), ",".join(markets), bookmakers, start, end)
                    all_events.extend(ev); metas.append(meta)
                except Exception as e:
                    errors.append(f"{sports_map.get(s, s)}: {e}")
        for e in errors: st.error(e)
        if metas: st.success(f"Fetched {len(all_events)} events. Used: {metas[-1].get('used')} | Remaining: {metas[-1].get('remaining')}")
        st.session_state.v3_events = all_events
        st.session_state.v3_markets = markets

    events = st.session_state.get("v3_events", [])
    scan_markets = st.session_state.get("v3_markets", markets)
    if events:
        board = build_board(events, scan_markets)
        if board.empty:
            st.warning("No price candidates found. Try a different sport, market, time filter, or region.")
        else:
            decs = []
            ap = approved_today(logdf); ls = loss_streak(logdf)
            for _, r in board.iterrows(): decs.append(decide(r, rules, flags, ap, ls))
            decdf = pd.DataFrame(decs)
            scan = pd.concat([board.reset_index(drop=True), decdf], axis=1)
            scan["sort_decision"] = scan["decision"].map({"ELITE PAPER PICK":0, "APPROVED PAPER PICK":1, "WATCHLIST — PINNACLE NOT CONFIRMED":2}).fillna(9)
            scan = scan.sort_values(["sort_decision", "edge", "best_odds"], ascending=[True, False, False]).reset_index(drop=True)
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Candidates", len(scan)); c2.metric("Approved/Elite", int(scan.decision.astype(str).str.contains("APPROVED|ELITE", regex=True).sum()))
            c3.metric("Watchlist", int(scan.decision.astype(str).str.contains("WATCHLIST").sum()))
            c4.metric("Rejected/Locked", int(scan.decision.astype(str).str.contains("REJECTED|LOCKED", regex=True).sum()))
            if scan.decision.astype(str).str.contains("APPROVED|ELITE", regex=True).sum(): st.success(summarize(scan))
            else: st.warning(summarize(scan))
            if "reject_bucket" in scan.columns:
                top = Counter(scan.reject_bucket[scan.reject_bucket != "Approved"]).most_common(6)
                if top:
                    st.write("Main no-pick reasons:")
                    for k, v in top: st.write(f"- {k}: {v}")
            cols = ["decision","score","sport","game","market","pick","best_odds","best_bookmaker","avg_odds","pinnacle","tab_betcha","bet365","edge","books","reasons","all_prices"]
            st.dataframe(scan[cols].style.format({"edge":"{:.2%}","best_odds":"{:.2f}","avg_odds":"{:.2f}"}), use_container_width=True, hide_index=True)
            st.subheader("👀 Closest missed picks")
            misses = scan[~scan.decision.astype(str).str.contains("APPROVED|ELITE", regex=True)].sort_values(["edge","score"], ascending=[False, False]).head(5)
            if len(misses): st.dataframe(misses[["decision","market","pick","best_odds","best_bookmaker","edge","reasons","all_prices"]].style.format({"edge":"{:.2%}"}), use_container_width=True, hide_index=True)
            else: st.info("No close misses to show.")
            approved = scan[scan.decision.astype(str).str.contains("APPROVED|ELITE", regex=True)]
            if len(approved):
                labels = approved.apply(lambda r: f"{r.name}: {r.decision} — {r.pick} @ {r.best_odds} ({r.best_bookmaker})", axis=1).tolist()
                sel = st.selectbox("Approved/elite candidate", labels)
                idx = int(sel.split(":")[0])
                if st.button("Log this as PAPER pick"):
                    r = scan.loc[idx]
                    new = {"created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "sport": r.sport, "game": r.game, "market": r.market, "pick_label": r.pick, "best_odds": r.best_odds, "best_bookmaker": r.best_bookmaker, "edge": r.edge, "decision": r.decision, "score": r.score, "result":"Pending", "closing_odds":"", "profit_units":"", "clv":"", "all_prices": r.all_prices}
                    save_log(pd.concat([pd.DataFrame([new]), logdf], ignore_index=True)); st.success("Logged as paper pick only."); st.rerun()

with tabs[1]:
    st.subheader("📒 Paper Log")
    df = load_log()
    if df.empty: st.info("No paper picks logged yet.")
    else:
        st.dataframe(df, use_container_width=True)
        st.download_button("Download paper log CSV", df.to_csv(index=False).encode("utf-8"), "goat_shield_paper_log.csv", "text/csv")

with tabs[2]:
    st.subheader("✅ Update Results")
    df = load_log()
    if df.empty or "result" not in df.columns: st.info("No pending picks yet.")
    else:
        pending = df[df.result.astype(str).eq("Pending")]
        if pending.empty: st.info("No pending paper picks.")
        else:
            labels = pending.apply(lambda r: f"{r.name}: {r.get('pick_label','Pick')} @ {r.get('best_odds','')}", axis=1).tolist()
            choice = st.selectbox("Select pending pick", labels); idx = int(choice.split(":")[0])
            result = st.selectbox("Result", ["Won","Lost","Push"])
            closing = st.number_input("Closing odds if known", min_value=0.0, value=0.0, step=0.01)
            if st.button("Save result"):
                odds = fnum(df.loc[idx].get("best_odds", 0), 0)
                profit = odds - 1 if result == "Won" else (-1 if result == "Lost" else 0)
                df.loc[idx, "result"] = result; df.loc[idx, "profit_units"] = profit
                if closing > 0: df.loc[idx, "closing_odds"] = closing; df.loc[idx, "clv"] = (odds - closing) / closing
                save_log(df); st.success("Result updated."); st.rerun()

with tabs[3]:
    st.subheader("📊 Dashboard")
    df = load_log()
    if df.empty: st.info("No paper picks logged yet.")
    else:
        settled = df[df.result.isin(["Won","Lost","Push"])] if "result" in df.columns else pd.DataFrame()
        pending = df[df.result.astype(str).eq("Pending")] if "result" in df.columns else pd.DataFrame()
        profit = pd.to_numeric(settled.get("profit_units", pd.Series(dtype=float)), errors="coerce").fillna(0).sum() if not settled.empty else 0
        roi = profit / max(len(settled), 1); clv = pd.to_numeric(settled.get("clv", pd.Series(dtype=float)), errors="coerce").dropna() if not settled.empty else pd.Series(dtype=float)
        x1,x2,x3,x4 = st.columns(4); x1.metric("Settled", len(settled)); x2.metric("Pending", len(pending)); x3.metric("Profit units", f"{profit:+.2f}u"); x4.metric("ROI", f"{roi*100:.1f}%")
        st.progress(min(1, len(settled)/300), text=f"Proof progress: {len(settled)}/300 settled paper picks")
        st.metric("Positive CLV", f"{((clv>0).mean()*100 if len(clv) else 0):.1f}%")
        st.warning("System verdict: NOT PROVEN — keep paper testing." if len(settled) < 300 else "Check ROI and CLV before any serious decision.")

with tabs[4]:
    st.subheader("🛡️ Backup / Restore")
    df = load_log()
    if not df.empty: st.download_button("Download backup CSV", df.to_csv(index=False).encode("utf-8"), "goat_shield_backup.csv", "text/csv")
    up = st.file_uploader("Restore CSV backup", type=["csv"])
    if up is not None:
        try:
            new_df = pd.read_csv(up)
            if st.button("Restore this CSV"):
                save_log(new_df); st.success("Restored."); st.rerun()
        except Exception as e: st.error(f"Restore failed: {e}")
    if st.button("Delete all local paper picks"):
        save_log(pd.DataFrame()); st.success("Deleted local paper log."); st.rerun()

st.divider()
st.caption("GOAT Shield Live v3 is a paper-betting proof system only. It does not place real-money bets, log into sportsbooks, scrape bookmakers, or bypass betting rules.")
