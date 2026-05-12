"""
migrate_cache_tables.py
Run once to create the four caching tables.
  python migrate_cache_tables.py
"""
import psycopg2
import os
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

# ── 1. game_results ───────────────────────────────────────────────────────────
cur.execute("""
CREATE TABLE IF NOT EXISTS game_results (
    game_pk          INTEGER PRIMARY KEY,
    game_date        DATE    NOT NULL,
    season           INTEGER NOT NULL,
    home_team        TEXT    NOT NULL,
    away_team        TEXT    NOT NULL,
    home_team_id     INTEGER,
    away_team_id     INTEGER,
    home_score       INTEGER,
    away_score       INTEGER,
    home_team_woba   NUMERIC(5,4),
    away_team_woba   NUMERIC(5,4),
    sp_home_id       INTEGER,
    sp_away_id       INTEGER,
    sp_home_name     TEXT,
    sp_away_name     TEXT,
    home_lineup      JSONB,   -- [{spot, player_id, name}]
    away_lineup      JSONB,
    created_at       TIMESTAMP DEFAULT NOW(),
    updated_at       TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS game_results_date_idx    ON game_results (game_date);
CREATE INDEX IF NOT EXISTS game_results_home_idx    ON game_results (home_team);
CREATE INDEX IF NOT EXISTS game_results_away_idx    ON game_results (away_team);
CREATE INDEX IF NOT EXISTS game_results_season_idx  ON game_results (season);
""")
print("✓ game_results")

# ── 2. team_stats_cache ───────────────────────────────────────────────────────
cur.execute("""
CREATE TABLE IF NOT EXISTS team_stats_cache (
    team_id          INTEGER PRIMARY KEY,
    team_name        TEXT    NOT NULL,
    abbreviation     TEXT,
    league_id        INTEGER,
    season           INTEGER NOT NULL,
    season_era       NUMERIC(4,2),
    season_whip      NUMERIC(4,3),
    win_pct          NUMERIC(4,3),
    l10_win_pct      NUMERIC(4,3),
    wins             INTEGER,
    losses           INTEGER,
    fetched_date     DATE    NOT NULL,
    updated_at       TIMESTAMP DEFAULT NOW()
);
""")
print("✓ team_stats_cache")

# ── 3. pitcher_profiles ───────────────────────────────────────────────────────
cur.execute("""
CREATE TABLE IF NOT EXISTS pitcher_profiles (
    player_id        INTEGER PRIMARY KEY,
    player_name      TEXT    NOT NULL,
    team             TEXT,
    team_id          INTEGER,
    league_id        INTEGER,
    hand             TEXT,
    season           INTEGER NOT NULL,
    season_era       NUMERIC(4,2),
    season_whip      NUMERIC(4,3),
    k_per_9          NUMERIC(4,2),
    bb_per_9         NUMERIC(4,2),
    innings_pitched  NUMERIC(6,1),
    games_started    INTEGER,
    median_ip        TEXT,
    era_rank_al      INTEGER,
    era_rank_nl      INTEGER,
    fetched_date     DATE    NOT NULL,
    updated_at       TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS pitcher_profiles_team_idx ON pitcher_profiles (team);
""")
print("✓ pitcher_profiles")

# ── 4. backtest_results ───────────────────────────────────────────────────────
cur.execute("""
CREATE TABLE IF NOT EXISTS backtest_results (
    team             TEXT    NOT NULL,
    run_date         DATE    NOT NULL,
    days_window      INTEGER NOT NULL,
    season           INTEGER NOT NULL,
    games_eval       INTEGER,
    win_accuracy     NUMERIC(4,3),
    avg_run_mae      NUMERIC(4,2),
    avg_woba_mae     NUMERIC(5,4),
    game_rows        JSONB,
    suggested_lineup JSONB,
    weights_snapshot JSONB,
    created_at       TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (team, run_date, days_window)
);
ALTER TABLE backtest_results ADD COLUMN IF NOT EXISTS weights_snapshot JSONB;
CREATE INDEX IF NOT EXISTS backtest_results_team_idx ON backtest_results (team);
CREATE INDEX IF NOT EXISTS backtest_results_date_idx ON backtest_results (run_date);
""")
print("✓ backtest_results")

# ── 5. model_weights ──────────────────────────────────────────────────────────
cur.execute("""
CREATE TABLE IF NOT EXISTS model_weights (
    weight_set_name     TEXT PRIMARY KEY,
    woba_run_scale      NUMERIC NOT NULL,
    max_predicted_runs  NUMERIC NOT NULL,
    score_boost         NUMERIC NOT NULL,
    opp_era_scale       NUMERIC NOT NULL,
    spot_weights        JSONB   NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_by          TEXT
);
INSERT INTO model_weights
    (weight_set_name, woba_run_scale, max_predicted_runs, score_boost, opp_era_scale, spot_weights, updated_by)
VALUES
    ('live',     12.0, 7.0, 0.08, 0.85, '[1.15,1.12,1.10,1.05,1.02,0.95,0.90,0.88,0.83]'::jsonb, 'seed'),
    ('backtest', 12.0, 7.0, 0.08, 0.85, '[1.15,1.12,1.10,1.05,1.02,0.95,0.90,0.88,0.83]'::jsonb, 'seed')
ON CONFLICT (weight_set_name) DO NOTHING;
""")
print("✓ model_weights")

conn.commit()
cur.close()
conn.close()
print("\nAll cache tables created successfully.")
