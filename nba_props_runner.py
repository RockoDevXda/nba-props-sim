import datetime
import json
import os
import re
from collections import defaultdict
from zoneinfo import ZoneInfo
import requests

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
ESPN_ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster"
ODDS_GAMES_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
ODDS_EVENT_URL = "https://api.the-odds-api.com/v4/sports/basketball_nba/events/{event_id}/odds"

PROP_MARKET_MAP = {
    "player_points": ("PTS", "Pts"),
    "player_rebounds": ("REB", "Reb"),
    "player_assists": ("AST", "Ast"),
    "player_threes": ("3PM", "3PM"),
    "player_points_rebounds_assists": ("PRA", "PRA"),
}

BOOK_TITLE_ALIASES = {
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "betmgm": "BetMGM",
    "caesars": "Caesars",
    "espnbet": "ESPN BET",
    "pinnacle": "Pinnacle",
    "prizepicks": "PrizePicks",
    "underdog": "Underdog",
    "hardrockbet": "Hard Rock Bet",
}


def normalize_name(name: str) -> str:
    s = re.sub(r"[^a-z0-9\s]", "", (name or "").lower())
    s = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b", "", s)
    return re.sub(r"\s+", " ", s).strip()


def format_book_name(key: str, title: str = "") -> str:
    k = (key or "").lower().strip()
    if k in BOOK_TITLE_ALIASES:
        return BOOK_TITLE_ALIASES[k]
    return title.strip() or k.upper()


def american_to_implied(am: float) -> float:
    am = float(am)
    if am == 0:
        return 0.5
    if am > 0:
        return 100.0 / (am + 100.0)
    return abs(am) / (abs(am) + 100.0)


def american_to_decimal(am: float) -> float:
    am = float(am)
    if am == 0:
        return 1.909
    if am > 0:
        return 1.0 + (am / 100.0)
    return 1.0 + (100.0 / abs(am))


def implied_to_american(prob: float) -> int:
    p = max(0.01, min(0.99, float(prob)))
    if p >= 0.5:
        return int(round(-100.0 * p / (1.0 - p)))
    return int(round(100.0 * (1.0 - p) / p))


def calc_ev_pct(model_prob_pct: float, american_odds: float) -> float:
    p = max(0.0, min(1.0, float(model_prob_pct) / 100.0))
    q = 1.0 - p
    b = max(0.01, american_to_decimal(american_odds) - 1.0)
    return round(((p * b) - q) * 100.0, 1)


def compute_no_vig_and_best(book_offers: list[dict], model_prob_pct: float) -> dict:
    if not book_offers:
        fallback_ev = calc_ev_pct(model_prob_pct, -110)
        return {
            "best_book": "Consensus (-110)",
            "best_odds": -110,
            "no_vig_odds": -110,
            "no_vig_prob": 50.0,
            "market_ev_pct": fallback_ev,
        }

    best_entry = max(
        book_offers,
        key=lambda x: american_to_decimal(x["over_odds"] if x.get("over_odds") is not None else x["under_odds"]),
    )
    best_odds = int(best_entry["over_odds"] if best_entry.get("over_odds") is not None else best_entry["under_odds"])
    best_book = best_entry["book"]

    fair_probs = []
    for offer in book_offers:
        o_odds = offer.get("over_odds")
        u_odds = offer.get("under_odds")
        if o_odds is not None and u_odds is not None:
            p_o = american_to_implied(o_odds)
            p_u = american_to_implied(u_odds)
            total_imp = p_o + p_u
            if total_imp > 0:
                fair_probs.append(p_o / total_imp)
        elif o_odds is not None:
            fair_probs.append(american_to_implied(o_odds))
        elif u_odds is not None:
            fair_probs.append(1.0 - american_to_implied(u_odds))

    no_vig_p = (sum(fair_probs) / len(fair_probs)) if fair_probs else 0.5
    no_vig_prob_pct = round(no_vig_p * 100.0, 1)
    no_vig_odds = implied_to_american(no_vig_p)
    market_ev_pct = calc_ev_pct(model_prob_pct, best_odds)

    return {
        "best_book": best_book,
        "best_odds": best_odds,
        "no_vig_odds": no_vig_odds,
        "no_vig_prob": no_vig_prob_pct,
        "market_ev_pct": market_ev_pct,
    }


def fetch_espn_scoreboard(date_yyyymmdd: str) -> list[dict]:
    try:
        resp = requests.get(ESPN_SCOREBOARD_URL, params={"dates": date_yyyymmdd, "limit": 100}, timeout=15)
        if resp.ok:
            return resp.json().get("events") or []
    except Exception as e:
        print(f"ESPN scoreboard error ({date_yyyymmdd}): {e}")
    return []


def fetch_slate_events(us_now: datetime.datetime) -> list[dict]:
    """Fetches ESPN NBA games for the US slate date, falling back to adjacent day if empty."""
    candidates = [
        us_now.strftime("%Y%m%d"),
        (us_now + datetime.timedelta(days=1)).strftime("%Y%m%d"),
        (us_now - datetime.timedelta(days=1)).strftime("%Y%m%d"),
    ]
    seen_ids = set()
    merged = []
    for d in candidates:
        evs = fetch_espn_scoreboard(d)
        print(f"ESPN scoreboard {d}: {len(evs)} events")
        for ev in evs:
            eid = str(ev.get("id", ""))
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                merged.append(ev)
        if merged and d == candidates[0]:
            break
    return merged


def fetch_team_roster(team_id: str, team_abbr: str) -> dict[str, dict]:
    players = {}
    if not team_id:
        return players
    try:
        resp = requests.get(ESPN_ROSTER_URL.format(team_id=team_id), timeout=15)
        if not resp.ok:
            return players
        data = resp.json()
        athletes = data.get("athletes") or []
        for item in athletes:
            group = item.get("items") if isinstance(item, dict) and "items" in item else [item]
            for ath in group:
                if not isinstance(ath, dict):
                    continue
                status_type = (ath.get("status") or {}).get("type") or ""
                if str(status_type).lower() == "injured":
                    continue
                full_name = ath.get("displayName") or ath.get("fullName") or ""
                if not full_name:
                    continue
                norm = normalize_name(full_name)
                players[norm] = {
                    "id": str(ath.get("id") or norm),
                    "player": full_name,
                    "team": team_abbr,
                    "min": 30.0,
                }
    except Exception:
        pass
    return players


def fetch_espn_summary_players(event_id: str) -> dict[str, dict]:
    players = {}
    try:
        resp = requests.get(ESPN_SUMMARY_URL, params={"event": event_id}, timeout=15)
        if not resp.ok:
            return players
        data = resp.json()
        for box_team in (data.get("boxscore", {}).get("players") or []):
            team_abbr = box_team.get("team", {}).get("abbreviation", "NBA")
            for stat_group in (box_team.get("statistics") or []):
                for ath_entry in (stat_group.get("athletes") or []):
                    ath = ath_entry.get("athlete") or {}
                    full_name = ath.get("displayName") or ""
                    if not full_name:
                        continue
                    norm = normalize_name(full_name)
                    players[norm] = {
                        "id": str(ath.get("id") or norm),
                        "player": full_name,
                        "team": team_abbr,
                        "min": 30.0,
                    }
    except Exception:
        pass
    return players


def fetch_odds_api_games() -> list[dict]:
    if not ODDS_API_KEY:
        print("ODDS_API_KEY not set; using fallback -110 odds.")
        return []
    try:
        resp = requests.get(
            ODDS_GAMES_URL,
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us,eu",
                "markets": "h2h,spreads,totals",
                "oddsFormat": "american",
            },
            timeout=15,
        )
        print(f"Odds API games status: {resp.status_code}")
        data = resp.json() if resp.ok else []
        print(f"Odds API games count: {len(data) if isinstance(data, list) else 0}")
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"Odds API games error: {e}")
        return []


def fetch_odds_api_event_props(event_id: str) -> dict:
    if not ODDS_API_KEY or not event_id:
        return {}
    try:
        resp = requests.get(
            ODDS_EVENT_URL.format(event_id=event_id),
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": ",".join(PROP_MARKET_MAP.keys()),
                "oddsFormat": "american",
            },
            timeout=15,
        )
        print(f"Odds API event {event_id} props status: {resp.status_code}")
        return resp.json() if resp.ok and isinstance(resp.json(), dict) else {}
    except Exception as e:
        print(f"Odds API event {event_id} error: {e}")
        return {}


def match_odds_event(home_team: str, away_team: str, odds_games: list[dict]) -> dict | None:
    h_norm = normalize_name(home_team)
    a_norm = normalize_name(away_team)
    h_words = [w for w in h_norm.split() if len(w) >= 4]
    a_words = [w for w in a_norm.split() if len(w) >= 4]
    for ev in odds_games:
        oh = normalize_name(ev.get("home_team", ""))
        oa = normalize_name(ev.get("away_team", ""))
        h_match = (h_norm in oh or oh in h_norm) or any(w in oh for w in h_words)
        a_match = (a_norm in oa or oa in a_norm) or any(w in oa for w in a_words)
        if h_match and a_match:
            return ev
    return None
