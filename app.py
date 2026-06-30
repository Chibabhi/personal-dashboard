
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
    "max_decimal_odds": 1.90,
    "min_books_compared": 10,
    "max_daily": 3,
    "lock_losses": 3,
    "min_minutes_before_start": 0,
    "require_home_pick": True,
    "require_home_favourite": True,
    "require_pinnacle_value": False,
    "show_pinnacle_reference": True,
    "pinnacle_score_bonus": True,
    "reject_red_flags": True,
    "apply_home_rules_to_team_markets": True,
    "auto_verify_mode": True,
    "lock_low_confidence": True,
    "min_data_confidence_score": 75,
    "max_stale_seconds": 180,
    "max_line_move_pct": 3.0,
    "alignment_lock_mode": True,
    "require_high_confidence_to_log": True,
    "allow_full_alignment_override": True,
    "allow_partial_alignment_watchlist_log": False,
    "post_start_grace_minutes": 5,
    "hide_after_post_start_grace": True,
    "picks_mode_high_conf_only": True,
    "require_sharp_support": True,
    "min_sharp_books": 1,
    "sharp_support_score_bonus": True,
    "retail_only_warning": True,
    "dynamic_book_thresholds": True,
    "major_sport_books": 8,
    "mid_sport_books": 6,
    "college_sport_books": 5,
    "other_sport_books": 4,
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

# Sharp/core reference books or exchanges when returned by your selected Odds API plan/region.
# The app detects these only if they appear in the API response or the separate Pinnacle reference pull.
SHARP_BOOK_HINTS = (
    "pinnacle",
    "circa",
    "bookmaker",
    "bookmaker.eu",
    "cris",
    "betcris",
    "betfair",
    "betfair exchange",
    "matchbook",
    "smarkets",
)

MAJOR_BOOK_SPORTS = {
    "baseball_mlb",
    "basketball_nba",
    "americanfootball_nfl",
    "icehockey_nhl",
}

MID_BOOK_SPORTS = {
    "basketball_wnba",
    "soccer_usa_mls",
}

COLLEGE_BOOK_SPORTS = {
    "americanfootball_ncaaf",
    "basketball_ncaab",
    "basketball_ncaawb",
}


def required_books_for_candidate(c: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[int, str]:
    """Dynamic bookmaker threshold by sport. Falls back to the manual/global setting."""
    if not rules.get("dynamic_book_thresholds", True):
        return int(rules.get("min_books_compared", 10)), "Manual/global threshold"

    sport_key = str(c.get("sport_key", "") or "").lower()
    sport_title = str(c.get("sport", c.get("sport_title", "")) or "").lower()

    if sport_key in MAJOR_BOOK_SPORTS:
        return int(rules.get("major_sport_books", 8)), "Major US sport"
    if sport_key in MID_BOOK_SPORTS:
        return int(rules.get("mid_sport_books", 6)), "WNBA/MLS"
    if sport_key in COLLEGE_BOOK_SPORTS:
        return int(rules.get("college_sport_books", 5)), "College sport"

    # Fallback by title text in case an API sport key changes slightly.
    if any(x in sport_title for x in ["mlb", "nba", "nfl", "nhl"]):
        return int(rules.get("major_sport_books", 8)), "Major US sport"
    if any(x in sport_title for x in ["wnba", "mls"]):
        return int(rules.get("mid_sport_books", 6)), "WNBA/MLS"
    if any(x in sport_title for x in ["college", "ncaa", "ncaab", "ncaaf", "ncaawb"]):
        return int(rules.get("college_sport_books", 5)), "College sport"

    return int(rules.get("other_sport_books", 4)), "Other/low-coverage sport"


def resolved_required_books(c: Dict[str, Any], rules: Dict[str, Any]) -> Tuple[int, str]:
    """Safe dynamic threshold resolver. Handles None, blank strings, NaN, and cached rows."""
    fallback_books, fallback_group = required_books_for_candidate(c, rules)
    raw_books = c.get("required_books", None)

    if raw_books is None or str(raw_books).strip() == "" or str(raw_books).lower() == "nan":
        books = fallback_books
    else:
        try:
            books = int(float(raw_books))
        except Exception:
            books = fallback_books

    group = str(c.get("book_threshold_group", "") or "").strip() or fallback_group
    c["required_books"] = int(books)
    c["book_threshold_group"] = group
    return int(books), group


# =========================================================
# STREAMLIT PAGE
# =========================================================
st.set_page_config(
    page_title="GOAT Shield Live v4.4.5 LOOSE BOOKS",
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


def canonical_book_name(row: Dict[str, Any]) -> str:
    key = str(row.get("book_key", "") or "").strip()
    title = str(row.get("book", "") or "").strip()
    return key.lower() if key else title.lower()


def sharp_rows_from_group(group: List[Dict[str, Any]], pinnacle_available: bool = False) -> List[Dict[str, Any]]:
    sharp = []
    seen = set()

    for x in group:
        if has_hint(x.get("book_key", ""), SHARP_BOOK_HINTS) or has_hint(x.get("book", ""), SHARP_BOOK_HINTS):
            k = canonical_book_name(x)
            if k and k not in seen:
                seen.add(k)
                sharp.append(x)

    # Pinnacle can be pulled separately as a reference source, so count it as sharp support even
    # if it was not part of the main region/bookmaker response.
    if pinnacle_available and "pinnacle_reference" not in seen:
        sharp.append({"book": "Pinnacle reference", "book_key": "pinnacle_reference"})

    return sharp


def sharp_status_text(sharp_count: int, sharp_books: str) -> str:
    if sharp_count <= 0:
        return "RETAIL ONLY — no sharp/core support detected"
    return f"SHARP SUPPORT — {sharp_count} core source(s): {sharp_books}"


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


def game_time_status(dt: Optional[datetime], min_minutes: int, post_start_grace_minutes: int = 5) -> Tuple[str, bool]:
    if dt is None:
        return "Unknown time", False
    now = datetime.now(timezone.utc)
    diff_min = (dt - now).total_seconds() / 60

    # User rule: show qualifying paper picks until start, or up to 5 minutes into the game.
    if diff_min < -post_start_grace_minutes:
        return f"Past +{post_start_grace_minutes}m / hidden", True
    if diff_min < 0:
        return f"Live first {post_start_grace_minutes}m only / paper-only", False

    if min_minutes > 0 and diff_min <= min_minutes:
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
            bm_last_update = str(bm.get("last_update", ""))
            bm_last_update_dt = parse_api_datetime(bm_last_update)
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
                        "book_last_update": bm_last_update,
                        "book_last_update_dt": bm_last_update_dt,
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



def build_candidates(events: List[Dict[str, Any]], selected_markets: List[str], min_minutes_before_start: int, pinnacle_ref: Optional[Dict[Tuple, Dict[str, Any]]] = None, post_start_grace_minutes: int = 5) -> List[Dict[str, Any]]:
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
        now_for_window = datetime.now(timezone.utc)
        if start_dt is not None and (now_for_window - start_dt).total_seconds() / 60 > post_start_grace_minutes:
            # User rule: after the +5 minute grace window, do not show this game at all.
            continue
        status, time_locked = game_time_status(start_dt, min_minutes_before_start, post_start_grace_minutes)

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

            unique_books = sorted({canonical_book_name(x) for x in group if canonical_book_name(x)})
            sharp_rows = sharp_rows_from_group(group, pinnacle_available=bool(pin_best))
            sharp_book_names = []
            seen_sharp_names = set()
            for x in sharp_rows:
                nm = str(x.get("book", x.get("book_key", "")) or "").strip()
                key_nm = nm.lower()
                if nm and key_nm not in seen_sharp_names:
                    seen_sharp_names.add(key_nm)
                    sharp_book_names.append(nm)
            sharp_core_count = len(sharp_book_names)
            sharp_books_text = ", ".join(sharp_book_names) if sharp_book_names else "None"
            sharp_support = sharp_core_count >= 1
            retail_books_count = max(0, len(unique_books) - len([b for b in unique_books if has_hint(b, SHARP_BOOK_HINTS)]))

            is_team = market in ("h2h", "spreads") and outcome in (home, away)
            is_home = bool(is_team and outcome == home)
            if market == "h2h":
                is_home_fav = bool(is_home and home_fav_h2h)
            elif market == "spreads":
                is_home_fav = bool(is_home and point is not None and point < 0)
            else:
                is_home_fav = False

            all_prices = sorted(group, key=lambda x: x["price"], reverse=True)
            update_dts = [x.get("book_last_update_dt") for x in group if x.get("book_last_update_dt") is not None]
            now_utc_for_age = datetime.now(timezone.utc)
            oldest_update_dt = min(update_dts) if update_dts else None
            newest_update_dt = max(update_dts) if update_dts else None
            oldest_update_age_sec = round((now_utc_for_age - oldest_update_dt).total_seconds(), 0) if oldest_update_dt else None
            newest_update_age_sec = round((now_utc_for_age - newest_update_dt).total_seconds(), 0) if newest_update_dt else None
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
                "post_start_grace_minutes": post_start_grace_minutes,
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
                "sharp_support": bool(sharp_support),
                "sharp_core_count": int(sharp_core_count),
                "sharp_books": sharp_books_text,
                "sharp_status": sharp_status_text(sharp_core_count, sharp_books_text),
                "retail_books_count": int(retail_books_count),
                "tab_betcha": round(nz_best["price"], 3) if nz_best else None,
                "bet365": round(b365_best["price"], 3) if b365_best else None,
                "consensus_prob": round(consensus_prob, 5),
                "implied_prob": round(implied, 5),
                "edge": round(edge, 5),
                "books": len(group),
                "required_books": None,
                "book_threshold_group": "",
                "home_pick": is_home,
                "home_fav": is_home_fav,
                "pinnacle_ok": bool(pin_best and best["price"] >= pin_best["price"]),
                "market_win_pct": round(consensus_prob * 100, 2),
                "best_implied_win_pct": round(implied * 100, 2),
                "avg_implied_win_pct": round((1 / avg_odds) * 100, 2) if avg_odds > 1 else None,
                "oldest_book_update": iso_z(oldest_update_dt) if oldest_update_dt else "",
                "newest_book_update": iso_z(newest_update_dt) if newest_update_dt else "",
                "oldest_book_update_age_sec": oldest_update_age_sec,
                "newest_book_update_age_sec": newest_update_age_sec,
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
            int(resolved_required_books(c, rules)[0]),
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
        required_books, threshold_group = resolved_required_books(c, rules)
        return f"Only {c.get('books', 0)} bookmaker prices were compared. Required for this sport: {required_books} ({threshold_group}). Not enough market coverage."
    if "RETAIL ONLY" in decision:
        return f"Only retail/soft-book support was detected. Sharp status: {c.get('sharp_status', 'unknown')}. Keep as watchlist only."
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
    min_books, threshold_group = resolved_required_books(c, rules)
    parts = goat_score_breakdown(c, rules, flags, approved_count, loss_streak)
    score = goat_score_total(parts)
    min_odds = float(rules["min_decimal_odds"])
    max_odds = float(rules["max_decimal_odds"])

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
            decision, bucket, reason = "WATCHLIST — LOW BOOK COVERAGE", "Low book coverage", f"Only {c.get('books', 0)} books compared; required for this sport is {min_books} ({threshold_group})"
        elif rules.get("require_sharp_support", True) and int(c.get("sharp_core_count", 0)) < int(rules.get("min_sharp_books", 1)):
            decision, bucket, reason = "WATCHLIST — RETAIL ONLY", "No sharp support", f"No sharp/core bookmaker support detected. Need at least {int(rules.get('min_sharp_books', 1))} sharp/core source."
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

        st.markdown(f"**Sharp/Core support:** {row.get('sharp_status', 'Unknown')}")
        st.caption(f"Sharp books: {row.get('sharp_books', 'None')} | Retail books counted: {row.get('retail_books_count', 0)}")

        conf = str(row.get("data_confidence", "UNKNOWN"))
        conf_score = int(safe_float(row.get("data_confidence_score", 0), 0))
        market_win = safe_float(row.get("market_win_pct", 0), 0)
        implied_win = safe_float(row.get("best_implied_win_pct", 0), 0)
        data_age = str(row.get("data_age", "unknown"))
        line_stability = str(row.get("line_stability", "No prior refresh comparison yet"))
        st.markdown(f"**Auto Verify:** {conf} confidence ({conf_score}/100) | Market win %: {market_win:.2f}% | Best implied: {implied_win:.2f}%")
        st.caption(f"Data age: {data_age} | Line check: {line_stability}")
        if conf == "LOW":
            st.warning(str(row.get("data_confidence_reasons", "Low data confidence")))
        elif conf == "MEDIUM":
            st.info(str(row.get("data_confidence_reasons", "Medium data confidence")))
        else:
            st.success("Auto Verify passed: " + str(row.get("data_confidence_passed", "High confidence")))

        lock_status = str(row.get("log_lock_status", "LOCK STATUS UNKNOWN"))
        lock_reason = str(row.get("log_lock_reason", ""))
        if bool(row.get("paper_log_allowed", False)):
            st.success(f"**Alignment Lock:** {lock_status} — {lock_reason}")
        else:
            st.error(f"**Alignment Lock:** {lock_status} — {lock_reason}")

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
# AUTO VERIFY + DATA CONFIDENCE — v4.1
# =========================================================
def candidate_auto_key(c: Dict[str, Any]) -> str:
    return "|".join([
        str(c.get("sport_key", "")),
        str(c.get("event_id", "")),
        str(c.get("market", "")),
        str(c.get("outcome", c.get("pick", ""))),
        str(c.get("point", "")),
        str(c.get("start", "")),
    ])


def age_label(seconds: Any) -> str:
    if seconds is None or str(seconds) == "nan" or str(seconds) == "":
        return "unknown"
    try:
        sec = float(seconds)
    except Exception:
        return "unknown"
    if sec < 60:
        return f"{int(sec)}s"
    if sec < 3600:
        return f"{int(sec // 60)}m {int(sec % 60)}s"
    return f"{int(sec // 3600)}h {int((sec % 3600) // 60)}m"


def confidence_class(score: float) -> str:
    if score >= 85:
        return "HIGH"
    if score >= 70:
        return "MEDIUM"
    return "LOW"


def confidence_parts(c: Dict[str, Any], rules: Dict[str, Any], previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    score = 100.0
    passed = []
    warnings = []

    min_books, threshold_group = resolved_required_books(c, rules)
    max_stale = float(rules.get("max_stale_seconds", 180))
    max_line_move = float(rules.get("max_line_move_pct", 3.0))
    min_odds = float(rules.get("min_decimal_odds", 1.40))
    max_odds = float(rules.get("max_decimal_odds", 2.20))

    books = int(c.get("books", 0))
    if books >= min_books:
        passed.append(f"{books} bookmakers compared; required {min_books} for {threshold_group}")
    else:
        score -= 20
        warnings.append(f"Only {books} bookmakers compared; required {min_books} for {threshold_group}")

    if c.get("pinnacle_available"):
        gap = c.get("pinnacle_gap_pct")
        if gap is None:
            passed.append("Pinnacle available")
        elif safe_float(gap, 0) >= 0:
            passed.append(f"Best price near/above Pinnacle ({safe_float(gap, 0):+.2f}%)")
        else:
            score -= 10
            warnings.append(f"Best price below Pinnacle ({safe_float(gap, 0):+.2f}%)")
    else:
        score -= 15
        warnings.append("Pinnacle reference missing")

    min_sharp = int(rules.get("min_sharp_books", 1))
    sharp_count = int(c.get("sharp_core_count", 0))
    if sharp_count >= min_sharp:
        passed.append(str(c.get("sharp_status", f"Sharp support: {sharp_count}")))
    else:
        if rules.get("require_sharp_support", True):
            score -= 22
            warnings.append(f"No sharp/core support detected; minimum is {min_sharp}")
        elif rules.get("retail_only_warning", True):
            score -= 8
            warnings.append("Retail-only support warning")

    age = c.get("oldest_book_update_age_sec")
    if age is None or str(age) == "nan" or age == "":
        score -= 8
        warnings.append("Bookmaker update timestamp missing")
    else:
        age_f = safe_float(age, 999999)
        if age_f <= max_stale / 2:
            passed.append(f"Bookmaker updates fresh enough: {age_label(age_f)}")
        elif age_f <= max_stale:
            score -= 5
            warnings.append(f"Bookmaker updates getting older: {age_label(age_f)}")
        else:
            score -= 25
            warnings.append(f"Bookmaker updates stale: {age_label(age_f)}")

    if c.get("time_locked"):
        score -= 35
        warnings.append(str(c.get("time_status", "Time window locked")))
    else:
        passed.append(str(c.get("time_status", "Time window safe")))

    best_odds = safe_float(c.get("best_odds", 0), 0)
    if min_odds <= best_odds <= max_odds:
        passed.append(f"Odds inside range {min_odds:.2f}-{max_odds:.2f}")
    else:
        score -= 20
        warnings.append(f"Odds outside range {min_odds:.2f}-{max_odds:.2f}")

    if c.get("home_pick"):
        passed.append("Home pick confirmed")
    else:
        score -= 10
        warnings.append("Not a home pick")

    if c.get("home_fav"):
        passed.append("Home favourite confirmed by market odds")
    else:
        score -= 12
        warnings.append("Home favourite not confirmed")

    line_move_pct = None
    line_status = "No prior refresh comparison yet"
    if previous and previous.get("best_odds"):
        prev_odds = safe_float(previous.get("best_odds"), 0)
        if prev_odds > 1 and best_odds > 1:
            line_move_pct = round((best_odds / prev_odds - 1) * 100, 2)
            if abs(line_move_pct) <= 0.25:
                line_status = "Stable since last fetch"
                passed.append(line_status)
            elif abs(line_move_pct) <= max_line_move:
                line_status = f"Moved {line_move_pct:+.2f}% since last fetch"
                score -= 6
                warnings.append(line_status)
            else:
                line_status = f"Large move {line_move_pct:+.2f}% since last fetch"
                score -= 25
                warnings.append(line_status)
    else:
        score -= 3
        warnings.append("No previous fetch to verify line movement yet")

    score = max(0, min(100, round(score, 0)))
    conf = confidence_class(score)
    reasons = " | ".join(warnings) if warnings else "All auto-verification checks passed"
    passed_text = " | ".join(passed) if passed else "No strong verification positives yet"

    return {
        "data_confidence_score": int(score),
        "data_confidence": conf,
        "data_confidence_reasons": reasons,
        "data_confidence_passed": passed_text,
        "line_move_pct_since_last_fetch": line_move_pct,
        "line_stability": line_status,
        "source_stack": "The Odds API market data + Pinnacle reference + internal implied probability/home-favourite checks",
        "data_age": age_label(c.get("oldest_book_update_age_sec")),
    }


def apply_auto_verify_to_rows(rows: List[Dict[str, Any]], rules: Dict[str, Any], update_snapshot: bool = False) -> List[Dict[str, Any]]:
    previous_snapshot = st.session_state.get("candidate_snapshot_v41", {})
    new_snapshot: Dict[str, Dict[str, Any]] = {}
    out = []

    for row in rows:
        r = dict(row)
        key = candidate_auto_key(r)
        previous = previous_snapshot.get(key)
        verify = confidence_parts(r, rules, previous)
        r.update(verify)

        if rules.get("auto_verify_mode", True):
            min_conf = int(rules.get("min_data_confidence_score", 75))
            if rules.get("lock_low_confidence", True) and int(r.get("data_confidence_score", 0)) < min_conf:
                if "APPROVED" in str(r.get("decision", "")) or "ELITE" in str(r.get("decision", "")):
                    r["decision"] = "WATCHLIST — LOW DATA CONFIDENCE"
                    r["reject_bucket"] = "Low data confidence"
                    r["reasons"] = f"Auto Verify confidence {r.get('data_confidence_score')}/100 below required {min_conf}. {r.get('data_confidence_reasons')}"
                    r["plain_explanation"] = "Auto Verify blocked this from paper approval because data confidence is not high enough. " + str(r.get("data_confidence_reasons", ""))
                    r["action"] = "Do not paper-log yet. Refresh later or wait for stronger data confidence."

        new_snapshot[key] = {
            "best_odds": r.get("best_odds"),
            "pinnacle": r.get("pinnacle"),
            "fetched_at": iso_z(datetime.now(timezone.utc)),
        }
        out.append(r)

    if update_snapshot:
        st.session_state["candidate_snapshot_v41"] = new_snapshot
        st.session_state["last_auto_verify_utc_v41"] = iso_z(datetime.now(timezone.utc))

    return out


def auto_verify_summary(board: pd.DataFrame) -> Dict[str, Any]:
    if board.empty or "data_confidence" not in board.columns:
        return {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    counts = board["data_confidence"].value_counts().to_dict()
    return {
        "HIGH": int(counts.get("HIGH", 0)),
        "MEDIUM": int(counts.get("MEDIUM", 0)),
        "LOW": int(counts.get("LOW", 0)),
    }


# =========================================================
# ALIGNMENT LOCK — v4.2
# =========================================================
def log_lock_evaluation(row: Dict[str, Any], rules: Dict[str, Any]) -> Dict[str, Any]:
    """Final paper-log gate. Prevents logging weak picks even if they appear approved."""
    if not rules.get("alignment_lock_mode", True):
        return {
            "paper_log_allowed": True,
            "log_lock_status": "UNLOCKED — ALIGNMENT LOCK OFF",
            "log_lock_reason": "Alignment Lock is turned off.",
            "alignment_status_saved": alignment_status(get_public_proof(row)),
            "parlay_leg_status_saved": parlay_leg_status(get_public_proof(row)),
        }

    proof = get_public_proof(row)
    align = alignment_status(proof)
    parlay_status = parlay_leg_status(proof)
    conf = str(row.get("data_confidence", "")).upper()
    conf_score = int(safe_float(row.get("data_confidence_score", 0), 0))
    decision = str(row.get("decision", ""))

    # Source conflict always blocks. This is non-negotiable.
    if align.startswith("REJECT"):
        return {
            "paper_log_allowed": False,
            "log_lock_status": "LOCKED — SOURCE CONFLICT",
            "log_lock_reason": f"Saved 3-source alignment says: {align}. Do not paper-log.",
            "alignment_status_saved": align,
            "parlay_leg_status_saved": parlay_status,
        }

    # Only approved/elite rows can be logged through normal paper-log.
    if not ("APPROVED" in decision or "ELITE" in decision):
        return {
            "paper_log_allowed": False,
            "log_lock_status": "LOCKED — NOT APPROVED",
            "log_lock_reason": f"Decision is {decision}. Only approved/elite candidates can be paper-logged.",
            "alignment_status_saved": align,
            "parlay_leg_status_saved": parlay_status,
        }

    # Main automatic route: high confidence market data.
    if rules.get("require_high_confidence_to_log", True) and conf == "HIGH":
        return {
            "paper_log_allowed": True,
            "log_lock_status": "UNLOCKED — HIGH AUTO VERIFY",
            "log_lock_reason": f"Auto Verify is HIGH confidence ({conf_score}/100). Paper-log allowed.",
            "alignment_status_saved": align,
            "parlay_leg_status_saved": parlay_status,
        }

    # Manual proof route: full alignment can override non-high confidence.
    if rules.get("allow_full_alignment_override", True) and align == "FULL GOAT ALIGNMENT":
        return {
            "paper_log_allowed": True,
            "log_lock_status": "UNLOCKED — FULL GOAT ALIGNMENT",
            "log_lock_reason": "Saved 3-source proof has FULL GOAT ALIGNMENT. Paper-log allowed.",
            "alignment_status_saved": align,
            "parlay_leg_status_saved": parlay_status,
        }

    # Optional watchlist route; default OFF.
    if rules.get("allow_partial_alignment_watchlist_log", False) and align == "PARTIAL ALIGNMENT":
        return {
            "paper_log_allowed": True,
            "log_lock_status": "UNLOCKED — PARTIAL WATCHLIST LOG",
            "log_lock_reason": "Partial alignment saved. This is a watchlist paper-log only, not a strong pick.",
            "alignment_status_saved": align,
            "parlay_leg_status_saved": parlay_status,
        }

    reason_bits = []
    if conf != "HIGH":
        reason_bits.append(f"Auto Verify is {conf or 'UNKNOWN'} ({conf_score}/100), not HIGH")
    if align != "FULL GOAT ALIGNMENT":
        reason_bits.append(f"3-source alignment is {align}")
    if not reason_bits:
        reason_bits.append("Alignment Lock did not find a valid unlock condition")

    return {
        "paper_log_allowed": False,
        "log_lock_status": "LOCKED — NEED HIGH CONFIDENCE OR FULL ALIGNMENT",
        "log_lock_reason": " | ".join(reason_bits),
        "alignment_status_saved": align,
        "parlay_leg_status_saved": parlay_status,
    }


def apply_alignment_lock_to_rows(rows: List[Dict[str, Any]], rules: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for row in rows:
        r = dict(row)
        lock = log_lock_evaluation(r, rules)
        r.update(lock)
        out.append(r)
    return out


def alignment_lock_summary(board: pd.DataFrame) -> Dict[str, int]:
    if board.empty or "paper_log_allowed" not in board.columns:
        return {"UNLOCKED": 0, "LOCKED": 0}
    unlocked = int(board["paper_log_allowed"].fillna(False).astype(bool).sum())
    return {"UNLOCKED": unlocked, "LOCKED": int(len(board) - unlocked)}


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



def agreement_bucket(value: str) -> str:
    """Classify each public/source proof result into exact, lean, conflict, neutral, or missing."""
    v = str(value or "Not set").lower()
    if v in ("not set", ""):
        return "missing"
    if "opposite" in v or "disagree" in v:
        return "conflict"
    if "same team ml" in v or "agrees" in v or "candidate" in v:
        return "exact"
    if "spread" in v or "-1.5" in v or "run line" in v:
        return "lean"
    if "over" in v or "under" in v or "parlay" in v:
        return "neutral"
    if "no clear" in v:
        return "neutral"
    return "neutral"


def source_agrees(value: str) -> bool:
    return agreement_bucket(value) in ("exact", "lean")


def source_conflicts(value: str) -> bool:
    return agreement_bucket(value) == "conflict"


def alignment_status(proof: Dict[str, Any]) -> str:
    if not proof:
        return "NOT CHECKED"

    sa_checked = bool(proof.get("sa_checked", False))
    sa_home_fav = bool(proof.get("sa_home_favourite", False))
    sa_win_pct = safe_float(proof.get("sa_win_pct", 0), 0)
    sa_pick = proof.get("sa_pick_agreement", "Not set")

    scp_checked = bool(proof.get("scp_checked", proof.get("checked", False)))
    scp_agreement = proof.get("scp_agreement", proof.get("agreement", "Not set"))

    pp_checked = bool(proof.get("pp_checked", False))
    pp_agreement = proof.get("pp_agreement", "Not set")

    threshold = safe_float(proof.get("sa_win_threshold", 60), 60)

    has_conflict = (
        bool(proof.get("public_heavy", False))
        or source_conflicts(sa_pick)
        or source_conflicts(scp_agreement)
        or source_conflicts(pp_agreement)
    )

    sports_alerts_ok = (
        sa_checked
        and sa_home_fav
        and sa_win_pct >= threshold
        and source_agrees(sa_pick)
    )

    scp_ok = scp_checked and source_agrees(scp_agreement)
    pp_ok = pp_checked and source_agrees(pp_agreement)

    if has_conflict:
        return "REJECT — SOURCE CONFLICT"
    if sports_alerts_ok and scp_ok and pp_ok:
        return "FULL GOAT ALIGNMENT"
    if sports_alerts_ok and (scp_ok or pp_ok):
        return "PARTIAL ALIGNMENT"
    if sports_alerts_ok:
        return "SPORTS ALERTS ONLY — WATCHLIST"
    return "WATCHLIST ONLY"


def parlay_leg_status(proof: Dict[str, Any]) -> str:
    status = alignment_status(proof)
    if status == "FULL GOAT ALIGNMENT":
        return "PARLAY LEG ELIGIBLE — paper only, max 2 legs"
    return "NOT PARLAY ELIGIBLE"


def proof_summary_text(proof: Dict[str, Any]) -> str:
    if not proof:
        return "3-source alignment: not checked"

    # Backward compatible with v3.9/v3.9.1 saved SCP-only proofs.
    sa_checked = bool(proof.get("sa_checked", False))
    sa_home_fav = bool(proof.get("sa_home_favourite", False))
    sa_win_pct = proof.get("sa_win_pct", "")
    sa_pick = proof.get("sa_pick_agreement", "Not set")

    scp_checked = bool(proof.get("scp_checked", proof.get("checked", False)))
    scp_agreement = proof.get("scp_agreement", proof.get("agreement", "Not set"))

    pp_checked = bool(proof.get("pp_checked", False))
    pp_agreement = proof.get("pp_agreement", "Not set")

    status = alignment_status(proof)
    parlay_status = parlay_leg_status(proof)
    public = "PUBLIC-HEAVY RISK" if proof.get("public_heavy") else "no public-heavy risk"

    sa_text = f"Sports Alerts: {'checked' if sa_checked else 'not checked'} | home fav: {'yes' if sa_home_fav else 'no'} | win%: {sa_win_pct} | {sa_pick}"
    scp_text = f"SCP: {'checked' if scp_checked else 'not checked'} | {scp_agreement}"
    pp_text = f"Picks & Parlays: {'checked' if pp_checked else 'not checked'} | {pp_agreement}"

    return f"{status} || {sa_text} || {scp_text} || {pp_text} || {public} || {parlay_status}"


def render_public_proof_badge(row: Dict[str, Any]) -> None:
    proof = get_public_proof(row)
    summary = proof_summary_text(proof)
    status = alignment_status(proof)
    if status.startswith("REJECT"):
        st.error(summary)
    elif status == "FULL GOAT ALIGNMENT":
        st.success(summary)
    elif "PARTIAL" in status or "SPORTS ALERTS ONLY" in status:
        st.warning(summary)
    elif proof:
        st.info(summary)
    else:
        st.caption(summary)


def main():
    st.title("🐐 GOAT Shield Live v4.4.5 LOOSE BOOKS")
    st.caption("Looser dynamic bookmaker thresholds. Major sports need 8 books, WNBA/MLS 6, college 5, other 4, plus sharp/core support. Paper-only.")

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
        rules.setdefault("auto_verify_mode", True)
        rules.setdefault("lock_low_confidence", True)
        rules.setdefault("min_data_confidence_score", 75)
        rules.setdefault("max_stale_seconds", 180)
        rules.setdefault("max_line_move_pct", 3.0)
        rules.setdefault("alignment_lock_mode", True)
        rules.setdefault("require_high_confidence_to_log", True)
        rules.setdefault("allow_full_alignment_override", True)
        rules.setdefault("allow_partial_alignment_watchlist_log", False)
        rules.setdefault("post_start_grace_minutes", 5)
        rules.setdefault("hide_after_post_start_grace", True)
        rules.setdefault("picks_mode_high_conf_only", True)
        rules["min_decimal_odds"] = st.number_input("Min NZD decimal odds", 1.01, 10.0, float(rules["min_decimal_odds"]), 0.01)
        rules["max_decimal_odds"] = st.number_input("Max NZD decimal odds", 1.01, 10.0, float(rules["max_decimal_odds"]), 0.01)
        rules.setdefault("dynamic_book_thresholds", True)
        rules.setdefault("major_sport_books", 10)
        rules.setdefault("mid_sport_books", 8)
        rules.setdefault("college_sport_books", 6)
        rules.setdefault("other_sport_books", 5)

        rules["dynamic_book_thresholds"] = st.checkbox(
            "Dynamic bookmaker threshold by sport",
            bool(rules.get("dynamic_book_thresholds", True)),
            help="Major US sports need more books; WNBA/MLS and college need slightly fewer due coverage.",
        )

        if rules.get("dynamic_book_thresholds", True):
            st.caption("Looser dynamic defaults: MLB/NBA/NFL/NHL = 8 books, WNBA/MLS = 6, college = 5, other = 4.")
            rules["major_sport_books"] = st.number_input("Major sports required books", 5, 30, int(rules.get("major_sport_books", 8)), 1)
            rules["mid_sport_books"] = st.number_input("WNBA/MLS required books", 5, 30, int(rules.get("mid_sport_books", 6)), 1)
            rules["college_sport_books"] = st.number_input("College required books", 4, 30, int(rules.get("college_sport_books", 5)), 1)
            rules["other_sport_books"] = st.number_input("Other sports required books", 3, 30, int(rules.get("other_sport_books", 4)), 1)
            rules["min_books_compared"] = int(rules.get("major_sport_books", 8))
        else:
            rules["min_books_compared"] = st.number_input("Minimum bookmakers compared", 1, 30, int(rules["min_books_compared"]), 1)

        rules["require_sharp_support"] = st.checkbox(
            "Require sharp/core bookmaker support",
            bool(rules.get("require_sharp_support", True)),
            help="Sharp/core support means Pinnacle reference or detected books/exchanges like Circa, BookMaker/CRIS, Betfair/Matchbook/Smarkets when available.",
        )
        rules["min_sharp_books"] = st.number_input(
            "Minimum sharp/core sources",
            0,
            5,
            int(rules.get("min_sharp_books", 1)),
            1,
        )
        rules["retail_only_warning"] = st.checkbox(
            "Warn/penalise retail-only support",
            bool(rules.get("retail_only_warning", True)),
        )
        rules["max_daily"] = st.number_input("Max approved paper picks per NZ day", 1, 20, int(rules["max_daily"]), 1)
        rules["lock_losses"] = st.number_input("Loss-streak lockout", 1, 20, int(rules["lock_losses"]), 1)
        rules["min_minutes_before_start"] = st.number_input(
            "Lock before start within minutes",
            0,
            240,
            int(rules["min_minutes_before_start"]),
            5,
            help="Default is 0 in v4.4 because Picks Mode can show picks until start, and up to 5 minutes after start.",
        )
        rules["post_start_grace_minutes"] = st.number_input(
            "Show picks until minutes after start",
            0,
            30,
            int(rules.get("post_start_grace_minutes", 5)),
            1,
            help="User rule: after this window, the game is hidden and no pick is shown.",
        )
        rules["hide_after_post_start_grace"] = st.checkbox(
            "Hide games after grace window",
            bool(rules.get("hide_after_post_start_grace", True)),
        )
        rules["picks_mode_high_conf_only"] = st.checkbox(
            "Picks tab: HIGH confidence only",
            bool(rules.get("picks_mode_high_conf_only", True)),
        )
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

        st.markdown("### Auto Verify")
        rules["auto_verify_mode"] = st.checkbox(
            "Auto Verify + Data Confidence Mode",
            bool(rules.get("auto_verify_mode", True)),
            help="Uses odds source quality, Pinnacle, bookmaker count, line movement, freshness, and home-favourite rules.",
        )
        rules["lock_low_confidence"] = st.checkbox(
            "Block approved picks if confidence is low",
            bool(rules.get("lock_low_confidence", True)),
        )
        rules["min_data_confidence_score"] = st.slider(
            "Minimum data confidence score",
            50,
            95,
            int(rules.get("min_data_confidence_score", 75)),
            5,
        )
        rules["max_stale_seconds"] = st.number_input(
            "Max bookmaker data age in seconds",
            30,
            1800,
            int(rules.get("max_stale_seconds", 180)),
            30,
        )
        rules["max_line_move_pct"] = st.number_input(
            "Max allowed line move % between refreshes",
            0.25,
            20.0,
            float(rules.get("max_line_move_pct", 3.0)),
            0.25,
        )
        st.caption("For best verification, press Fetch twice 30–60 seconds apart and compare line movement.")

        st.markdown("### Alignment Lock")
        rules["alignment_lock_mode"] = st.checkbox(
            "Alignment Lock for paper-log",
            bool(rules.get("alignment_lock_mode", True)),
            help="Stops paper logging unless the pick has HIGH Auto Verify or saved FULL GOAT Alignment.",
        )
        rules["require_high_confidence_to_log"] = st.checkbox(
            "Unlock paper-log with HIGH Auto Verify",
            bool(rules.get("require_high_confidence_to_log", True)),
        )
        rules["allow_full_alignment_override"] = st.checkbox(
            "Unlock paper-log with FULL GOAT Alignment",
            bool(rules.get("allow_full_alignment_override", True)),
        )
        rules["allow_partial_alignment_watchlist_log"] = st.checkbox(
            "Allow PARTIAL alignment watchlist logs",
            bool(rules.get("allow_partial_alignment_watchlist_log", False)),
            help="Default OFF. Keep this off unless you only want to test weak watchlist picks on paper.",
        )

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

    tabs = st.tabs(["🇳🇿 NZ Bettor Board", "🎯 Picks", "📱 Mobile Cards", "🛡️ Auto Verify", "🔒 Alignment Lock", "🧠 3-Source Alignment", "🟢 Best Price Board", "📒 Paper Log", "✅ Results", "📊 Dashboard", "🛡️ Backup", "ℹ️ Health Check"])

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
            if start and rules.get("hide_after_post_start_grace", True):
                start_dt_for_grace = parse_api_datetime(start)
                if start_dt_for_grace is not None:
                    start = iso_z(start_dt_for_grace - timedelta(minutes=int(rules.get("post_start_grace_minutes", 5))))
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
            st.session_state["last_fetch_utc_v41"] = iso_z(datetime.now(timezone.utc))

            for e in errors:
                st.error(e)
            if metas:
                pin_note = f" Pinnacle reference matches: {len(st.session_state.get('pinnacle_ref_v38', {}))}." if rules.get("show_pinnacle_reference") else ""
                st.success(f"Fetched {len(events)} events. Requests used: {metas[-1].get('requests_used')}. Remaining: {metas[-1].get('requests_remaining')}.{pin_note}")

        events = st.session_state.get("events_v36", [])
        last_markets = st.session_state.get("markets_v36", markets)

        if events:
            pinnacle_ref = st.session_state.get("pinnacle_ref_v38", {}) if rules.get("show_pinnacle_reference") else {}
            candidates = build_candidates(events, last_markets, int(rules["min_minutes_before_start"]), pinnacle_ref, int(rules.get("post_start_grace_minutes", 5)))
            rows = []
            app_count = approved_today_count(log_df)
            streak = loss_streak_count(log_df)

            for c in candidates:
                decision, score, bucket, reason, plain, action, score_parts = decide(c, rules, flags, app_count, streak)
                r = dict(c)
                r.update({"decision": decision, "score": score, "reject_bucket": bucket, "reasons": reason, "plain_explanation": plain, "action": action, "score_parts": score_parts})
                rows.append(r)

            rows = apply_auto_verify_to_rows(rows, rules, update_snapshot=True)
            rows = apply_alignment_lock_to_rows(rows, rules)

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

                conf_counts = auto_verify_summary(pd.DataFrame(rows))
                v1, v2, v3, v4 = st.columns(4)
                v1.metric("Data confidence HIGH", conf_counts.get("HIGH", 0))
                v2.metric("MEDIUM", conf_counts.get("MEDIUM", 0))
                v3.metric("LOW", conf_counts.get("LOW", 0))
                v4.metric("Last fetch NZ", fmt_dt(parse_api_datetime(st.session_state.get("last_fetch_utc_v41", "")), NZ_TZ, "NZ") if st.session_state.get("last_fetch_utc_v41") else "—")
                lock_counts = alignment_lock_summary(pd.DataFrame(rows))
                l1, l2 = st.columns(2)
                l1.metric("Paper-log unlocked", lock_counts.get("UNLOCKED", 0))
                l2.metric("Paper-log locked", lock_counts.get("LOCKED", 0))
                st.caption("Sources: The Odds API market odds + The Odds API Pinnacle reference + internal implied probability, home favourite, freshness, and line-movement checks. Alignment Lock controls paper logging.")

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
                    "market_win_pct", "best_implied_win_pct",
                    "pinnacle", "pinnacle_gap_pct", "pinnacle_status", "sharp_status", "sharp_core_count", "sharp_books", "retail_books_count", "tab_betcha", "bet365",
                    "data_confidence", "data_confidence_score", "data_age", "line_stability",
                    "log_lock_status", "log_lock_reason", "alignment_status_saved", "parlay_leg_status_saved",
                    "price_lift_pct", "books", "required_books", "book_threshold_group", "reasons", "data_confidence_reasons", "score_parts", "all_prices",
                ]
                st.dataframe(
                    board[cols].style.format({"best_odds": "{:.2f}", "avg_odds": "{:.2f}", "market_win_pct": "{:.2f}%", "best_implied_win_pct": "{:.2f}%", "price_lift_pct": "{:.2f}%", "pinnacle": "{:.2f}", "pinnacle_gap_pct": "{:.2f}%"}),
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
                        missed[["decision", "score", "data_confidence", "data_confidence_score", "plain_explanation", "action", "time_status", "starts_in", "start_nz", "start_et", "market_label", "pick", "best_odds", "best_bookmaker", "pinnacle", "pinnacle_gap_pct", "pinnacle_status", "sharp_status", "sharp_books", "books", "required_books", "book_threshold_group", "price_lift_pct", "reasons", "data_confidence_reasons", "all_prices"]].style.format({"best_odds": "{:.2f}", "pinnacle": "{:.2f}", "pinnacle_gap_pct": "{:.2f}%", "price_lift_pct": "{:.2f}%"}),
                        use_container_width=True,
                        hide_index=True,
                    )

                approved_rows = board[board["decision"].astype(str).str.contains("APPROVED|ELITE", regex=True, na=False)]
                if not approved_rows.empty:
                    # Alignment Lock controls which approved/elite rows are allowed to be paper-logged.
                    if "paper_log_allowed" in approved_rows.columns:
                        unlocked_rows = approved_rows[approved_rows["paper_log_allowed"].fillna(False).astype(bool)]
                        locked_rows = approved_rows[~approved_rows["paper_log_allowed"].fillna(False).astype(bool)]
                    else:
                        unlocked_rows = approved_rows.copy()
                        locked_rows = approved_rows.iloc[0:0].copy()

                    if not locked_rows.empty:
                        st.warning(f"Alignment Lock blocked {len(locked_rows)} approved/elite candidate(s) from paper-log.")
                        st.dataframe(
                            locked_rows[["decision", "score", "data_confidence", "data_confidence_score", "pick", "best_odds", "sharp_status", "sharp_books", "books", "required_books", "book_threshold_group", "log_lock_status", "log_lock_reason", "alignment_status_saved"]].style.format({"best_odds": "{:.2f}"}),
                            use_container_width=True,
                            hide_index=True,
                        )

                    if unlocked_rows.empty:
                        st.error("No approved/elite candidate is unlocked for paper-log. Need HIGH Auto Verify or saved FULL GOAT Alignment.")
                    else:
                        labels = unlocked_rows.apply(
                            lambda x: f"{x.name}: {x['log_lock_status']} — {x['decision']} — {x['pick']} @ {x['best_odds']} ({x['best_bookmaker']}) — {x['start_nz']}",
                            axis=1
                        ).tolist()

                        choice = st.selectbox("Unlocked approved/elite candidate to paper-log", labels)
                        idx = int(choice.split(":")[0])
                        chosen_preview = board.loc[idx].to_dict()
                        st.success(f"Paper-log unlocked: {chosen_preview.get('log_lock_status')} — {chosen_preview.get('log_lock_reason')}")

                        if st.button("Log selected as PAPER pick"):
                            chosen = board.loc[idx].to_dict()
                            log_row = {
                                "created_at": iso_z(datetime.now(timezone.utc)),
                                "nz_date": chosen.get("nz_date", ""),
                                "us_et_date": chosen.get("us_et_date", ""),
                                "start_nz": chosen.get("start_nz", ""),
                                "start_et": chosen.get("start_et", ""),
                                "starts_in": chosen.get("starts_in", ""),
                                "sport": chosen.get("sport", ""),
                                "game": chosen.get("game", ""),
                                "market": chosen.get("market_label", ""),
                                "pick_label": chosen.get("pick", ""),
                                "best_odds": chosen.get("best_odds", ""),
                                "best_bookmaker": chosen.get("best_bookmaker", ""),
                                "avg_odds": chosen.get("avg_odds", ""),
                                "pinnacle": chosen.get("pinnacle", ""),
                                "pinnacle_gap_pct": chosen.get("pinnacle_gap_pct", ""),
                                "pinnacle_status": chosen.get("pinnacle_status", ""),
                                "sharp_status": chosen.get("sharp_status", ""),
                                "sharp_core_count": chosen.get("sharp_core_count", ""),
                                "sharp_books": chosen.get("sharp_books", ""),
                                "retail_books_count": chosen.get("retail_books_count", ""),
                                "books": chosen.get("books", ""),
                                "required_books": chosen.get("required_books", ""),
                                "book_threshold_group": chosen.get("book_threshold_group", ""),
                                "tab_betcha": chosen.get("tab_betcha", ""),
                                "bet365": chosen.get("bet365", ""),
                                "price_lift_pct": chosen.get("price_lift_pct", ""),
                                "decision": chosen.get("decision", ""),
                                "score": chosen.get("score", ""),
                                "plain_explanation": chosen.get("plain_explanation", ""),
                                "score_parts": chosen.get("score_parts", ""),
                                "data_confidence": chosen.get("data_confidence", ""),
                                "data_confidence_score": chosen.get("data_confidence_score", ""),
                                "data_confidence_reasons": chosen.get("data_confidence_reasons", ""),
                                "market_win_pct": chosen.get("market_win_pct", ""),
                                "best_implied_win_pct": chosen.get("best_implied_win_pct", ""),
                                "line_stability": chosen.get("line_stability", ""),
                                "data_age": chosen.get("data_age", ""),
                                "log_lock_status": chosen.get("log_lock_status", ""),
                                "log_lock_reason": chosen.get("log_lock_reason", ""),
                                "alignment_status_saved": chosen.get("alignment_status_saved", ""),
                                "parlay_leg_status_saved": chosen.get("parlay_leg_status_saved", ""),
                                "three_source_alignment": proof_summary_text(get_public_proof(chosen)),
                                "result": "Pending",
                                "profit_units": "",
                                "closing_odds": "",
                                "closing_price_movement": "",
                                "all_prices": chosen.get("all_prices", ""),
                            }
                            new_df = pd.concat([pd.DataFrame([log_row]), log_df], ignore_index=True)
                            save_log(new_df)
                            st.success("Logged as paper pick only.")
                            st.rerun()

    with tabs[1]:
        st.subheader("🎯 Picks — qualifying paper picks only")
        st.warning("Paper-only. This tab is not a real-money betting screen. It only shows candidates that pass your current rules and Alignment Lock.")
        st.caption("v4.4.5 rule: looser dynamic bookmaker thresholds are ON. Major sports need 8 books, WNBA/MLS 6, college 5, other 4, plus sharp/core support.")

        events_picks = st.session_state.get("events_v36", [])
        last_markets_picks = st.session_state.get("markets_v36", markets)
        if not events_picks:
            st.info("Run Fetch NZ bettor board first. Then come back here and press Picks.")
        else:
            if st.button("🎯 Picks — show qualifying paper picks"):
                st.session_state["show_picks_v44"] = True

            if st.session_state.get("show_picks_v44", False):
                pinnacle_ref_picks = st.session_state.get("pinnacle_ref_v38", {}) if rules.get("show_pinnacle_reference") else {}
                candidates_picks = build_candidates(
                    events_picks,
                    last_markets_picks,
                    int(rules["min_minutes_before_start"]),
                    pinnacle_ref_picks,
                    int(rules.get("post_start_grace_minutes", 5)),
                )

                rows_picks = []
                app_count_picks = approved_today_count(log_df)
                streak_picks = loss_streak_count(log_df)

                for cand in candidates_picks:
                    decision, score, bucket, reason, plain, action, score_parts = decide(cand, rules, flags, app_count_picks, streak_picks)
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
                    rows_picks.append(rr)

                rows_picks = apply_auto_verify_to_rows(rows_picks, rules, update_snapshot=False)
                rows_picks = apply_alignment_lock_to_rows(rows_picks, rules)
                picks_df = pd.DataFrame(rows_picks)

                if picks_df.empty:
                    st.error("No qualifying games in the current window. Fetch again later.")
                else:
                    min_odds = float(rules.get("min_decimal_odds", 1.40))
                    max_odds = float(rules.get("max_decimal_odds", 1.90))

                    qualified = picks_df[
                        picks_df["decision"].astype(str).str.contains("APPROVED|ELITE", regex=True, na=False)
                        & picks_df["paper_log_allowed"].fillna(False).astype(bool)
                        & (picks_df["best_odds"].astype(float) >= min_odds)
                        & (picks_df["best_odds"].astype(float) <= max_odds)
                    ].copy()

                    if rules.get("picks_mode_high_conf_only", True):
                        qualified = qualified[qualified["data_confidence"].astype(str).str.upper() == "HIGH"].copy()

                    qualified = qualified.sort_values(["score", "data_confidence_score", "start_nz"], ascending=[False, False, True]).reset_index(drop=True)

                    p1, p2, p3, p4 = st.columns(4)
                    p1.metric("Qualifying paper picks", len(qualified))
                    p2.metric("Odds range", f"{min_odds:.2f}-{max_odds:.2f}")
                    p3.metric("Post-start grace", f"{int(rules.get('post_start_grace_minutes', 5))}m")
                    p4.metric("HIGH only", "ON" if rules.get("picks_mode_high_conf_only", True) else "OFF")

                    if qualified.empty:
                        st.error("No unlocked HIGH-confidence paper picks qualify right now. Good — the shield is blocking weak spots.")
                        st.caption("Try Fetch again closer to game time, but do not chase.")
                    else:
                        st.success("These are the current qualifying PAPER picks. They passed odds range, Auto Verify, and Alignment Lock.")
                        show_cols = [
                            "decision", "score", "data_confidence", "data_confidence_score",
                            "sport", "game", "market_label", "pick", "best_odds", "best_bookmaker",
                            "market_win_pct", "pinnacle", "pinnacle_gap_pct", "sharp_status", "sharp_core_count", "sharp_books", "books", "required_books", "book_threshold_group",
                            "time_status", "starts_in", "start_nz", "start_et",
                            "log_lock_status", "log_lock_reason",
                        ]
                        st.dataframe(
                            qualified[show_cols].style.format({
                                "best_odds": "{:.2f}",
                                "market_win_pct": "{:.2f}%",
                                "pinnacle": "{:.2f}",
                                "pinnacle_gap_pct": "{:.2f}%",
                            }),
                            use_container_width=True,
                            hide_index=True,
                        )

                        st.markdown("### Quick cards")
                        max_cards = min(len(qualified), 10)
                        for i in range(max_cards):
                            row = qualified.iloc[i].to_dict()
                            with st.container(border=True):
                                st.markdown(f"### {i+1}. {row.get('pick', '')} @ {safe_float(row.get('best_odds', 0), 0):.2f}")
                                st.markdown(f"**Game:** {row.get('game', '')}")
                                st.markdown(f"**Sport/Market:** {row.get('sport', '')} • {row.get('market_label', '')}")
                                st.markdown(f"**Book:** {row.get('best_bookmaker', '')}")
                                st.markdown(f"**Auto Verify:** {row.get('data_confidence', '')} ({row.get('data_confidence_score', '')}/100)")
                                st.markdown(f"**Sharp/Core:** {row.get('sharp_status', 'Unknown')}")
                                st.markdown(f"**Book coverage:** {row.get('books', 0)} / required {row.get('required_books', '?')} ({row.get('book_threshold_group', '')})")
                                st.markdown(f"**Time:** {row.get('time_status', '')} • starts in {row.get('starts_in', '')}")
                                st.caption(f"Lock: {row.get('log_lock_status', '')} — {row.get('log_lock_reason', '')}")

                        st.warning("Daily discipline: showing many qualifying paper picks does not mean you should bet them. For proof-building, log paper only and keep your daily limit.")

    with tabs[2]:
        st.subheader("📱 Mobile Cards")
        st.write("Clean iPhone view with no-edge GOAT Score, plain-English explanations, and clear action labels.")
        events_cards = st.session_state.get("events_v36", [])
        last_markets_cards = st.session_state.get("markets_v36", markets)
        if not events_cards:
            st.info("Run Fetch NZ bettor board first, then come here.")
        else:
            pinnacle_ref_cards = st.session_state.get("pinnacle_ref_v38", {}) if rules.get("show_pinnacle_reference") else {}
            candidates_cards = build_candidates(events_cards, last_markets_cards, int(rules["min_minutes_before_start"]), pinnacle_ref_cards, int(rules.get("post_start_grace_minutes", 5)))
            rows_cards = []
            app_count_cards = approved_today_count(log_df)
            streak_cards = loss_streak_count(log_df)
            for cand in candidates_cards:
                decision, score, bucket, reason, plain, action, score_parts = decide(cand, rules, flags, app_count_cards, streak_cards)
                rr = dict(cand)
                rr.update({"decision": decision, "score": score, "reject_bucket": bucket, "reasons": reason, "plain_explanation": plain, "action": action, "score_parts": score_parts})
                rows_cards.append(rr)
            rows_cards = apply_auto_verify_to_rows(rows_cards, rules, update_snapshot=False)
            rows_cards = apply_alignment_lock_to_rows(rows_cards, rules)
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

    with tabs[3]:
        st.subheader("🛡️ Auto Verify + Data Confidence")
        st.info("This is the automatic source-quality check. It does not use Sports Alerts, Sports Chat Place, or Picks & Parlays as required inputs.")

        events_verify = st.session_state.get("events_v36", [])
        last_markets_verify = st.session_state.get("markets_v36", markets)
        if not events_verify:
            st.info("Run Fetch NZ bettor board first, then come here.")
        else:
            pinnacle_ref_verify = st.session_state.get("pinnacle_ref_v38", {}) if rules.get("show_pinnacle_reference") else {}
            candidates_verify = build_candidates(events_verify, last_markets_verify, int(rules["min_minutes_before_start"]), pinnacle_ref_verify, int(rules.get("post_start_grace_minutes", 5)))

            rows_verify = []
            app_count_verify = approved_today_count(log_df)
            streak_verify = loss_streak_count(log_df)

            for cand in candidates_verify:
                decision, score, bucket, reason, plain, action, score_parts = decide(cand, rules, flags, app_count_verify, streak_verify)
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
                rows_verify.append(rr)

            rows_verify = apply_auto_verify_to_rows(rows_verify, rules, update_snapshot=False)
            rows_verify = apply_alignment_lock_to_rows(rows_verify, rules)
            verify_df = pd.DataFrame(rows_verify)
            if verify_df.empty:
                st.warning("No candidates to verify.")
            else:
                counts = auto_verify_summary(verify_df)
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("HIGH", counts.get("HIGH", 0))
                c2.metric("MEDIUM", counts.get("MEDIUM", 0))
                c3.metric("LOW", counts.get("LOW", 0))
                c4.metric("Last fetch", fmt_dt(parse_api_datetime(st.session_state.get("last_fetch_utc_v41", "")), NZ_TZ, "NZ") if st.session_state.get("last_fetch_utc_v41") else "—")

                st.markdown("#### Source hierarchy")
                st.write("Tier 1: The Odds API market odds and bookmaker data")
                st.write("Tier 2: Pinnacle reference via The Odds API")
                st.write("Tier 3: Internal calculated market-implied win %, home favourite, freshness, and line movement")
                st.write("Tier 4: Sports Alerts / Sports Chat Place / Picks & Parlays are optional manual notes only")

                verify_df["sort"] = verify_df["data_confidence"].map({"HIGH": 0, "MEDIUM": 1, "LOW": 2}).fillna(9)
                verify_df = verify_df.sort_values(["sort", "data_confidence_score", "score"], ascending=[True, False, False]).reset_index(drop=True)
                show_cols = [
                    "data_confidence", "data_confidence_score", "decision", "score",
                    "sport", "game", "pick", "best_odds", "market_win_pct", "best_implied_win_pct",
                    "pinnacle", "pinnacle_gap_pct", "sharp_status", "sharp_core_count", "sharp_books", "books", "required_books", "book_threshold_group", "data_age", "line_stability",
                    "log_lock_status", "log_lock_reason",
                    "data_confidence_reasons", "data_confidence_passed",
                ]
                st.dataframe(
                    verify_df[show_cols].style.format({
                        "best_odds": "{:.2f}",
                        "market_win_pct": "{:.2f}%",
                        "best_implied_win_pct": "{:.2f}%",
                        "pinnacle": "{:.2f}",
                        "pinnacle_gap_pct": "{:.2f}%",
                    }),
                    use_container_width=True,
                    hide_index=True,
                )

                st.caption("Tip: press Fetch again after 30–60 seconds. Auto Verify will compare line movement since the previous fetch.")

    with tabs[4]:
        st.subheader("🔒 Alignment Lock")
        st.info("This is the final paper-log gate. A pick must be approved/elite AND unlocked by HIGH Auto Verify or FULL GOAT Alignment.")

        events_lock = st.session_state.get("events_v36", [])
        last_markets_lock = st.session_state.get("markets_v36", markets)
        if not events_lock:
            st.info("Run Fetch NZ bettor board first, then come here.")
        else:
            pinnacle_ref_lock = st.session_state.get("pinnacle_ref_v38", {}) if rules.get("show_pinnacle_reference") else {}
            candidates_lock = build_candidates(events_lock, last_markets_lock, int(rules["min_minutes_before_start"]), pinnacle_ref_lock, int(rules.get("post_start_grace_minutes", 5)))

            rows_lock = []
            app_count_lock = approved_today_count(log_df)
            streak_lock = loss_streak_count(log_df)

            for cand in candidates_lock:
                decision, score, bucket, reason, plain, action, score_parts = decide(cand, rules, flags, app_count_lock, streak_lock)
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
                rows_lock.append(rr)

            rows_lock = apply_auto_verify_to_rows(rows_lock, rules, update_snapshot=False)
            rows_lock = apply_alignment_lock_to_rows(rows_lock, rules)
            lock_df = pd.DataFrame(rows_lock)

            if lock_df.empty:
                st.warning("No candidates found.")
            else:
                approved_lock_df = lock_df[lock_df["decision"].astype(str).str.contains("APPROVED|ELITE", regex=True, na=False)].copy()
                if approved_lock_df.empty:
                    st.info("No approved/elite candidates to unlock.")
                else:
                    counts = alignment_lock_summary(approved_lock_df)
                    c1, c2 = st.columns(2)
                    c1.metric("Unlocked approved/elite", counts.get("UNLOCKED", 0))
                    c2.metric("Locked approved/elite", counts.get("LOCKED", 0))

                    show_cols = [
                        "paper_log_allowed", "log_lock_status", "decision", "score",
                        "data_confidence", "data_confidence_score",
                        "alignment_status_saved", "parlay_leg_status_saved",
                        "sport", "game", "pick", "best_odds", "pinnacle", "pinnacle_gap_pct",
                        "sharp_status", "sharp_core_count", "sharp_books", "books", "required_books", "book_threshold_group",
                        "log_lock_reason",
                    ]
                    st.dataframe(
                        approved_lock_df[show_cols].style.format({"best_odds": "{:.2f}", "pinnacle": "{:.2f}", "pinnacle_gap_pct": "{:.2f}%"}),
                        use_container_width=True,
                        hide_index=True,
                    )

                    st.caption("Rule: source conflict always blocks. HIGH Auto Verify unlocks. FULL GOAT Alignment unlocks. Partial alignment is watchlist only unless you enable partial watchlist logs in settings.")

    with tabs[5]:
        st.subheader("🧠 3-Source Alignment")
        st.info("Use this exactly like your manual system: Sports Alerts first, then Sports Chat Place, then Picks & Parlays. This does not scrape public-pick sites and does not make them the main decision maker.")

        events_proof = st.session_state.get("events_v36", [])
        last_markets_proof = st.session_state.get("markets_v36", markets)
        if not events_proof:
            st.info("Run Fetch NZ bettor board first, then come here.")
        else:
            pinnacle_ref_proof = st.session_state.get("pinnacle_ref_v38", {}) if rules.get("show_pinnacle_reference") else {}
            candidates_proof = build_candidates(events_proof, last_markets_proof, int(rules["min_minutes_before_start"]), pinnacle_ref_proof, int(rules.get("post_start_grace_minutes", 5)))

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

            rows_proof = apply_auto_verify_to_rows(rows_proof, rules, update_snapshot=False)
            rows_proof = apply_alignment_lock_to_rows(rows_proof, rules)

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

                st.markdown("#### Manual 3-source alignment")
                key = proof_key(chosen)
                existing = load_public_proofs().get(key, {})

                agreement_options = [
                    "Not set",
                    "Same team ML / candidate agrees",
                    "Same team spread / -1.5 / run line lean",
                    "Disagrees / opposite side",
                    "Pick is Over",
                    "Pick is Under",
                    "Pick is Parlay only",
                    "No clear pick found",
                ]

                st.markdown("##### 1) Sports Alerts")
                st.caption("This is your first filter: game today, home favourite, high win %, acceptable odds.")

                sa_checked = st.checkbox(
                    "Sports Alerts checked for this exact game/date",
                    value=bool(existing.get("sa_checked", False)),
                )
                st.write(f"App candidate home pick: {'YES' if chosen.get('home_pick') else 'NO'} | App home favourite: {'YES' if chosen.get('home_fav') else 'NO'}")
                sa_home_favourite = st.checkbox(
                    "Sports Alerts says this team is home favourite",
                    value=bool(existing.get("sa_home_favourite", bool(chosen.get("home_fav", False)))),
                )
                sa_win_pct = st.number_input(
                    "Sports Alerts win percentage",
                    min_value=0.0,
                    max_value=100.0,
                    value=float(existing.get("sa_win_pct", 0.0) or 0.0),
                    step=1.0,
                )
                sa_win_threshold = st.number_input(
                    "Minimum win % required",
                    min_value=50.0,
                    max_value=90.0,
                    value=float(existing.get("sa_win_threshold", 60.0) or 60.0),
                    step=1.0,
                )
                sa_odds = st.number_input(
                    "Sports Alerts decimal odds shown",
                    min_value=0.0,
                    max_value=20.0,
                    value=float(existing.get("sa_odds", 0.0) or 0.0),
                    step=0.01,
                )
                sa_existing = existing.get("sa_pick_agreement", "Not set")
                sa_pick_agreement = st.selectbox(
                    "Sports Alerts pick compared with our candidate",
                    agreement_options,
                    index=agreement_options.index(sa_existing) if sa_existing in agreement_options else 0,
                )

                st.markdown("##### 2) Sports Chat Place")
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

                st.markdown("##### 3) Picks & Parlays")
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

                preview_proof = {
                    "sa_checked": sa_checked,
                    "sa_home_favourite": sa_home_favourite,
                    "sa_win_pct": sa_win_pct,
                    "sa_win_threshold": sa_win_threshold,
                    "sa_odds": sa_odds,
                    "sa_pick_agreement": sa_pick_agreement,
                    "scp_checked": scp_checked,
                    "scp_agreement": scp_agreement,
                    "scp_url": scp_url,
                    "pp_checked": pp_checked,
                    "pp_agreement": pp_agreement,
                    "pp_url": pp_url,
                    "public_heavy": public_heavy,
                    "notes": notes,
                }

                st.markdown("#### Alignment verdict preview")
                preview_status = alignment_status(preview_proof)
                if preview_status == "FULL GOAT ALIGNMENT":
                    st.success(proof_summary_text(preview_proof))
                elif preview_status.startswith("REJECT"):
                    st.error(proof_summary_text(preview_proof))
                elif "PARTIAL" in preview_status or "SPORTS ALERTS ONLY" in preview_status:
                    st.warning(proof_summary_text(preview_proof))
                else:
                    st.info(proof_summary_text(preview_proof))

                if st.button("Save 3-source alignment for this pick"):
                    preview_proof["saved_at"] = iso_z(datetime.now(timezone.utc))
                    save_public_proof(key, preview_proof)
                    st.success("3-source alignment saved for this scan/session.")

                st.markdown("#### Saved alignment summary")
                st.write(proof_summary_text(load_public_proofs().get(key, {})))

                if public_heavy:
                    st.warning("Public-heavy risk marked. Treat this as a red flag. Do not turn this into a real bet.")

    with tabs[6]:
        st.subheader("🟢 Best Price Board")
        st.write("Use the NZ Bettor Board first. It includes all best-price board columns plus NZ/US time conversion.")
        st.caption("v3.3 keeps this tab as a simple explanation so the phone UI stays cleaner.")

    with tabs[7]:
        st.subheader("📒 Paper Log")
        df = load_log()
        if df.empty:
            st.info("No paper picks logged yet.")
        else:
            st.dataframe(df, use_container_width=True)
            st.download_button("Download CSV", df.to_csv(index=False).encode("utf-8"), "goat_shield_paper_log.csv", "text/csv")

    with tabs[8]:
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

    with tabs[9]:
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

    with tabs[10]:
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

    with tabs[11]:
        st.subheader("ℹ️ Health Check / About")
        st.write("This page tells you whether the app is running correctly, what each command does, and what to check before trusting any paper pick.")

        app_version = "GOAT Shield Live v4.4.5 LOOSE BOOKS"
        events_health = st.session_state.get("events_v36", [])
        markets_health = st.session_state.get("markets_v36", markets)
        metas_health = st.session_state.get("metas_v36", [])
        pinnacle_ref_health = st.session_state.get("pinnacle_ref_v38", {})
        last_fetch_raw = st.session_state.get("last_fetch_utc_v41", "")
        last_fetch_dt = parse_api_datetime(last_fetch_raw) if last_fetch_raw else None

        latest_meta = metas_health[-1] if metas_health else {}
        requests_used = latest_meta.get("requests_used", "—")
        requests_remaining = latest_meta.get("requests_remaining", "—")

        log_health = load_log()

        health_rows = []
        if events_health:
            try:
                pinnacle_ref_h = pinnacle_ref_health if rules.get("show_pinnacle_reference") else {}
                candidates_h = build_candidates(events_health, markets_health, int(rules["min_minutes_before_start"]), pinnacle_ref_h, int(rules.get("post_start_grace_minutes", 5)))

                app_count_h = approved_today_count(log_health)
                streak_h = loss_streak_count(log_health)

                for cand in candidates_h:
                    decision, score, bucket, reason, plain, action, score_parts = decide(cand, rules, flags, app_count_h, streak_h)
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
                    health_rows.append(rr)

                health_rows = apply_auto_verify_to_rows(health_rows, rules, update_snapshot=False)
                health_rows = apply_alignment_lock_to_rows(health_rows, rules)
            except Exception as e:
                st.error(f"Health check could not rebuild candidates: {e}")

        health_df = pd.DataFrame(health_rows)

        api_key_ok = bool(str(api_key).strip())
        fetch_ok = bool(events_health)
        pinnacle_matches = len(pinnacle_ref_health) if isinstance(pinnacle_ref_health, dict) else 0
        conf_counts = auto_verify_summary(health_df) if not health_df.empty else {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
        lock_counts = alignment_lock_summary(health_df) if not health_df.empty else {"UNLOCKED": 0, "LOCKED": 0}
        sharp_supported_count = int(health_df["sharp_support"].fillna(False).astype(bool).sum()) if not health_df.empty and "sharp_support" in health_df.columns else 0
        retail_only_count = int(len(health_df) - sharp_supported_count) if not health_df.empty else 0
        below_required_books = int((health_df["books"].fillna(0).astype(float) < health_df["required_books"].fillna(0).astype(float)).sum()) if not health_df.empty and "required_books" in health_df.columns else 0

        def status_text(ok: bool) -> str:
            return "✅ OK" if ok else "❌ CHECK"

        st.markdown("### App status")
        c1, c2, c3 = st.columns(3)
        c1.metric("Version", app_version.replace("GOAT Shield Live ", ""))
        c2.metric("API key", "Set" if api_key_ok else "Missing")
        c3.metric("Last fetch NZ", fmt_dt(last_fetch_dt, NZ_TZ, "NZ") if last_fetch_dt else "Not fetched yet")

        c4, c5, c6 = st.columns(3)
        c4.metric("Events loaded", len(events_health))
        c5.metric("Candidates rebuilt", len(health_rows))
        c6.metric("Markets", ", ".join(markets_health) if markets_health else "—")

        c7, c8, c9 = st.columns(3)
        c7.metric("Requests used", requests_used)
        c8.metric("Requests remaining", requests_remaining)
        c9.metric("Pinnacle matches", pinnacle_matches)

        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Sharp-supported candidates", sharp_supported_count)
        s2.metric("Retail-only candidates", retail_only_count)
        s3.metric("Below required books", below_required_books)
        s4.metric("Dynamic books", "ON" if rules.get("dynamic_book_thresholds", True) else "OFF")

        st.markdown("### Data confidence")
        d1, d2, d3, d4, d5 = st.columns(5)
        d1.metric("HIGH", conf_counts.get("HIGH", 0))
        d2.metric("MEDIUM", conf_counts.get("MEDIUM", 0))
        d3.metric("LOW", conf_counts.get("LOW", 0))
        d4.metric("Paper-log unlocked", lock_counts.get("UNLOCKED", 0))
        d5.metric("Paper-log locked", lock_counts.get("LOCKED", 0))

        st.markdown("### Health verdict")
        verdicts = []
        if not api_key_ok:
            verdicts.append(("❌ API key missing", "Add ODDS_API_KEY in Streamlit secrets."))
        if not fetch_ok:
            verdicts.append(("⚠️ No scan loaded", "Go to NZ Bettor Board and press Fetch NZ bettor board."))
        if fetch_ok and rules.get("show_pinnacle_reference") and pinnacle_matches == 0:
            verdicts.append(("⚠️ Pinnacle reference missing", "This can happen for some sports/markets, but if it is always zero, check provider coverage or settings."))
        if fetch_ok and len(health_rows) == 0:
            verdicts.append(("⚠️ No candidates rebuilt", "Check market/sport selection and API response."))
        if fetch_ok and conf_counts.get("HIGH", 0) == 0:
            verdicts.append(("⚠️ No HIGH confidence picks", "Do not paper-log unless Alignment Lock unlocks through FULL GOAT Alignment."))
        if fetch_ok and below_required_books > 0:
            verdicts.append(("ℹ️ Some candidates below sport-specific bookmaker threshold", "This is normal. Dynamic threshold is filtering by sport coverage instead of using one fixed number."))
        if fetch_ok and rules.get("require_sharp_support", True) and sharp_supported_count == 0:
            verdicts.append(("⚠️ No sharp/core support found", "Do not paper-log retail-only candidates. Check sport/region coverage or wait for better market depth."))
        if fetch_ok and lock_counts.get("UNLOCKED", 0) == 0:
            verdicts.append(("ℹ️ No paper-log unlocked picks", "This is not always bad. It means the shield is blocking weak candidates."))

        if not verdicts:
            st.success("✅ App looks healthy. Fetch worked, data loaded, confidence checks ran, bookmaker coverage checked, and Alignment Lock is active.")
        else:
            for title, detail in verdicts:
                if title.startswith("❌"):
                    st.error(f"{title} — {detail}")
                elif title.startswith("⚠️"):
                    st.warning(f"{title} — {detail}")
                else:
                    st.info(f"{title} — {detail}")

        st.markdown("### What every command/tab means")
        command_rows = [
            {"Command / Tab": "🇳🇿 NZ Bettor Board", "Meaning": "Main scan. Pulls games, odds, Pinnacle reference, Auto Verify, and Alignment Lock."},
            {"Command / Tab": "Fetch NZ bettor board", "Meaning": "Runs the scan. Press this first, then again after 30–60 seconds for line-movement comparison."},
            {"Command / Tab": "🎯 Picks", "Meaning": "Shows only qualifying unlocked paper picks in your 1.40-1.90 odds range, using dynamic bookmaker thresholds and sharp/core support."},
            {"Command / Tab": "📱 Mobile Cards", "Meaning": "Best iPhone view. Shows pick, odds, Pinnacle, Auto Verify, and Alignment Lock reason."},
            {"Command / Tab": "🛡️ Auto Verify", "Meaning": "Shows data confidence, data age, market win %, Pinnacle gap, sharp/core support, and line stability."},
            {"Command / Tab": "🔒 Alignment Lock", "Meaning": "Final gate. Shows which approved/elite picks are unlocked or blocked for paper-log."},
            {"Command / Tab": "🧠 3-Source Alignment", "Meaning": "Optional manual proof for Sports Alerts, Sports Chat Place, and Picks & Parlays."},
            {"Command / Tab": "🟢 Best Price Board", "Meaning": "Compares bookmaker prices so you can see best available odds."},
            {"Command / Tab": "📒 Paper Log", "Meaning": "Stores paper picks only. This is your proof record."},
            {"Command / Tab": "✅ Results", "Meaning": "Settle paper picks later as Win/Loss/Push and record closing odds."},
            {"Command / Tab": "📊 Dashboard", "Meaning": "Tracks proof over time: number of picks, ROI, CLV, and system verdict."},
            {"Command / Tab": "🛡️ Backup", "Meaning": "Download or restore your paper-log CSV."},
            {"Command / Tab": "ℹ️ Health Check", "Meaning": "This page. Checks if app, API, data, confidence, and lock system look healthy."},
        ]
        st.dataframe(pd.DataFrame(command_rows), use_container_width=True, hide_index=True)

        st.markdown("### Daily safe-use checklist")
        checklist = pd.DataFrame([
            {"Step": 1, "Check": "Confirm version says v4.4.5 LOOSE BOOKS", "Why": "Avoid running old broken files."},
            {"Step": 2, "Check": "Press Fetch NZ bettor board", "Why": "Loads latest games and odds."},
            {"Step": 3, "Check": "Wait 30–60 seconds and Fetch again", "Why": "Lets Auto Verify compare line movement."},
            {"Step": 4, "Check": "Pinnacle/sharp support and dynamic bookmaker thresholds look healthy", "Why": "Confirms sharp-reference and sport-specific bookmaker coverage when available."},
            {"Step": 5, "Check": "Open Picks, Auto Verify, and Alignment Lock", "Why": "Only use qualifying HIGH-confidence paper picks."},
            {"Step": 6, "Check": "Open Alignment Lock", "Why": "Only paper-log unlocked picks; hide games after +5 minutes."},
            {"Step": 7, "Check": "Log paper pick only", "Why": "Build 300-pick proof before real-money thinking."},
        ])
        st.dataframe(checklist, use_container_width=True, hide_index=True)

        st.markdown("### Good signs")
        st.success("Version correct • Fetch works • Requests remaining > 0 • Pinnacle references appear • HIGH confidence exists • Alignment Lock unlocks only clean picks • No red error box")

        st.markdown("### Bad signs")
        st.error("Old version • Red error box • Requests remaining = 0 • All confidence LOW • Game times look wrong • Pinnacle always zero • Paper Log button errors")

        st.markdown("### Source truth")
        st.write("Trusted automatic sources:")
        st.write("1. The Odds API market odds and bookmaker data")
        st.write("2. Pinnacle reference from The Odds API")
        st.write("3. Sharp/core support detection: Pinnacle reference plus returned books/exchanges such as Circa, BookMaker/CRIS, Betfair, Matchbook, or Smarkets when available")
        st.write("4. Dynamic bookmaker threshold: MLB/NBA/NFL/NHL need 8, WNBA/MLS need 6, college need 5, other sports need 4 by default")
        st.write("5. Internal calculations: implied probability, home favourite, time safety, line movement, data confidence")
        st.write("Optional/manual sources only:")
        st.write("Sports Alerts, Sports Chat Place, Picks & Parlays")

        st.warning("This app is a paper-betting shield, not proof of profit yet. You still need 300 settled paper picks before treating the system as proven.")


    st.divider()
    st.caption("GOAT Shield Live v4.4.5 LOOSE BOOKS is paper-only. It does not place real-money bets, log into sportsbooks, scrape bookmakers, or bypass betting rules.")


if __name__ == "__main__":
    main()
