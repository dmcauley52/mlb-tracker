import statsapi
import psycopg2
import os
import time
from dotenv import load_dotenv
load_dotenv()

SEASON = 2025
MIN_AT_BATS = 100

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()

# ── Step 1: Get all players with 100+ AB this season ──────────────────
print("Fetching qualified batters...")
leaders = statsapi.get("stats", {
    "stats": "season",
    "group": "hitting",
    "season": SEASON,
    "playerPool": "ALL",
    "limit": 500,
    "offset": 0,
    "fields": "stats,splits,stat,atBats,player,id,fullName"
})

qualified = []
for entry in leaders.get("stats", [{}])[0].get("splits", []):
    ab = entry.get("stat", {}).get("atBats", 0)
    player = entry.get("player", {})
    if ab >= MIN_AT_BATS:
        qualified.append({
            "id": player.get("id"),
            "name": player.get("fullName"),
            "ab": ab
        })

print(f"Found {len(qualified)} players with {MIN_AT_BATS}+ at bats\n")

# ── Step 2: Fetch game logs for each player ────────────────────────────
for i, player in enumerate(qualified):
    pid = player["id"]
    name = player["name"]
    print(f"[{i+1}/{len(qualified)}] {name} ({player['ab']} AB)...")

    try:
        stats = statsapi.player_stat_data(
            pid, group="hitting", type="gameLog"
        )
        inserted = 0
        for game in stats.get("stats", []):
            s = game["stats"]
            gdate = game.get("date")
            if not gdate:
                continue
        # Get team and position from the game entry
        team = game.get("team", {}).get("name", "")
        position = game.get("position", {}).get("abbreviation", "")
        
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
        inserted += 1
        
        conn.commit()
        print(f"  ✓ {inserted} games inserted")

    except Exception as e:
        print(f"  ✗ Error: {e}")
        conn.rollback()

    # Be polite to the MLB API - don't hammer it
    time.sleep(0.5)

cur.close()
conn.close()
print("\nBackfill complete!")
