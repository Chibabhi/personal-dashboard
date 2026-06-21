
from __future__ import annotations

import os
import math
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter, defaultdict

import pandas as pd
import streamlit as st

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


# =========================================================
# CONFIG
# =========================================================
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
PICKS_PATH = Path("data/paper_picks.csv")

NZ_TZ_NAME = "Pacific/Auckland"
ET_TZ_NAME = "America/New_York"

DEFAULT_RULES = {
    "min_odds": 1.40,
    "max_odds": 2.20,
    "min_edge_pct": 2.0,
    "elite_edge_pct": 5.0,
    "max_daily": 3,
    "lock_losses": 3,
    "min_minutes_before_start": 90,
    "require_home_pick": True,
    "require_home_favourite": True,
    "require_pinnacle_value": False,
    "reject_red_flags": True,
    "apply_home_rules_to_team_markets": True,
}

FALLBACK_SPORTS = {
    "baseball_mlb": "MLB",
    "basketball_nba": "NBA",
    "icehockey_nhl": "NHL",
    "americanfootball_nfl": "NFL",
    "soccer_epl": "EPL",
    "aussierules_afl": "AFL",
    "rugbyleague_nrl": "NRL",
}

PINNACLE_HINTS = ("pinnacle",)
NZ_BOOK_HINTS = ("tab", "tab nz", "betcha", "entain")
BET365_HINTS = ("bet365",)


# =========================================================
# STREAMLIT PAGE
# =========================================================
st.set_page_config(
    page_title="GOAT Shield Live v3.5",
    page_icon="🐐",
    layout="wide",
    initial_sidebar_state="expanded",
)


# =========================================================
# BASIC HELPERS
# =========================================================
def get_tz(name: str):
    if ZoneInfo is None:
        return timezone.utc
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


NZ_TZ = get_tz(NZ_TZ_NAME)
ET_TZ = get_tz(ET_TZ_NAME)


def secret(name: str, default: str = "") -> str:
    try:
        return st.secrets.get(name, default)
    except Exception:
        return os.environ.get(name, default)


def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        if math.isfinite(v):
            return v
    except Exception:
        pass
    return default


def safe_point(x: Any) -> Optional[float]:
    if x is None:
        return None
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except Exception:
        return None


def has_hint(text: str, hints: Tuple[str, ...]) -> bool:
    t = str(text or "").lower()
    return any(h in t for h in hints)


def market_label(m: str) -> str:
    return {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}.get(m, m)


def fmt_point(p: Optional[float]) -> str:
    if p is None:
        return ""
    return f"+{p:g}" if p > 0 else f"{p:g}"


def make_pick_label(market: str, outcome: str, point: Optional[float]) -> str:
    if market == "h2h":
        return f"{outcome} ML"
    if market == "spreads":
        return f"{outcome} {fmt_point(point)}"
    if market == "totals":
        return f"{outcome} {point:g}" if point is not None else outcome
    return f"{outcome} {fmt_point(point)}".strip()


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_api_datetime(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        text = str(s)
        if text.endswith("Z"):
            text = text.replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fmt_dt(dt: Optional[datetime], tz, label: str) -> str:
    if dt is None:
        return ""
    local = dt.astimezone(tz)
    return local.strftime("%a %d %b, %I:%M %p").lstrip("0") + f" {label}"


def fmt_date(dt: Optional[datetime], tz) -> str:
    if dt is None:
        return ""
    return dt.astimezone(tz).strftime("%Y-%m-%d")


def starts_in_text(dt: Optional[datetime]) -> str:
    if dt is None:
        return ""
    diff = dt - datetime.now(timezone.utc)
    seconds = int(diff.total_seconds())
    if seconds < 0:
        return "Started"
    mins = seconds // 60
    if mins < 60:
        return f"{mins} min"
    hours = mins // 60
    rem = mins % 60
    if hours < 48:
        return f"{hours}h {rem}m"
    days = hours // 24
    return f"{days}d {hours % 24}h"


def game_time_status(dt: Optional[datetime], min_minutes: int) -> Tuple[str, bool]:
    if dt is None:
        return "Unknown time", False
    now = datetime.now(timezone.utc)
    diff_min = (dt - now).total_seconds() / 60
    if diff_min < 0:
        return "Started / locked", True
    if diff_min <= min_minutes:
        return f"Too close / locked (≤{min_minutes}m)", True
    return "Upcoming", False


def clean_api_error(e: Exception) -> str:
    msg = str(e)
    if "401" in msg:
        return "401 Unauthorized — API key is wrong, expired, or placeholder. Rotate/copy the real key."
    if "422" in msg:
        return "422 Unprocessable Entity — usually invalid market/sport/time-filter combination. Try another time filter, sport, or market."
    if "429" in msg:
        return "429 Rate limit / credit limit issue."
    if "404" in msg:
        return "404 Not found — sport/bookmaker/market not available."
    return msg.split(" for url:")[0]


# =========================================================
# NZ / US TIME FILTERS
# =========================================================
def time_window(mode: str) -> Tuple[Optional[str], Optional[str]]:
    now_utc = datetime.now(timezone.utc)

    if mode == "No time filter":
        return None, None

    if mode == "NZ Bettor Mode: Next 24 hours":
        return iso_z(now_utc), iso_z(now_utc + timedelta(hours=24))

    if mode == "NZ Bettor Mode: Next 36 hours":
        return iso_z(now_utc), iso_z(now_utc + timedelta(hours=36))

    if mode == "Today NZ":
        now_nz = now_utc.astimezone(NZ_TZ)
        start_nz = now_nz.replace(hour=0, minute=0, second=0, microsecond=0)
        end_nz = start_nz + timedelta(days=1)
        return iso_z(start_nz), iso_z(end_nz)

    if mode == "Today US Eastern":
        now_et = now_utc.astimezone(ET_TZ)
        start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
        end_et = start_et + timedelta(days=1)
        return iso_z(start_et), iso_z(end_et)

    if mode == "Tomorrow US Eastern":
        now_et = now_utc.astimezone(ET_TZ)
        start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
        end_et = start_et + timedelta(days=1)
        return iso_z(start_et), iso_z(end_et)

    return None, None


# =========================================================
# ODDS API
# =========================================================
def fetch_sports(api_key: str):
    r = requests.get(f"{ODDS_API_BASE}/sports/", params={"apiKey": api_key}, timeout=25)
    r.raise_for_status()
    return r.json()


def fetch_odds(
    api_key: str,
    sport_key: str,
    regions: str,
    markets: str,
    bookmakers: str = "",
    start: Optional[str] = None,
    end: Optional[str] = None,
):
    params = {
        "apiKey": api_key,
        "markets": markets,
        "oddsFormat": "decimal",
        "dateFormat": "iso",
    }
    if bookmakers.strip():
        params["bookmakers"] = bookmakers.strip()
    else:
        params["regions"] = regions

    if start:
        params["commenceTimeFrom"] = start
    if end:
        params["commenceTimeTo"] = end

    r = requests.get(f"{ODDS_API_BASE}/sports/{sport_key}/odds/", params=params, timeout=35)
    r.raise_for_status()

    meta = {
        "sport": sport_key,
        "requests_remaining": r.headers.get("x-requests-remaining"),
        "requests_used": r.headers.get("x-requests-used"),
        "last_fetch_utc": iso_z(datetime.now(timezone.utc)),
    }
    return r.json(), meta


def get_sports_map(api_key: str):
    if not api_key:
        return FALLBACK_SPORTS
    if "sports_map_v33" in st.session_state:
        return st.session_state["sports_map_v33"]
    try:
        sports = fetch_sports(api_key)
        active = {
            s["key"]: s.get("title", s["key"])
            for s in sports
            if s.get("active", False) and not s.get("has_outrights", False)
        }
        if active:
            st.session_state["sports_map_v33"] = active
            return active
    except Exception as e:
        st.sidebar.warning(f"Could not load active sports yet: {clean_api_error(e)}")
    return FALLBACK_SPORTS


# =========================================================
# BEST PRICE BOARD ENGINE
# =========================================================
def extract_price_rows(events: List[Dict[str, Any]], selected_markets: List[str]) -> List[Dict[str, Any]]:
    rows = []
    for ev in events:
        home = ev.get("home_team", "") or ""
        away = ev.get("away_team", "") or ""
        sport_key = ev.get("sport_key", "")
        sport_title = ev.get("sport_title", sport_key)
        start_utc_dt = parse_api_datetime(ev.get("commence_time"))
        status, _ = game_time_status(start_utc_dt, DEFAULT_RULES["min_minutes_before_start"])

        for bm in ev.get("bookmakers", []):
            bm_key = str(bm.get("key", ""))
            bm_title = str(bm.get("title", bm_key or "unknown"))
            for mk in bm.get("markets", []):
                mkey = str(mk.get("key", ""))
                if mkey not in selected_markets:
                    continue
                for out in mk.get("outcomes", []):
                    price = safe_float(out.get("price"), 0)
                    if price <= 1:
                        continue
                    point = safe_point(out.get("point"))
                    outcome = str(out.get("name", ""))
                    rows.append({
                        "sport_key": sport_key,
                        "sport_title": sport_title,
                        "event_id": str(ev.get("id", "")),
                        "commence_time": str(ev.get("commence_time", "")),
                        "start_utc_dt": start_utc_dt,
                        "start_nz": fmt_dt(start_utc_dt, NZ_TZ, "NZ"),
                        "start_et": fmt_dt(start_utc_dt, ET_TZ, "ET"),
                        "nz_date": fmt_date(start_utc_dt, NZ_TZ),
                        "us_et_date": fmt_date(start_utc_dt, ET_TZ),
                        "starts_in": starts_in_text(start_utc_dt),
                        "time_status": status,
                        "home": home,
                        "away": away,
                        "market": mkey,
                        "market_label": market_label(mkey),
                        "outcome": outcome,
                        "point": point,
                        "pick": make_pick_label(mkey, outcome, point),
                        "book_key": bm_key,
                        "book": bm_title,
                        "price": round(price, 3),
                    })
    return rows


def no_vig_probs(event_rows: List[Dict[str, Any]]) -> Dict[Tuple[str, str, Optional[float]], float]:
    groups = defaultdict(list)
    for r in event_rows:
        line = r["point"] if r["market"] in ("spreads", "totals") else None
        groups[(r["book_key"], r["market"], line)].append(r)

    probs = defaultdict(list)
    for _, g in groups.items():
        if len(g) < 2:
            continue
        invs = [1 / r["price"] for r in g if r["price"] > 1]
        s = sum(invs)
        if s <= 0:
            continue
        for r, inv in zip(g, invs):
            key = (r["market"], r["outcome"], r["point"])
            probs[key].append(inv / s)
    return {k: sum(v) / len(v) for k, v in probs.items() if v}


def build_candidates(events: List[Dict[str, Any]], selected_markets: List[str], min_minutes_before_start: int) -> List[Dict[str, Any]]:
    rows = extract_price_rows(events, selected_markets)
    by_event = defaultdict(list)
    for r in rows:
        by_event[r["event_id"]].append(r)

    candidates = []
    for _, ev_rows in by_event.items():
        if not ev_rows:
            continue

        probs = no_vig_probs(ev_rows)
        home = ev_rows[0]["home"]
        away = ev_rows[0]["away"]
        start_dt = ev_rows[0]["start_utc_dt"]
        status, time_locked = game_time_status(start_dt, min_minutes_before_start)

        by_pick = defaultdict(list)
        for r in ev_rows:
            by_pick[(r["market"], r["outcome"], r["point"])].append(r)

        best_h2h = {}
        for (m, outcome, _), group in by_pick.items():
            if m == "h2h":
                best_h2h[outcome] = max([x["price"] for x in group], default=0)
        home_fav_h2h = bool(best_h2h.get(home, 0) > 1 and best_h2h.get(away, 0) > 1 and best_h2h[home] < best_h2h[away])

        for key, group in by_pick.items():
            market, outcome, point = key
            best = max(group, key=lambda x: x["price"])
            avg_odds = sum(x["price"] for x in group) / len(group)
            consensus_prob = probs.get(key, 1 / avg_odds if avg_odds > 1 else 0)
            implied = 1 / best["price"] if best["price"] > 1 else 0
            edge = consensus_prob - implied

            pin = [x for x in group if has_hint(x["book_key"], PINNACLE_HINTS) or has_hint(x["book"], PINNACLE_HINTS)]
            nzb = [x for x in group if has_hint(x["book_key"], NZ_BOOK_HINTS) or has_hint(x["book"], NZ_BOOK_HINTS)]
            b365 = [x for x in group if has_hint(x["book_key"], BET365_HINTS) or has_hint(x["book"], BET365_HINTS)]
            pin_best = max(pin, key=lambda x: x["price"]) if pin else None
            nz_best = max(nzb, key=lambda x: x["price"]) if nzb else None
            b365_best = max(b365, key=lambda x: x["price"]) if b365 else None

            is_team = market in ("h2h", "spreads") and outcome in (home, away)
            is_home = bool(is_team and outcome == home)
            if market == "h2h":
                is_home_fav = bool(is_home and home_fav_h2h)
            elif market == "spreads":
                is_home_fav = bool(is_home and point is not None and point < 0)
            else:
                is_home_fav = False

            all_prices = sorted(group, key=lambda x: x["price"], reverse=True)
            candidates.append({
                "sport": best["sport_title"],
                "sport_key": best["sport_key"],
                "event_id": best["event_id"],
                "start": best["commence_time"],
                "start_nz": best["start_nz"],
                "start_et": best["start_et"],
                "nz_date": best["nz_date"],
                "us_et_date": best["us_et_date"],
                "starts_in": starts_in_text(start_dt),
                "time_status": status,
                "time_locked": time_locked,
                "game": f'{best["away"]} @ {best["home"]}',
                "home": best["home"],
                "away": best["away"],
                "market": market,
                "market_label": market_label(market),
                "pick": best["pick"],
                "outcome": outcome,
                "point": point,
                "best_odds": round(best["price"], 3),
                "best_bookmaker": best["book"],
                "avg_odds": round(avg_odds, 3),
                "pinnacle": round(pin_best["price"], 3) if pin_best else None,
                "tab_betcha": round(nz_best["price"], 3) if nz_best else None,
                "bet365": round(b365_best["price"], 3) if b365_best else None,
                "consensus_prob": round(consensus_prob, 5),
                "implied_prob": round(implied, 5),
                "edge": round(edge, 5),
                "books": len(group),
                "home_pick": is_home,
                "home_fav": is_home_fav,
                "pinnacle_ok": bool(pin_best and best["price"] >= pin_best["price"]),
                "all_prices": " | ".join([f'{x["book"]}: {x["price"]:.2f}' for x in all_prices[:8]]),
            })
    return candidates



def score_edge_component(edge: float, min_edge: float, elite_edge: float) -> int:
    """0–30 points. Negative edge is weak. Elite edge is strong."""
    if edge >= elite_edge:
        return 30
    if edge >= min_edge:
        span = max(elite_edge - min_edge, 0.0001)
        return int(22 + 8 * ((edge - min_edge) / span))
    if edge > 0:
        return int(6 + 14 * min(edge / max(min_edge, 0.0001), 1))
    return max(0, int(5 + edge * 100))


def score_odds_component(best_odds: float, min_odds: float, max_odds: float) -> int:
    """0–10 points. Your safe odds range is protected."""
    return 10 if min_odds <= best_odds <= max_odds else 0


def score_book_component(books: int, pinnacle_ok: bool, best_odds: float, avg_odds: float) -> int:
    """0–20 points. More books + best price above average = stronger signal."""
    pts = min(10, int(books) * 2)
    if best_odds >= avg_odds:
        pts += 5
    if pinnacle_ok:
        pts += 5
    return min(20, pts)


def score_time_component(c: Dict[str, Any]) -> int:
    """0–10 points. Started/too-close games are not safe."""
    return 0 if c.get("time_locked") else 10


def score_rule_component(c: Dict[str, Any], rules: Dict[str, Any]) -> int:
    """0–15 points. Home/team rules matter only for team markets."""
    pts = 15
    if rules["apply_home_rules_to_team_markets"] and c["market"] in ("h2h", "spreads"):
        if rules["require_home_pick"] and not c["home_pick"]:
            pts -= 8
        if rules["require_home_favourite"] and not c["home_fav"]:
            pts -= 7
    return max(0, pts)


def score_discipline_component(flags: Dict[str, bool], approved_count: int, loss_streak: int, rules: Dict[str, Any]) -> int:
    """0–15 points. This is Abhi-protection logic."""
    pts = 15
    if flags.get("late_chase_feeling"):
        pts -= 15
    manual_reds = ["injury_red", "public_red", "fatigue_red", "line_against", "key_player_red"]
    pts -= 3 * sum(1 for f in manual_reds if flags.get(f))
    if loss_streak >= int(rules["lock_losses"]):
        pts -= 10
    if approved_count >= int(rules["max_daily"]):
        pts -= 10
    return max(0, pts)


def goat_score_breakdown(c: Dict[str, Any], rules: Dict[str, Any], flags: Dict[str, bool], approved_count: int, loss_streak: int) -> Dict[str, int]:
    min_odds = float(rules["min_odds"])
    max_odds = float(rules["max_odds"])
    min_edge = float(rules["min_edge_pct"]) / 100
    elite_edge = float(rules["elite_edge_pct"]) / 100

    parts = {
        "Edge quality": score_edge_component(float(c.get("edge", 0)), min_edge, elite_edge),
        "Odds range": score_odds_component(float(c.get("best_odds", 0)), min_odds, max_odds),
        "Bookmaker support": score_book_component(int(c.get("books", 0)), bool(c.get("pinnacle_ok")), float(c.get("best_odds", 0)), float(c.get("avg_odds", 0))),
        "Time safety": score_time_component(c),
        "GOAT rules": score_rule_component(c, rules),
        "Discipline": score_discipline_component(flags, approved_count, loss_streak, rules),
    }
    return parts


def goat_score_total(parts: Dict[str, int]) -> int:
    return int(max(0, min(100, sum(parts.values()))))


def score_parts_text(parts: Dict[str, int]) -> str:
    return " | ".join([f"{k}: {v}" for k, v in parts.items()])


def plain_explanation(decision: str, c: Dict[str, Any], reason: str, rules: Dict[str, Any]) -> str:
    edge_pct = float(c.get("edge", 0)) * 100
    best_odds = float(c.get("best_odds", 0))
    avg_odds = float(c.get("avg_odds", 0))
    min_edge = float(rules["min_edge_pct"])
    min_odds = float(rules["min_odds"])
    max_odds = float(rules["max_odds"])

    if "APPROVED" in decision or "ELITE" in decision:
        return "Clean enough for PAPER tracking only. Best price, time window, and GOAT rules passed. Do not use real money until the 300-pick proof is positive."
    if "WATCHLIST" in decision:
        return "This is not approved yet. It needs extra confirmation, usually sharp-book/Pinnacle support. Watch only; do not log as approved."
    if "TIME WINDOW" in decision:
        return "Game is already started or too close. This is exactly where rushed/chase bets happen. Leave it."
    if "LOSS STREAK" in decision:
        return "System is protecting you from chasing after losses. Stop scanning and review later."
    if "DAILY LIMIT" in decision:
        return "Daily limit reached. The system is stopping over-action."
    if "EMOTIONAL" in decision:
        return "Late/chase feeling is marked. That is an automatic no."
    if "RED FLAG" in decision:
        return f"A manual red flag is active: {reason}. No pick."
    if "ODDS RANGE" in decision:
        return f"Best odds are {best_odds:.2f}, outside your safe range {min_odds:.2f}–{max_odds:.2f}. No pick."
    if "EDGE TOO LOW" in decision:
        if edge_pct < 0:
            return f"No real value. Best odds {best_odds:.2f} are not better than the market estimate. Edge is negative ({edge_pct:.2f}%). Do not chase."
        return f"Small positive signal, but not enough. Edge is {edge_pct:.2f}% and your minimum is {min_edge:.2f}%. No paper pick."
    if "NOT HOME PICK" in decision:
        return "Your team-market rule requires the home team. This pick is not the home team."
    if "NOT HOME FAVOURITE" in decision:
        return "Your rule requires the home favourite. This pick does not pass that safety gate."
    return reason or "No clean GOAT approval."


def action_text(decision: str) -> str:
    if "ELITE" in decision:
        return "Action: log as ELITE paper pick only."
    if "APPROVED" in decision:
        return "Action: log as PAPER pick only."
    if "WATCHLIST" in decision:
        return "Action: watch only. Do not log as approved."
    return "Action: do not log. Do not bet."


def decide(c: Dict[str, Any], rules: Dict[str, Any], flags: Dict[str, bool], approved_count: int, loss_streak: int):
    min_odds = float(rules["min_odds"])
    max_odds = float(rules["max_odds"])
    min_edge = float(rules["min_edge_pct"]) / 100
    elite_edge = float(rules["elite_edge_pct"]) / 100

    parts = goat_score_breakdown(c, rules, flags, approved_count, loss_streak)
    score = goat_score_total(parts)

    if c.get("time_locked"):
        decision, bucket, reason = "LOCKED — TIME WINDOW", "Time window", c.get("time_status", "Game time locked")
        return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)

    if flags.get("late_chase_feeling"):
        decision, bucket, reason = "LOCKED — EMOTIONAL RISK", "Locked/chase", "Late/chase feeling marked"
        return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)

    if loss_streak >= int(rules["lock_losses"]):
        decision, bucket, reason = "LOCKED — LOSS STREAK", "Locked/loss streak", f"Loss streak {loss_streak}"
        return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)

    if approved_count >= int(rules["max_daily"]):
        decision, bucket, reason = "LOCKED — DAILY LIMIT", "Locked/daily limit", "Daily limit reached"
        return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)

    red_list = []
    if flags.get("injury_red"): red_list.append("Injury/news red flag")
    if flags.get("public_red"): red_list.append("Public-heavy red flag")
    if flags.get("fatigue_red"): red_list.append("Schedule/fatigue red flag")
    if flags.get("line_against"): red_list.append("Line moved against pick")
    if flags.get("key_player_red"): red_list.append("Key player uncertainty")
    if red_list and rules["reject_red_flags"]:
        decision, bucket, reason = "REJECTED — RED FLAG", "Red flag", "; ".join(red_list)
        return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)

    if not (min_odds <= c["best_odds"] <= max_odds):
        decision, bucket, reason = "REJECTED — ODDS RANGE", "Odds range", f'Best odds {c["best_odds"]:.2f} outside {min_odds:.2f}-{max_odds:.2f}'
        return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)

    if c["edge"] < min_edge:
        decision, bucket, reason = "REJECTED — EDGE TOO LOW", "Edge too low", f'Edge {c["edge"]*100:.2f}% below {min_edge*100:.2f}%'
        return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)

    if rules["apply_home_rules_to_team_markets"] and c["market"] in ("h2h", "spreads"):
        if rules["require_home_pick"] and not c["home_pick"]:
            decision, bucket, reason = "REJECTED — NOT HOME PICK", "Not home pick", "Pick is not home team"
            return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)
        if rules["require_home_favourite"] and not c["home_fav"]:
            decision, bucket, reason = "REJECTED — NOT HOME FAVOURITE", "Not home favourite", "Home favourite rule failed"
            return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)

    if rules["require_pinnacle_value"] and not c["pinnacle_ok"]:
        decision, bucket, reason = "WATCHLIST — PINNACLE NOT CONFIRMED", "Pinnacle missing", "Pinnacle value not confirmed"
        return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)

    if c["edge"] >= elite_edge and score >= 85:
        decision, bucket, reason = "ELITE PAPER PICK", "Approved", "GOAT Score 85+ with elite edge and safety gates passed"
        return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)

    if score >= 75:
        decision, bucket, reason = "APPROVED PAPER PICK", "Approved", "GOAT Score 75+ and safety gates passed"
        return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)

    decision, bucket, reason = "WATCHLIST — SCORE TOO LOW", "Score too low", f"GOAT Score {score}/100 below 75"
    return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)




# =========================================================
# MOBILE CARD VIEW HELPERS — v3.4
# =========================================================
def short_prices(all_prices: str, max_books: int = 4) -> str:
    parts = [p.strip() for p in str(all_prices or "").split("|") if p.strip()]
    if not parts:
        return "No bookmaker prices shown"
    shown = parts[:max_books]
    extra = len(parts) - len(shown)
    text = " • ".join(shown)
    if extra > 0:
        text += f" • +{extra} more"
    return text


def edge_badge(edge: float) -> str:
    try:
        e = float(edge) * 100
    except Exception:
        e = 0.0
    if e >= 5:
        return f"🔥 Elite edge {e:.2f}%"
    if e >= 2:
        return f"✅ Edge {e:.2f}%"
    if e >= 0:
        return f"🟡 Near but low edge {e:.2f}%"
    return f"🔴 Negative edge {e:.2f}%"



def render_candidate_card(row: dict, idx: int):
    decision = str(row.get("decision", ""))
    pick = str(row.get("pick", ""))
    game = str(row.get("game", ""))
    market = str(row.get("market_label", row.get("market", "")))
    best_odds = row.get("best_odds", "")
    best_book = str(row.get("best_bookmaker", ""))
    avg_odds = row.get("avg_odds", "")
    edge = safe_float(row.get("edge", 0), 0)
    reasons = str(row.get("reasons", ""))
    plain = str(row.get("plain_explanation", reasons))
    action = str(row.get("action", action_text(decision)))
    score = int(safe_float(row.get("score", 0), 0))
    score_parts = str(row.get("score_parts", ""))
    status = str(row.get("time_status", ""))
    starts_in = str(row.get("starts_in", ""))
    start_nz = str(row.get("start_nz", ""))
    start_et = str(row.get("start_et", ""))
    us_date = str(row.get("us_et_date", ""))
    nz_date = str(row.get("nz_date", ""))
    prices = short_prices(str(row.get("all_prices", "")), max_books=5)

    if "ELITE" in decision:
        top_line = "🔥 ELITE PAPER CANDIDATE"
    elif "APPROVED" in decision:
        top_line = "✅ APPROVED PAPER CANDIDATE"
    elif "WATCHLIST" in decision:
        top_line = "👀 WATCHLIST"
    elif "LOCKED" in decision:
        top_line = "🔒 LOCKED"
    else:
        top_line = "❌ REJECTED"

    with st.container(border=True):
        st.markdown(f"### {idx}. {top_line}")
        st.markdown(f"**{market}: {pick}**")
        st.write(game)

        c1, c2 = st.columns(2)
        c1.metric("GOAT Score", f"{score}/100")
        c2.metric("Edge", f"{edge*100:.2f}%")

        st.progress(min(max(score, 0), 100) / 100)

        c3, c4 = st.columns(2)
        c3.metric("Best odds", f"{safe_float(best_odds, 0):.2f}" if best_odds != "" else "-")
        c4.metric("Avg odds", f"{safe_float(avg_odds, 0):.2f}" if avg_odds != "" else "-")

        st.markdown(f"**Best bookmaker:** {best_book}")
        st.markdown(f"**Time:** {status} • starts in **{starts_in}**")
        st.markdown(f"**NZ:** {start_nz}")
        st.markdown(f"**US ET:** {start_et}")
        st.caption(f"NZ betting date: {nz_date} | US game date: {us_date}")

        if "APPROVED" in decision or "ELITE" in decision:
            st.success(plain)
        elif "WATCHLIST" in decision:
            st.warning(plain)
        else:
            st.error(plain)

        st.info(action)
        st.caption(f"Raw reason: {reasons}")
        st.caption(f"Prices: {prices}")

        with st.expander("GOAT Score breakdown"):
            if score_parts:
                for item in score_parts.split(" | "):
                    st.write(f"- {item}")
            else:
                st.write("No score breakdown available.")



def mobile_card_dataframe(board: pd.DataFrame, mode: str, max_cards: int) -> pd.DataFrame:
    if board.empty:
        return board

    df = board.copy()
    if mode == "Approved / Elite only":
        df = df[df["decision"].astype(str).str.contains("APPROVED|ELITE", regex=True, na=False)]
        return df.sort_values(["edge", "score"], ascending=[False, False]).head(max_cards)
    if mode == "Closest missed only":
        df = df[~df["decision"].astype(str).str.contains("APPROVED|ELITE", regex=True, na=False)]
        return df.sort_values(["edge", "score"], ascending=[False, False]).head(max_cards)
    if mode == "Upcoming only":
        df = df[df["time_status"].astype(str).eq("Upcoming")]
        return df.sort_values(["sort", "edge", "best_odds"], ascending=[True, False, False]).head(max_cards)
    return df.sort_values(["sort", "time_locked", "edge", "best_odds"], ascending=[True, True, False, False]).head(max_cards)

# =========================================================
# PAPER LOG
# =========================================================
def load_log():
    if "paper_log_v33" not in st.session_state:
        if PICKS_PATH.exists():
            try:
                st.session_state.paper_log_v33 = pd.read_csv(PICKS_PATH)
            except Exception:
                st.session_state.paper_log_v33 = pd.DataFrame()
        else:
            st.session_state.paper_log_v33 = pd.DataFrame()
    return st.session_state.paper_log_v33


def save_log(df):
    st.session_state.paper_log_v33 = df
    PICKS_PATH.parent.mkdir(exist_ok=True)
    try:
        df.to_csv(PICKS_PATH, index=False)
    except Exception:
        pass


def approved_today_count(df):
    if df.empty or "created_at" not in df.columns or "decision" not in df.columns:
        return 0
    today = datetime.now(NZ_TZ).date().isoformat()
    if "nz_date" in df.columns:
        return int((df["nz_date"].astype(str).eq(today) & df["decision"].astype(str).str.contains("APPROVED|ELITE", regex=True)).sum())
    return int(df["decision"].astype(str).str.contains("APPROVED|ELITE", regex=True).sum())


def loss_streak_count(df):
    if df.empty or "result" not in df.columns:
        return 0
    streak = 0
    for _, row in df.sort_values("created_at", ascending=False).iterrows():
        r = str(row.get("result", "Pending"))
        if r == "Lost":
            streak += 1
        elif r in ("Won", "Push"):
            break
    return streak


# =========================================================
# UI
# =========================================================
def main():
    st.title("🐐 GOAT Shield Live v3.5")
    st.caption("GOAT Score + better card explanations + NZ Bettor Mode + Best Price Board + US sports time conversion. Paper-only. No sportsbook login. No real-money auto-betting.")

    api_key_default = secret("ODDS_API_KEY", "")

    with st.sidebar:
        st.markdown("### 🔌 Connection status")
        st.write("Odds API:", "✅ key found" if api_key_default else "Paste key below / Secrets")
        st.caption("No sportsbook login. No auto-betting.")

        api_key = st.text_input("The Odds API key", value=api_key_default, type="password")

        if st.button("Reload active sports"):
            st.session_state.pop("sports_map_v33", None)
            st.rerun()

        sports_map = get_sports_map(api_key)
        sport_keys = list(sports_map.keys())
        default_sport = "baseball_mlb" if "baseball_mlb" in sport_keys else sport_keys[0]

        selected_sports = st.multiselect(
            "Active sports to scan",
            sport_keys,
            default=[default_sport],
            format_func=lambda k: sports_map.get(k, k),
        )

        markets = st.multiselect(
            "Markets",
            ["h2h", "spreads", "totals"],
            default=["h2h"],
            format_func=lambda x: {"h2h": "Moneyline / h2h", "spreads": "Spreads", "totals": "Totals"}[x],
        )

        regions = st.multiselect("Regions", ["us", "uk", "au", "eu"], default=["us"])
        bookmakers_filter = st.text_input("Optional bookmaker filter", value="", help="Example: pinnacle,draftkings. Leave blank to use regions.")

        time_filter = st.selectbox(
            "Time filter",
            [
                "NZ Bettor Mode: Next 24 hours",
                "NZ Bettor Mode: Next 36 hours",
                "Today NZ",
                "Today US Eastern",
                "Tomorrow US Eastern",
                "No time filter",
            ],
            index=0,
        )

        start, end = time_window(time_filter)
        if start and end:
            st.caption(f"API window UTC: {start} → {end}")

        est = max(1, len(selected_sports)) * max(1, len(markets)) * (1 if bookmakers_filter.strip() else max(1, len(regions)))
        st.caption(f"Estimated credits/fetch: about {est}. Start small.")

        st.markdown("### GOAT rules")
        rules = dict(DEFAULT_RULES)
        rules["min_odds"] = st.number_input("Min decimal odds", 1.01, 10.0, float(rules["min_odds"]), 0.01)
        rules["max_odds"] = st.number_input("Max decimal odds", 1.01, 10.0, float(rules["max_odds"]), 0.01)
        rules["min_edge_pct"] = st.number_input("Min edge %", 0.0, 50.0, float(rules["min_edge_pct"]), 0.1)
        rules["elite_edge_pct"] = st.number_input("Elite edge %", 0.0, 50.0, float(rules["elite_edge_pct"]), 0.1)
        rules["max_daily"] = st.number_input("Max approved paper picks per NZ day", 1, 20, int(rules["max_daily"]), 1)
        rules["lock_losses"] = st.number_input("Loss-streak lockout", 1, 20, int(rules["lock_losses"]), 1)
        rules["min_minutes_before_start"] = st.number_input("Lock if game starts within minutes", 0, 240, int(rules["min_minutes_before_start"]), 5)
        rules["apply_home_rules_to_team_markets"] = st.checkbox("Apply home rules to team markets only", True)
        rules["require_home_pick"] = st.checkbox("Require team-market pick to be home team", True)
        rules["require_home_favourite"] = st.checkbox("Require home favourite for team markets", True)
        rules["require_pinnacle_value"] = st.checkbox("Require Pinnacle value if present", False)
        rules["reject_red_flags"] = st.checkbox("Reject any manual red flag", True)

        st.markdown("### Manual red flags")
        flags = {
            "injury_red": st.checkbox("Injury/news red flag", False),
            "public_red": st.checkbox("Public-heavy red flag", False),
            "fatigue_red": st.checkbox("Schedule/fatigue red flag", False),
            "line_against": st.checkbox("Line moved against pick", False),
            "key_player_red": st.checkbox("Key player uncertainty", False),
            "late_chase_feeling": st.checkbox("Late/chase feeling", False),
        }

    log_df = load_log()

    tabs = st.tabs(["🇳🇿 NZ Bettor Board", "📱 Mobile Cards", "🟢 Best Price Board", "📒 Paper Log", "✅ Results", "📊 Dashboard", "🛡️ Backup"])

    with tabs[0]:
        st.subheader("🇳🇿 NZ Bettor Board")
        st.info("This board is built for betting from NZ on US sports. It shows NZ time, US Eastern time, US game date, NZ betting date, and locks games that are already started or too close.")

        if st.button("Fetch NZ bettor board"):
            if not api_key:
                st.error("Add your Odds API key first.")
                st.stop()
            if not selected_sports:
                st.error("Choose at least one sport.")
                st.stop()
            if not markets:
                st.error("Choose at least one market.")
                st.stop()
            if not regions and not bookmakers_filter.strip():
                st.error("Choose region or bookmaker filter.")
                st.stop()

            start, end = time_window(time_filter)
            events = []
            metas = []
            errors = []

            with st.spinner("Fetching bookmaker prices with NZ/US time conversion..."):
                for sport in selected_sports:
                    try:
                        ev, meta = fetch_odds(api_key, sport, ",".join(regions), ",".join(markets), bookmakers_filter, start, end)
                        events.extend(ev)
                        metas.append(meta)
                    except Exception as e:
                        errors.append(f"{sports_map.get(sport, sport)}: {clean_api_error(e)}")

            st.session_state["events_v33"] = events
            st.session_state["markets_v33"] = markets
            st.session_state["metas_v33"] = metas
            st.session_state["rules_v33"] = rules

            for e in errors:
                st.error(e)
            if metas:
                st.success(f"Fetched {len(events)} events. Requests used: {metas[-1].get('requests_used')}. Remaining: {metas[-1].get('requests_remaining')}.")

        events = st.session_state.get("events_v33", [])
        last_markets = st.session_state.get("markets_v33", markets)

        if events:
            candidates = build_candidates(events, last_markets, int(rules["min_minutes_before_start"]))
            rows = []
            app_count = approved_today_count(log_df)
            streak = loss_streak_count(log_df)

            for c in candidates:
                decision, score, bucket, reason, plain, action, score_parts = decide(c, rules, flags, app_count, streak)
                r = dict(c)
                r.update({"decision": decision, "score": score, "reject_bucket": bucket, "reasons": reason, "plain_explanation": plain, "action": action, "score_parts": score_parts})
                rows.append(r)

            if not rows:
                st.warning("No price candidates found. Try another sport, market, region, or time filter.")
            else:
                summary = Counter([r["reject_bucket"] for r in rows if r["reject_bucket"] != "Approved"])
                approved_n = sum(("APPROVED" in r["decision"] or "ELITE" in r["decision"]) for r in rows)
                watch_n = sum("WATCHLIST" in r["decision"] for r in rows)
                reject_n = sum(("REJECTED" in r["decision"] or "LOCKED" in r["decision"]) for r in rows)

                c1, c2, c3, c4 = st.columns(4)
                c1.metric("Candidates", len(rows))
                c2.metric("Approved/Elite", approved_n)
                c3.metric("Watchlist", watch_n)
                c4.metric("Rejected/Locked", reject_n)

                if approved_n == 0:
                    text = ", ".join([f"{k}: {v}" for k, v in summary.most_common(6)]) or "No strong edge found"
                    st.warning(f"No approved paper picks. Main blockers: {text}")
                else:
                    st.success(f"{approved_n} approved/elite paper candidate(s). Paper-log only.")

                board = pd.DataFrame(rows)
                board["sort"] = board["decision"].map({"ELITE PAPER PICK": 0, "APPROVED PAPER PICK": 1, "WATCHLIST — PINNACLE NOT CONFIRMED": 2}).fillna(9)
                board = board.sort_values(["sort", "time_locked", "edge", "best_odds"], ascending=[True, True, False, False]).reset_index(drop=True)

                cols = [
                    "decision", "score", "plain_explanation", "action", "time_status", "starts_in",
                    "start_nz", "start_et", "nz_date", "us_et_date",
                    "sport", "game", "market_label", "pick",
                    "best_odds", "best_bookmaker", "avg_odds",
                    "pinnacle", "tab_betcha", "bet365",
                    "edge", "books", "reasons", "score_parts", "all_prices",
                ]
                st.dataframe(
                    board[cols].style.format({"edge": "{:.2%}", "best_odds": "{:.2f}", "avg_odds": "{:.2f}"}),
                    use_container_width=True,
                    hide_index=True,
                )

                st.subheader("📱 Mobile-friendly view")
                st.caption("For clean iPhone reading, open the 📱 Mobile Cards tab after this scan.")

                st.subheader("👀 Closest missed picks")
                missed = board[~board["decision"].astype(str).str.contains("APPROVED|ELITE", regex=True, na=False)].sort_values(["edge", "score"], ascending=[False, False]).head(5)
                if missed.empty:
                    st.info("No closest misses.")
                else:
                    st.dataframe(
                        missed[["decision", "score", "plain_explanation", "action", "time_status", "starts_in", "start_nz", "start_et", "market_label", "pick", "best_odds", "best_bookmaker", "edge", "reasons", "all_prices"]].style.format({"edge": "{:.2%}", "best_odds": "{:.2f}"}),
                        use_container_width=True,
                        hide_index=True,
                    )

                approved_rows = board[board["decision"].astype(str).str.contains("APPROVED|ELITE", regex=True, na=False)]
                if not approved_rows.empty:
                    labels = approved_rows.apply(lambda x: f"{x.name}: {x['decision']} — {x['pick']} @ {x['best_odds']} ({x['best_bookmaker']}) — {x['start_nz']}", axis=1).tolist()
                    choice = st.selectbox("Approved/elite candidate to paper-log", labels)
                    idx = int(choice.split(":")[0])
                    if st.button("Log selected as PAPER pick"):
                        chosen = board.loc[idx].to_dict()
                        log_row = {
                            "created_at": iso_z(datetime.now(timezone.utc)),
                            "nz_date": chosen["nz_date"],
                            "us_et_date": chosen["us_et_date"],
                            "start_nz": chosen["start_nz"],
                            "start_et": chosen["start_et"],
                            "starts_in": chosen["starts_in"],
                            "sport": chosen["sport"],
                            "game": chosen["game"],
                            "market": chosen["market_label"],
                            "pick_label": chosen["pick"],
                            "best_odds": chosen["best_odds"],
                            "best_bookmaker": chosen["best_bookmaker"],
                            "avg_odds": chosen["avg_odds"],
                            "pinnacle": chosen["pinnacle"],
                            "tab_betcha": chosen["tab_betcha"],
                            "bet365": chosen["bet365"],
                            "edge": chosen["edge"],
                            "decision": chosen["decision"],
                            "score": chosen["score"],
                            "plain_explanation": chosen.get("plain_explanation", ""),
                            "score_parts": chosen.get("score_parts", ""),
                            "result": "Pending",
                            "profit_units": "",
                            "closing_odds": "",
                            "clv": "",
                            "all_prices": chosen["all_prices"],
                        }
                        new_df = pd.concat([pd.DataFrame([log_row]), log_df], ignore_index=True)
                        save_log(new_df)
                        st.success("Logged as paper pick only.")
                        st.rerun()

    with tabs[1]:
        st.subheader("📱 Mobile Cards")
        st.write("Clean iPhone view with GOAT Score, plain-English explanations, and clear action labels.")
        events_cards = st.session_state.get("events_v33", [])
        last_markets_cards = st.session_state.get("markets_v33", markets)
        if not events_cards:
            st.info("Run Fetch NZ bettor board first, then come here.")
        else:
            candidates_cards = build_candidates(events_cards, last_markets_cards, int(rules["min_minutes_before_start"]))
            rows_cards = []
            app_count_cards = approved_today_count(log_df)
            streak_cards = loss_streak_count(log_df)
            for cand in candidates_cards:
                decision, score, bucket, reason, plain, action, score_parts = decide(cand, rules, flags, app_count_cards, streak_cards)
                rr = dict(cand)
                rr.update({"decision": decision, "score": score, "reject_bucket": bucket, "reasons": reason, "plain_explanation": plain, "action": action, "score_parts": score_parts})
                rows_cards.append(rr)
            if not rows_cards:
                st.warning("No card candidates found.")
            else:
                cards_df = pd.DataFrame(rows_cards)
                cards_df["sort"] = cards_df["decision"].map({"ELITE PAPER PICK": 0, "APPROVED PAPER PICK": 1, "WATCHLIST — PINNACLE NOT CONFIRMED": 2}).fillna(9)
                c_mode = st.selectbox("Card view", ["Closest missed only", "Approved / Elite only", "Upcoming only", "All ranked"], index=0)
                st.caption("GOAT Score guide: 0–59 reject, 60–74 watchlist, 75–84 approved paper, 85+ elite paper.")
                c_max = st.slider("Number of cards", 3, 15, 5)
                picked_cards = mobile_card_dataframe(cards_df, c_mode, c_max)
                if picked_cards.empty:
                    st.info("No cards match this filter.")
                else:
                    for n, (_, card_row) in enumerate(picked_cards.iterrows(), start=1):
                        render_candidate_card(card_row.to_dict(), n)

    with tabs[2]:
        st.subheader("🟢 Best Price Board")
        st.write("Use the NZ Bettor Board first. It includes all best-price board columns plus NZ/US time conversion.")
        st.caption("v3.3 keeps this tab as a simple explanation so the phone UI stays cleaner.")

    with tabs[3]:
        st.subheader("📒 Paper Log")
        df = load_log()
        if df.empty:
            st.info("No paper picks logged yet.")
        else:
            st.dataframe(df, use_container_width=True)
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8"), "goat_shield_paper_log.csv", "text/csv")

    with tabs[4]:
        st.subheader("✅ Results")
        df = load_log()
        if df.empty or "result" not in df.columns:
            st.info("No pending picks.")
        else:
            pending = df[df["result"].astype(str).eq("Pending")]
            if pending.empty:
                st.info("No pending picks.")
            else:
                labels = pending.apply(lambda r: f"{r.name}: {r.get('pick_label','Pick')} @ {r.get('best_odds','')} — {r.get('start_nz','')}", axis=1).tolist()
                selected = st.selectbox("Select pick", labels)
                idx = int(selected.split(":")[0])
                result = st.selectbox("Result", ["Won", "Lost", "Push"])
                closing = st.number_input("Closing odds if known", min_value=0.0, value=0.0, step=0.01)
                if st.button("Save result"):
                    odds = safe_float(df.loc[idx].get("best_odds", 0), 0)
                    df.loc[idx, "result"] = result
                    df.loc[idx, "profit_units"] = odds - 1 if result == "Won" else (-1 if result == "Lost" else 0)
                    if closing > 0:
                        df.loc[idx, "closing_odds"] = closing
                        df.loc[idx, "clv"] = (odds - closing) / closing
                    save_log(df)
                    st.success("Saved.")
                    st.rerun()

    with tabs[5]:
        st.subheader("📊 Dashboard")
        df = load_log()
        if df.empty:
            st.info("No paper picks logged yet.")
        else:
            settled = df[df["result"].isin(["Won", "Lost", "Push"])] if "result" in df.columns else pd.DataFrame()
            pending = df[df["result"].astype(str).eq("Pending")] if "result" in df.columns else pd.DataFrame()
            profit = pd.to_numeric(settled.get("profit_units", pd.Series(dtype=float)), errors="coerce").fillna(0).sum() if not settled.empty else 0
            roi = profit / max(len(settled), 1)
            clv = pd.to_numeric(settled.get("clv", pd.Series(dtype=float)), errors="coerce").dropna() if not settled.empty else pd.Series(dtype=float)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Settled", len(settled))
            c2.metric("Pending", len(pending))
            c3.metric("Profit units", f"{profit:+.2f}u")
            c4.metric("ROI", f"{roi*100:.1f}%")

            st.progress(min(1, len(settled) / 300), text=f"Proof progress: {len(settled)}/300 settled paper picks")
            st.metric("Positive CLV", f"{((clv > 0).mean()*100 if len(clv) else 0):.1f}%")
            st.warning("System verdict: NOT PROVEN — keep paper testing." if len(settled) < 300 else "300-pick proof reached. Review ROI and CLV carefully.")

            if "market" in df.columns:
                st.subheader("Performance by market")
                st.dataframe(df.groupby("market", dropna=False).size().reset_index(name="paper_picks"), use_container_width=True)

    with tabs[6]:
        st.subheader("🛡️ Backup")
        df = load_log()
        if not df.empty:
            st.download_button("Download backup CSV", df.to_csv(index=False).encode("utf-8"), "goat_shield_backup.csv", "text/csv")
        up = st.file_uploader("Restore CSV", type=["csv"])
        if up is not None:
            try:
                new_df = pd.read_csv(up)
                if st.button("Restore"):
                    save_log(new_df)
                    st.success("Restored.")
                    st.rerun()
            except Exception as e:
                st.error(f"Restore failed: {e}")

    st.divider()
    st.caption("GOAT Shield Live v3.5 is paper-only. It does not place real-money bets, log into sportsbooks, scrape bookmakers, or bypass betting rules.")


if __name__ == "__main__":
    main()
