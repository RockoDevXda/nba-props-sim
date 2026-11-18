import os
import json
import time
import math
import random
import statistics
import requests
from datetime import datetime

DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")  # Optional: free key from the-odds-api.com for live sportsbook prop lines

HEADERS = {"User-Agent": "Mozilla/5.0"}


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
        games.append({
            "game_id": event["id"],
            "matchup": f"{away['team']['abbreviation']} @ {home['team']['abbreviation']}",
            "away_id": away["team"]["id"],
            "away_abbr": away["team"]["abbreviation"],
            "home_id": home["team"]["id"],
            "home_abbr": home["team"]["abbreviation"],
            "spread": odds_info.get("details", "PK"),
            "over_under": float(odds_info.get("overUnder", 226.5) or 226.5),
        })
    return games


def fetch_team_roster_and_injuries(team_id, team_abbr, opponent_abbr, game_total, is_home):
    """Fetches active rotation players and adjusts usage if key teammates are OUT."""
    url = f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster"
    resp = requests.get(url, headers=HEADERS, timeout=15)
    if resp.status_code != 200:
        return []

    athletes = resp.json().get("athletes", [])
    out_players = []
    active_players = []

    for p in athletes:
        injuries = p.get("injuries", [])
        status = injuries[0].get("status", "Active") if injuries else "Active"
        if status.lower() in ("out", "doubtful"):
            out_players.append(p.get("displayName"))
        else:
            active_players.append({
                "id": p["id"],
                "name": p["displayName"],
                "pos": p.get("position", {}).get("abbreviation", "G"),
                "team": team_abbr,
                "opponent": opponent_abbr,
                "game_total": game_total,
                "is_home": is_home,
                "status": status,
            })

    # Calculate usage bump if rotation players are sidelined
    usage_bump = min(1.0 + (len(out_players) * 0.035), 1.14)
    for p in active_players:
        p["usage_bump"] = usage_bump
        p["out_teammates"] = out_players

    return active_players


def fetch_player_gamelog(player_id):
    """Pulls current season game logs (MIN, PTS, REB, AST, 3PM) for distribution modeling."""
    url = f"https://site.web.api.espn.com/apis/common/v3/sports/basketball/nba/athletes/{player_id}/gamelog"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code != 200:
            return None
        data = resp.json()
        labels = data.get("labels", [])
        if not labels:
            return None

        idx_min = labels.index("MIN") if "MIN" in labels else -1
        idx_pts = labels.index("PTS") if "PTS" in labels else -1
        idx_reb = labels.index("REB") if "REB" in labels else -1
        idx_ast = labels.index("AST") if "AST" in labels else -1
        idx_3pt = labels.index("3PT") if "3PT" in labels else -1

        logs = []
        for season_type in data.get("seasonTypes", []):
            for cat in season_type.get("categories", []):
                for event in cat.get("events", []):
                    stats = event.get("stats", [])
                    if len(stats) <= max(idx_min, idx_pts, idx_reb, idx_ast):
                        continue
                    mins = float(stats[idx_min])
                    if mins < 18.0:  # Filter out injury-shortened appearances
                        continue
                    fg3_made = float(stats[idx_3pt].split("-")[0]) if idx_3pt != -1 and "-" in stats[idx_3pt] else 0.0
                    logs.append({
                        "min": mins,
                        "pts": float(stats[idx_pts]),
                        "reb": float(stats[idx_reb]),
                        "ast": float(stats[idx_ast]),
                        "fg3m": fg3_made,
                    })
        return logs[:15]  # Most recent 15 games
    except Exception:
        return None


def simulate_player_props(player, logs, n_sims=10000):
    if not logs or len(logs) < 3:
        return None

    avg_min = statistics.mean(g["min"] for g in logs)
    if avg_min < 24.0:  # Focus on primary starters & sixth men
        return None

    std_min = statistics.stdev(g["min"] for g in logs) if len(logs) > 1 else 3.2
    std_min = max(2.2, min(std_min, 5.5))

    # Per-minute rates with game-environment pace scaling (226.5 league avg total)
    pace_mult = player["game_total"] / 226.5
    home_mult = 1.015 if player["is_home"] else 0.985
    bump = player["usage_bump"]

    pts_per_min = (statistics.mean(g["pts"] for g in logs) / avg_min) * pace_mult * home_mult * bump
    reb_per_min = (statistics.mean(g["reb"] for g in logs) / avg_min) * pace_mult * (1.0 + (bump - 1.0) * 0.4)
    ast_per_min = (statistics.mean(g["ast"] for g in logs) / avg_min) * pace_mult * bump
    fg3_per_min = (statistics.mean(g["fg3m"] for g in logs) / avg_min) * pace_mult * bump

    # Set baseline prop lines (rounded to nearest 0.5 around raw season average)
    raw_pts = statistics.mean(g["pts"] for g in logs)
    raw_reb = statistics.mean(g["reb"] for g in logs)
    raw_ast = statistics.mean(g["ast"] for g in logs)
    raw_pra = raw_pts + raw_reb + raw_ast

    pts_line = math.floor(raw_pts) + 0.5
    reb_line = math.floor(raw_reb) + 0.5
    ast_line = math.floor(raw_ast) + 0.5
    pra_line = math.floor(raw_pra) + 0.5

    pts_sims, reb_sims, ast_sims, fg3_sims, pra_sims = [], [], [], [], []
    pts_overs, reb_overs, ast_overs, pra_overs = 0, 0, 0, 0

    for _ in range(n_sims):
        sim_min = max(16.0, min(44.0, random.gauss(avg_min, std_min)))

        # Correlated scoring & 3PM simulation
        sim_3pm = max(0, round(random.gauss(fg3_per_min * sim_min, 1.15)))
        two_pt_and_ft = max(0.0, random.gauss(max(0.0, (pts_per_min * sim_min) - (sim_3pm * 3.0)), 4.6))
        sim_pts = round((sim_3pm * 3) + two_pt_and_ft)

        sim_reb = max(0, round(random.gauss(reb_per_min * sim_min, 2.3)))
        sim_ast = max(0, round(random.gauss(ast_per_min * sim_min, 1.9)))
        sim_pra = sim_pts + sim_reb + sim_ast

        pts_sims.append(sim_pts)
        reb_sims.append(sim_reb)
        ast_sims.append(sim_ast)
        fg3_sims.append(sim_3pm)
        pra_sims.append(sim_pra)

        if sim_pts > pts_line:
            pts_overs += 1
        if sim_reb > reb_line:
            reb_overs += 1
        if sim_ast > ast_line:
            ast_overs += 1
        if sim_pra > pra_line:
            pra_overs += 1

    proj_pts = round(statistics.mean(pts_sims), 1)
    proj_reb = round(statistics.mean(reb_sims), 1)
    proj_ast = round(statistics.mean(ast_sims), 1)
    proj_3pm = round(statistics.mean(fg3_sims), 1)
    proj_pra = round(statistics.mean(pra_sims), 1)

    # Identify strongest prop edge for this player
    candidates = [
        {"market": f"O {pts_line} PTS", "proj": proj_pts, "line": pts_line, "prob": round((pts_overs / n_sims) * 100, 1)},
        {"market": f"O {reb_line} REB", "proj": proj_reb, "line": reb_line, "prob": round((reb_overs / n_sims) * 100, 1)},
        {"market": f"O {ast_line} AST", "proj": proj_ast, "line": ast_line, "prob": round((ast_overs / n_sims) * 100, 1)},
        {"market": f"O {pra_line} PRA", "proj": proj_pra, "line": pra_line, "prob": round((pra_overs / n_sims) * 100, 1)},
    ]
    best_prop = max(candidates, key=lambda x: x["prob"])

    return {
        "player": player["name"],
        "team": player["team"],
        "opponent": player["opponent"],
        "status": player["status"],
        "usage_bump_pct": round((bump - 1.0) * 100, 1),
        "out_teammates": player["out_teammates"][:3],
        "proj_min": round(avg_min, 1),
        "proj_pts": proj_pts,
        "proj_reb": proj_reb,
        "proj_ast": proj_ast,
        "proj_3pm": proj_3pm,
        "proj_pra": proj_pra,
        "best_prop": best_prop,
        "all_props": candidates,
    }


def prob_to_american_odds(prob_pct):
    p = prob_pct / 100.0
    if p <= 0 or p >= 1:
        return "N/A"
    if p >= 0.5:
        return f"{int(round(-100 * p / (1 - p)))}"
    return f"+{int(round(100 * (1 - p) / p))}"


def send_discord_props_report(results, num_games):
    if not DISCORD_WEBHOOK_URL:
        print("DISCORD_WEBHOOK_URL not set; skipping Discord post.")
        return

    date_label = datetime.now().strftime("%b %d, %Y")
    if not results:
        requests.post(
            DISCORD_WEBHOOK_URL,
            json={"username": "NBA Prop Sim Engine", "content": f"🏀 **{date_label}:** No NBA games scheduled tonight."},
            timeout=15,
        )
        return

    ranked = sorted(results, key=lambda x: x["best_prop"]["prob"], reverse=True)
    top_8 = ranked[:8]

    # Suggested 3-Leg Top Prop Slip
    top_3 = ranked[:3]
    parlay_prob = 1.0
    for r in top_3:
        parlay_prob *= (r["best_prop"]["prob"] / 100.0)
    parlay_pct = round(parlay_prob * 100, 1)

    fields = []
    for r in top_8:
        bp = r["best_prop"]
        bump_note = f" • ⚡ +{r['usage_bump_pct']}% Usg (Out: {', '.join(r['out_teammates'])})" if r["usage_bump_pct"] > 0 else ""
        fields.append({
            "name": f"🔥 {r['player']} ({r['team']} vs {r['opponent']}) — {bp['market']} ({bp['prob']}% | Fair {prob_to_american_odds(bp['prob'])})",
            "value": (
                f"Sim Proj: **{r['proj_pts']} PTS** / **{r['proj_reb']} REB** / **{r['proj_ast']} AST** / **{r['proj_3pm']} 3PM** "
                f"(Line `{bp['line']}` → Sim `{bp['proj']}`){bump_note}"
            ),
            "inline": False,
        })

    slip_text = " + ".join([f"**{r['player']} {r['best_prop']['market']}** ({r['best_prop']['prob']}%)" for r in top_3])
    fields.append({
        "name": f"🎯 Top 3-Leg Prop Slip — Hit Rate: {parlay_pct}% ({prob_to_american_odds(parlay_pct)})",
        "value": slip_text,
        "inline": False,
    })

    embed = {
        "title": f"🏀 NBA Player Props Monte Carlo Report — {date_label}",
        "description": f"Simulated **{len(results)} rotation players** across **{num_games} NBA games** (`10,000` runs each).",
        "color": 15105570,  # NBA Orange (#E67E22)
        "fields": fields,
        "footer": {"text": "NBA Prop Sim Engine • Full projections attached in nba_props_slate.json"},
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    payload = {"username": "NBA Prop Sim Engine", "embeds": [embed]}

    with open("nba_props_slate.json", "rb") as json_file:
        files = {
            "payload_json": (None, json.dumps(payload), "application/json"),
            "files[0]": ("nba_props_slate.json", json_file, "application/json"),
        }
        resp = requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=20)
        if resp.status_code == 429:
            time.sleep(float(resp.json().get("retry_after", 2)))
            json_file.seek(0)
            requests.post(DISCORD_WEBHOOK_URL, files=files, timeout=20)
        else:
            resp.raise_for_status()


if __name__ == "__main__":
    games = fetch_todays_nba_games()
    all_candidates = []

    for g in games:
        all_candidates.extend(
            fetch_team_roster_and_injuries(g["away_id"], g["away_abbr"], g["home_abbr"], g["over_under"], is_home=False)
        )
        all_candidates.extend(
            fetch_team_roster_and_injuries(g["home_id"], g["home_abbr"], g["away_abbr"], g["over_under"], is_home=True)
        )

    sim_results = []
    for player in all_candidates:
        logs = fetch_player_gamelog(player["id"])
        res = simulate_player_props(player, logs, n_sims=10000)
        if res:
            sim_results.append(res)
        time.sleep(0.15)  # Polite pacing for game log requests

    with open("nba_props_slate.json", "w") as f:
        json.dump(sim_results, f, indent=2)

    send_discord_props_report(sim_results, len(games))
    print(f"Simulated {len(sim_results)} NBA players across {len(games)} games.")
