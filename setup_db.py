import psycopg2
import os
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()
cur.execute("""
    CREATE TABLE IF NOT EXISTS player_gamelogs (
        id SERIAL PRIMARY KEY,
        player_id INT,
        player_name TEXT,
        game_date DATE,
        season INT,
        at_bats INT,
        hits INT,
        home_runs INT,
        rbi INT,
        batting_avg FLOAT,
        ops FLOAT,
        cycle_day INT,
        created_at TIMESTAMP DEFAULT NOW()
    );
""")
conn.commit()
cur.close()
conn.close()
print("Table created!")
