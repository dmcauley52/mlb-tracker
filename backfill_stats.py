import statsapi
import psycopg2
import os
from dotenv import load_dotenv
load_dotenv()

PLAYER_IDS = {
    "Aaron Judge": 592450,
    "Shohei Ohtani": 660271,
    "Freddie Freeman": 518692,
    "Mookie Betts": 605141,
}

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()

for name, pid in PLAYER_IDS.items():
    print(f"Fetching full season for {name}...")
    stats = statsapi.player_stat_data(
        pid, group="hitting", type="gameLog"
    )
    inserted = 0
    for game in stats.get("stats", []):
        s = game["stats"]
        gdate = game.get("date")
        season = 2025
        cur.execute("""
            INSERT INTO player_gamelogs
            (player_id, player_name, game_date, season, at_bats, hits,
             home_runs, rbi, batting_avg, ops)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
        """, (pid, name, gdate, season,
              s.get("atBats", 0), s.get("hits", 0),
              s.get("homeRuns", 0), s.get("rbi", 0),
              s.get("avg", 0), s.get("ops", 0)))
        inserted += 1
    conn.commit()
    print(f"  ✓ {inserted} games inserted for {name}")

cur.close()
conn.close()
print("Backfill complete!")
