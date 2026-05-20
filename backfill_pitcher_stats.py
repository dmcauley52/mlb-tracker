import psycopg2
import os
import time
import json
import urllib.request
from dotenv import load_dotenv
load_dotenv()

SEASON = int(os.getenv("SEASON", 2026))
MIN_INNINGS = 1.0   # at least 1 IP in the season to be included

conn = psycopg2.connect(os.getenv("DATABASE_URL"),
    keepalives=1, keepalives_idle=60, keepalives_interval=10, keepalives_count=5)
cur = conn.cursor()

# ── Step 0: Create table if it doesn't exist ──────────────────────────
cur.execute("""
    CREATE TABLE IF NOT EXISTS pitcher_gamelogs (
        player_id       int,
        player_name     text,
        game_date       date,
        season          int,
        team            text,
        opponent        text,
        innings_pitched numeric(4,1),
        hits_allowed    int,
        runs_allowed    int,
        earned_runs     int,
        walks           int,
        strikeouts      int,
        home_runs_allowed int,
        era             numeric(5,2),
        whip            numeric(4,2),
        pitches         int,
        strikes         int,
        game_score      int,
        is_starter      boolean,
        PRIMARY KEY (player_id, game_date)
    )
""")
conn.commit()
print("Table pitcher_gamelogs ready.\n")

# ── Step 1: Get qualified pitchers ─────────────────────────────────────
import statsapi
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
            "name": player.get("fullName"),
            "ip":   ip,
        })
print(f"Found {len(qualified)} pitchers with {MIN_INNINGS}+ IP\n")

# ── Step 2: Fetch game logs via direct API call ────────────────────────
for i, player in enumerate(qualified):
    pid  = player["id"]
    name = player["name"]
    print(f"[{i+1}/{len(qualified)}] {name} ({player['ip']} IP)...")
    try:
        url = (
            f"https://statsapi.mlb.com/api/v1/people/{pid}/stats"
            f"?stats=gameLog&group=pitching&season={SEASON}"
        )
        with urllib.request.urlopen(url) as response:
            raw = json.loads(response.read().decode())

        game_splits = raw.get("stats", [{}])[0].get("splits", [])
        inserted = 0

        for game in game_splits:
            s     = game.get("stat", {})
            gdate = game.get("date")
            if not gdate:
                continue

            team     = game.get("team",     {}).get("name", "")
            opponent = game.get("opponent", {}).get("name", "")

            ip = float(s.get("inningsPitched", 0) or 0)
            if ip < MIN_INNINGS:
                continue

            # Bill James game score (simplified):
            # 50 + 3*(outs recorded) + 2*(IP complete innings) + K - 2*H - 4*ER - 2*BB - HR
            outs = int(ip) * 3 + round((ip % 1) * 10)  # e.g. 6.2 IP → 20 outs
            gs = (50
                  + 3 * outs
                  + 2 * int(ip)
                  + s.get("strikeOuts", 0)
                  - 2 * s.get("hits", 0)
                  - 4 * s.get("earnedRuns", 0)
                  - 2 * s.get("baseOnBalls", 0)
                  - s.get("homeRuns", 0))

            # ERA and WHIP for this single outing
            era_game  = round((s.get("earnedRuns", 0) / ip * 9), 2) if ip > 0 else 0
            whip_game = round((s.get("hits", 0) + s.get("baseOnBalls", 0)) / ip, 2) if ip > 0 else 0

            # Starter = appeared as SP (gamesStarted flag or inferred from IP)
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
            inserted += 1

        conn.commit()
        print(f"  ✓ {inserted} appearances | team: {team}")

    except Exception as e:
        print(f"  ✗ Error: {e}")
        conn.rollback()

    time.sleep(0.5)

cur.close()
conn.close()
print("\nPitcher backfill complete!")
