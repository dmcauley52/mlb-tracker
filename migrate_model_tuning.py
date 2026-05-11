"""
migrate_model_tuning.py
Run once to create the model_tuning_log table.
  python migrate_model_tuning.py
"""
import psycopg2
import os
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS model_tuning_log (
    id                  SERIAL PRIMARY KEY,
    run_date            DATE    NOT NULL,
    season              INTEGER NOT NULL,

    -- Window of games evaluated
    days_window         INTEGER NOT NULL,
    games_evaluated     INTEGER NOT NULL,

    -- Current weights at time of run
    current_woba_run_scale   NUMERIC(5,2),
    current_opp_era_scale    NUMERIC(4,3),
    current_max_pred_runs    NUMERIC(4,2),
    current_score_boost      NUMERIC(4,3),

    -- Best weights found by grid search
    best_woba_run_scale      NUMERIC(5,2),
    best_opp_era_scale       NUMERIC(4,3),
    best_max_pred_runs       NUMERIC(4,2),

    -- Accuracy with current weights
    current_win_accuracy     NUMERIC(5,4),
    current_run_mae          NUMERIC(5,3),

    -- Accuracy with best weights (on same data)
    best_win_accuracy        NUMERIC(5,4),
    best_run_mae             NUMERIC(5,3),

    -- Whether a change is recommended (improvement crosses threshold)
    recommendation          TEXT,   -- 'APPLY', 'MONITOR', 'OK'
    recommendation_text     TEXT,

    -- Bias diagnostics
    avg_run_bias            NUMERIC(5,3),  -- mean(predicted - actual): positive = over-predicting
    pct_over_predicted      NUMERIC(5,4),  -- fraction of games where predicted > actual

    created_at              TIMESTAMP DEFAULT NOW(),

    UNIQUE (run_date, season)
);

CREATE INDEX IF NOT EXISTS mtl_date_idx   ON model_tuning_log (run_date);
CREATE INDEX IF NOT EXISTS mtl_season_idx ON model_tuning_log (season);
""")

conn.commit()
cur.close()
conn.close()
print("model_tuning_log table created")
