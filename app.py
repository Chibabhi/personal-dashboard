
from __future__ import annotations

import os
import math
import requests
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter, defaultdict
from urllib.parse import quote_plus

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
    "min_decimal_odds": 1.40,
    "max_decimal_odds": 2.20,
    "min_books_compared": 5,
    "max_daily": 3,
    "lock_losses": 3,
    "min_minutes_before_start": 90,
    "require_home_pick": True,
    "require_home_favourite": True,
    "require_pinnacle_value": False,
    "show_pinnacle_reference": True,
    "pinnacle_score_bonus": True,
    "reject_red_flags": True,
    "apply_home_rules_to_team_markets": True,
}

FALLBACK_SPORTS = {
    "baseball_mlb": "MLB",
    "basketball_nba": "NBA",
    "icehockey_nhl": "NHL",
    "americanfootball_nfl": "NFL",
    "soccer_usa_mls": "MLS",
    "basketball_wnba": "WNBA",
    "americanfootball_ncaaf": "NCAAF",
    "basketball_ncaab": "NCAAB",
    "basketball_ncaawb": "NCAAWB",
}

US_NATIONAL_SPORTS_PACK = {
    "baseball_mlb": "MLB",
    "basketball_nba": "NBA",
    "americanfootball_nfl": "NFL",
    "icehockey_nhl": "NHL",
    "soccer_usa_mls": "MLS",
    "basketball_wnba": "WNBA",
    "americanfootball_ncaaf": "NCAA Football",
    "basketball_ncaab": "NCAA Basketball",
    "basketball_ncaawb": "NCAA Women's Basketball",
}

PINNACLE_HINTS = ("pinnacle",)
NZ_BOOK_HINTS = ("tab", "tab nz", "betcha", "entain")
BET365_HINTS = ("bet365",)


# =========================================================
# STREAMLIT PAGE
# =========================================================
st.set_page_config(
    page_title="GOAT Shield Live v3.9.2 MULTI PROOF",
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
    if "sports_map_v36" in st.session_state:
        return st.session_state["sports_map_v36"]
    try:
        sports = fetch_sports(api_key)
        active = {
            s["key"]: s.get("title", s["key"])
            for s in sports
            if s.get("active", False) and not s.get("has_outrights", False)
        }
        if active:
            st.session_state["sports_map_v36"] = active
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


def active_us_national_sports(sports_map: Dict[str, str]) -> List[str]:
    """Return only US sports currently active/available from The Odds API."""
    return [k for k in US_NATIONAL_SPORTS_PACK.keys() if k in sports_map]


def default_us_sport_selection(sports_map: Dict[str, str], mode: str) -> List[str]:
    active_us = active_us_national_sports(sports_map)
    if not active_us:
        return ["baseball_mlb"] if "baseball_mlb" in sports_map else list(sports_map.keys())[:1]
    if mode == "All US National Sports":
        return active_us
    if mode == "Big 4 Pro Only":
        return [k for k in ["baseball_mlb", "basketball_nba", "americanfootball_nfl", "icehockey_nhl"] if k in sports_map]
    if mode == "US Pro + MLS + WNBA":
        return [k for k in ["baseball_mlb", "basketball_nba", "americanfootball_nfl", "icehockey_nhl", "soccer_usa_mls", "basketball_wnba"] if k in sports_map]
    if mode == "College Only":
        return [k for k in ["americanfootball_ncaaf", "basketball_ncaab", "basketball_ncaawb"] if k in sports_map]
    return ["baseball_mlb"] if "baseball_mlb" in sports_map else active_us[:1]



def build_pinnacle_reference(events: List[Dict[str, Any]], selected_markets: List[str]) -> Dict[Tuple, Dict[str, Any]]:
    """Build a Pinnacle-only reference map. This is a reference line only; no sportsbook login or scraping."""
    rows = extract_price_rows(events, selected_markets)
    ref: Dict[Tuple, Dict[str, Any]] = {}

    for r in rows:
        if not (has_hint(r.get("book_key", ""), PINNACLE_HINTS) or has_hint(r.get("book", ""), PINNACLE_HINTS)):
            continue

        id_key = (r["event_id"], r["market"], r["outcome"], r["point"])
        fallback_key = (
            "fallback",
            r["sport_key"],
            str(r["home"]).lower(),
            str(r["away"]).lower(),
            str(r["commence_time"])[:16],
            r["market"],
            r["outcome"],
            r["point"],
        )

        for k in (id_key, fallback_key):
            if k not in ref or r["price"] > ref[k]["price"]:
                ref[k] = r

    return ref


def get_pinnacle_ref_row(ref: Optional[Dict[Tuple, Dict[str, Any]]], best: Dict[str, Any], market: str, outcome: str, point: Any) -> Optional[Dict[str, Any]]:
    if not ref:
        return None

    id_key = (best["event_id"], market, outcome, point)
    if id_key in ref:
        return ref[id_key]

    fallback_key = (
        "fallback",
        best["sport_key"],
        str(best["home"]).lower(),
        str(best["away"]).lower(),
        str(best["commence_time"])[:16],
        market,
        outcome,
        point,
    )
    return ref.get(fallback_key)


def pinnacle_compare_status(best_odds: float, pinnacle_odds: Optional[float]) -> Tuple[Optional[float], str]:
    if pinnacle_odds is None or pinnacle_odds <= 1:
        return None, "Pinnacle not available"

    gap = (best_odds / pinnacle_odds - 1) * 100

    if gap >= 1.0:
        status = "Best price beats Pinnacle reference"
    elif gap >= -0.25:
        status = "Near Pinnacle reference"
    else:
        status = "Best price below Pinnacle reference"

    return round(gap, 2), status



def build_candidates(events: List[Dict[str, Any]], selected_markets: List[str], min_minutes_before_start: int, pinnacle_ref: Optional[Dict[Tuple, Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
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
            ref_pin = get_pinnacle_ref_row(pinnacle_ref, best, market, outcome, point)
            if ref_pin is not None and (pin_best is None or ref_pin["price"] > pin_best["price"]):
                pin_best = ref_pin
            nz_best = max(nzb, key=lambda x: x["price"]) if nzb else None
            b365_best = max(b365, key=lambda x: x["price"]) if b365 else None
            pinnacle_gap_pct, pinnacle_status = pinnacle_compare_status(best["price"], pin_best["price"] if pin_best else None)

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
                "price_lift_pct": round(((best["price"] / avg_odds - 1) * 100) if avg_odds else 0, 2),
                "pinnacle": round(pin_best["price"], 3) if pin_best else None,
                "pinnacle_gap_pct": pinnacle_gap_pct,
                "pinnacle_status": pinnacle_status,
                "pinnacle_available": bool(pin_best),
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



def score_price_quality(price_lift_pct: float) -> int:
    """0-25 points. This is NOT edge. It checks best price versus average bookmaker price."""
    if price_lift_pct >= 2.0:
        return 25
    if price_lift_pct >= 1.0:
        return 20
    if price_lift_pct > 0:
        return 12 + int(8 * min(price_lift_pct, 1.0))
    return 5


def score_odds_range(best_odds: float, min_odds: float, max_odds: float) -> int:
    return 15 if min_odds <= best_odds <= max_odds else 0


def score_book_support(books: int, min_books: int, pinnacle_ok: bool, pinnacle_available: bool = False, pinnacle_gap_pct: Optional[float] = None) -> int:
    pts = min(15, int(books) * 3)
    if books >= min_books:
        pts += 3
    if pinnacle_available:
        pts += 1
    if pinnacle_ok:
        pts += 2
    if pinnacle_gap_pct is not None and pinnacle_gap_pct >= 1.0:
        pts += 2
    return min(20, pts)


def score_time_safety(time_locked: bool) -> int:
    return 0 if time_locked else 15


def score_goat_rules(c: Dict[str, Any], rules: Dict[str, Any]) -> int:
    pts = 15
    if rules["apply_home_rules_to_team_markets"] and c["market"] in ("h2h", "spreads"):
        if rules["require_home_pick"] and not c["home_pick"]:
            pts -= 8
        if rules["require_home_favourite"] and not c["home_fav"]:
            pts -= 7
    return max(0, pts)


def score_discipline(flags: Dict[str, bool], approved_count: int, loss_streak: int, rules: Dict[str, Any]) -> int:
    pts = 10
    if flags.get("late_chase_feeling"):
        pts -= 10
    manual_reds = ["injury_red", "public_red", "fatigue_red", "line_against", "key_player_red"]
    pts -= 2 * sum(1 for f in manual_reds if flags.get(f))
    if loss_streak >= int(rules["lock_losses"]):
        pts -= 8
    if approved_count >= int(rules["max_daily"]):
        pts -= 8
    return max(0, pts)


def goat_score_breakdown(c: Dict[str, Any], rules: Dict[str, Any], flags: Dict[str, bool], approved_count: int, loss_streak: int) -> Dict[str, int]:
    return {
        "Best price quality": score_price_quality(float(c.get("price_lift_pct", 0))),
        "NZD decimal odds range": score_odds_range(float(c.get("best_odds", 0)), float(rules["min_decimal_odds"]), float(rules["max_decimal_odds"])),
        "Bookmaker support": score_book_support(
            int(c.get("books", 0)),
            int(rules["min_books_compared"]),
            bool(c.get("pinnacle_ok")),
            bool(c.get("pinnacle_available")),
            c.get("pinnacle_gap_pct"),
        ),
        "Time safety": score_time_safety(bool(c.get("time_locked"))),
        "GOAT rules": score_goat_rules(c, rules),
        "Discipline": score_discipline(flags, approved_count, loss_streak, rules),
    }


def goat_score_total(parts: Dict[str, int]) -> int:
    return int(max(0, min(100, sum(parts.values()))))


def score_parts_text(parts: Dict[str, int]) -> str:
    return " | ".join([f"{k}: {v}" for k, v in parts.items()])


def plain_explanation(decision: str, c: Dict[str, Any], reason: str, rules: Dict[str, Any]) -> str:
    best = float(c.get("best_odds", 0))
    avg = float(c.get("avg_odds", 0))
    lift = float(c.get("price_lift_pct", 0))
    min_odds = float(rules["min_decimal_odds"])
    max_odds = float(rules["max_decimal_odds"])
    pin = c.get("pinnacle")
    pin_status = c.get("pinnacle_status", "Pinnacle not available")
    pin_line = f" Pinnacle reference: {pin:.2f} — {pin_status}." if pin else " Pinnacle reference: not available for this pick/market."
    if "APPROVED" in decision or "ELITE" in decision:
        return "Passed no-edge GOAT checks: NZD decimal odds range, bookmaker support, NZ/US time safety, and discipline rules." + pin_line + " Paper log only."
    if "LOW BOOK COVERAGE" in decision:
        return f"Only {c.get('books', 0)} bookmaker prices were compared. Your minimum is {rules['min_books_compared']}. Not enough market coverage."
    if "ODDS RANGE" in decision:
        return f"Best decimal odds are {best:.2f}, outside your NZD staking range of {min_odds:.2f}-{max_odds:.2f}."
    if "TIME WINDOW" in decision:
        return "Game is started or too close. No rushed decisions."
    if "LOSS STREAK" in decision:
        return "Loss-streak lockout is active. This protects you from chasing."
    if "DAILY LIMIT" in decision:
        return "Daily paper-pick limit reached. Stop for the NZ day."
    if "EMOTIONAL" in decision:
        return "Late/chase feeling is marked. Automatic no."
    if "RED FLAG" in decision:
        return f"A manual red flag is active: {reason}."
    if "NOT HOME PICK" in decision:
        return "Your rule requires the home team for team markets. This pick is not the home team."
    if "NOT HOME FAVOURITE" in decision:
        return "Your rule requires the home favourite for team markets. This pick did not pass."
    if "PINNACLE" in decision:
        return "Pinnacle confirmation is required, but it is not confirmed. Watch only."
    if "SCORE TOO LOW" in decision:
        return f"No edge calculation is used. Score is too low because price quality/support/rules are not strong enough. Best price {best:.2f}, average book price {avg:.2f}, best-price lift {lift:.2f}%."
    return reason or "No GOAT approval."


def action_text(decision: str) -> str:
    if "ELITE" in decision:
        return "Action: log as ELITE paper pick only."
    if "APPROVED" in decision:
        return "Action: log as PAPER pick only."
    if "WATCHLIST" in decision:
        return "Action: watch only. Do not log as approved."
    return "Action: do not log. Do not bet."


def decide(c: Dict[str, Any], rules: Dict[str, Any], flags: Dict[str, bool], approved_count: int, loss_streak: int):
    parts = goat_score_breakdown(c, rules, flags, approved_count, loss_streak)
    score = goat_score_total(parts)
    min_odds = float(rules["min_decimal_odds"])
    max_odds = float(rules["max_decimal_odds"])
    min_books = int(rules["min_books_compared"])

    if c.get("time_locked"):
        decision, bucket, reason = "LOCKED — TIME WINDOW", "Time window", c.get("time_status", "Game time locked")
    elif flags.get("late_chase_feeling"):
        decision, bucket, reason = "LOCKED — EMOTIONAL RISK", "Locked/chase", "Late/chase feeling marked"
    elif loss_streak >= int(rules["lock_losses"]):
        decision, bucket, reason = "LOCKED — LOSS STREAK", "Locked/loss streak", f"Loss streak {loss_streak}"
    elif approved_count >= int(rules["max_daily"]):
        decision, bucket, reason = "LOCKED — DAILY LIMIT", "Locked/daily limit", "Daily limit reached"
    else:
        red_list = []
        if flags.get("injury_red"): red_list.append("Injury/news red flag")
        if flags.get("public_red"): red_list.append("Public-heavy red flag")
        if flags.get("fatigue_red"): red_list.append("Schedule/fatigue red flag")
        if flags.get("line_against"): red_list.append("Line moved against pick")
        if flags.get("key_player_red"): red_list.append("Key player uncertainty")

        if red_list and rules["reject_red_flags"]:
            decision, bucket, reason = "REJECTED — RED FLAG", "Red flag", "; ".join(red_list)
        elif not (min_odds <= c["best_odds"] <= max_odds):
            decision, bucket, reason = "REJECTED — ODDS RANGE", "Odds range", f"Best decimal odds {c['best_odds']:.2f} outside {min_odds:.2f}-{max_odds:.2f}"
        elif int(c.get("books", 0)) < min_books:
            decision, bucket, reason = "WATCHLIST — LOW BOOK COVERAGE", "Low book coverage", f"Only {c.get('books', 0)} books compared; minimum is {min_books}"
        elif rules["apply_home_rules_to_team_markets"] and c["market"] in ("h2h", "spreads") and rules["require_home_pick"] and not c["home_pick"]:
            decision, bucket, reason = "REJECTED — NOT HOME PICK", "Not home pick", "Pick is not home team"
        elif rules["apply_home_rules_to_team_markets"] and c["market"] in ("h2h", "spreads") and rules["require_home_favourite"] and not c["home_fav"]:
            decision, bucket, reason = "REJECTED — NOT HOME FAVOURITE", "Not home favourite", "Home favourite rule failed"
        elif rules["require_pinnacle_value"] and not c["pinnacle_ok"]:
            decision, bucket, reason = "WATCHLIST — PINNACLE NOT CONFIRMED", "Pinnacle missing", "Pinnacle value not confirmed"
        elif score >= 85:
            decision, bucket, reason = "ELITE PAPER PICK", "Approved", "GOAT Score 85+ and no-edge safety checks passed"
        elif score >= 75:
            decision, bucket, reason = "APPROVED PAPER PICK", "Approved", "GOAT Score 75+ and no-edge safety checks passed"
        else:
            decision, bucket, reason = "WATCHLIST — SCORE TOO LOW", "Score too low", f"GOAT Score {score}/100 below 75"

    return decision, score, bucket, reason, plain_explanation(decision, c, reason, rules), action_text(decision), score_parts_text(parts)


# =========================================================
# MOBILE CARD VIEW HELPERS — v3.6
# =========================================================
def short_prices(all_prices: str, max_books: int = 5) -> str:
    parts = [p.strip() for p in str(all_prices or "").split("|") if p.strip()]
    if not parts:
        return "No bookmaker prices shown"
    shown = parts[:max_books]
    extra = len(parts) - len(shown)
    text = " • ".join(shown)
    if extra > 0:
        text += f" • +{extra} more"
    return text


def render_candidate_card(row: dict, idx: int):
    decision = str(row.get("decision", ""))
    score = int(safe_float(row.get("score", 0), 0))
    pick = str(row.get("pick", ""))
    game = str(row.get("game", ""))
    market = str(row.get("market_label", row.get("market", "")))
    best_odds = safe_float(row.get("best_odds", 0), 0)
    avg_odds = safe_float(row.get("avg_odds", 0), 0)
    price_lift = safe_float(row.get("price_lift_pct", 0), 0)
    best_book = str(row.get("best_bookmaker", ""))
    plain = str(row.get("plain_explanation", row.get("reasons", "")))
    action = str(row.get("action", action_text(decision)))
    status = str(row.get("time_status", ""))
    starts_in = str(row.get("starts_in", ""))
    start_nz = str(row.get("start_nz", ""))
    start_et = str(row.get("start_et", ""))
    prices = short_prices(str(row.get("all_prices", "")))
    score_parts = str(row.get("score_parts", ""))

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
        c2.metric("Best price lift", f"{price_lift:.2f}%")
        st.progress(min(max(score, 0), 100) / 100)
        c3, c4 = st.columns(2)
        c3.metric("Best NZD decimal odds", f"{best_odds:.2f}")
        c4.metric("Avg book odds", f"{avg_odds:.2f}")
        pin = row.get("pinnacle")
        pin_gap = row.get("pinnacle_gap_pct")
        pin_status = row.get("pinnacle_status", "Pinnacle not available")
        st.markdown(f"**Best bookmaker:** {best_book}")
        if pin is not None and str(pin) != "nan":
            st.markdown(f"**Pinnacle reference:** {safe_float(pin, 0):.2f} | Gap: {safe_float(pin_gap, 0):+.2f}% | {pin_status}")
        else:
            st.markdown("**Pinnacle reference:** not available")
        st.markdown(f"**Time:** {status} • starts in **{starts_in}**")
        st.markdown(f"**NZ:** {start_nz}")
        st.markdown(f"**US ET:** {start_et}")
        if "APPROVED" in decision or "ELITE" in decision:
            st.success(plain)
        elif "WATCHLIST" in decision:
            st.warning(plain)
        else:
            st.error(plain)
        st.info(action)
        st.caption(f"Prices: {prices}")
        render_public_proof_badge(row)
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
        return df.sort_values(["score", "price_lift_pct"], ascending=[False, False]).head(max_cards)
    if mode == "Closest missed only":
        df = df[~df["decision"].astype(str).str.contains("APPROVED|ELITE", regex=True, na=False)]
        return df.sort_values(["score", "price_lift_pct"], ascending=[False, False]).head(max_cards)
    if mode == "Upcoming only":
        df = df[df["time_status"].astype(str).eq("Upcoming")]
        return df.sort_values(["sort", "score", "price_lift_pct"], ascending=[True, False, False]).head(max_cards)
    return df.sort_values(["sort", "time_locked", "score", "price_lift_pct"], ascending=[True, True, False, False]).head(max_cards)


# =========================================================
# PAPER LOG
# =========================================================
def load_log():
    if "paper_log_v36" not in st.session_state:
        if PICKS_PATH.exists():
            try:
                st.session_state.paper_log_v36 = pd.read_csv(PICKS_PATH)
            except Exception:
                st.session_state.paper_log_v36 = pd.DataFrame()
        else:
            st.session_state.paper_log_v36 = pd.DataFrame()
    return st.session_state.paper_log_v36


def save_log(df):
    st.session_state.paper_log_v36 = df
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

# -------------------------------
# Public pick proof helpers — v3.9.1
# -------------------------------
def proof_key(row: Dict[str, Any]) -> str:
    return "|".join([
        str(row.get("sport", "")),
        str(row.get("game", "")),
        str(row.get("market_label", row.get("market", ""))),
        str(row.get("pick", row.get("pick_label", ""))),
        str(row.get("start_nz", "")),
    ])


def load_public_proofs() -> Dict[str, Dict[str, Any]]:
    if "public_proofs_v39" not in st.session_state:
        st.session_state.public_proofs_v39 = {}
    return st.session_state.public_proofs_v39


def save_public_proof(key: str, proof: Dict[str, Any]) -> None:
    proofs = load_public_proofs()
    proofs[key] = proof
    st.session_state.public_proofs_v39 = proofs


def get_public_proof(row: Dict[str, Any]) -> Dict[str, Any]:
    return load_public_proofs().get(proof_key(row), {})


def scp_sport_slug(sport_text: str) -> str:
    s = str(sport_text or "").lower()
    if "mlb" in s or "baseball" in s:
        return "mlb-picks"
    if "nba" in s:
        return "nba-picks"
    if "nfl" in s:
        return "nfl-picks"
    if "nhl" in s:
        return "nhl-picks"
    if "wnba" in s:
        return "wnba-picks"
    if "mls" in s or "soccer" in s:
        return "soccer-picks"
    if "ncaa" in s and "football" in s:
        return "college-football-picks"
    if "ncaa" in s or "basketball" in s:
        return "college-basketball-picks"
    return "free-picks"


def sports_chat_place_links(row: Dict[str, Any]) -> Dict[str, str]:
    sport = str(row.get("sport", ""))
    game = str(row.get("game", ""))
    pick = str(row.get("pick", row.get("pick_label", "")))
    us_date = str(row.get("us_et_date", ""))
    slug = scp_sport_slug(sport)

    q1 = quote_plus(f"site:sportschatplace.com {game} {sport} prediction {us_date}")
    q2 = quote_plus(f"site:sportschatplace.com {pick} {game} free pick")
    q3 = quote_plus(f"{game} {sport} Sports Chat Place prediction {us_date}")

    return {
        "Sports Chat Place sport page": f"https://sportschatplace.com/{slug}/",
        "Google exact game search": f"https://www.google.com/search?q={q1}",
        "Google pick search": f"https://www.google.com/search?q={q2}",
        "Google broad search": f"https://www.google.com/search?q={q3}",
    }


def picks_parlays_sport_slug(sport_text: str) -> str:
    s = str(sport_text or "").lower()
    if "mlb" in s or "baseball" in s:
        return "mlb"
    if "nba" in s:
        return "nba"
    if "nfl" in s:
        return "nfl"
    if "nhl" in s:
        return "nhl"
    if "wnba" in s:
        return "wnba"
    if "mls" in s or "soccer" in s:
        return "soccer"
    if "ncaa" in s and "football" in s:
        return "college-football"
    if "ncaa" in s or "basketball" in s:
        return "college-basketball"
    return ""


def picks_and_parlays_links(row: Dict[str, Any]) -> Dict[str, str]:
    sport = str(row.get("sport", ""))
    game = str(row.get("game", ""))
    pick = str(row.get("pick", row.get("pick_label", "")))
    us_date = str(row.get("us_et_date", ""))
    slug = picks_parlays_sport_slug(sport)

    q1 = quote_plus(f"site:picksandparlays.net {game} {sport} prediction {us_date}")
    q2 = quote_plus(f"site:picksandparlays.net {pick} {game} free pick")
    q3 = quote_plus(f"{game} {sport} Picks and Parlays prediction {us_date}")

    links = {
        "Picks and Parlays home": "https://picksandparlays.net/",
        "Picks and Parlays exact game search": f"https://www.google.com/search?q={q1}",
        "Picks and Parlays pick search": f"https://www.google.com/search?q={q2}",
        "Picks and Parlays broad search": f"https://www.google.com/search?q={q3}",
    }
    if slug:
        links = {"Picks and Parlays sport page": f"https://picksandparlays.net/free-picks/{slug}", **links}
    return links



def proof_summary_text(proof: Dict[str, Any]) -> str:
    if not proof:
        return "Public proof: not checked"

    # Backward compatible with v3.9/v3.9.1 saved SCP-only proofs.
    scp_checked = bool(proof.get("scp_checked", proof.get("checked", False)))
    scp_agreement = proof.get("scp_agreement", proof.get("agreement", "Not set"))
    pp_checked = bool(proof.get("pp_checked", False))
    pp_agreement = proof.get("pp_agreement", "Not set")

    public = "PUBLIC-HEAVY RISK" if proof.get("public_heavy") else "no public-heavy risk marked"
    scp_text = f"SCP: {'checked' if scp_checked else 'not checked'} | {scp_agreement}"
    pp_text = f"Picks & Parlays: {'checked' if pp_checked else 'not checked'} | {pp_agreement}"
    return f"{scp_text} || {pp_text} || {public}"


def render_public_proof_badge(row: Dict[str, Any]) -> None:
    proof = get_public_proof(row)
    summary = proof_summary_text(proof)
    if proof.get("public_heavy"):
        st.warning(summary)
    elif proof.get("checked"):
        st.info(summary)
    else:
        st.caption(summary)


def main():
    st.title("🐐 GOAT Shield Live v3.9.2 MULTI PROOF")
    st.caption("Sports Chat Place + Picks and Parlays proof links + Pinnacle reference + NZD Decimal Odds + NZ Bettor Mode. Paper-only. No sportsbook login. No scraping.")

    api_key_default = secret("ODDS_API_KEY", "")

    with st.sidebar:
        st.markdown("### 🔌 Connection status")
        st.write("Odds API:", "✅ key found" if api_key_default else "Paste key below / Secrets")
        st.caption("No sportsbook login. No auto-betting.")

        api_key = st.text_input("The Odds API key", value=api_key_default, type="password")

        st.info("NZD note: 1.40-2.20 are decimal odds. If stake is NZD, payout = NZD stake x decimal odds.")

        if st.button("Reload active sports"):
            st.session_state.pop("sports_map_v36", None)
            st.rerun()

        sports_map = get_sports_map(api_key)
        sport_keys = list(sports_map.keys())

        sport_preset = st.selectbox(
            "Sport preset",
            [
                "Single / manual",
                "All US National Sports",
                "Big 4 Pro Only",
                "US Pro + MLS + WNBA",
                "College Only",
            ],
            index=0,
            help="All US National Sports includes active MLB, NBA, NFL, NHL, MLS, WNBA, NCAA Football, NCAA Basketball, and NCAA Women's Basketball when available from The Odds API.",
        )

        default_sports = default_us_sport_selection(sports_map, sport_preset)

        selected_sports = st.multiselect(
            "Active sports to scan",
            sport_keys,
            default=default_sports,
            format_func=lambda k: sports_map.get(k, US_NATIONAL_SPORTS_PACK.get(k, k)),
        )

        active_us_now = active_us_national_sports(sports_map)
        if sport_preset != "Single / manual":
            st.caption(f"Selected {len(selected_sports)} active US sports. API only returns sports currently available/active.")
        else:
            st.caption(f"US sports currently available: {', '.join([sports_map.get(k, k) for k in active_us_now]) if active_us_now else 'none found yet'}")

        markets = st.multiselect(
            "Markets",
            ["h2h", "spreads", "totals"],
            default=["h2h"],
            format_func=lambda x: {"h2h": "Moneyline / h2h", "spreads": "Spreads", "totals": "Totals"}[x],
        )

        regions = st.multiselect("Regions", ["us", "uk", "au", "eu"], default=["us"])
        bookmakers_filter = st.text_input("Optional bookmaker filter", value="", help="Example: draftkings,fanduel. Leave blank to use regions.")

        st.markdown("### Pinnacle reference")
        rules_pinnacle_ref = st.checkbox(
            "Pull Pinnacle reference odds",
            value=True,
            help="Uses The Odds API bookmaker=pinnacle as a reference line if available. This is not a sportsbook login and not scraping.",
        )
        st.caption("Pinnacle may not be available for every sport/market and provider coverage may not be instant.")

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
        if rules_pinnacle_ref:
            est += max(1, len(selected_sports)) * max(1, len(markets))
        st.caption(f"Estimated credits/fetch: about {est}. Start small. Pinnacle reference adds extra API calls.")
        if len(selected_sports) > 3 or len(markets) > 1:
            st.warning("Credit warning: multiple sports/markets can use API credits fast. Use h2h only first, then add spreads/totals later.")

        st.markdown("### GOAT rules — no edge gate")
        rules = dict(DEFAULT_RULES)
        # v3.7.3 safety guard: support old cached rule names and prevent KeyError.
        rules.setdefault("min_decimal_odds", rules.get("min_odds", 1.40))
        rules.setdefault("max_decimal_odds", rules.get("max_odds", 2.20))
        rules.setdefault("min_books_compared", 5)
        rules.setdefault("max_daily", 3)
        rules.setdefault("lock_losses", 3)
        rules.setdefault("min_minutes_before_start", 90)
        rules.setdefault("require_home_pick", True)
        rules.setdefault("require_home_favourite", True)
        rules.setdefault("require_pinnacle_value", False)
        rules.setdefault("show_pinnacle_reference", True)
        rules.setdefault("pinnacle_score_bonus", True)
        rules.setdefault("reject_red_flags", True)
        rules.setdefault("apply_home_rules_to_team_markets", True)
        rules["min_decimal_odds"] = st.number_input("Min NZD decimal odds", 1.01, 10.0, float(rules["min_decimal_odds"]), 0.01)
        rules["max_decimal_odds"] = st.number_input("Max NZD decimal odds", 1.01, 10.0, float(rules["max_decimal_odds"]), 0.01)
        rules["min_books_compared"] = st.number_input("Minimum bookmakers compared", 1, 20, int(rules["min_books_compared"]), 1)
        rules["max_daily"] = st.number_input("Max approved paper picks per NZ day", 1, 20, int(rules["max_daily"]), 1)
        rules["lock_losses"] = st.number_input("Loss-streak lockout", 1, 20, int(rules["lock_losses"]), 1)
        rules["min_minutes_before_start"] = st.number_input("Lock if game starts within minutes", 0, 240, int(rules["min_minutes_before_start"]), 5)
        rules["apply_home_rules_to_team_markets"] = st.checkbox("Apply home rules to team markets only", True)
        rules["require_home_pick"] = st.checkbox("Require team-market pick to be home team", True)
        rules["require_home_favourite"] = st.checkbox("Require home favourite for team markets", True)
        rules["show_pinnacle_reference"] = bool(rules_pinnacle_ref)
        rules["require_pinnacle_value"] = st.checkbox(
            "Require Pinnacle confirmation if available",
            False,
            help="OFF is safer. If ON and Pinnacle is missing or worse, candidate becomes watchlist.",
        )
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

    tabs = st.tabs(["🇳🇿 NZ Bettor Board", "📱 Mobile Cards", "🧾 Public Pick Proof", "🟢 Best Price Board", "📒 Paper Log", "✅ Results", "📊 Dashboard", "🛡️ Backup"])

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

            pinnacle_events = []
            pinnacle_metas = []

            with st.spinner("Fetching bookmaker prices + Pinnacle reference with NZ/US time conversion..."):
                for sport in selected_sports:
                    try:
                        ev, meta = fetch_odds(api_key, sport, ",".join(regions), ",".join(markets), bookmakers_filter, start, end)
                        events.extend(ev)
                        metas.append(meta)
                    except Exception as e:
                        errors.append(f"{sports_map.get(sport, sport)}: {clean_api_error(e)}")

                    if rules.get("show_pinnacle_reference") and not bookmakers_filter.strip():
                        try:
                            pin_ev, pin_meta = fetch_odds(api_key, sport, "", ",".join(markets), "pinnacle", start, end)
                            pinnacle_events.extend(pin_ev)
                            pinnacle_metas.append(pin_meta)
                        except Exception as e:
                            errors.append(f"Pinnacle reference {sports_map.get(sport, sport)}: {clean_api_error(e)}")

            pinnacle_ref = build_pinnacle_reference(pinnacle_events, markets) if pinnacle_events else {}
            st.session_state["events_v36"] = events
            st.session_state["pinnacle_ref_v38"] = pinnacle_ref
            st.session_state["pinnacle_events_v38"] = pinnacle_events
            st.session_state["markets_v36"] = markets
            st.session_state["metas_v36"] = metas
            st.session_state["pinnacle_metas_v38"] = pinnacle_metas
            st.session_state["rules_v36"] = rules

            for e in errors:
                st.error(e)
            if metas:
                pin_note = f" Pinnacle reference matches: {len(st.session_state.get('pinnacle_ref_v38', {}))}." if rules.get("show_pinnacle_reference") else ""
                st.success(f"Fetched {len(events)} events. Requests used: {metas[-1].get('requests_used')}. Remaining: {metas[-1].get('requests_remaining')}.{pin_note}")

        events = st.session_state.get("events_v36", [])
        last_markets = st.session_state.get("markets_v36", markets)

        if events:
            pinnacle_ref = st.session_state.get("pinnacle_ref_v38", {}) if rules.get("show_pinnacle_reference") else {}
            candidates = build_candidates(events, last_markets, int(rules["min_minutes_before_start"]), pinnacle_ref)
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
                    text = ", ".join([f"{k}: {v}" for k, v in summary.most_common(6)]) or "No clean no-edge rule pass"
                    st.warning(f"No approved paper picks. Main blockers: {text}")
                else:
                    st.success(f"{approved_n} approved/elite paper candidate(s). Paper-log only.")

                board = pd.DataFrame(rows)
                board["sort"] = board["decision"].map({"ELITE PAPER PICK": 0, "APPROVED PAPER PICK": 1, "WATCHLIST — PINNACLE NOT CONFIRMED": 2}).fillna(9)
                board = board.sort_values(["sort", "time_locked", "score", "price_lift_pct"], ascending=[True, True, False, False]).reset_index(drop=True)

                cols = [
                    "decision", "score", "plain_explanation", "action", "time_status", "starts_in",
                    "start_nz", "start_et", "nz_date", "us_et_date",
                    "sport", "game", "market_label", "pick",
                    "best_odds", "best_bookmaker", "avg_odds",
                    "pinnacle", "pinnacle_gap_pct", "pinnacle_status", "tab_betcha", "bet365",
                    "price_lift_pct", "books", "reasons", "score_parts", "all_prices",
                ]
                st.dataframe(
                    board[cols].style.format({"best_odds": "{:.2f}", "avg_odds": "{:.2f}", "price_lift_pct": "{:.2f}%", "pinnacle": "{:.2f}", "pinnacle_gap_pct": "{:.2f}%"}),
                    use_container_width=True,
                    hide_index=True,
                )

                st.subheader("📱 Mobile-friendly view")
                st.caption("For clean iPhone reading, open the 📱 Mobile Cards tab after this scan.")

                st.subheader("👀 Closest missed picks")
                missed = board[~board["decision"].astype(str).str.contains("APPROVED|ELITE", regex=True, na=False)].sort_values(["score", "price_lift_pct"], ascending=[False, False]).head(5)
                if missed.empty:
                    st.info("No closest misses.")
                else:
                    st.dataframe(
                        missed[["decision", "score", "plain_explanation", "action", "time_status", "starts_in", "start_nz", "start_et", "market_label", "pick", "best_odds", "best_bookmaker", "pinnacle", "pinnacle_gap_pct", "pinnacle_status", "price_lift_pct", "reasons", "all_prices"]].style.format({"best_odds": "{:.2f}", "pinnacle": "{:.2f}", "pinnacle_gap_pct": "{:.2f}%", "price_lift_pct": "{:.2f}%"}),
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
                            "pinnacle_gap_pct": chosen["pinnacle_gap_pct"],
                            "pinnacle_status": chosen["pinnacle_status"],
                            "tab_betcha": chosen["tab_betcha"],
                            "bet365": chosen["bet365"],
                            "price_lift_pct": chosen["price_lift_pct"],
                            "decision": chosen["decision"],
                            "score": chosen["score"],
                            "plain_explanation": chosen.get("plain_explanation", ""),
                            "score_parts": chosen.get("score_parts", ""),
                            "scp_proof_summary": proof_summary_text(get_public_proof(chosen)),
                            "result": "Pending",
                            "profit_units": "",
                            "closing_odds": "",
                            "closing_price_movement": "",
                            "all_prices": chosen["all_prices"],
                        }
                        new_df = pd.concat([pd.DataFrame([log_row]), log_df], ignore_index=True)
                        save_log(new_df)
                        st.success("Logged as paper pick only.")
                        st.rerun()

    with tabs[1]:
        st.subheader("📱 Mobile Cards")
        st.write("Clean iPhone view with no-edge GOAT Score, plain-English explanations, and clear action labels.")
        events_cards = st.session_state.get("events_v36", [])
        last_markets_cards = st.session_state.get("markets_v36", markets)
        if not events_cards:
            st.info("Run Fetch NZ bettor board first, then come here.")
        else:
            pinnacle_ref_cards = st.session_state.get("pinnacle_ref_v38", {}) if rules.get("show_pinnacle_reference") else {}
            candidates_cards = build_candidates(events_cards, last_markets_cards, int(rules["min_minutes_before_start"]), pinnacle_ref_cards)
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
                st.caption("No edge calculation is used. GOAT Score is based on price comparison, odds range, time, rules, and discipline.")
                c_max = st.slider("Number of cards", 3, 15, 5)
                picked_cards = mobile_card_dataframe(cards_df, c_mode, c_max)
                if picked_cards.empty:
                    st.info("No cards match this filter.")
                else:
                    for n, (_, card_row) in enumerate(picked_cards.iterrows(), start=1):
                        render_candidate_card(card_row.to_dict(), n)

    with tabs[2]:
        st.subheader("🧾 Public Pick Proof")
        st.info("Use this to manually check Sports Chat Place and Picks & Parlays free-pick pages for the same game/date. This does not scrape either site and does not make public-pick sites the main decision maker.")

        events_proof = st.session_state.get("events_v36", [])
        last_markets_proof = st.session_state.get("markets_v36", markets)
        if not events_proof:
            st.info("Run Fetch NZ bettor board first, then come here.")
        else:
            pinnacle_ref_proof = st.session_state.get("pinnacle_ref_v38", {}) if rules.get("show_pinnacle_reference") else {}
            candidates_proof = build_candidates(events_proof, last_markets_proof, int(rules["min_minutes_before_start"]), pinnacle_ref_proof)

            rows_proof = []
            app_count_proof = approved_today_count(log_df)
            streak_proof = loss_streak_count(log_df)

            for cand in candidates_proof:
                decision, score, bucket, reason, plain, action, score_parts = decide(cand, rules, flags, app_count_proof, streak_proof)
                rr = dict(cand)
                rr.update({
                    "decision": decision,
                    "score": score,
                    "reject_bucket": bucket,
                    "reasons": reason,
                    "plain_explanation": plain,
                    "action": action,
                    "score_parts": score_parts,
                })
                rows_proof.append(rr)

            if not rows_proof:
                st.warning("No candidates available for public proof.")
            else:
                proof_df = pd.DataFrame(rows_proof)
                proof_df["sort"] = proof_df["decision"].map({
                    "ELITE PAPER PICK": 0,
                    "APPROVED PAPER PICK": 1,
                    "WATCHLIST — LOW BOOK COVERAGE": 2,
                    "WATCHLIST — PINNACLE NOT CONFIRMED": 2,
                    "WATCHLIST — SCORE TOO LOW": 2,
                }).fillna(9)
                proof_df = proof_df.sort_values(["sort", "time_locked", "score", "price_lift_pct"], ascending=[True, True, False, False]).reset_index(drop=True)

                labels = proof_df.apply(
                    lambda x: f"{x.name}: {x['decision']} — {x['pick']} — {x['game']} — {x['start_nz']}",
                    axis=1
                ).tolist()

                choice = st.selectbox("Pick/game to check", labels)
                idx = int(choice.split(":")[0])
                chosen = proof_df.loc[idx].to_dict()

                st.markdown(f"### {chosen.get('pick')} — {chosen.get('game')}")
                st.write(f"NZ time: {chosen.get('start_nz')} | US ET: {chosen.get('start_et')}")
                st.write(f"Decision: {chosen.get('decision')} | GOAT Score: {chosen.get('score')}/100")
                st.caption(chosen.get("plain_explanation", ""))

                st.markdown("#### Search links — Sports Chat Place")
                for label, url in sports_chat_place_links(chosen).items():
                    st.link_button(label, url)

                st.markdown("#### Search links — Picks & Parlays")
                for label, url in picks_and_parlays_links(chosen).items():
                    st.link_button(label, url)

                st.markdown("#### Manual proof result")
                key = proof_key(chosen)
                existing = load_public_proofs().get(key, {})

                agreement_options = [
                        "Not set",
                        "Agrees with our candidate",
                        "Disagrees / opposite side",
                        "Pick is Over",
                        "Pick is Under",
                        "Pick is Spread only",
                        "Pick is Parlay only",
                        "No clear pick found",
                    ]

                st.markdown("##### Sports Chat Place")
                scp_checked = st.checkbox(
                    "Sports Chat Place checked for this exact game/date",
                    value=bool(existing.get("scp_checked", existing.get("checked", False))),
                )
                scp_existing = existing.get("scp_agreement", existing.get("agreement", "Not set"))
                scp_agreement = st.selectbox(
                    "SCP result compared with our candidate",
                    agreement_options,
                    index=agreement_options.index(scp_existing) if scp_existing in agreement_options else 0,
                )
                scp_url = st.text_input("Paste Sports Chat Place article URL if found", value=str(existing.get("scp_url", existing.get("url", ""))))

                st.markdown("##### Picks & Parlays")
                pp_checked = st.checkbox(
                    "Picks & Parlays checked for this exact game/date",
                    value=bool(existing.get("pp_checked", False)),
                )
                pp_existing = existing.get("pp_agreement", "Not set")
                pp_agreement = st.selectbox(
                    "Picks & Parlays result compared with our candidate",
                    agreement_options,
                    index=agreement_options.index(pp_existing) if pp_existing in agreement_options else 0,
                )
                pp_url = st.text_input("Paste Picks & Parlays article URL if found", value=str(existing.get("pp_url", "")))

                public_heavy = st.checkbox("Public-heavy risk / too many public sources on same side", value=bool(existing.get("public_heavy", False)))
                notes = st.text_area("Notes", value=str(existing.get("notes", "")), height=100)

                if st.button("Save public proof for this pick"):
                    save_public_proof(key, {
                        "scp_checked": scp_checked,
                        "scp_agreement": scp_agreement,
                        "scp_url": scp_url,
                        "pp_checked": pp_checked,
                        "pp_agreement": pp_agreement,
                        "pp_url": pp_url,
                        "public_heavy": public_heavy,
                        "notes": notes,
                        "saved_at": iso_z(datetime.now(timezone.utc)),
                    })
                    st.success("Public proof saved for this scan/session.")

                st.markdown("#### Saved proof summary")
                st.write(proof_summary_text(load_public_proofs().get(key, {})))

                if public_heavy:
                    st.warning("Public-heavy risk marked. Treat this as a red flag. Do not turn this into a real bet.")

    with tabs[3]:
        st.subheader("🟢 Best Price Board")
        st.write("Use the NZ Bettor Board first. It includes all best-price board columns plus NZ/US time conversion.")
        st.caption("v3.3 keeps this tab as a simple explanation so the phone UI stays cleaner.")

    with tabs[4]:
        st.subheader("📒 Paper Log")
        df = load_log()
        if df.empty:
            st.info("No paper picks logged yet.")
        else:
            st.dataframe(df, use_container_width=True)
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8"), "goat_shield_paper_log.csv", "text/csv")

    with tabs[5]:
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

    with tabs[6]:
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

    with tabs[7]:
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
    st.caption("GOAT Shield Live v3.9.2 MULTI PROOF is paper-only. It does not place real-money bets, log into sportsbooks, scrape bookmakers, or bypass betting rules.")


if __name__ == "__main__":
    main()
