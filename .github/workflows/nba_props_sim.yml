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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
}

team_defense_cache = {}


def fetch_yesterdays_teams():
    """Detects teams playing on a Back-to-Back (0 days rest)."""
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


def fetch_team_defensive_factor(team_id):
    """Pulls opponent points allowed per game from ESPN to adjust matchup difficulty."""
    if team_id in team_defense_cache:
        return team_defense_cache[team_id]

    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/statistics"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            cats = resp.json().get("results", {}).get("stats", {}).get("categories", [])
            for cat in cats:
                for stat in cat.get("stats", []):
                    if stat.get("name") in ("avgPointsAgainst", "pointsAgainst"):
                        val = float(stat.get("value", 113.5) or 113.5)
                        if 95.0 < val < 135.0:
                            # League avg ~113.5 PPG allowed; >117 is soft defense, <109 is elite defense
                            mult = max(0.94, min(1.06, val / 113.5))
                            res = {"pts_allowed": round(val, 1), "mult": mult}
                            team_defense_cache[team_id] = res
                            return res
    except Exception:
        pass

    res = {"pts_allowed": 113.5, "mult": 1.0}
    team_defense_cache[team_id] = res
    return res


def fetch_todays_nba_games():
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

        odds_info = comp.get("odds", [{}])[0] if comp.get("odds") else {}
        game_total = float(odds_info.get("overUnder", 226.0) or 226.0)
        home_spread = float(odds_info.get("spread", 0.0) or 0.0)

        games.append({
            "game_id": event["id"],
            "matchup": f"{away['team']['abbreviation']} @ {home['team']['abbreviation']}",
            "game_total": game_total,
            "home_spread": home_spread,
            "away_id": away["team"]["id"],
            "away_abbr": away["team"]["abbreviation"],
            "home_id": home["team"]["id"],
            "home_abbr": home["team"]["abbreviation"],
        })
    return games


def fetch_team_roster_and_injuries(team_id):
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return [], 0
        athletes = resp.json().get("athletes", [])
        active_players = []
        out_count = 0

        for ath in athletes:
            injuries = ath.get("injuries", [])
            status = injuries[0].get("status", "").lower() if injuries else ""
            if status in ("out", "doubtful", "injured reserve", "suspension"):
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
                        if mins < 10.0:
                            continue
                        pts = float(stats[idx_pts])
                        reb = float(stats[idx_reb])
                        ast = float(stats[idx_ast])
                        tpm = 0.0
                        if idx_3pt >= 0 and "-" in str(stats[idx_3pt]):
                            tpm = float(str(stats[idx_3pt]).split("-")[0])
                        elif idx_3pt >= 0:
                            tpm = float(stats[idx_3pt])
                        logs.append({
                            "min": mins,
                            "pts": pts,
                            "reb": reb,
                            "ast": ast,
                            "tpm": tpm,
                            "pra": pts + reb + ast,
                        })
                    except (ValueError, TypeError):
                        continue

        return logs[:12] if len(logs) >= 4 else None
    except Exception:
        return None


def simulate_player_props(player, logs, game, is_home, out_teammates, opp_def, is_b2b, opp_b2b, n_sims=10000):
    avg_min = statistics.mean(g["min"] for g in logs)
    if avg_min < 22.0:
        return None

    pace_factor = max(0.92, min(1.08, game["game_total"] / 226.0))
    usage_bump = 1.0 + min(0.14, out_teammates * 0.035)
    team_spread = game["home_spread"] if is_home else -game["home_spread"]
    blowout_factor = 0.96 if abs(team_spread) >= 13.5 else 1.0
    def_mult = opp_def["mult"]
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

        overs = 0
        for _ in range(n_sims):
            sim_val = max(0.0, random.gauss(proj_mean, proj_std))
            if sim_val > line:
                overs += 1

        over_pct = round((overs / n_sims) * 100, 1)
        hits = sum(1 for g in recent_10 if g[log_key] > line)
        streak_dots = "".join("1" if g[log_key] > line else "0" for g in recent_10)

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
            "dots": streak_dots,
        })

    if not prop_edges:
        return None

    prop_edges.sort(key=lambda x: (x["prob"], x["hits"]), reverse=True)
    best_prop = prop_edges[0]

    # Build sharp situational tags
    tags = []
    usg_pct = round((usage_bump - 1.0) * 100, 1)
    if usg_pct > 0:
        tags.append(f"⚡ +{usg_pct}% USG")
    if opp_def["pts_allowed"] >= 116.5:
        tags.append(f"🎯 SOFT DEF ({opp_def['pts_allowed']})")
    if opp_b2b:
        tags.append("😴 OPP B2B")
    if best_prop["hits"] >= 8 and best_prop["sample"] >= 9:
        tags.append(f"🔥 {best_prop['hit_str']} L10")

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
        "opp_pts_allowed": opp_def["pts_allowed"],
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
    if p >= 0.5:
        return f"{int(round(-100 * p / (1 - p)))}"
    return f"+{int(round(100 * (1 - p) / p))}"


def send_discord_props_report(players_data):
    if not DISCORD_WEBHOOK_URL:
        return

    date_label = datetime.now().strftime("%b %d, %Y")
    if not players_data:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"username": "NBA Prop Engine", "content": f"🔥 **{date_label}:** No NBA games scheduled today."},
            timeout=15,
        )
        return

    ranked = sorted(players_data, key=lambda x: (x["best_prop"]["prob"], x["best_prop"]["hits"]), reverse=True)

    parlay_legs = []
    used_games = set()
    for p in ranked:
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
        fields.append({
            "name": f"Leg {i+1}: {p['player']} {bp['market']} ({bp['prob']}% | L10: {bp['hit_str']})",
            "value": f"**{p['team']}** vs {p['opponent']} • Sim: `{bp['proj']}`{tag_line}",
            "inline": False,
        })

    embed_parlay = {
        "title": f"🔥 Top 4-Leg NBA Sharp Prop Parlay — {date_label}",
        "url": DASHBOARD_URL,
        "description": (
            f"**Combined Hit Rate:** `{parlay_pct}%` (**Fair Odds: {prob_to_american_odds(parlay_pct)}**)\n"
            f"10,000-run Monte Carlo with Defense, B2B & Usage adjustments. [Open Command Center]({DASHBOARD_URL})"
        ),
        "color": 15105570,
        "fields": fields,
        "footer": {"text": "SimLab NBA Pro Engine • ESPN Telemetry"},
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }

    top_lines = [
        f"• **{p['player']}** ({p['team']}) — `{p['best_prop']['market']}` | Sim: `{p['best_prop']['proj']}` (**{p['best_prop']['prob']}%** | {p['best_prop']['hit_str']})"
        for p in ranked[:20]
    ]
    embed_board = {
        "title": f"📊 Top 20 NBA Prop Edges ({len(players_data)} Players Simulated)",
        "description": "\n".join(top_lines)[:3900],
        "color": 3447003,
    }

    payload = {"username": "NBA Prop Engine", "embeds": [embed_parlay, embed_board]}
    with open("nba_props_slate.json", "rb") as jf:
        files = {
            "payload_json": (None, json.dumps(payload), "application/json"),
            "files[0]": ("nba_props_slate.json", jf, "application/json"),
        }
        resp = requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=20)
        if resp.status_code == 429:
            time.sleep(float(resp.json().get("retry_after", 2)))
            jf.seek(0)
            requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=20)
        else:
            resp.raise_for_status()


if __name__ == "__main__":
    b2b_set = fetch_yesterdays_teams()
    games = fetch_todays_nba_games()
    all_simulated_players = []

    for g in games:
        away_def = fetch_team_defensive_factor(g["away_id"])
        home_def = fetch_team_defensive_factor(g["home_id"])

        for side, is_home, opp_def in [("away", False, home_def), ("home", True, away_def)]:
            team_abbr = g[f"{side}_abbr"]
            opp_abbr = g["home_abbr" if side == "away" else "away_abbr"]
            roster, out_cnt = fetch_team_roster_and_injuries(g[f"{side}_id"])

            for ath in roster[:9]:
                logs = fetch_player_gamelog(ath["id"])
                if logs:
                    sim = simulate_player_props(
                        ath,
                        logs,
                        g,
                        is_home,
                        out_cnt,
                        opp_def,
                        is_b2b=(team_abbr in b2b_set),
                        opp_b2b=(opp_abbr in b2b_set),
                        n_sims=10000,
                    )
                    if sim:
                        all_simulated_players.append(sim)
                time.sleep(0.1)

    with open("nba_props_slate.json", "w") as f:
        json.dump(all_simulated_players, f, indent=2)

    send_discord_props_report(all_simulated_players)
    print(f"Completed NBA prop simulations for {len(all_simulated_players)} players via ESPN.")
