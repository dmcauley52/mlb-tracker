"""
apply_weights.py
Reads the latest APPLY recommendation from model_tuning_log and patches
weight constants in all four files that use them, then commits the changes.

Run automatically after tune_model.py, or manually:
  python apply_weights.py

Exits with code 0 in all cases (no APPLY row = no-op, not an error).
"""
import psycopg2
import os
import re
import subprocess
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
cur.close()
conn.close()

if not row:
    print("No APPLY recommendation found — nothing to do.")
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


def patch_file(path, replacements):
    """Apply regex substitutions to a file. replacements = [(pattern, replacement), ...]"""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    original = content
    for pattern, replacement in replacements:
        content = re.sub(pattern, replacement, content)
    if content == original:
        print(f"  (no change needed) {path}")
        return False
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"  patched {path}")
    return True


changed = []

# ── fetch_backtest_cache.py ───────────────────────────────────────────────────
# Targets:
#   "woba_run_scale": 17.0,
#   "max_predicted_runs": 7.5,
#   "opp_era_scale":  0.75,
if patch_file("fetch_backtest_cache.py", [
    (r'("woba_run_scale":\s*)\d+\.?\d*', rf'\g<1>{best_scale}'),
    (r'("max_predicted_runs":\s*)\d+\.?\d*', rf'\g<1>{best_max}'),
    (r'("opp_era_scale":\s*)\d+\.?\d*', rf'\g<1>{best_era}'),
]):
    changed.append("fetch_backtest_cache.py")

# ── fetch_game_predictions.py ─────────────────────────────────────────────────
# Targets:
#   WOBA_RUN_SCALE   = 17.0
#   MAX_PRED_RUNS    = 7.5
#   OPP_ERA_SCALE    = 0.75
if patch_file("fetch_game_predictions.py", [
    (r'(WOBA_RUN_SCALE\s*=\s*)\d+\.?\d*', rf'\g<1>{best_scale}'),
    (r'(MAX_PRED_RUNS\s*=\s*)\d+\.?\d*', rf'\g<1>{best_max}'),
    (r'(OPP_ERA_SCALE\s*=\s*)\d+\.?\d*', rf'\g<1>{best_era}'),
]):
    changed.append("fetch_game_predictions.py")

# ── fetch_kalshi_outcomes.py ──────────────────────────────────────────────────
# Targets:
#   WOBA_RUN_SCALE  = 17.0
#   MAX_RUNS        = 7.5
if patch_file("fetch_kalshi_outcomes.py", [
    (r'(WOBA_RUN_SCALE\s*=\s*)\d+\.?\d*', rf'\g<1>{best_scale}'),
    (r'(MAX_RUNS\s*=\s*)\d+\.?\d*', rf'\g<1>{best_max}'),
]):
    changed.append("fetch_kalshi_outcomes.py")

# ── analytics.js ─────────────────────────────────────────────────────────────
# Targets:
#   wobaRunScale:    14.1,
#   maxPredictedRuns: 9.0,
#   oppEraScale:     0.75,
if patch_file("analytics.js", [
    (r'(wobaRunScale:\s*)\d+\.?\d*', rf'\g<1>{best_scale}'),
    (r'(maxPredictedRuns:\s*)\d+\.?\d*', rf'\g<1>{best_max}'),
    (r'(oppEraScale:\s*)\d+\.?\d*', rf'\g<1>{best_era}'),
]):
    changed.append("analytics.js")

# ── Also update CURRENT_WEIGHTS in tune_model.py so next run uses new baseline ──
if patch_file("tune_model.py", [
    (r'("woba_run_scale":\s*)\d+\.?\d*', rf'\g<1>{best_scale}'),
    (r'("opp_era_scale":\s*)\d+\.?\d*', rf'\g<1>{best_era}'),
    (r'("max_predicted_runs":\s*)\d+\.?\d*', rf'\g<1>{best_max}'),
]):
    changed.append("tune_model.py")

if not changed:
    print("All files already at target values — nothing committed.")
    exit(0)

# ── Git commit ────────────────────────────────────────────────────────────────
commit_msg = (
    f"auto-tune: weights updated {run_date}\n\n"
    f"woba_run_scale {cur_scale} → {best_scale}, "
    f"opp_era_scale {cur_era} → {best_era}, "
    f"max_predicted_runs {cur_max} → {best_max}\n"
    f"RunMAE {cur_mae} → {best_mae}  WinAcc {float(cur_win):.1%} → {float(best_win):.1%}\n\n"
    f"{rec_text}"
)

try:
    subprocess.run(["git", "add"] + changed, check=True)
    subprocess.run(["git", "commit", "-m", commit_msg], check=True)
    print(f"\nCommitted weight changes: {', '.join(changed)}")
except subprocess.CalledProcessError as e:
    print(f"\nGit commit failed: {e}")
    print("Files were patched but not committed — commit manually.")
    exit(1)
