import os
import requests

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()

def american_to_implied(am):
    am = float(am)
    return (100.0 / (am + 100.0)) if am > 0 else (abs(am) / (abs(am) + 100.0))

def implied_to_american(p):
    if p <= 0.01 or p >= 0.99:
        return -110
    return int(round(-100 * p / (1 - p))) if p >= 0.5 else int(round(100 * (1 - p) / p))

def american_to_decimal(am):
    am = float(am)
    return (1.0 + am / 100.0) if am > 0 else (1.0 + 100.0 / abs(am))

def compute_no_vig_and_best(book_offers):
    """
    book_offers: list of dicts [{"book": "DraftKings", "over": -105, "under": -115}, ...]
    Returns best over/under book & odds plus consensus no-vig fair probability/odds.
    """
    if not book_offers:
        return None

    best_over = max(book_offers, key=lambda x: american_to_decimal(x["over"]))
    best_under = max(book_offers, key=lambda x: american_to_decimal(x["under"]))

    nv_overs = []
    for b in book_offers:
        p_o = american_to_implied(b["over"])
        p_u = american_to_implied(b["under"])
        tot = p_o + p_u
        if tot > 0:
            nv_overs.append(p_o / tot)

    fair_over_p = sum(nv_overs) / len(nv_overs) if nv_overs else 0.5
    fair_under_p = 1.0 - fair_over_p

    return {
        "best_over_book": best_over["book"],
        "best_over_odds": best_over["over"],
        "best_under_book": best_under["book"],
        "best_under_odds": best_under["under"],
        "no_vig_over_prob": round(fair_over_p * 100, 1),
        "no_vig_under_prob": round(fair_under_p * 100, 1),
        "no_vig_over_odds": implied_to_american(fair_over_p),
        "no_vig_under_odds": implied_to_american(fair_under_p),
    }

def fetch_nba_game_odds():
    if not ODDS_API_KEY:
        return []
    url = "https://api.the-odds-api.com/v4/sports/basketball_nba/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us,eu",
        "markets": "h2h,spreads,totals",
        "oddsFormat": "american",
    }
    r = requests.get(url, params=params, timeout=15)
    return r.json() if r.ok else []

def fetch_nba_event_props(event_id):
    if not ODDS_API_KEY:
        return {}
    url = f"https://api.the-odds-api.com/v4/sports/basketball_nba/events/{event_id}/odds"
    params = {
        "apiKey": ODDS_API_KEY,
        "regions": "us",
        "markets": "player_points,player_rebounds,player_assists,player_threes,player_points_rebounds_assists",
        "oddsFormat": "american",
    }
    r = requests.get(url, params=params, timeout=15)
    return r.json() if r.ok else {}
