"""
migrate_game_predictions.py
Run once to create the game_predictions table.
  python migrate_game_predictions.py
"""
import psycopg2
import os
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS game_predictions (
    game_pk              INTEGER PRIMARY KEY,
    game_date            DATE    NOT NULL,
    season               INTEGER NOT NULL,

    -- Teams
    home_team            TEXT    NOT NULL,
    away_team            TEXT    NOT NULL,
    home_team_id         INTEGER,
    away_team_id         INTEGER,

    -- Lineups used for prediction
    home_lineup          JSONB,   -- ["Player Name", ...]  in batting order
    away_lineup          JSONB,

    -- Starting pitchers
    home_sp_name         TEXT,
    away_sp_name         TEXT,
    home_sp_era          NUMERIC(4,2),
    away_sp_era          NUMERIC(4,2),

    -- Team context
    home_win_pct         NUMERIC(4,3),
    away_win_pct         NUMERIC(4,3),
    home_lineup_woba     NUMERIC(5,4),
    away_lineup_woba     NUMERIC(5,4),
    home_db_coverage     NUMERIC(4,3),   -- fraction of batters with DB data (0–1)
    away_db_coverage     NUMERIC(4,3),

    -- Monte Carlo predictions
    mc_home_runs         NUMERIC(4,1),
    mc_away_runs         NUMERIC(4,1),
    mc_home_p10          INTEGER,
    mc_home_p90          INTEGER,
    mc_away_p10          INTEGER,
    mc_away_p90          INTEGER,
    mc_home_win_prob     NUMERIC(4,3),
    mc_home_inn_means    JSONB,   -- [0.42, 0.38, ...]  9 values
    mc_away_inn_means    JSONB,

    -- wOBA model predictions
    woba_home_runs       NUMERIC(4,2),
    woba_away_runs       NUMERIC(4,2),
    woba_home_win_prob   NUMERIC(4,3),

    -- Actuals (filled by fetch_prediction_outcomes.py)
    actual_home_runs     INTEGER,
    actual_away_runs     INTEGER,
    actual_home_win      BOOLEAN,

    -- Model errors (filled by fetch_prediction_outcomes.py)
    mc_home_run_err      NUMERIC(4,2),
    mc_away_run_err      NUMERIC(4,2),
    woba_home_run_err    NUMERIC(4,2),
    woba_away_run_err    NUMERIC(4,2),
    mc_win_correct       BOOLEAN,
    woba_win_correct     BOOLEAN,

    predicted_at         TIMESTAMP,
    outcomes_fetched_at  TIMESTAMP,
    created_at           TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS gp_date_idx   ON game_predictions (game_date);
CREATE INDEX IF NOT EXISTS gp_season_idx ON game_predictions (season);
CREATE INDEX IF NOT EXISTS gp_home_idx   ON game_predictions (home_team);
CREATE INDEX IF NOT EXISTS gp_away_idx   ON game_predictions (away_team);
""")

conn.commit()
cur.close()
conn.close()
print("✓ game_predictions table created")
