"""
migrate_kalshi_tracker.py
Creates the kalshi_tracker table for logging model vs Kalshi disagreements.
Run once: python migrate_kalshi_tracker.py
"""
import psycopg2, os
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS kalshi_tracker (
    id              SERIAL PRIMARY KEY,
    game_date       DATE NOT NULL,
    season          INTEGER NOT NULL,
    home_team       TEXT NOT NULL,
    away_team       TEXT NOT NULL,
    game_pk         INTEGER,
    -- Model predictions
    model_home_prob NUMERIC(5,3),
    model_away_prob NUMERIC(5,3),
    model_pick      TEXT,           -- 'home' or 'away'
    model_confidence NUMERIC(5,3),  -- mismatch 0-1
    -- Kalshi market
    kalshi_home_prob NUMERIC(5,3),
    kalshi_away_prob NUMERIC(5,3),
    kalshi_pick      TEXT,
    -- Gap
    prob_gap        NUMERIC(5,3),   -- model_home_prob - kalshi_home_prob
    signal_type     TEXT,           -- 'strong_edge' or 'disagreement'
    -- CycleEdge
    cycle_home_score INTEGER,
    cycle_away_score INTEGER,
    cycle_pick       TEXT,
    -- Outcome (filled in next day by fetch_kalshi_outcomes.py)
    actual_winner    TEXT,          -- 'home' or 'away'
    model_correct    BOOLEAN,
    kalshi_correct   BOOLEAN,
    cycle_correct    BOOLEAN,
    -- Metadata
    logged_at       TIMESTAMP DEFAULT NOW(),
    UNIQUE (game_pk, game_date)
);
""")

conn.commit()

# Add Vegas columns if they don't exist yet
for col, typedef in [
    ("vegas_home_prob", "NUMERIC(5,3)"),
    ("vegas_away_prob", "NUMERIC(5,3)"),
    ("vegas_pick",      "TEXT"),
    ("vegas_correct",   "BOOLEAN"),
]:
    cur.execute(f"""
        ALTER TABLE kalshi_tracker ADD COLUMN IF NOT EXISTS {col} {typedef};
    """)

conn.commit()

# Add cycle probability columns if they don't exist yet
for col, typedef in [
    ("cycle_home_prob", "NUMERIC(5,3)"),
    ("cycle_away_prob", "NUMERIC(5,3)"),
]:
    cur.execute(f"""
        ALTER TABLE kalshi_tracker ADD COLUMN IF NOT EXISTS {col} {typedef};
    """)

conn.commit()

# Add Kalshi liquidity columns (morning snapshot) — used by analyze_divergence.py
# to filter to tradeable markets (a "5pp edge" behind a 6pp spread is nothing).
for col, typedef in [
    ("kalshi_spread",        "NUMERIC(5,3)"),  # yes ask - bid, dollars, on matched market
    ("kalshi_volume",        "INTEGER"),       # 24h contract volume
    ("kalshi_open_interest", "INTEGER"),       # resting open interest
]:
    cur.execute(f"""
        ALTER TABLE kalshi_tracker ADD COLUMN IF NOT EXISTS {col} {typedef};
    """)

conn.commit()
cur.close()
conn.close()
print("kalshi_tracker table ready (cycle prob + kalshi liquidity columns added if missing)")
