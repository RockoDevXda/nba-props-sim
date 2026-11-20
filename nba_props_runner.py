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


def normalize
