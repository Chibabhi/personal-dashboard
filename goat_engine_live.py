
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from collections import Counter
import math, requests

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
FALLBACK_SPORTS = {
    "baseball_mlb": "MLB", "basketball_nba": "NBA", "icehockey_nhl": "NHL",
    "americanfootball_nfl": "NFL", "soccer_epl": "EPL", "aussierules_afl": "AFL",
    "rugbyleague_nrl": "NRL",
}

@dataclass
class Candidate:
    event_id: str
    sport_key: str
    sport_title: str
    commence_time: str
    home_team: str
    away_team: str
    pick_label: str
    outcome_name: str
    market_key: str
    market_label: str
    point: Optional[float]
    best_odds: float
    best_bookmaker: str
    consensus_prob: float
    implied_prob: float
    edge: float
    is_team_market: bool
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
    reject_bucket: str
    next_action: str

def safe_float(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
        if math.isfinite(v): return v
    except Exception:
        pass
    return default

def fetch_sports(api_key: str) -> List[Dict[str, Any]]:
    r = requests.get(f"{ODDS_API_BASE}/sports/", params={"apiKey": api_key}, timeout=25)
    r.raise_for_status()
    return r.json()

def fetch_odds(api_key: str, sport_key: str, regions: str = "us", markets: str = "h2h") -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    params = {"apiKey": api_key, "regions": regions, "markets": markets, "oddsFormat": "decimal", "dateFormat": "iso"}
    r = requests.get(f"{ODDS_API_BASE}/sports/{sport_key}/odds/", params=params, timeout=35)
    r.raise_for_status()
    meta = {"requests_remaining": r.headers.get("x-requests-remaining"),
            "requests_used": r.headers.get("x-requests-used"),
            "last_fetch_utc": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return r.json(), meta

def _point(outcome: Dict[str, Any]) -> Optional[float]:
    if outcome.get("point") is None: return None
    return safe_float(outcome.get("point"), None)

def _key(market_key: str, outcome: Dict[str, Any]) -> Tuple[str, str, Optional[float]]:
    return (market_key, str(outcome.get("name", "")), _point(outcome))

def _fmt_point(p: Optional[float]) -> str:
    if p is None: return ""
    return f"+{p:g}" if p > 0 else f"{p:g}"

def _market_label(k: str) -> str:
    return {"h2h": "Moneyline", "spreads": "Spread", "totals": "Total"}.get(k, k)

def _pick_label(k: str, name: str, p: Optional[float]) -> str:
    if k == "h2h": return f"{name} ML"
    if k == "spreads": return f"{name} {_fmt_point(p)}"
    if k == "totals": return f"{name} {p:g}" if p is not None else name
    return f"{name} {_fmt_point(p)}".strip()

def _home_fav_h2h(best: Dict[Tuple[str, str, Optional[float]], Tuple[float, str]], home: str, away: str) -> bool:
    ho = best.get(("h2h", home, None), (0.0, ""))[0]
    ao = best.get(("h2h", away, None), (0.0, ""))[0]
    return bool(ho > 1 and ao > 1 and ho < ao)

def analyse_event(event: Dict[str, Any], sport_key: str, selected_markets: List[str]) -> List[Candidate]:
    home, away = event.get("home_team", "") or "", event.get("away_team", "") or ""
    sport_title = event.get("sport_title", sport_key)
    best, probs, pinn, books = {}, {}, {}, Counter()

    for bm in event.get("bookmakers", []):
        bname = bm.get("title", bm.get("key", "unknown"))
        is_pin = "pinnacle" in str(bm.get("key","")).lower() or "pinnacle" in str(bm.get("title","")).lower()
        for m in bm.get("markets", []):
            mkey = m.get("key")
            if mkey not in selected_markets: continue
            outcomes = [o for o in m.get("outcomes", []) if safe_float(o.get("price")) > 1]
            if len(outcomes) < 2: continue
            invs = [1 / safe_float(o.get("price")) for o in outcomes]
            total_inv = sum(invs)
            if total_inv <= 0: continue
            for o, inv in zip(outcomes, invs):
                k = _key(mkey, o)
                price = safe_float(o.get("price"))
                books[k] += 1
                if price > best.get(k, (0.0, ""))[0]:
                    best[k] = (price, bname)
                probs.setdefault(k, []).append(inv / total_inv)
                if is_pin: pinn[k] = price

    home_fav = _home_fav_h2h(best, home, away)
    out = []
    for k, prob_list in probs.items():
        mkey, outcome_name, p = k
        if k not in best: continue
        odds, book = best[k]
        consensus = sum(prob_list) / len(prob_list)
        implied = 1 / odds
        edge = consensus - implied
        is_team_market = mkey in ("h2h", "spreads") and outcome_name in (home, away)
        is_home_pick = bool(is_team_market and outcome_name == home)
        if mkey == "h2h":
            is_home_favourite = bool(is_home_pick and home_fav)
        elif mkey == "spreads":
            is_home_favourite = bool(is_home_pick and p is not None and p < 0)
        else:
            is_home_favourite = False
        pin_price = pinn.get(k)
        pin_ok = bool(pin_price and odds >= pin_price)
        out.append(Candidate(
            event_id=event.get("id",""), sport_key=sport_key, sport_title=sport_title,
            commence_time=event.get("commence_time",""), home_team=home, away_team=away,
            pick_label=_pick_label(mkey, outcome_name, p), outcome_name=outcome_name,
            market_key=mkey, market_label=_market_label(mkey), point=round(p, 3) if isinstance(p, float) else p,
            best_odds=round(odds, 3), best_bookmaker=book, consensus_prob=round(consensus, 5),
            implied_prob=round(implied, 5), edge=round(edge, 5), is_team_market=is_team_market,
            is_home_pick=is_home_pick, is_home_favourite=is_home_favourite,
            pinnacle_price=round(pin_price, 3) if pin_price else None, pinnacle_value_ok=pin_ok,
            bookmaker_count=int(books[k]), source_summary=f"{books[k]} bookmaker prices. Best: {book} @ {odds:.2f}."
        ))
    return out

def scan_events(events: List[Dict[str, Any]], sport_key: str, selected_markets: List[str]) -> List[Candidate]:
    out = []
    for e in events:
        out.extend(analyse_event(e, sport_key, selected_markets))
    return out

def decide_candidate(c: Candidate, rules: Dict[str, Any], manual_signals: Optional[Dict[str, Any]] = None, approved_today: int = 0, loss_streak: int = 0) -> Decision:
    manual_signals = manual_signals or {}
    min_odds = safe_float(rules.get("min_odds", 1.40), 1.40)
    max_odds = safe_float(rules.get("max_odds", 2.20), 2.20)
    min_edge = safe_float(rules.get("min_edge_pct", 2), 2) / 100
    elite_edge = safe_float(rules.get("elite_edge_pct", 5), 5) / 100
    max_daily = int(safe_float(rules.get("max_daily", 3), 3))
    lock_losses = int(safe_float(rules.get("lock_losses", 3), 3))

    if manual_signals.get("late_chase_feeling"):
        return Decision("LOCKED — EMOTIONAL RISK", "High", 0, ["Late/chase feeling marked. Walk away."], "Locked/chase", "Do not log.")
    if loss_streak >= lock_losses:
        return Decision("LOCKED — EMOTIONAL RISK", "High", 0, [f"Loss-streak lockout active: {loss_streak} losses."], "Locked/loss streak", "Stop.")
    if approved_today >= max_daily:
        return Decision("LOCKED — DAILY LIMIT", "High", 0, [f"Daily approved-pick limit reached: {approved_today}/{max_daily}."], "Locked/daily limit", "Stop.")

    reds = [("Injury/news red flag", manual_signals.get("injury_red")), ("Public-heavy red flag", manual_signals.get("public_red")),
            ("Schedule/fatigue red flag", manual_signals.get("fatigue_red")), ("Line moved against pick", manual_signals.get("line_against")),
            ("Key player uncertainty", manual_signals.get("key_player_red"))]
    active_reds = [name for name, active in reds if active]
    if active_reds and rules.get("reject_red_flags", True):
        return Decision("REJECTED — RED FLAG", "High", 0, active_reds, "Red flag", "Do not log.")

    if not (min_odds <= c.best_odds <= max_odds):
        return Decision("REJECTED — ODDS RANGE", "Medium", 0, [f"Odds {c.best_odds:.2f} outside allowed {min_odds:.2f}-{max_odds:.2f}."], "Odds range", "Do not log.")
    if c.edge < min_edge:
        return Decision("REJECTED — EDGE TOO LOW", "Medium", 0, [f"Edge {c.edge*100:.2f}% below minimum {min_edge*100:.2f}%."], "Edge too low", "Do not log.")

    if rules.get("apply_home_rules_to_team_markets", True) and c.is_team_market:
        if rules.get("require_home_pick", True) and not c.is_home_pick:
            return Decision("REJECTED — NOT HOME PICK", "Medium", 0, ["Team-market pick is not home team."], "Not home pick", "Do not log.")
        if rules.get("require_home_favourite", True) and not c.is_home_favourite:
            return Decision("REJECTED — NOT HOME FAVOURITE", "Medium", 0, ["Home favourite rule failed."], "Not home favourite", "Do not log.")
    if rules.get("require_pinnacle_value", False) and not c.pinnacle_value_ok:
        return Decision("WATCHLIST — PINNACLE NOT CONFIRMED", "Medium", 45, ["Pinnacle value not available/confirmed."], "Pinnacle missing", "Manual proof needed.")

    score = 25 + (20 if c.edge >= elite_edge else 0) + min(15, c.bookmaker_count)
    score += {"h2h": 15, "spreads": 12, "totals": 10}.get(c.market_key, 8)
    score += 15 if c.is_home_pick else (10 if not c.is_team_market else 0)
    score += 15 if c.is_home_favourite else 0
    score += 10 if c.pinnacle_value_ok else 0
    score += 3 if manual_signals.get("sports_alerts_support") else 0
    score += 3 if manual_signals.get("scp_support") else 0
    score += 3 if manual_signals.get("picks_parlays_support") else 0
    score = min(score, 100)

    if c.edge >= elite_edge and score >= 75:
        return Decision("ELITE PAPER PICK", "Low/Medium", score, ["High edge and GOAT gates passed."], "Approved", "Paper-log only.")
    return Decision("APPROVED PAPER PICK", "Medium", score, ["Core live-data GOAT gates passed."], "Approved", "Paper-log only.")

def summarize_decisions(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not rows:
        return {"total": 0, "approved": 0, "elite": 0, "watchlist": 0, "rejected": 0, "locked": 0, "top_reasons": [], "plain": "No candidates generated. Try another active sport, region, or market."}
    decisions = [str(r.get("decision","")) for r in rows]
    buckets = [str(r.get("reject_bucket","")) for r in rows if str(r.get("reject_bucket","")) not in ("", "Approved")]
    approved = sum(("APPROVED" in d or "ELITE" in d) for d in decisions)
    top = Counter(buckets).most_common(5)
    plain = f"{approved} approved/elite paper candidate(s)." if approved else "No approved paper picks. Main blockers: " + (", ".join(f"{k}: {v}" for k, v in top) if top else "No strong edge.")
    return {"total": len(rows), "approved": approved, "elite": sum("ELITE" in d for d in decisions),
            "watchlist": sum("WATCHLIST" in d for d in decisions), "rejected": sum("REJECTED" in d for d in decisions),
            "locked": sum("LOCKED" in d for d in decisions), "top_reasons": top, "plain": plain}

def ai_review_text(candidate: Candidate, decision: Decision) -> str:
    return (f"Paper-only review:\n- Pick: {candidate.pick_label} ({candidate.market_label}) @ {candidate.best_odds}\n"
            f"- Game: {candidate.away_team} @ {candidate.home_team}\n"
            f"- Edge: {candidate.edge*100:.2f}%\n- Decision: {decision.decision}\n"
            f"- Reasons: {'; '.join(decision.reasons)}\nReminder: paper tracking only.")
