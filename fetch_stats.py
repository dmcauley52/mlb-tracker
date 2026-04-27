import statsapi
import psycopg2
import os
from datetime import date, timedelta
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

yesterday = date.today() - timedelta(days=1)
season = yesterday.year

for name, pid in PLAYER_IDS.items():
    stats = statsapi.player_stat_data(
        pid, group="hitting", type="gameLog"
    )
    for game in stats.get("stats", []):
        s = game["stats"]
        gdate = game.get("date", str(yesterday))
        cur.execute("""
            INSERT INTO player_gamelogs
            (player_id, player_name, game_date, season, at_bats, hits,
             home_runs, rbi, batting_avg, ops)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
        """, (pid, name, gdate, season,
              s.get("atBats",0), s.get("hits",0),
              s.get("homeRuns",0), s.get("rbi",0),
              s.get("avg",0), s.get("ops",0)))

conn.commit()
cur.close()
conn.close()
print(f"Stats downloaded for {yesterday}")
