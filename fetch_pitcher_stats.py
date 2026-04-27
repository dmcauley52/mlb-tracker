import psycopg2
import os
import time
import json
import urllib.request
import statsapi
from datetime import date, timedelta
from dotenv import load_dotenv
load_dotenv()

SEASON = 2026
MIN_INNINGS = 1.0

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()

yesterday = date.today() - timedelta(days=1)
yesterday_str = str(yesterday)

# ── Get qualified pitchers ─────────────────────────────────────────────
print("Fetching qualified pitchers...")
leaders = statsapi.get("stats", {
    "stats": "season",
    "group": "pitching",
    "season": SEASON,
    "playerPool": "ALL",
    "limit": 500,
})
splits = leaders.get("stats", [{}])[0].get("splits", [])
qualified = []
for entry in splits:
    ip = float(entry.get("stat", {}).get("inningsPitched", 0) or 0)
    player = entry.get("player", {})
    if ip >= MIN_INNINGS:
        qualified.append({
            "id":   player.get("id"),
            "name": player.get("fullName")
        })
print(f"Fetching {yesterday_str} stats for {len(qualified)} pitchers...")

# ── Fetch game logs via direct API call ───────────────────────────────
for player in qualified:
    pid  = player["id"]
    name = player["name"]
    try:
        url = f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=pitching&season={SEASON}"
        with urllib.request.urlopen(url) as response:
            raw = json.loads(response.read().decode())
        game_splits = raw.get("stats", [{}])[0].get("splits", [])
        for game in game_splits:
            gdate = game.get("date")
            if gdate != yesterday_str:
                continue
            s        = game.get("stat", {})
            team     = game.get("team",     {}).get("name", "")
            opponent = game.get("opponent", {}).get("name", "")
            ip       = float(s.get("inningsPitched", 0) or 0)
            if ip < MIN_INNINGS:
                continue
            # Bill James game score
            outs = int(ip) * 3 + round((ip % 1) * 10)
            gs = (50
                  + 3 * outs
                  + 2 * int(ip)
                  + s.get("strikeOuts", 0)
                  - 2 * s.get("hits", 0)
                  - 4 * s.get("earnedRuns", 0)
                  - 2 * s.get("baseOnBalls", 0)
                  - s.get("homeRuns", 0))
            era_game  = round(s.get("earnedRuns", 0) / ip * 9, 2) if ip > 0 else 0
            whip_game = round((s.get("hits", 0) + s.get("baseOnBalls", 0)) / ip, 2) if ip > 0 else 0
            is_starter = bool(s.get("gamesStarted", 0))
            cur.execute("""
                INSERT INTO pitcher_gamelogs
                (player_id, player_name, game_date, season, team, opponent,
                 innings_pitched, hits_allowed, runs_allowed, earned_runs,
                 walks, strikeouts, home_runs_allowed, era, whip,
                 pitches, strikes, game_score, is_starter)
                VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s)
                ON CONFLICT (player_id, game_date) DO NOTHING
            """, (
                pid, name, gdate, SEASON, team, opponent,
                ip,
                s.get("hits", 0),
                s.get("runs", 0),
                s.get("earnedRuns", 0),
                s.get("baseOnBalls", 0),
                s.get("strikeOuts", 0),
                s.get("homeRuns", 0),
                era_game,
                whip_game,
                s.get("numberOfPitches", 0),
                s.get("strikes", 0),
                gs,
                is_starter,
            ))
        conn.commit()
        time.sleep(0.3)
    except Exception as e:
        print(f"  ✗ {name}: {e}")
        conn.rollback()

cur.close()
conn.close()
print(f"Nightly pitcher update complete for {yesterday_str}")
