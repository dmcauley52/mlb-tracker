import psycopg2
import os
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()

cols = [
    ("woba",             "NUMERIC(6,4)"),
    ("obp",              "NUMERIC(6,3)"),
    ("slg",              "NUMERIC(6,3)"),
    ("doubles",          "INTEGER"),
    ("triples",          "INTEGER"),
    ("walks",            "INTEGER"),
    ("hit_by_pitch",     "INTEGER"),
    ("sac_flies",        "INTEGER"),
    ("strikeouts",       "INTEGER"),
    ("stolen_bases",     "INTEGER"),
    ("plate_appearances","INTEGER"),
    ("total_bases",      "INTEGER"),
    ("runs",             "INTEGER"),
    ("babip",            "NUMERIC(6,3)"),
    ("batting_order",    "SMALLINT"),
]

for col, dtype in cols:
    cur.execute(f"ALTER TABLE player_gamelogs ADD COLUMN IF NOT EXISTS {col} {dtype}")
    print(f"  + {col} {dtype}")

conn.commit()
cur.close()
conn.close()
print("Done — all columns added to player_gamelogs.")
