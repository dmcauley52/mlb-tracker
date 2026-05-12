"""
apply_weights.py
Reads the latest APPLY recommendation from model_tuning_log and updates
the model_weights table. Live UI and Python scripts read from that table
on startup, so no source-code edits or git commits are needed.

Run automatically after tune_model.py, or manually:
  python apply_weights.py

Exits with code 0 in all cases (no APPLY row = no-op, not an error).
"""
import psycopg2
import os
from datetime import date
from dotenv import load_dotenv
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
SEASON       = 2026

conn = psycopg2.connect(DATABASE_URL)
cur  = conn.cursor()

# ── Fetch latest APPLY row ────────────────────────────────────────────────────
cur.execute("""
    SELECT run_date, best_woba_run_scale, best_opp_era_scale, best_max_pred_runs,
           current_woba_run_scale, current_opp_era_scale, current_max_pred_runs,
           current_run_mae, best_run_mae, current_win_accuracy, best_win_accuracy,
           recommendation_text
    FROM model_tuning_log
    WHERE recommendation = 'APPLY' AND season = %s
    ORDER BY run_date DESC
    LIMIT 1
""", (SEASON,))
row = cur.fetchone()

if not row:
    print("No APPLY recommendation found — nothing to do.")
    cur.close()
    conn.close()
    exit(0)

(run_date, best_scale, best_era, best_max,
 cur_scale, cur_era, cur_max,
 cur_mae, best_mae, cur_win, best_win,
 rec_text) = row

best_scale = float(best_scale)
best_era   = float(best_era)
best_max   = float(best_max)

print(f"APPLY recommendation from {run_date}:")
print(f"  woba_run_scale:    {cur_scale} → {best_scale}")
print(f"  opp_era_scale:     {cur_era} → {best_era}")
print(f"  max_predicted_runs:{cur_max} → {best_max}")
print(f"  RunMAE: {cur_mae} → {best_mae}  WinAcc: {cur_win:.1%} → {best_win:.1%}")

# ── Update model_weights table (both 'live' and 'backtest' rows) ──────────────
cur.execute("""
    UPDATE model_weights
       SET woba_run_scale     = %s,
           max_predicted_runs = %s,
           opp_era_scale      = %s,
           updated_at         = NOW(),
           updated_by         = 'auto-tune'
     WHERE weight_set_name IN ('live', 'backtest')
""", (best_scale, best_max, best_era))
print(f"Updated {cur.rowcount} rows in model_weights")
conn.commit()

# ── Snapshot to weight_history ────────────────────────────────────────────────
cur.execute("""
    INSERT INTO weight_history
    (effective_date, season,
     woba_run_scale, opp_era_scale, max_predicted_runs, score_boost,
     prev_woba_run_scale, prev_opp_era_scale, prev_max_predicted_runs,
     source, notes)
    VALUES (%s,%s, %s,%s,%s,%s, %s,%s,%s, %s,%s)
""", (
    str(date.today()), SEASON,
    best_scale, best_era, best_max, 0.08,
    float(cur_scale), float(cur_era), float(cur_max),
    "auto-tune", rec_text,
))
conn.commit()
print("Recorded weight_history snapshot.")

cur.close()
conn.close()
