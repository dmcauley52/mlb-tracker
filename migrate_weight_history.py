"""
migrate_weight_history.py
Run once to create weight_history and weight_performance tables.
  python migrate_weight_history.py
"""
import psycopg2
import os
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

cur.execute("""
-- Append-only log of every weight change made by apply_weights.py
CREATE TABLE IF NOT EXISTS weight_history (
    id              SERIAL PRIMARY KEY,
    effective_date  DATE NOT NULL,        -- date the weights became active
    season          INTEGER NOT NULL,

    woba_run_scale     NUMERIC(5,2) NOT NULL,
    opp_era_scale      NUMERIC(4,3) NOT NULL,
    max_predicted_runs NUMERIC(4,2) NOT NULL,
    score_boost        NUMERIC(4,3) NOT NULL,

    -- Previous values (the ones being replaced) — null for the very first row
    prev_woba_run_scale     NUMERIC(5,2),
    prev_opp_era_scale      NUMERIC(4,3),
    prev_max_predicted_runs NUMERIC(4,2),

    source          TEXT NOT NULL,        -- 'auto-tune', 'manual', 'seed'
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS wh_eff_date_idx ON weight_history (effective_date);
CREATE INDEX IF NOT EXISTS wh_season_idx   ON weight_history (season);

-- Rolling comparison: how does the CURRENT weight set perform on recent games
-- vs how would the PREVIOUS weight set have performed on the same games?
CREATE TABLE IF NOT EXISTS weight_performance (
    id              SERIAL PRIMARY KEY,
    run_date        DATE NOT NULL,
    season          INTEGER NOT NULL,
    window_days     INTEGER NOT NULL,
    games_evaluated INTEGER NOT NULL,

    -- Current (active) weights and their performance
    current_weight_id     INTEGER REFERENCES weight_history(id),
    current_run_mae       NUMERIC(5,3),
    current_win_accuracy  NUMERIC(5,4),
    current_run_bias      NUMERIC(5,3),

    -- Previous weights and how they WOULD have performed on the same games
    previous_weight_id    INTEGER REFERENCES weight_history(id),
    previous_run_mae      NUMERIC(5,3),
    previous_win_accuracy NUMERIC(5,4),
    previous_run_bias     NUMERIC(5,3),

    -- Deltas (current - previous; negative MAE delta means current is better)
    run_mae_delta       NUMERIC(5,3),
    win_accuracy_delta  NUMERIC(5,4),

    verdict         TEXT,  -- 'CURRENT_BETTER', 'PREVIOUS_BETTER', 'TIE', 'NO_CHANGE'
    notes           TEXT,
    created_at      TIMESTAMP DEFAULT NOW(),

    UNIQUE (run_date, window_days, season)
);

CREATE INDEX IF NOT EXISTS wp_run_date_idx ON weight_performance (run_date);
""")

conn.commit()
cur.close()
conn.close()
print("weight_history and weight_performance tables created")
