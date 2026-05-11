"""
tune_model.py
Daily model evaluation and weight tuning suggestions.

Loads the last DAYS_WINDOW days of game_rows from backtest_results,
runs a grid search over woba_run_scale and opp_era_scale,
and writes a suggestion row to model_tuning_log.

Does NOT auto-apply weights. If recommendation == 'APPLY', manually update:
  - fetch_backtest_cache.py  WEIGHTS dict
  - fetch_game_predictions.py  WOBA_RUN_SCALE / OPP_ERA_SCALE
  - analytics.js  BACKTEST_WEIGHTS

Usage:
  python tune_model.py
  DAYS=14 python tune_model.py
"""
import psycopg2
import os
import json
import math
from decimal import Decimal
from datetime import date, timedelta
from dotenv import load_dotenv
load_dotenv()

SEASON      = 2026
DAYS_WINDOW = int(os.getenv("DAYS", 21))
LEAGUE_AVG_ERA = 4.20

# Current production weights — must match fetch_backtest_cache.py WEIGHTS
CURRENT_WEIGHTS = {
    "woba_run_scale":    17.0,
    "opp_era_scale":     0.75,
    "max_predicted_runs": 7.5,
    "score_boost":        0.08,
    "spot_weights": [1.15, 1.12, 1.10, 1.05, 1.02, 0.95, 0.90, 0.88, 0.83],
}

# Grid search space
SCALE_CANDIDATES    = [12.0, 13.0, 14.0, 14.1, 15.0, 16.0, 17.0, 18.0, 18.5]
ERA_SCALE_CANDIDATES = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85]
MAX_RUNS_CANDIDATES  = [7.0, 7.5, 8.0, 8.5, 9.0]

# Thresholds to recommend a weight change
MAE_IMPROVEMENT_THRESHOLD = 0.10   # must improve run MAE by at least 0.10 runs
WIN_ACC_FLOOR             = 0.005  # must not lose more than 0.5% win accuracy

class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)


def predict_runs(avg_woba, avg_score, opp_era, opp_win_pct, my_win_pct, w):
    era_factor     = max(0.80, min(1.30, opp_era / LEAGUE_AVG_ERA))
    score_factor   = 1 + (avg_score - 50) / 99 * w["score_boost"]
    adj_era_factor = era_factor * w["opp_era_scale"] + 1.0 * (1 - w["opp_era_scale"])
    team_quality   = max(0.88, min(1.12, 1.0 + (my_win_pct - 0.500) * 0.5))
    return min(
        avg_woba * w["woba_run_scale"] * score_factor * adj_era_factor * team_quality,
        w["max_predicted_runs"]
    )


def predict_win(my_pred_runs, opp_era, opp_win_pct, opp_lineup_woba, w):
    opp_base = 4.65 + (opp_win_pct - 0.500) * 13.0
    if opp_lineup_woba:
        opp_runs = opp_lineup_woba * w["woba_run_scale"] * 0.6 + opp_base * 0.4
    else:
        opp_runs = opp_base
    run_diff = my_pred_runs - opp_runs
    return 1 / (1 + math.exp(-run_diff * 0.40)) >= 0.50


def evaluate_weights(game_rows, w):
    run_errors = []
    win_correct = 0
    run_biases  = []

    for g in game_rows:
        actual_runs = g.get("actual_runs")
        actual_win  = g.get("actual_win")
        avg_woba    = g.get("avg_woba")
        avg_score   = g.get("avg_score", 50)
        opp_era     = g.get("opp_era", LEAGUE_AVG_ERA)
        opp_win_pct = g.get("opp_win_pct", 0.500)
        my_win_pct  = g.get("my_win_pct", 0.500)
        opp_lineup_woba = g.get("opp_lineup_woba")

        if actual_runs is None or avg_woba is None:
            continue

        pred_runs = predict_runs(avg_woba, avg_score, opp_era, opp_win_pct, my_win_pct, w)
        pred_win  = predict_win(pred_runs, opp_era, opp_win_pct, opp_lineup_woba, w)

        run_errors.append(abs(pred_runs - actual_runs))
        run_biases.append(pred_runs - actual_runs)
        if actual_win is not None:
            win_correct += int(pred_win == actual_win)

    n = len(run_errors)
    if n == 0:
        return None

    return {
        "run_mae":      sum(run_errors) / n,
        "win_accuracy": win_correct / n if n > 0 else None,
        "run_bias":     sum(run_biases) / n,
        "pct_over":     sum(1 for b in run_biases if b > 0) / n,
        "n":            n,
    }


# ── DB connection ─────────────────────────────────────────────────────────────
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()
today      = date.today()
start_date = today - timedelta(days=DAYS_WINDOW)

# ── Load all game_rows from backtest_results for this window ──────────────────
print(f"Loading backtest_results (last {DAYS_WINDOW} days, run_date = {today})...")
cur.execute("""
    SELECT team, game_rows
    FROM backtest_results
    WHERE run_date = %s AND season = %s AND days_window = %s
""", (str(today), SEASON, DAYS_WINDOW))
rows = cur.fetchall()
print(f"  {len(rows)} teams loaded")

if not rows:
    print("No backtest_results for today — run fetch_backtest_cache.py first. Exiting.")
    cur.close()
    conn.close()
    exit(0)

# ── Flatten game_rows into a list of dicts the evaluator can use ──────────────
# game_rows JSON structure per game (from fetch_backtest_cache.py):
#   date, game_pk, opponent, is_home, actual_win, actual_runs, opp_runs,
#   actual_woba, opp_era, opp_win_pct, sp, actual_lineup, actual{...}, suggested{...}
#
# We reconstruct what the evaluator needs from the stored predictions + raw fields.
# The stored actual.predicted_runs used CURRENT_WEIGHTS, but we need the raw inputs
# to re-evaluate with candidate weights.
# Stored fields we can reuse: avg_woba = actual.predicted_woba (that IS the avg lineup wOBA),
# opp_era, opp_win_pct, actual_runs, actual_win.
# avg_score is not stored per-game, so we use 50 (neutral — scoreBoost is a tiny nudge).
# my_win_pct is not stored; we use 0.500 as neutral. Both approximations are consistent
# across all weight candidates so they don't bias the comparison.

flat_games = []
for team, game_rows_json in rows:
    if isinstance(game_rows_json, str):
        data = json.loads(game_rows_json)
    else:
        data = game_rows_json

    for g in (data.get("games") or []):
        actual_obj = g.get("actual") or {}
        flat_games.append({
            "actual_runs":    g.get("actual_runs"),
            "actual_win":     g.get("actual_win"),
            "avg_woba":       actual_obj.get("predicted_woba"),  # lineup avg wOBA
            "avg_score":      50,
            "opp_era":        float(g["opp_era"]) if g.get("opp_era") else LEAGUE_AVG_ERA,
            "opp_win_pct":    float(g["opp_win_pct"]) if g.get("opp_win_pct") else 0.500,
            "my_win_pct":     0.500,
            "opp_lineup_woba": None,
        })

print(f"  {len(flat_games)} game samples for tuning")

if len(flat_games) < 30:
    print(f"Too few samples ({len(flat_games)}) — need at least 30. Exiting.")
    cur.close()
    conn.close()
    exit(0)

# ── Evaluate current weights first ───────────────────────────────────────────
current_metrics = evaluate_weights(flat_games, CURRENT_WEIGHTS)
print(f"\nCurrent weights:")
print(f"  woba_run_scale={CURRENT_WEIGHTS['woba_run_scale']}  opp_era_scale={CURRENT_WEIGHTS['opp_era_scale']}  max_pred_runs={CURRENT_WEIGHTS['max_predicted_runs']}")
print(f"  RunMAE={current_metrics['run_mae']:.3f}  WinAcc={current_metrics['win_accuracy']:.3f}  Bias={current_metrics['run_bias']:+.3f}  OverPct={current_metrics['pct_over']:.1%}  N={current_metrics['n']}")

# ── Grid search ───────────────────────────────────────────────────────────────
print(f"\nGrid search ({len(SCALE_CANDIDATES)}×{len(ERA_SCALE_CANDIDATES)}×{len(MAX_RUNS_CANDIDATES)} = {len(SCALE_CANDIDATES)*len(ERA_SCALE_CANDIDATES)*len(MAX_RUNS_CANDIDATES)} combos)...")

best_mae    = current_metrics["run_mae"]
best_weights = None
best_metrics = None

for scale in SCALE_CANDIDATES:
    for era_scale in ERA_SCALE_CANDIDATES:
        for max_runs in MAX_RUNS_CANDIDATES:
            w = {**CURRENT_WEIGHTS, "woba_run_scale": scale, "opp_era_scale": era_scale, "max_predicted_runs": max_runs}
            m = evaluate_weights(flat_games, w)
            if m and m["run_mae"] < best_mae:
                best_mae     = m["run_mae"]
                best_weights = w
                best_metrics = m

if best_weights is None:
    print("No improvement found — current weights are already optimal for this window.")
    best_weights = CURRENT_WEIGHTS
    best_metrics = current_metrics

print(f"\nBest weights found:")
print(f"  woba_run_scale={best_weights['woba_run_scale']}  opp_era_scale={best_weights['opp_era_scale']}  max_pred_runs={best_weights['max_predicted_runs']}")
print(f"  RunMAE={best_metrics['run_mae']:.3f}  WinAcc={best_metrics['win_accuracy']:.3f}  Bias={best_metrics['run_bias']:+.3f}")

# ── Determine recommendation ──────────────────────────────────────────────────
mae_delta = current_metrics["run_mae"] - best_metrics["run_mae"]   # positive = improvement
win_delta = best_metrics["win_accuracy"] - current_metrics["win_accuracy"]

weights_unchanged = (
    best_weights["woba_run_scale"]    == CURRENT_WEIGHTS["woba_run_scale"] and
    best_weights["opp_era_scale"]     == CURRENT_WEIGHTS["opp_era_scale"] and
    best_weights["max_predicted_runs"] == CURRENT_WEIGHTS["max_predicted_runs"]
)

if weights_unchanged:
    recommendation = "OK"
    rec_text = (
        f"Current weights are optimal for this {DAYS_WINDOW}-day window. "
        f"RunMAE={current_metrics['run_mae']:.3f}, WinAcc={current_metrics['win_accuracy']:.1%}, "
        f"Bias={current_metrics['run_bias']:+.3f} runs/game."
    )
elif mae_delta >= MAE_IMPROVEMENT_THRESHOLD and win_delta >= -WIN_ACC_FLOOR:
    recommendation = "APPLY"
    rec_text = (
        f"Suggest updating weights: "
        f"woba_run_scale {CURRENT_WEIGHTS['woba_run_scale']} → {best_weights['woba_run_scale']}, "
        f"opp_era_scale {CURRENT_WEIGHTS['opp_era_scale']} → {best_weights['opp_era_scale']}, "
        f"max_pred_runs {CURRENT_WEIGHTS['max_predicted_runs']} → {best_weights['max_predicted_runs']}. "
        f"RunMAE improves {current_metrics['run_mae']:.3f} → {best_metrics['run_mae']:.3f} "
        f"({mae_delta:+.3f} runs/game). "
        f"WinAcc {current_metrics['win_accuracy']:.1%} → {best_metrics['win_accuracy']:.1%}. "
        f"Current bias: {current_metrics['run_bias']:+.3f} runs/game "
        f"({'over' if current_metrics['run_bias'] > 0 else 'under'}-predicting). "
        f"Evaluated on {current_metrics['n']} games."
    )
else:
    recommendation = "MONITOR"
    rec_text = (
        f"Marginal improvement found but below threshold ({mae_delta:+.3f} runs/game MAE delta). "
        f"Current: RunMAE={current_metrics['run_mae']:.3f}, WinAcc={current_metrics['win_accuracy']:.1%}, Bias={current_metrics['run_bias']:+.3f}. "
        f"Candidate: woba_run_scale={best_weights['woba_run_scale']}, opp_era_scale={best_weights['opp_era_scale']}, max_pred_runs={best_weights['max_predicted_runs']}. "
        f"Monitor for 3+ consecutive MONITOR days before applying."
    )

print(f"\nRecommendation: {recommendation}")
print(f"  {rec_text}")

# ── Upsert into model_tuning_log ──────────────────────────────────────────────
cur.execute("""
    INSERT INTO model_tuning_log
    (run_date, season, days_window, games_evaluated,
     current_woba_run_scale, current_opp_era_scale, current_max_pred_runs, current_score_boost,
     best_woba_run_scale, best_opp_era_scale, best_max_pred_runs,
     current_win_accuracy, current_run_mae,
     best_win_accuracy, best_run_mae,
     recommendation, recommendation_text,
     avg_run_bias, pct_over_predicted)
    VALUES (%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s, %s,%s, %s,%s, %s,%s, %s,%s)
    ON CONFLICT (run_date, season) DO UPDATE SET
        days_window             = EXCLUDED.days_window,
        games_evaluated         = EXCLUDED.games_evaluated,
        current_woba_run_scale  = EXCLUDED.current_woba_run_scale,
        current_opp_era_scale   = EXCLUDED.current_opp_era_scale,
        current_max_pred_runs   = EXCLUDED.current_max_pred_runs,
        current_score_boost     = EXCLUDED.current_score_boost,
        best_woba_run_scale     = EXCLUDED.best_woba_run_scale,
        best_opp_era_scale      = EXCLUDED.best_opp_era_scale,
        best_max_pred_runs      = EXCLUDED.best_max_pred_runs,
        current_win_accuracy    = EXCLUDED.current_win_accuracy,
        current_run_mae         = EXCLUDED.current_run_mae,
        best_win_accuracy       = EXCLUDED.best_win_accuracy,
        best_run_mae            = EXCLUDED.best_run_mae,
        recommendation          = EXCLUDED.recommendation,
        recommendation_text     = EXCLUDED.recommendation_text,
        avg_run_bias            = EXCLUDED.avg_run_bias,
        pct_over_predicted      = EXCLUDED.pct_over_predicted,
        created_at              = NOW()
""", (
    str(today), SEASON, DAYS_WINDOW, current_metrics["n"],
    CURRENT_WEIGHTS["woba_run_scale"], CURRENT_WEIGHTS["opp_era_scale"],
    CURRENT_WEIGHTS["max_predicted_runs"], CURRENT_WEIGHTS["score_boost"],
    best_weights["woba_run_scale"], best_weights["opp_era_scale"], best_weights["max_predicted_runs"],
    round(current_metrics["win_accuracy"], 4), round(current_metrics["run_mae"], 3),
    round(best_metrics["win_accuracy"], 4), round(best_metrics["run_mae"], 3),
    recommendation, rec_text,
    round(current_metrics["run_bias"], 3), round(current_metrics["pct_over"], 4),
))
conn.commit()
print(f"\nSaved to model_tuning_log: {recommendation} for {today}")

cur.close()
conn.close()
