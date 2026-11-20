import datetime
import json
import math
import os
import re
from collections import defaultdict
from zoneinfo import ZoneInfo
import requests

ODDS_API_KEY = os.getenv("ODDS_API_KEY", "").strip()

ESPN_SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"
ESPN_TEAMS_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams"
ESPN_TEAM_DETAIL_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}"
ESPN_ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster"
ESPN_GAMELOG_URL = "https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{athlete_id}/gamelog"
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
    return (title or "").strip() or k.upper()


def fmt_american(am: float) -> str:
    n = int(round(float(am)))
    return f"+{n}" if n > 0 else f"{n}"


def american_to_implied(am: float) -> float:
    am = float(am)
    if am == 0:
        return 0.5
    return (100.0 / (am + 100.0)) if am > 0 else (abs(am) / (abs(am) + 100.0))


def american_to_decimal(am: float) -> float:
    am = float(am)
    if am == 0 or (-100 < am < 100):
        return 1.909
    return (1.0 + am / 100.0) if am > 0 else (1.0 + 100.0 / abs(am))


def implied_to_american(prob: float) -> int:
    p = max(0.01, min(0.99, float(prob)))
    return int(round(-100.0 * p / (1.0 - p))) if p >= 0.5 else int(round(100.0 * (1.0 - p) / p))


def calc_ev_pct(model_prob_pct: float, american_odds: float) -> float:
    p = max(0.0, min(1.0, float(model_prob_pct) / 100.0))
    q = 1.0 - p
    b = max(0.01, american_to_decimal(american_odds) - 1.0)
    return round(((p * b) - q) * 100.0, 1)


def normal_cdf(x: float, mean: float, std: float) -> float:
    sigma = max(0.75, float(std))
    z = (float(x) - float(mean)) / (sigma * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def calc_side_prob_pct(proj_mean: float, line_val: float, side: str, std_val: float) -> float:
    p_under = normal_cdf(line_val, proj_mean, std_val)
    p_over = 1.0 - p_under
    raw = p_under if side == "U" else p_over
    return round(max(5.0, min(95.0, raw * 100.0)), 1)


def compute_no_vig_and_best(book_offers: list[dict], model_prob_pct: float) -> dict:
    if not book_offers:
        return {
            "best_book": "Consensus (-110)",
            "best_odds": -110,
            "no_vig_odds": -110,
            "no_vig_prob": 50.0,
            "market_ev_pct": calc_ev_pct(model_prob_pct, -110),
        }

    best_entry = max(
        book_offers,
        key=lambda x: american_to_decimal(x["price"]),
    )
    best_odds = int(best_entry["price"])
    best_book = best_entry["book"]

    fair_probs = []
    for offer in book_offers:
        p_side = american_to_implied(offer["price"])
        opp_odds = offer.get("opp_price")
        if opp_odds is not None:
            p_opp = american_to_implied(opp_odds)
            total_imp = p_side + p_opp
            if total_imp > 0:
                fair_probs.append(p_side / total_imp)
        else:
            fair_probs.append(p_side)

    no_vig_p = (sum(fair_probs) / len(fair_probs)) if fair_probs else 0.5
    return {
        "best_book": best_book,
        "best_odds": best_odds,
        "no_vig_odds": implied_to_american(no_vig_p),
        "no_vig_prob": round(no_vig_p * 100.0, 1),
        "market_ev_pct": calc_ev_pct(model_prob_pct, best_odds),
    }


def load_existing_json_list(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def fetch_espn_scoreboard(date_yyyymmdd: str) -> list[dict]:
    try:
        resp = requests.get(ESPN_SCOREBOARD_URL, params={"dates": date_yyyymmdd, "limit": 100}, timeout=15)
        if resp.ok:
            return resp.json().get("events") or []
    except Exception as e:
        print(f"ESPN scoreboard error ({date_yyyymmdd}): {e}")
    return []


def fetch_slate_events(us_now: datetime.datetime) -> list[dict]:
    today_str = us_now.strftime("%Y%m%d")
    tomorrow_str = (us_now + datetime.timedelta(days=1)).strftime("%Y%m%d")
    yesterday_str = (us_now - datetime.timedelta(days=1)).strftime("%Y%m%d")

    today_evs = fetch_espn_scoreboard(today_str)
    print(f"ESPN scoreboard {today_str}: {len(today_evs)} events")

    all_final_today = bool(today_evs) and all(
        (ev.get("status") or {}).get("type", {}).get("state") == "post"
        for ev in today_evs
    )

    seen_ids = set()
    merged = []

    if not today_evs:
        for d in (tomorrow_str, yesterday_str):
            evs = fetch_espn_scoreboard(d)
            print(f"ESPN scoreboard {d}: {len(evs)} events")
            for ev in evs:
                eid = str(ev.get("id", ""))
                if eid and eid not in seen_ids:
                    seen_ids.add(eid)
                    merged.append(ev)
            if merged:
                break
        return merged

    if all_final_today:
        tomorrow_evs = fetch_espn_scoreboard(tomorrow_str)
        print(f"ESPN scoreboard {tomorrow_str}: {len(tomorrow_evs)} events")
        for ev in tomorrow_evs:
            eid = str(ev.get("id", ""))
            if eid and eid not in seen_ids:
                seen_ids.add(eid)
                merged.append(ev)

    for ev in today_evs:
        eid = str(ev.get("id", ""))
        if eid and eid not in seen_ids:
            seen_ids.add(eid)
            merged.append(ev)

    return merged


def fetch_team_ratings_map() -> dict[str, dict]:
    ratings = {}
    try:
        resp = requests.get(ESPN_TEAMS_URL, timeout=15)
        if not resp.ok:
            return ratings
        teams = (
            ((resp.json().get("sports") or [{}])[0].get("leagues") or [{}])[0].get("teams")
            or []
        )
        for entry in teams:
            t = entry.get("team") or {}
            tid = str(t.get("id") or "")
            if not tid:
                continue
            ppg = 114.0
            oppg = 114.0
            try:
                d_resp = requests.get(ESPN_TEAM_DETAIL_URL.format(team_id=tid), timeout=10)
                if d_resp.ok:
                    tdata = d_resp.json().get("team") or {}
                    for cat in ((tdata.get("record") or {}).get("items") or []):
                        for st in (cat.get("stats") or []):
                            nm = (st.get("name") or "").lower()
                            val = float(st.get("value") or 0)
                            if nm in ("avgpointsfor", "pointsfor") and 0 < val <= 150:
                                ppg = val
                            elif nm in ("avgpointsagainst", "pointsagainst") and 0 < val <= 150:
                                oppg = val
            except Exception:
                pass
            pace = round(96.0 + ((ppg + oppg - 228.0) * 0.18), 1)
            ratings[tid] = {
                "ppg": round(ppg, 1),
                "oppg": round(oppg, 1),
                "pace": max(92.0, min(106.0, pace)),
            }
    except Exception as e:
        print(f"ESPN team ratings error: {e}")
    return ratings


def fetch_team_roster(team_id: str, team_abbr: str) -> dict[str, dict]:
    players = {}
    if not team_id:
        return players
    try:
        resp = requests.get(ESPN_ROSTER_URL.format(team_id=team_id), timeout=15)
        if not resp.ok:
            return players
        athletes = resp.json().get("athletes") or []
        for item in athletes:
            group = item.get("items") if isinstance(item, dict) and "items" in item else [item]
            for ath in group:
                if not isinstance(ath, dict):
                    continue
                injuries = ath.get("injuries") or []
                out_flag = any(
                    str((inj.get("status") or "")).lower() in ("out", "doubtful", "suspended")
                    for inj in injuries
                    if isinstance(inj, dict)
                )
                if out_flag:
                    continue
                full_name = ath.get("displayName") or ath.get("fullName") or ""
                if not full_name:
                    continue
                norm = normalize_name(full_name)
                players[norm] = {
                    "id": str(ath.get("id") or norm),
                    "player": full_name,
                    "team": team_abbr,
                }
    except Exception as e:
        print(f"Roster fetch error ({team_abbr}): {e}")
    return players


def fetch_player_last10_logs(athlete_id: str) -> dict:
    empty = {"games": 0, "avg_min": 0.0, "stats": {"PTS": [], "REB": [], "AST": [], "3PM": [], "PRA": []}}
    if not athlete_id or not str(athlete_id).isdigit():
        return empty
    try:
        resp = requests.get(ESPN_GAMELOG_URL.format(athlete_id=athlete_id), timeout=12)
        if not resp.ok:
            return empty
        data = resp.json()
        labels = [str(x).upper() for x in (data.get("labels") or data.get("names") or [])]

        def idx_of(*cands):
            for c in cands:
                if c in labels:
                    return labels.index(c)
            return -1

        i_min = idx_of("MIN")
        i_pts = idx_of("PTS")
        i_reb = idx_of("REB")
        i_ast = idx_of("AST")
        i_3pm = idx_of("3PM", "3PT")

        raw_events = []
        for season_grp in (data.get("seasonTypes") or []):
            for cat in (season_grp.get("categories") or []):
                for ev_row in (cat.get("events") or []):
                    st_list = ev_row.get("stats") or []
                    if st_list:
                        raw_events.append(st_list)

        if not raw_events:
            for ev_row in (data.get("events") or []):
                st_list = ev_row.get("stats") or []
                if st_list:
                    raw_events.append(st_list)

        last10 = raw_events[:10]
        if not last10:
            return empty

        mins, pts_l, reb_l, ast_l, tpm_l, pra_l = [], [], [], [], [], []
        for s in last10:
            m_val = float(s[i_min]) if 0 <= i_min < len(s) and str(s[i_min]).replace(".", "", 1).isdigit() else 0.0
            if m_val <= 0:
                continue
            p_val = float(s[i_pts]) if 0 <= i_pts < len(s) and str(s[i_pts]).replace(".", "", 1).isdigit() else 0.0
            r_val = float(s[i_reb]) if 0 <= i_reb < len(s) and str(s[i_reb]).replace(".", "", 1).isdigit() else 0.0
            a_val = float(s[i_ast]) if 0 <= i_ast < len(s) and str(s[i_ast]).replace(".", "", 1).isdigit() else 0.0
            raw_3 = str(s[i_3pm]) if 0 <= i_3pm < len(s) else "0"
            t_val = float(raw_3.split("-")[0]) if raw_3.split("-")[0].replace(".", "", 1).isdigit() else 0.0

            mins.append(m_val)
            pts_l.append(p_val)
            reb_l.append(r_val)
            ast_l.append(a_val)
            tpm_l.append(t_val)
            pra_l.append(round(p_val + r_val + a_val, 1))

        if not mins:
            return empty

        return {
            "games": len(mins),
            "avg_min": round(sum(mins) / len(mins), 1),
            "stats": {
                "PTS": pts_l,
                "REB": reb_l,
                "AST": ast_l,
                "3PM": tpm_l,
                "PRA": pra_l,
            },
        }
    except Exception:
        return empty


def mean_and_std(vals: list[float]) -> tuple[float, float]:
    if not vals:
        return 0.0, 2.5
    m = sum(vals) / len(vals)
    if len(vals) < 2:
        return round(m, 1), max(1.5, round(m * 0.25, 1))
    var = sum((x - m) ** 2 for x in vals) / (len(vals) - 1)
    sd = max(1.0, math.sqrt(var))
    return round(m, 1), round(sd, 1)


def build_streak_info(vals: list[float], line_val: float, side: str) -> tuple[str, str, int]:
    if not vals:
        return "", "", 0
    ordered = list(reversed(vals))
    flags = []
    hits = 0
    for v in ordered:
        ok = (v < line_val) if side == "U" else (v > line_val)
        if ok:
            hits += 1
            flags.append("1")
        else:
            flags.append("0")
    return "".join(flags), f"{hits}/{len(flags)}", hits


def fetch_odds_api_games() -> list[dict]:
    if not ODDS_API_KEY:
        print("ODDS_API_KEY not set; skipping live Odds API lines.")
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
        payload = resp.json() if resp.ok else {}
        return payload if isinstance(payload, dict) else {}
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
        if ((h_norm in oh or oh in h_norm) or any(w in oh for w in h_words)) and (
            (a_norm in oa or oa in a_norm) or any(w in oa for w in a_words)
        ):
            return ev
    return None


def extract_real_game_markets(odds_event: dict | None) -> dict:
    spread_pts = None
    total_pts = None
    home_sp_offers = []
    away_sp_offers = []
    over_offers = []
    under_offers = []

    if odds_event:
        oh_name = odds_event.get("home_team", "")
        oa_name = odds_event.get("away_team", "")
        for bm in (odds_event.get("bookmakers") or []):
            book_label = format_book_name(bm.get("key", ""), bm.get("title", ""))
            for mkt in (bm.get("markets") or []):
                mk = mkt.get("key")
                outs = mkt.get("outcomes") or []
                if mk == "spreads":
                    h_out = next((o for o in outs if o.get("name") == oh_name), None)
                    a_out = next((o for o in outs if o.get("name") == oa_name), None)
                    if h_out and h_out.get("point") is not None and h_out.get("price") is not None:
                        pt = float(h_out["point"])
                        if spread_pts is None:
                            spread_pts = pt
                        home_sp_offers.append({
                            "book": book_label,
                            "price": int(h_out["price"]),
                            "opp_price": int(a_out["price"]) if a_out and a_out.get("price") is not None else None,
                        })
                    if a_out and a_out.get("point") is not None and a_out.get("price") is not None:
                        away_sp_offers.append({
                            "book": book_label,
                            "price": int(a_out["price"]),
                            "opp_price": int(h_out["price"]) if h_out and h_out.get("price") is not None else None,
                        })
                elif mk == "totals":
                    o_out = next((o for o in outs if (o.get("name") or "").lower() == "over"), None)
                    u_out = next((o for o in outs if (o.get("name") or "").lower() == "under"), None)
                    if o_out and o_out.get("point") is not None and o_out.get("price") is not None:
                        pt = float(o_out["point"])
                        if total_pts is None:
                            total_pts = pt
                        over_offers.append({
                            "book": book_label,
                            "price": int(o_out["price"]),
                            "opp_price": int(u_out["price"]) if u_out and u_out.get("price") is not None else None,
                        })
                    if u_out and u_out.get("point") is not None and u_out.get("price") is not None:
                        under_offers.append({
                            "book": book_label,
                            "price": int(u_out["price"]),
                            "opp_price": int(o_out["price"]) if o_out and o_out.get("price") is not None else None,
                        })

    return {
        "spread_pts": spread_pts,
        "total_pts": total_pts,
        "home_sp_offers": home_sp_offers,
        "away_sp_offers": away_sp_offers,
        "over_offers": over_offers,
        "under_offers": under_offers,
    }


def extract_real_player_props(event_props_payload: dict) -> tuple[dict, dict[str, str]]:
    grouped = defaultdict(lambda: defaultdict(lambda: {"over_odds": None, "under_odds": None}))
    display_names = {}

    for bm in (event_props_payload.get("bookmakers") or []):
        book_label = format_book_name(bm.get("key", ""), bm.get("title", ""))
        for mkt in (bm.get("markets") or []):
            m_key = mkt.get("key")
            if m_key not in PROP_MARKET_MAP:
                continue
            stat_code, _ = PROP_MARKET_MAP[m_key]
            for out in (mkt.get("outcomes") or []):
                raw_name = out.get("description") or out.get("name") or ""
                p_norm = normalize_name(raw_name)
                point = out.get("point")
                price = out.get("price")
                if not p_norm or point is None or price is None:
                    continue
                display_names[p_norm] = raw_name
                line_val = float(point)
                side_lbl = (out.get("name") or "").lower()
                bucket = grouped[(p_norm, stat_code, line_val)][book_label]
                bucket["book"] = book_label
                if "over" in side_lbl:
                    bucket["over_odds"] = int(price)
                elif "under" in side_lbl:
                    bucket["under_odds"] = int(price)

    return grouped, display_names


def run_slate():
    us_now = datetime.datetime.now(ZoneInfo("America/New_York"))
    generated_at = us_now.strftime("%b %d, %I:%M %p ET")

    prev_games_list = load_existing_json_list("nba_games_slate.json")
    prev_props_list = load_existing_json_list("nba_props_slate.json")
    prev_games_by_id = {str(g.get("id")): g for g in prev_games_list if isinstance(g, dict) and g.get("id")}
    prev_props_by_gid = defaultdict(list)
    for pr in prev_props_list:
        if isinstance(pr, dict) and pr.get("game_id"):
            prev_props_by_gid[str(pr.get("game_id"))].append(pr)

    events = fetch_slate_events(us_now)
    odds_games = fetch_odds_api_games()
    team_ratings = fetch_team_ratings_map()

    nba_games_slate = []
    nba_props_slate = []
    active_game_ids = set()

    for ev in events:
        espn_gid = str(ev.get("id", ""))
        active_game_ids.add(espn_gid)
        status_obj = (ev.get("status") or {}).get("type") or {}
        game_state = status_obj.get("state", "pre")
        tipoff_lbl = status_obj.get("shortDetail", "Today")

        comp = (ev.get("competitions") or [{}])[0]
        competitors = comp.get("competitors") or []
        home_c = next((c for c in competitors if c.get("homeAway") == "home"), {})
        away_c = next((c for c in competitors if c.get("homeAway") == "away"), {})

        home_team = home_c.get("team", {}).get("displayName", "Home")
        away_team = away_c.get("team", {}).get("displayName", "Away")
        home_abbr = home_c.get("team", {}).get("abbreviation", "HOM")
        away_abbr = away_c.get("team", {}).get("abbreviation", "AWY")
        home_tid = str(home_c.get("team", {}).get("id", ""))
        away_tid = str(away_c.get("team", {}).get("id", ""))

        matched_odds = match_odds_event(home_team, away_team, odds_games)
        odds_event_id = matched_odds.get("id") if matched_odds else ""
        mkt_data = extract_real_game_markets(matched_odds)

        h_rat = team_ratings.get(home_tid, {"ppg": 114.0, "oppg": 114.0, "pace": 99.0})
        a_rat = team_ratings.get(away_tid, {"ppg": 114.0, "oppg": 114.0, "pace": 99.0})

        home_proj = round(((h_rat["ppg"] + a_rat["oppg"]) / 2.0) + 1.5, 1)
        away_proj = round(((a_rat["ppg"] + h_rat["oppg"]) / 2.0) - 1.5, 1)
        total_proj = round(home_proj + away_proj, 1)
        exp_pace = round((h_rat["pace"] + a_rat["pace"]) / 2.0, 1)

        has_line = mkt_data["spread_pts"] is not None
        has_total = mkt_data["total_pts"] is not None
        prev_game = prev_games_by_id.get(espn_gid)

        if not has_line and not has_total and prev_game and (prev_game.get("has_line") or prev_game.get("has_total")):
            preserved_game = dict(prev_game)
            preserved_game["tipoff"] = tipoff_lbl
            preserved_game["generated_at"] = generated_at
            nba_games_slate.append(preserved_game)
            if prev_props_by_gid.get(espn_gid):
                nba_props_slate.extend(prev_props_by_gid[espn_gid])
            continue

        home_spread = mkt_data["spread_pts"] if has_line else 0.0
        over_under = mkt_data["total_pts"] if has_total else 0.0

        proj_margin_home = home_proj - away_proj
        market_margin_home = -home_spread if has_line else proj_margin_home
        edge_pts = round(abs(proj_margin_home - market_margin_home), 1)

        if has_line:
            if proj_margin_home >= market_margin_home:
                spread_pick = f"{home_abbr} {home_spread:+g}".replace("+-", "-")
                sp_offers = mkt_data["home_sp_offers"]
                sp_prob = calc_side_prob_pct(proj_margin_home, market_margin_home, "O", 11.5)
            else:
                away_spread = round(-home_spread, 1)
                spread_pick = f"{away_abbr} {away_spread:+g}".replace("+-", "-")
                sp_offers = mkt_data["away_sp_offers"]
                sp_prob = calc_side_prob_pct(-proj_margin_home, -market_margin_home, "O", 11.5)
        else:
            spread_pick = f"{home_abbr} PK"
            sp_offers = []
            sp_prob = 50.0

        sp_meta = compute_no_vig_and_best(sp_offers, sp_prob)

        if has_total:
            if total_proj >= over_under:
                total_pick = f"O {over_under}"
                tot_offers = mkt_data["over_offers"]
                tot_prob = calc_side_prob_pct(total_proj, over_under, "O", 13.0)
            else:
                total_pick = f"U {over_under}"
                tot_offers = mkt_data["under_offers"]
                tot_prob = calc_side_prob_pct(total_proj, over_under, "U", 13.0)
            total_edge_pts = round(abs(total_proj - over_under), 1)
        else:
            total_pick = "No Total"
            tot_offers = []
            tot_prob = 50.0
            total_edge_pts = 0.0

        tot_meta = compute_no_vig_and_best(tot_offers, tot_prob)
        favored_team = home_abbr if home_proj >= away_proj else away_abbr
        favored_prob = calc_side_prob_pct(
            home_proj - away_proj if home_proj >= away_proj else away_proj - home_proj,
            0.0,
            "O",
            11.5,
        )

        nba_games_slate.append({
            "id": espn_gid,
            "away_team": away_team,
            "home_team": home_team,
            "away_abbr": away_abbr,
            "home_abbr": home_abbr,
            "away_short": away_abbr,
            "home_short": home_abbr,
            "tipoff": tipoff_lbl,
            "has_line": has_line,
            "has_total": has_total,
            "market_line": f"{home_abbr} {home_spread:+g}".replace("+-", "-") if has_line else "PK",
            "over_under": over_under,
            "away_proj": away_proj,
            "home_proj": home_proj,
            "total_proj": total_proj,
            "exp_pace": exp_pace,
            "edge_pts": edge_pts,
            "total_edge_pts": total_edge_pts,
            "spread_pick": spread_pick,
            "total_pick": total_pick,
            "spread_cover_pct": sp_prob,
            "total_cover_pct": tot_prob,
            "favored_team": favored_team,
            "favored_prob": favored_prob,
            "best_book": sp_meta["best_book"],
            "best_odds": sp_meta["best_odds"],
            "no_vig_odds": sp_meta["no_vig_odds"],
            "no_vig_prob": sp_meta["no_vig_prob"],
            "market_ev_pct": sp_meta["market_ev_pct"],
            "total_best_book": tot_meta["best_book"],
            "total_best_odds": tot_meta["best_odds"],
            "total_no_vig_odds": tot_meta["no_vig_odds"],
            "total_no_vig_prob": tot_meta["no_vig_prob"],
            "total_market_ev_pct": tot_meta["market_ev_pct"],
            "game_total": over_under,
            "generated_at": generated_at,
        })

        event_props = fetch_odds_api_event_props(odds_event_id) if odds_event_id else {}
        grouped_props, odds_display_names = extract_real_player_props(event_props)

        if not grouped_props and game_state != "pre" and prev_props_by_gid.get(espn_gid):
            nba_props_slate.extend(prev_props_by_gid[espn_gid])
            continue

        roster = {}
        roster.update(fetch_team_roster(away_tid, away_abbr))
        roster.update(fetch_team_roster(home_tid, home_abbr))
        for p_norm, disp_name in odds_display_names.items():
            if p_norm not in roster:
                roster[p_norm] = {"id": p_norm, "player": disp_name, "team": away_abbr}

        for p_norm, p_info in roster.items():
            player_market_entries = [
                (stat_code, line_val, book_dict)
                for (offer_p_norm, stat_code, line_val), book_dict in grouped_props.items()
                if offer_p_norm == p_norm
            ]
            if not player_market_entries:
                continue

            glog = fetch_player_last10_logs(p_info["id"])
            if glog["games"] > 0 and glog["avg_min"] < 15.0:
                continue

            opp_abbr = away_abbr if p_info["team"] == home_abbr else home_abbr
            all_props = []

            for stat_code, line_val, book_dict in player_market_entries:
                vals = glog["stats"].get(stat_code) or []
                mu, sd = mean_and_std(vals) if vals else (round(line_val + 0.5, 1), max(1.5, round(line_val * 0.22, 1)))

                over_offers = []
                under_offers = []
                for b_label, sides in book_dict.items():
                    if sides.get("over_odds") is not None:
                        over_offers.append({"book": b_label, "price": sides["over_odds"], "opp_price": sides.get("under_odds")})
                    if sides.get("under_odds") is not None:
                        under_offers.append({"book": b_label, "price": sides["under_odds"], "opp_price": sides.get("over_odds")})

                _, short_lbl = PROP_MARKET_MAP.get(
                    next((k for k, v in PROP_MARKET_MAP.items() if v[0] == stat_code), "player_points"),
                    (stat_code, stat_code),
                )

                for side_code, side_offers in (("O", over_offers), ("U", under_offers)):
                    if not side_offers:
                        continue
                    prob_pct = calc_side_prob_pct(mu, line_val, side_code, sd)
                    odds_info = compute_no_vig_and_best(side_offers, prob_pct)
                    dots, hit_str, hits_cnt = build_streak_info(vals, line_val, side_code)
                    all_props.append({
                        "stat": stat_code,
                        "market": f"{side_code} {line_val} {short_lbl}",
                        "side": side_code,
                        "line": line_val,
                        "proj": mu,
                        "prob": prob_pct,
                        "hits": hits_cnt,
                        "hit_str": hit_str,
                        "dots": dots,
                        "best_book": odds_info["best_book"],
                        "best_odds": odds_info["best_odds"],
                        "no_vig_odds": odds_info["no_vig_odds"],
                        "no_vig_prob": odds_info["no_vig_prob"],
                        "market_ev_pct": odds_info["market_ev_pct"],
                    })

            if not all_props:
                continue

            all_props.sort(key=lambda x: x["market_ev_pct"], reverse=True)
            best_prop = all_props[0]
            has_under = any(x["side"] == "U" for x in all_props)

            nba_props_slate.append({
                "id": p_info["id"],
                "player": p_info["player"],
                "team": p_info["team"],
                "opponent": opp_abbr,
                "game_id": espn_gid,
                "game_total": over_under,
                "avg_min": glog["avg_min"] if glog["avg_min"] > 0 else 28.0,
                "has_under": has_under,
                "best_prop": best_prop,
                "all_props": all_props,
                "alt_props": [],
                "tags": [best_prop["best_book"], f"No-Vig {fmt_american(best_prop['no_vig_odds'])}"],
                "generated_at": generated_at,
            })

    for gid, prev_g in prev_games_by_id.items():
        if gid not in active_game_ids and (prev_g.get("has_line") or prev_g.get("has_total")):
            nba_games_slate.append(prev_g)
            if prev_props_by_gid.get(gid):
                nba_props_slate.extend(prev_props_by_gid[gid])

    with open("nba_games_slate.json", "w", encoding="utf-8") as f:
        json.dump(nba_games_slate, f, indent=2)

    with open("nba_props_slate.json", "w", encoding="utf-8") as f:
        json.dump(nba_props_slate, f, indent=2)

    print(f"Wrote {len(nba_games_slate)} games and {len(nba_props_slate)} active prop players ({generated_at}).")


if __name__ == "__main__":
    run_slate()
