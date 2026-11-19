import os
import math
import json
import time
import random
import statistics
import requests
from datetime import datetime, timedelta, timezone

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://rockodevxda.github.io/sports-command-center/")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"}
team_profile_cache = {}


def safe_float(val, default=0.0):
    try:
        return float(val) if val is not None else default
    except (ValueError, TypeError):
        return default


def load_previous_nba_lines():
    if not os.path.exists("nba_games_slate.json"):
        return {}
    try:
        with open("nba_games_slate.json", "r") as f:
            prev = json.load(f)
        if not isinstance(prev, list):
            return {}
        out = {}
        for g in prev:
            gid = str(g.get("id", ""))
            if gid:
                out[gid] = {
                    "open_line": str(g.get("open_line") or g.get("market_line") or "PK"),
                    "open_spread": safe_float(g.get("open_spread"), 0.0),
                    "open_total": safe_float(g.get("open_total"), 0.0),
                }
        return out
    except Exception:
        return {}


def fetch_yesterdays_teams():
    yest_str = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={yest_str}"
    b2b_teams = set()
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code == 200:
            for event in resp.json().get("events", []):
                for comp in event.get("competitions", [{}])[0].get("competitors", []):
                    abbr = comp.get("team", {}).get("abbreviation")
                    if abbr:
                        b2b_teams.add(abbr)
    except Exception:
        pass
    return b2b_teams


def fetch_team_profile(team_id, record_str="0-0"):
    if team_id in team_profile_cache:
        return team_profile_cache[team_id]
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/statistics"
    ppg, pts_allowed, pace = 114.0, 114.0, 99.5
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            cats = resp.json().get("results", {}).get("stats", {}).get("categories", [])
            fga, fta, orb, tov = 88.0, 22.0, 10.5, 13.5
            for cat in cats:
                for stat in cat.get("stats", []):
                    n = stat.get("name", "")
                    v = safe_float(stat.get("value"), 0.0)
                    if n == "avgPoints" and v > 80:
                        ppg = v
                    elif n in ("avgPointsAgainst", "pointsAgainst") and 80 < v < 140:
                        pts_allowed = v
                    elif n == "avgFieldGoalsAttempted" and v > 50:
                        fga = v
                    elif n == "avgFreeThrowsAttempted" and v > 5:
                        fta = v
                    elif n == "avgOffensiveRebounds" and v > 0:
                        orb = v
                    elif n == "avgTurnovers" and v > 0:
                        tov = v
            pace = max(94.0, min(105.0, fga - orb + tov + (0.44 * fta)))
    except Exception:
        pass

    parts = record_str.split("-")
    win_pct = int(parts[0]) / max(1, int(parts[0]) + int(parts[1])) if len(parts) == 2 else 0.5
    if pts_allowed == 114.0:
        pts_allowed = round(117.5 - (win_pct * 7.0), 1)

    off_rtg = round((ppg / pace) * 100.0, 1)
    def_rtg = round((pts_allowed / pace) * 100.0, 1)
    res = {
        "ppg": round(ppg, 1),
        "pts_allowed": round(pts_allowed, 1),
        "pace": round(pace, 1),
        "off_rtg": off_rtg,
        "def_rtg": def_rtg,
        "def_mult": max(0.94, min(1.06, pts_allowed / 114.0)),
    }
    team_profile_cache[team_id] = res
    return res


def fetch_h2h_history(away_id, away_abbr, home_id, home_abbr):
    """Pulls season/recent head-to-head matchups between these two NBA teams from ESPN schedule."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{home_id}/schedule"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return {"summary": "First meeting this season", "avg_total": 0.0, "games": 0}
        events = resp.json().get("events", [])
        h2h_games = []
        for ev in events:
            comp = ev.get("competitions", [{}])[0]
            if not comp.get("status", {}).get("type", {}).get("completed"):
                continue
            comps = comp.get("competitors", [])
            ids = {str(c.get("team", {}).get("id")) for c in comps}
            if str(away_id) in ids and str(home_id) in ids:
                h_c = next(c for c in comps if str(c["team"]["id"]) == str(home_id))
                a_c = next(c for c in comps if str(c["team"]["id"]) == str(away_id))
                h_pts = safe_float(h_c.get("score", {}).get("value") if isinstance(h_c.get("score"), dict) else h_c.get("score"), 0)
                a_pts = safe_float(a_c.get("score", {}).get("value") if isinstance(a_c.get("score"), dict) else a_c.get("score"), 0)
                if h_pts > 0 and a_pts > 0:
                    h2h_games.append((a_pts, h_pts))
        if not h2h_games:
            return {"summary": "1st H2H meeting this season", "avg_total": 0.0, "games": 0}
        away_w = sum(1 for a, h in h2h_games if a > h)
        home_w = len(h2h_games) - away_w
        avg_tot = round(sum(a + h for a, h in h2h_games) / len(h2h_games), 1)
        last_a, last_h = h2h_games[-1]
        leader = f"{away_abbr} {away_w}-{home_w}" if away_w > home_w else (f"{home_abbr} {home_w}-{away_w}" if home_w > away_w else f"Tied {away_w}-{home_w}")
        return {
            "summary": f"H2H: {leader} • Last: {away_abbr} {int(last_a)}-{int(last_h)} • Avg Tot {avg_tot}",
            "avg_total": avg_tot,
            "games": len(h2h_games),
        }
    except Exception:
        return {"summary": "H2H telemetry active", "avg_total": 0.0, "games": 0}


def fetch_todays_nba_games(prev_lines, b2b_set):
    today_str = datetime.now().strftime("%Y%m%d")
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={today_str}"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    games = []
    for event in data.get("events", []):
        comp = event["competitions"][0]
        competitors = comp["competitors"]
        home = next(c for c in competitors if c["homeAway"] == "home")
        away = next(c for c in competitors if c["homeAway"] == "away")

        away_abbr = away["team"]["abbreviation"]
        home_abbr = home["team"]["abbreviation"]
        away_short = away["team"].get("shortDisplayName", away_abbr)
        home_short = home["team"].get("shortDisplayName", home_abbr)

        odds_list = comp.get("odds") or []
        odds_info = odds_list[0] if isinstance(odds_list, list) and len(odds_list) > 0 and isinstance(odds_list[0], dict) else {}
        spread_details = str(odds_info.get("details") or "PK")
        home_spread = safe_float(odds_info.get("spread"), 0.0)
        game_ou = safe_float(odds_info.get("overUnder"), 226.0)
        has_line = bool(odds_info.get("details"))
        has_total = game_ou > 180.0

        gid = str(event["id"])
        prior = prev_lines.get(gid, {})
        prior_open_line = prior.get("open_line", "PK")
        prior_open_spread = safe_float(prior.get("open_spread"), 0.0)
        prior_open_total = safe_float(prior.get("open_total"), 0.0)

        open_line = prior_open_line if (prior_open_line and prior_open_line != "PK") else spread_details
        open_spread = prior_open_spread if (prior_open_spread != 0.0 or not has_line) else home_spread
        open_total = prior_open_total if prior_open_total > 180.0 else game_ou

        spread_move = round(home_spread - open_spread, 1) if has_line else 0.0
        total_move = round(game_ou - open_total, 1) if (has_total and open_total > 180.0) else 0.0

        move_badges = []
        if has_line and abs(spread_move) >= 1.0 and open_line != spread_details:
            move_badges.append(f"📉 {open_line} → {spread_details}")
        if has_total and abs(total_move) >= 1.5:
            arrow = "⬆️" if total_move > 0 else "⬇️"
            move_badges.append(f"{arrow} O/U {open_total:g} → {game_ou:g}")

        away_b2b = away_abbr in b2b_set
        home_b2b = home_abbr in b2b_set
        if away_b2b:
            move_badges.append(f"😴 {away_abbr} ROAD B2B")
        if home_b2b:
            move_badges.append(f"😴 {home_abbr} HOME B2B")

        tip_dt = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        tip_str = tip_dt.strftime("%I:%M %p UTC")

        games.append({
            "id": gid,
            "game_id": gid,
            "tipoff": tip_str,
            "matchup": f"{away_abbr} @ {home_abbr}",
            "open_line": open_line,
            "open_spread": open_spread,
            "market_line": spread_details,
            "home_spread": home_spread,
            "spread_move": spread_move,
            "open_total": open_total,
            "over_under": game_ou,
            "game_total": game_ou,
            "total_move": total_move,
            "move_badges": move_badges,
            "has_move": abs(spread_move) >= 1.0 or abs(total_move) >= 1.5,
            "has_b2b": away_b2b or home_b2b,
            "away_b2b": away_b2b,
            "home_b2b": home_b2b,
            "has_line": has_line,
            "has_total": has_total,
            "away_id": away["team"]["id"],
            "away_abbr": away_abbr,
            "away_short": away_short,
            "away_team": away["team"]["displayName"],
            "away_record": away.get("records", [{}])[0].get("summary", "0-0"),
            "home_id": home["team"]["id"],
            "home_abbr": home_abbr,
            "home_short": home_short,
            "home_team": home["team"]["displayName"],
            "home_record": home.get("records", [{}])[0].get("summary", "0-0"),
        })
    return games


def simulate_nba_game(game, away_prof, home_prof, h2h, n_sims=10000):
    exp_pace = (away_prof["pace"] + home_prof["pace"]) / 2.0
    league_rtg = 114.2
    hca = 2.4

    away_fatigue = -2.5 if game["away_b2b"] else 0.0
    home_fatigue = -1.3 if game["home_b2b"] else 0.0

    away_exp_rtg = league_rtg + (away_prof["off_rtg"] - league_rtg) + (home_prof["def_rtg"] - league_rtg) + away_fatigue
    home_exp_rtg = league_rtg + (home_prof["off_rtg"] - league_rtg) + (away_prof["def_rtg"] - league_rtg) + hca + home_fatigue

    away_wins, home_covers, away_covers, total_overs, total_unders = 0, 0, 0, 0, 0
    away_pts_sum, home_pts_sum = 0.0, 0.0
    home_spread, game_ou = game["home_spread"], game["over_under"]

    for _ in range(n_sims):
        poss = random.gauss(exp_pace, 3.1)
        a_pts = (random.gauss(away_exp_rtg, 11.4) / 100.0) * poss
        h_pts = (random.gauss(home_exp_rtg, 11.4) / 100.0) * poss
        comb = a_pts + h_pts
        away_pts_sum += a_pts
        home_pts_sum += h_pts

        if a_pts > h_pts:
            away_wins += 1
        if (h_pts + home_spread) > a_pts:
            home_covers += 1
        elif (h_pts + home_spread) < a_pts:
            away_covers += 1
        if game["has_total"]:
            if comb > game_ou:
                total_overs += 1
            elif comb < game_ou:
                total_unders += 1

    away_proj = round(away_pts_sum / n_sims, 1)
    home_proj = round(home_pts_sum / n_sims, 1)
    total_proj = round(away_proj + home_proj, 1)

    if h2h["games"] > 0 and h2h["avg_total"] > 180:
        total_proj = round((total_proj * 0.85) + (h2h["avg_total"] * 0.15), 1)

    away_prob = round((away_wins / n_sims) * 100, 1)
    home_prob = round(100.0 - away_prob, 1)
    favored_team = game["away_abbr"] if away_prob >= home_prob else game["home_abbr"]
    favored_prob = max(away_prob, home_prob)

    home_cover_pct = round((home_covers / n_sims) * 100, 1)
    away_cover_pct = round((away_covers / n_sims) * 100, 1)
    edge_pts = round(abs((home_proj - away_proj) - (-home_spread)), 1)

    if home_cover_pct >= away_cover_pct:
        spread_pick = f"{game['home_abbr']} {home_spread:+g}" if home_spread != 0 else f"{game['home_abbr']} PK"
        spread_cover_pct = home_cover_pct
    else:
        away_spread = -home_spread
        spread_pick = f"{game['away_abbr']} {away_spread:+g}" if away_spread != 0 else f"{game['away_abbr']} PK"
        spread_cover_pct = away_cover_pct

    if game["has_total"]:
        over_pct = round((total_overs / n_sims) * 100, 1)
        under_pct = round((total_unders / n_sims) * 100, 1)
        total_edge_pts = round(abs(total_proj - game_ou), 1)
        if over_pct >= under_pct:
            total_pick, total_cover_pct = f"O {game_ou:g}", over_pct
        else:
            total_pick, total_cover_pct = f"U {game_ou:g}", under_pct
    else:
        total_pick, total_cover_pct, total_edge_pts = "No Total", 0.0, 0.0

    return {
        **game,
        "away_proj": away_proj,
        "home_proj": home_proj,
        "total_proj": total_proj,
        "exp_pace": round(exp_pace, 1),
        "h2h_summary": h2h["summary"],
        "h2h_avg_total": h2h["avg_total"],
        "away_win_pct": away_prob,
        "home_win_pct": home_prob,
        "favored_team": favored_team,
        "favored_prob": favored_prob,
        "spread_pick": spread_pick,
        "spread_cover_pct": spread_cover_pct,
        "edge_pts": edge_pts,
        "total_pick": total_pick,
        "total_cover_pct": total_cover_pct,
        "total_edge_pts": total_edge_pts,
        "generated_at": datetime.now().strftime("%Y-%m-%d %I:%M %p"),
    }


def fetch_team_roster_and_injuries(team_id):
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return [], 0
        athletes = resp.json().get("athletes", [])
        active_players, out_count = [], 0
        for ath in athletes:
            injuries = ath.get("injuries", [])
            status = injuries[0].get("status", "").lower() if injuries else ""
            if status in ("out", "doubtful", "injured reserve", "suspension"):
                if status == "out":
                    out_count += 1
                continue
            active_players.append({
                "id": ath["id"],
                "name": ath["displayName"],
                "pos": ath.get("position", {}).get("abbreviation", "F"),
                "questionable": status == "questionable",
            })
        return active_players, out_count
    except Exception:
        return [], 0


def fetch_player_gamelog(player_id):
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{player_id}/gamelog"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        labels = [lbl.upper() for lbl in data.get("labels", [])]
        if not labels:
            return None
        idx_min = labels.index("MIN") if "MIN" in labels else -1
        idx_pts = labels.index("PTS") if "PTS" in labels else -1
        idx_reb = labels.index("REB") if "REB" in labels else -1
        idx_ast = labels.index("AST") if "AST" in labels else -1
        idx_3pt = next((i for i, l in enumerate(labels) if l in ("3PT", "3PM")), -1)
        if min(idx_min, idx_pts, idx_reb, idx_ast) < 0:
            return None
        logs = []
        for st in data.get("seasonTypes", []):
            for cat in st.get("categories", []):
                for ev in cat.get("events", []):
                    stats = ev.get("stats", [])
                    if len(stats) <= max(idx_min, idx_pts, idx_reb, idx_ast):
                        continue
                    try:
                        mins = float(stats[idx_min])
                        if mins < 8.0:
                            continue
                        pts, reb, ast = float(stats[idx_pts]), float(stats[idx_reb]), float(stats[idx_ast])
                        tpm = float(str(stats[idx_3pt]).split("-")[0]) if (idx_3pt >= 0 and "-" in str(stats[idx_3pt])) else (float(stats[idx_3pt]) if idx_3pt >= 0 else 0.0)
                        logs.append({"min": mins, "pts": pts, "reb": reb, "ast": ast, "tpm": tpm, "pra": pts + reb + ast})
                    except (ValueError, TypeError):
                        continue
        return logs[:12] if len(logs) >= 3 else None
    except Exception:
        return None


def simulate_player_props(player, logs, game, is_home, out_teammates, opp_prof, is_b2b, opp_b2b, n_sims=10000):
    avg_min = statistics.mean(g["min"] for g in logs)
    if avg_min < 18.0:
        return None

    pace_factor = max(0.92, min(1.08, game["game_total"] / 226.0))
    usage_bump = 1.0 + min(0.06, max(0, out_teammates - 1) * 0.02)
    team_spread = game["home_spread"] if is_home else -game["home_spread"]
    blowout_factor = 0.96 if abs(team_spread) >= 13.5 else 1.0
    def_mult = opp_prof["def_mult"]
    fatigue_mult = 0.97 if (is_b2b and not is_home) else (1.02 if opp_b2b else 1.0)

    def weighted_mean_std(vals):
        weights = [1.35 if i < 5 else 1.0 for i in range(len(vals))]
        w_mean = sum(v * w for v, w in zip(vals, weights)) / sum(weights)
        w_std = statistics.pstdev(vals) if len(vals) > 1 else max(1.5, w_mean * 0.25)
        return w_mean, max(1.1, w_std)

    pts_m, pts_s = weighted_mean_std([g["pts"] for g in logs])
    reb_m, reb_s = weighted_mean_std([g["reb"] for g in logs])
    ast_m, ast_s = weighted_mean_std([g["ast"] for g in logs])
    tpm_m, tpm_s = weighted_mean_std([g["tpm"] for g in logs])

    adj_pts = pts_m * pace_factor * usage_bump * blowout_factor * def_mult * fatigue_mult
    adj_reb = reb_m * (1.0 + (pace_factor - 1.0) * 0.6) * blowout_factor
    adj_ast = ast_m * pace_factor * (1.0 + (usage_bump - 1.0) * 0.75) * blowout_factor * ((1.0 + def_mult) / 2.0)
    adj_tpm = tpm_m * pace_factor * usage_bump * blowout_factor * def_mult

    recent_10 = logs[:10]
    markets_spec = [
        ("PTS", "pts", adj_pts, pts_s, math.floor(pts_m) + 0.5),
        ("REB", "reb", adj_reb, reb_s, math.floor(reb_m) + 0.5),
        ("AST", "ast", adj_ast, ast_s, math.floor(ast_m) + 0.5),
        ("3PM", "tpm", adj_tpm, tpm_s, math.floor(tpm_m) + 0.5),
        ("PRA", "pra", adj_pts + adj_reb + adj_ast, math.sqrt(pts_s**2 + reb_s**2 + ast_s**2), math.floor(pts_m + reb_m + ast_m) + 0.5),
    ]

    prop_edges = []
    for stat_name, log_key, proj_mean, proj_std, line in markets_spec:
        if line < 0.5:
            continue
        overs = sum(1 for _ in range(n_sims) if max(0.0, random.gauss(proj_mean, proj_std)) > line)
        over_pct = round((overs / n_sims) * 100, 1)
        hits = sum(1 for g in recent_10 if g[log_key] > line)
        prop_edges.append({
            "stat": stat_name,
            "market": f"O {line} {stat_name}",
            "side": "O",
            "line": line,
            "proj": round(proj_mean, 1),
            "prob": over_pct,
            "hit_str": f"{hits}/{len(recent_10)}",
            "hits": hits,
            "sample": len(recent_10),
            "dots": "".join("1" if g[log_key] > line else "0" for g in recent_10),
        })

    if not prop_edges:
        return None
    prop_edges.sort(key=lambda x: (x["prob"], x["hits"]), reverse=True)
    best_prop = prop_edges[0]

    tags = []
    usg_pct = round((usage_bump - 1.0) * 100, 1)
    if usg_pct > 0:
        tags.append(f"⚡ +{usg_pct}% USG")
    if opp_prof["pts_allowed"] >= 116.5:
        tags.append(f"🎯 SOFT DEF ({opp_prof['pts_allowed']})")
    if opp_b2b:
        tags.append("😴 OPP B2B")
    if best_prop["hits"] >= 8 and best_prop["sample"] >= 9:
        tags.append(f"🔥 {best_prop['hit_str']} L10")
    if player.get("questionable"):
        tags.append("⚠️ GTD")

    return {
        "id": player["id"],
        "game_id": game["game_id"],
        "player": player["name"],
        "pos": player["pos"],
        "team": game["home_abbr"] if is_home else game["away_abbr"],
        "opponent": game["away_abbr"] if is_home else game["home_abbr"],
        "matchup": game["matchup"],
        "game_total": game["game_total"],
        "avg_min": round(avg_min, 1),
        "usage_bump_pct": usg_pct,
        "opp_pts_allowed": opp_prof["pts_allowed"],
        "is_b2b": is_b2b,
        "opp_b2b": opp_b2b,
        "tags": tags,
        "proj_pts": round(adj_pts, 1),
        "proj_reb": round(adj_reb, 1),
        "proj_ast": round(adj_ast, 1),
        "proj_3pm": round(adj_tpm, 1),
        "proj_pra": round(adj_pts + adj_reb + adj_ast, 1),
        "best_prop": best_prop,
        "all_props": prop_edges[:4],
        "generated_at": datetime.now().strftime("%Y-%m-%d %I:%M %p"),
    }


def prob_to_american_odds(prob_pct):
    p = prob_pct / 100.0
    if p <= 0 or p >= 1:
        return "N/A"
    return f"{int(round(-100 * p / (1 - p)))}" if p >= 0.5 else f"+{int(round(100 * (1 - p) / p))}"


def send_discord_nba_report(players_data, games_data):
    if not DISCORD_WEBHOOK_URL:
        return
    try:
        date_label = datetime.now().strftime("%b %d, %Y")
        if not games_data and not players_data:
            requests.post(DISCORD_WEBHOOK_URL, json={"username": "NBA Pro Engine", "content": f"🔥 **{date_label}:** No NBA games scheduled today."}, timeout=15)
            return

        ranked_p = sorted(players_data, key=lambda x: (x["best_prop"]["prob"], x["best_prop"]["hits"]), reverse=True)
        parlay_legs, used_games = [], set()
        for p in ranked_p:
            if len(parlay_legs) >= 4:
                break
            if p["game_id"] not in used_games:
                parlay_legs.append(p)
                used_games.add(p["game_id"])

        parlay_prob = 1.0
        for leg in parlay_legs:
            parlay_prob *= (leg["best_prop"]["prob"] / 100.0)
        parlay_pct = round(parlay_prob * 100, 1) if parlay_legs else 0.0

        fields = []
        for i, p in enumerate(parlay_legs):
            bp = p["best_prop"]
            tag_line = f" • {' '.join(p['tags'])}" if p.get("tags") else ""
            fields.append({"name": f"Leg {i+1}: {p['player']} {bp['market']} ({bp['prob']}% | L10: {bp['hit_str']})", "value": f"**{p['team']}** vs {p['opponent']} • Sim: `{bp['proj']}`{tag_line}", "inline": False})

        embed_parlay = {
            "title": f"🔥 Top 4-Leg NBA Sharp Prop Parlay — {date_label}",
            "url": DASHBOARD_URL,
            "description": f"**Combined Hit Rate:** `{parlay_pct}%` (**Fair Odds: {prob_to_american_odds(parlay_pct)}**)\nFull-game & player Monte Carlo with H2H, Defense, B2B & Usage adjustments. [Open Command Center]({DASHBOARD_URL})",
            "color": 15105570,
            "fields": fields,
            "footer": {"text": "SimLab NBA Pro Engine • Games + Props Telemetry"},
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

        game_lines = [
            f"• **{g['away_abbr']}** `{g['away_proj']}` @ **{g['home_abbr']}** `{g['home_proj']}` | Tot `{g['total_proj']}` `[{g['spread_pick']} ({g['spread_cover_pct']}%) | {g['total_pick']} ({g['total_cover_pct']}%)]`"
            for g in games_data[:15]
        ]
        embed_games = {
            "title": f"🏀 Full NBA Game Slate Simulations ({len(games_data)} Matchups)",
            "description": "\n".join(game_lines)[:3900] or "No games.",
            "color": 3447003,
        }

        payload = {"username": "NBA Pro Engine", "embeds": [embed_parlay, embed_games]}
        with open("nba_props_slate.json", "rb") as jf1, open("nba_games_slate.json", "rb") as jf2:
            files = {
                "payload_json": (None, json.dumps(payload), "application/json"),
                "files[0]": ("nba_props_slate.json", jf1, "application/json"),
                "files[1]": ("nba_games_slate.json", jf2, "application/json"),
            }
            resp = requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=20)
            if resp.status_code == 429:
                time.sleep(float(resp.json().get("retry_after", 2)))
                jf1.seek(0)
                jf2.seek(0)
                requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=20)
            else:
                resp.raise_for_status()
    except Exception as e:
        print(f"Discord notification warning: {e}")


if __name__ == "__main__":
    prev_lines = load_previous_nba_lines()
    b2b_set = fetch_yesterdays_teams()
    games = fetch_todays_nba_games(prev_lines, b2b_set)

    all_simulated_games = []
    all_simulated_players = []

    for g in games:
        away_prof = fetch_team_profile(g["away_id"], g["away_record"])
        home_prof = fetch_team_profile(g["home_id"], g["home_record"])
        h2h = fetch_h2h_history(g["away_id"], g["away_abbr"], g["home_id"], g["home_abbr"])

        game_sim = simulate_nba_game(g, away_prof, home_prof, h2h, n_sims=10000)
        all_simulated_games.append(game_sim)

        for side, is_home, opp_prof in [("away", False, home_prof), ("home", True, away_prof)]:
            team_abbr = g[f"{side}_abbr"]
            opp_abbr = g["home_abbr" if side == "away" else "away_abbr"]
            roster, out_cnt = fetch_team_roster_and_injuries(g[f"{side}_id"])
            for ath in roster:
                logs = fetch_player_gamelog(ath["id"])
                if logs:
                    sim = simulate_player_props(
                        ath, logs, g, is_home, out_cnt, opp_prof,
                        is_b2b=(team_abbr in b2b_set),
                        opp_b2b=(opp_abbr in b2b_set),
                        n_sims=10000,
                    )
                    if sim:
                        all_simulated_players.append(sim)
                time.sleep(0.08)

    with open("nba_games_slate.json", "w") as f:
        json.dump(all_simulated_games, f, indent=2)
    with open("nba_props_slate.json", "w") as f:
        json.dump(all_simulated_players, f, indent=2)

    send_discord_nba_report(all_simulated_players, all_simulated_games)
    print(f"Completed {len(all_simulated_games)} NBA game sims & {len(all_simulated_players)} player prop sims.")
