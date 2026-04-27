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
MIN_AT_BATS = 20

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()

yesterday = date.today() - timedelta(days=1)
yesterday_str = str(yesterday)

# ── Get qualified batters ──────────────────────────────────────────────
print("Fetching qualified batters...")
leaders = statsapi.get("stats", {
    "stats": "season",
    "group": "hitting",
    "season": SEASON,
    "playerPool": "ALL",
    "limit": 500,
})

splits = leaders.get("stats", [{}])[0].get("splits", [])
qualified = []
for entry in splits:
    ab = entry.get("stat", {}).get("atBats", 0)
    player = entry.get("player", {})
    if ab >= MIN_AT_BATS:
        qualified.append({
            "id": player.get("id"),
            "name": player.get("fullName")
        })

print(f"Fetching {yesterday_str} stats for {len(qualified)} players...")

# ── Fetch game logs via direct API call ───────────────────────────────
for player in qualified:
    pid = player["id"]
    name = player["name"]
    try:
        url = f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=hitting&season={SEASON}"
        with urllib.request.urlopen(url) as response:
            raw = json.loads(response.read().decode())

        game_splits = raw.get("stats", [{}])[0].get("splits", [])

        for game in game_splits:
            gdate = game.get("date")
            if gdate != yesterday_str:
                continue

            s = game.get("stat", {})
            team = game.get("team", {}).get("name", "")
            positions = game.get("positionsPlayed", [{}])
            position = positions[0].get("abbreviation", "") if positions else ""

            cur.execute("""
                INSERT INTO player_gamelogs
                (player_id, player_name, game_date, season, at_bats, hits,
                 home_runs, rbi, batting_avg, ops, team, position)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (player_id, game_date) DO NOTHING
            """, (pid, name, gdate, SEASON,
                  s.get("atBats", 0), s.get("hits", 0),
                  s.get("homeRuns", 0), s.get("rbi", 0),
                  s.get("avg", 0), s.get("ops", 0),
                  team, position))

        conn.commit()
        time.sleep(0.3)

    except Exception as e:
        print(f"  ✗ {name}: {e}")
        conn.rollback()

cur.close()
conn.close()
print(f"Nightly update complete for {yesterday_str}")
