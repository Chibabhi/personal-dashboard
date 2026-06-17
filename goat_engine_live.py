
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timezone
import math, requests

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
DEFAULT_SPORTS = {
    "baseball_mlb": "MLB",
    "basketball_nba": "NBA",
    "icehockey_nhl": "NHL",
    "americanfootball_nfl": "NFL",
    "soccer_epl": "EPL",
    "aussierules_afl": "AFL",
    "rugbyleague_nrl": "NRL",
}

@dataclass
class Candidate:
    event_id: str
    sport_key: str
    commence_time: str
    home_team: str
    away_team: str
    pick_team: str
    market: str
    best_odds: float
    best_bookmaker: str
    consensus_prob: float
    implied_prob: float
    edge: float
    is_home_pick: bool
    is_home_favourite: bool
    pinnacle_price: Optional[float]
    pinnacle_value_ok: bool
    bookmaker_count: int
    source_summary: str

@dataclass
class Decision:
    decision: str
    risk: str
    score: int
    reasons: List[str]
    next_action: str

def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        return v if math.isfinite(v) else default
    except Exception:
        return default

def fetch_odds(api_key: str, sport_key: str, regions: str = "us,uk,au,eu", markets: str = "h2h") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    r = requests.get(
        f"{ODDS_API_BASE}/sports/{sport_key}/odds/",
        params={"apiKey": api_key, "regions": regions, "markets": markets, "oddsFormat": "decimal", "dateFormat": "iso"},
        timeout=30,
    )
    r.raise_for_status()
    meta = {
        "requests_remaining": r.headers.get("x-requests-remaining"),
        "requests_used": r.headers.get("x-requests-used"),
        "last_fetch_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return r.json(), meta

def _market_outcomes(bookmaker: Dict[str, Any], market_key: str = "h2h") -> Optional[List[Dict[str, Any]]]:
    for m in bookmaker.get("markets", []):
        if m.get("key") == market_key:
            return m.get("outcomes", [])
    return None

def analyse_event_moneyline(event: Dict[str, Any], sport_key: str) -> List[Candidate]:
    home = event.get("home_team", "")
    away = event.get("away_team", "")
    teams = [home, away]
    if not home or not away:
        return []
    best = {home: (0.0, ""), away: (0.0, "")}
    no_vig_probs = {home: [], away: []}
    pinnacle_prices = {home: None, away: None}
    bookmaker_count = 0
    for bm in event.get("bookmakers", []):
        outcomes = _market_outcomes(bm, "h2h")
        if not outcomes:
            continue
        price_by_team = {}
        for o in outcomes:
            name, price = o.get("name"), safe_float(o.get("price"))
            if name in teams and price > 1:
                price_by_team[name] = price
                if price > best[name][0]:
                    best[name] = (price, bm.get("title", bm.get("key", "unknown")))
        if home in price_by_team and away in price_by_team:
            bookmaker_count += 1
            inv_home, inv_away = 1 / price_by_team[home], 1 / price_by_team[away]
            total = inv_home + inv_away
            if total > 0:
                no_vig_probs[home].append(inv_home / total)
                no_vig_probs[away].append(inv_away / total)
        bm_key = str(bm.get("key", "")).lower()
        bm_title = str(bm.get("title", "")).lower()
        if "pinnacle" in bm_key or "pinnacle" in bm_title:
            for t in teams:
                if t in price_by_team:
                    pinnacle_prices[t] = price_by_team[t]
    candidates = []
    for team in teams:
        odds, book = best[team]
        if odds <= 1 or not no_vig_probs[team]:
            continue
        consensus_prob = sum(no_vig_probs[team]) / len(no_vig_probs[team])
        implied = 1 / odds
        edge = consensus_prob - implied
        opponent = away if team == home else home
        opp_odds = best[opponent][0]
        is_home_pick = team == home
        is_home_fav = is_home_pick and opp_odds > 0 and odds < opp_odds
        pin_price = pinnacle_prices[team]
        pin_ok = bool(pin_price and odds >= pin_price)
        candidates.append(Candidate(
            event_id=event.get("id", ""), sport_key=sport_key, commence_time=event.get("commence_time", ""),
            home_team=home, away_team=away, pick_team=team, market="Moneyline", best_odds=round(odds, 3),
            best_bookmaker=book, consensus_prob=round(consensus_prob, 5), implied_prob=round(implied, 5),
            edge=round(edge, 5), is_home_pick=is_home_pick, is_home_favourite=is_home_fav,
            pinnacle_price=round(pin_price, 3) if pin_price else None, pinnacle_value_ok=pin_ok,
            bookmaker_count=bookmaker_count, source_summary=f"{bookmaker_count} bookmakers scanned. Best price: {book} @ {odds:.2f}."
        ))
    return candidates

def scan_all_moneyline(events: List[Dict[str, Any]], sport_key: str) -> List[Candidate]:
    out = []
    for event in events:
        out.extend(analyse_event_moneyline(event, sport_key))
    return out

def decide_candidate(c: Candidate, rules: Dict[str, Any], manual_signals: Dict[str, Any], approved_today: int = 0, loss_streak: int = 0) -> Decision:
    reasons, score = [], 0
    min_odds = safe_float(rules.get("min_odds", 1.40), 1.40)
    max_odds = safe_float(rules.get("max_odds", 2.20), 2.20)
    min_edge = safe_float(rules.get("min_edge_pct", 2), 2) / 100
    elite_edge = safe_float(rules.get("elite_edge_pct", 5), 5) / 100
    max_daily = int(safe_float(rules.get("max_daily", 3), 3))
    lock_losses = int(safe_float(rules.get("lock_losses", 3), 3))
    if manual_signals.get("late_chase_feeling"):
        return Decision("LOCKED — EMOTIONAL RISK", "High", 0, ["Late/chase feeling marked. Walk away."], "Do not log this pick.")
    if loss_streak >= lock_losses:
        return Decision("LOCKED — EMOTIONAL RISK", "High", 0, [f"Loss-streak lockout active: {loss_streak} losses."], "Stop for today.")
    if approved_today >= max_daily:
        return Decision("LOCKED — DAILY LIMIT", "High", 0, [f"Daily limit reached: {approved_today}/{max_daily}."], "Stop for today.")
    red_flags = [("Injury/news red flag", manual_signals.get("injury_red")), ("Public-heavy red flag", manual_signals.get("public_red")), ("Schedule/fatigue red flag", manual_signals.get("fatigue_red")), ("Line moved against pick", manual_signals.get("line_against")), ("Key player uncertainty", manual_signals.get("key_player_red"))]
    active_reds = [name for name, active in red_flags if active]
    if active_reds and rules.get("reject_red_flags", True):
        return Decision("REJECTED — RED FLAG", "High", 0, active_reds, "Do not log this pick.")
    if not (min_odds <= c.best_odds <= max_odds):
        return Decision("REJECTED — ODDS RANGE", "Medium", 0, [f"Odds {c.best_odds:.2f} outside {min_odds:.2f}-{max_odds:.2f}."], "Do not log this pick.")
    if c.edge < min_edge:
        return Decision("REJECTED — EDGE TOO LOW", "Medium", 0, [f"Edge {c.edge*100:.2f}% below minimum {min_edge*100:.2f}%."], "Do not log this pick.")
    if rules.get("require_home_pick", True) and not c.is_home_pick:
        return Decision("REJECTED — NOT HOME PICK", "Medium", 0, ["Your system requires pick team to be home team."], "Do not log this pick.")
    if rules.get("require_home_favourite", True) and not c.is_home_favourite:
        return Decision("REJECTED — NOT HOME FAVOURITE", "Medium", 0, ["Your system requires home favourite."], "Do not log this pick.")
    if rules.get("require_pinnacle_value", False) and not c.pinnacle_value_ok:
        return Decision("WATCHLIST — PINNACLE NOT CONFIRMED", "Medium", 45, ["Pinnacle value not available/confirmed in feed."], "Manual proof needed before logging.")
    score += 25 if c.edge >= min_edge else 0
    score += 20 if c.is_home_pick else 0
    score += 20 if c.is_home_favourite else 0
    score += 15 if c.pinnacle_value_ok else 0
    score += min(15, c.bookmaker_count)
    if manual_signals.get("sports_alerts_support"): score += 5
    if manual_signals.get("scp_support"): score += 5
    if manual_signals.get("picks_parlays_support"): score += 5
    score = min(score, 100)
    if c.edge >= elite_edge and score >= 80:
        return Decision("ELITE PAPER PICK", "Low/Medium", score, ["High edge and core gates passed."], "Auto-log as PAPER only if you accept.")
    return Decision("APPROVED PAPER PICK", "Medium", score, ["Core live-data GOAT gates passed."], "Auto-log as PAPER only if you accept.")

def ai_review_text(candidate: Candidate, decision: Decision) -> str:
    return f"""Paper-only discipline review:\n- Pick: {candidate.pick_team} {candidate.market} @ {candidate.best_odds}\n- Book: {candidate.best_bookmaker}\n- Consensus probability: {candidate.consensus_prob*100:.1f}%\n- Implied probability: {candidate.implied_prob*100:.1f}%\n- Edge: {candidate.edge*100:.2f}%\n- Decision: {decision.decision}\n- Reasons: {'; '.join(decision.reasons)}\nReminder: paper tracking only. No real-money automation."""
