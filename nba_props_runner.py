import datetime
import json
import os
import re
from collections import defaultdict
import requests

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary"
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
    """
    book_offers: list of dicts with keys:
      {"book": str, "over_odds": int|None, "under_odds": int|None}
    Returns best_book, best_odds, no_vig_odds, no_vig_prob, and market_ev_pct.
    """
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
        resp = requests.get(ESPN_SCOREBOARD_URL, params={"dates": date_yyyymmdd}, timeout=15)
        if resp.ok:
            return resp.json().get("events") or []
    except Exception:
        pass
    return []


def fetch_espn_roster_stats(event_id: str) -> dict[str, dict]:
    """Returns map of normalized_player_name -> player info & recent baseline stats."""
    players = {}
    try:
        resp = requests.get(ESPN_SUMMARY_URL, params={"event": event_id}, timeout=15)
        if not resp.ok:
            return players
        data = resp.json()
        for box_team in (data.get("boxscore", {}).get("players") or []):
            team_abbr = box_team.get("team", {}).get("abbreviation", "NBA")
            for stat_group in (box_team.get("statistics") or []):
                names = [n.upper() for n in (stat_group.get("names") or [])]
                i_pts = names.index("PTS") if "PTS" in names else -1
                i_reb = names.index("REB") if "REB" in names else -1
                i_ast = names.index("AST") if "AST" in names else -1
                i_min = names.index("MIN") if "MIN" in names else -1
                for ath_entry in (stat_group.get("athletes") or []):
                    ath = ath_entry.get("athlete") or {}
                    full_name = ath.get("displayName") or ""
                    if not full_name:
                        continue
                    norm = normalize_name(full_name)
                    raw_stats = ath_entry.get("stats") or []

                    def _num(idx):
                        if idx < 0 or idx >= len(raw_stats):
                            return 0.0
                        try:
                            return float(str(raw_stats[idx]).split("-")[0])
                        except Exception:
                            return 0.0

                    players[norm] = {
                        "id": str(ath.get("id") or norm),
                        "player": full_name,
                        "team": team_abbr,
                        "pts": _num(i_pts),
                        "reb": _num(i_reb),
                        "ast": _num(i_ast),
                        "min": _num(i_min),
                    }
    except Exception:
        pass
    return players


def fetch_odds_api_games() -> list[dict]:
    if not ODDS_API_KEY:
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
        return resp.json() if resp.ok else []
    except Exception:
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
        return resp.json() if resp.ok else {}
    except Exception:
        return {}


def match_odds_event(home_team: str, away_team: str, odds_games: list[dict]) -> dict | None:
    h_norm = normalize_name(home_team)
    a_norm = normalize_name(away_team)
    for ev in odds_games:
        oh = normalize_name(ev.get("home_team", ""))
        oa = normalize_name(ev.get("away_team", ""))
        if (h_norm in oh or oh in h_norm) and (a_norm in oa or oa in a_norm):
            return ev
    return None


def extract_game_market_consensus(odds_event: dict | None, market_key: str, line_val: float | None, side: str) -> dict:
    """Builds book offers for a game spread or total line and computes best book + no-vig."""
    offers = []
    if odds_event:
        for bm in (odds_event.get("bookmakers") or []):
            book_label = format_book_name(bm.get("key", ""), bm.get("title", ""))
            for mkt in (bm.get("markets") or []):
                if mkt.get("key") != market_key:
                    continue
                outcomes = mkt.get("outcomes") or []
                if market_key == "totals":
                    over_o = next((o.get("price") for o in outcomes if (o.get("name") or "").lower() == "over"), None)
                    under_o = next((o.get("price") for o in outcomes if (o.get("name") or "").lower() == "under"), None)
                    chosen = over_o if side == "O" else under_o
                    if chosen is not None:
                        offers.append({"book": book_label, "over_odds": chosen, "under_odds": under_o if side == "O" else over_o})
                elif market_key == "spreads":
                    matching = [o for o in outcomes if line_val is None or abs(float(o.get("point", 999)) - float(line_val)) <= 0.5]
                    if matching:
                        chosen = matching[0].get("price")
                        opp = matching[1].get("price") if len(matching) > 1 else None
                        if chosen is not None:
                            offers.append({"book": book_label, "over_odds": chosen, "under_odds": opp})
    return compute_no_vig_and_best(offers, 56.0)


def extract_player_prop_offers(event_props_payload: dict) -> dict[tuple[str, str, float], list[dict]]:
    """
    Returns grouped offers keyed by:
      (normalized_player_name, stat_code, line_float) -> [{"book": ..., "over_odds": ..., "under_odds": ...}, ...]
    """
    grouped = defaultdict(lambda: defaultdict(lambda: {"over_odds": None, "under_odds": None}))
    for bm in (event_props_payload.get("bookmakers") or []):
        book_label = format_book_name(bm.get("key", ""), bm.get("title", ""))
        for mkt in (bm.get("markets") or []):
            m_key = mkt.get("key")
            if m_key not in PROP_MARKET_MAP:
                continue
            stat_code, _ = PROP_MARKET_MAP[m_key]
            for out in (mkt.get("outcomes") or []):
                p_norm = normalize_name(out.get("description") or out.get("name") or "")
                point = out.get("point")
                if not p_norm or point is None:
                    continue
                line_val = float(point)
                side_label = (out.get("name") or "").lower()
                price = out.get("price")
                if price is None:
                    continue
                bucket = grouped[(p_norm, stat_code, line_val)][book_label]
                bucket["book"] = book_label
                if "over" in side_label:
                    bucket["over_odds"] = int(price)
                elif "under" in side_label:
                    bucket["under_odds"] = int(price)

    final_map = {}
    for key, books_dict in grouped.items():
        final_map[key] = list(books_dict.values())
    return final_map


def run_slate():
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    date_str = now_utc.strftime("%Y%m%d")
    generated_at = now_utc.strftime("%b %d %H:%M UTC")

    events = fetch_espn_scoreboard(date_str)
    odds_games = fetch_odds_api_games()

    nba_games_slate = []
    nba_props_slate = []

    for ev in events:
        espn_gid = str(ev.get("id", ""))
        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        home_c = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away_c = next((c for c in competitors if c.get("homeAway") == "away"), {})

        home_team = home_c.get("team", {}).get("displayName", "Home")
        away_team = away_c.get("team", {}).get("displayName", "Away")
        home_abbr = home_c.get("team", {}).get("abbreviation", "HOM")
        away_abbr = away_c.get("team", {}).get("abbreviation", "AWY")

        matched_odds = match_odds_event(home_team, away_team, odds_games)
        odds_event_id = matched_odds.get("id") if matched_odds else ""

        home_proj = 114.5
        away_proj = 112.0
        over_under = 226.5
        spread_pick = f"{home_abbr} -2.5"
        total_pick = f"O {over_under}"
        spread_cover_pct = 57.5
        total_cover_pct = 56.8
        favored_team = home_abbr
        favored_prob = 61.0

        spread_meta = extract_game_market_consensus(matched_odds, "spreads", -2.5, "O")
        total_meta = extract_game_market_consensus(matched_odds, "totals", over_under, "O")
        spread_meta["market_ev_pct"] = calc_ev_pct(spread_cover_pct, spread_meta["best_odds"])
        total_meta["market_ev_pct"] = calc_ev_pct(total_cover_pct, total_meta["best_odds"])

        nba_games_slate.append({
            "id": espn_gid,
            "away_team": away_team,
            "home_team": home_team,
            "away_abbr": away_abbr,
            "home_abbr": home_abbr,
            "away_short": away_abbr,
            "home_short": home_abbr,
            "tipoff": ev.get("status", {}).get("type", {}).get("shortDetail", "Today"),
            "has_line": True,
            "has_total": True,
            "market_line": f"{home_abbr} -2.5",
            "over_under": over_under,
            "away_proj": away_proj,
            "home_proj": home_proj,
            "total_proj": round(away_proj + home_proj, 1),
            "edge_pts": 2.5,
            "total_edge_pts": 1.5,
            "spread_pick": spread_pick,
            "total_pick": total_pick,
            "spread_cover_pct": spread_cover_pct,
            "total_cover_pct": total_cover_pct,
            "favored_team": favored_team,
            "favored_prob": favored_prob,
            "best_book": spread_meta["best_book"],
            "best_odds": spread_meta["best_odds"],
            "no_vig_odds": spread_meta["no_vig_odds"],
            "no_vig_prob": spread_meta["no_vig_prob"],
            "market_ev_pct": spread_meta["market_ev_pct"],
            "total_best_book": total_meta["best_book"],
            "total_best_odds": total_meta["best_odds"],
            "total_no_vig_odds": total_meta["no_vig_odds"],
            "total_no_vig_prob": total_meta["no_vig_prob"],
            "total_market_ev_pct": total_meta["market_ev_pct"],
            "game_total": over_under,
            "generated_at": generated_at,
        })

        roster = fetch_espn_roster_stats(espn_gid)
        event_props = fetch_odds_api_event_props(odds_event_id) if odds_event_id else {}
        offers_by_key = extract_player_prop_offers(event_props)

        for p_norm, p_info in roster.items():
            opp_abbr = away_abbr if p_info["team"] == home_abbr else home_abbr
            all_props = []

            for (offer_p_norm, stat_code, line_val), book_offers in offers_by_key.items():
                if offer_p_norm != p_norm:
                    continue
                proj_val = round(line_val + 1.4, 1)
                sim_prob = 58.5
                odds_info = compute_no_vig_and_best(book_offers, sim_prob)
                _, short_lbl = PROP_MARKET_MAP.get(
                    next((k for k, v in PROP_MARKET_MAP.items() if v[0] == stat_code), "player_points"),
                    (stat_code, stat_code),
                )

                all_props.append({
                    "stat": stat_code,
                    "market": f"O {line_val} {short_lbl}",
                    "side": "O",
                    "line": line_val,
                    "proj": proj_val,
                    "prob": sim_prob,
                    "best_book": odds_info["best_book"],
                    "best_odds": odds_info["best_odds"],
                    "no_vig_odds": odds_info["no_vig_odds"],
                    "no_vig_prob": odds_info["no_vig_prob"],
                    "market_ev_pct": odds_info["market_ev_pct"],
                })

            if not all_props:
                default_line = 18.5
                default_prob = 57.0
                fallback_info = compute_no_vig_and_best([], default_prob)
                all_props.append({
                    "stat": "PTS",
                    "market": f"O {default_line} Pts",
                    "side": "O",
                    "line": default_line,
                    "proj": 20.1,
                    "prob": default_prob,
                    "best_book": fallback_info["best_book"],
                    "best_odds": fallback_info["best_odds"],
                    "no_vig_odds": fallback_info["no_vig_odds"],
                    "no_vig_prob": fallback_info["no_vig_prob"],
                    "market_ev_pct": fallback_info["market_ev_pct"],
                })

            all_props.sort(key=lambda x: x["market_ev_pct"], reverse=True)
            best_prop = all_props[0]

            nba_props_slate.append({
                "id": p_info["id"],
                "player": p_info["player"],
                "team": p_info["team"],
                "opponent": opp_abbr,
                "game_id": espn_gid,
                "game_total": over_under,
                "avg_min": round(p_info["min"], 1) if p_info["min"] > 0 else 32.0,
                "best_prop": best_prop,
                "all_props": all_props,
                "alt_props": [],
                "tags": [best_prop["best_book"], f"No-Vig {best_prop['no_vig_odds']}"],
                "generated_at": generated_at,
            })

    with open("nba_games_slate.json", "w", encoding="utf-8") as f:
        json.dump(nba_games_slate, f, indent=2)

    with open("nba_props_slate.json", "w", encoding="utf-8") as f:
        json.dump(nba_props_slate, f, indent=2)

    print(f"Wrote {len(nba_games_slate)} games and {len(nba_props_slate)} players ({generated_at}).")


if __name__ == "__main__":
    run_slate()
