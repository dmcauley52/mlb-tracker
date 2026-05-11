"""
compare_weights.py
Rolling A/B comparison: how does the CURRENT weight set perform on recent
completed games vs how would the PREVIOUS weight set have performed on the
same games?

Loads game rows from today's backtest_results (which contain all the raw
inputs needed to recompute predictions), evaluates with both weight sets,
and writes a row to weight_performance.

Run AFTER fetch_backtest_cache.py and apply_weights.py.
  python compare_weights.py
  DAYS=14 python compare_weights.py
"""
import psycopg2
import os
import json
import math
from datetime import date, timedelta
from dotenv import load_dotenv
load_dotenv()

SEASON         = 2026
DAYS_WINDOW    = int(os.getenv("DAYS", 21))
LEAGUE_AVG_ERA = 4.20
TIE_TOLERANCE  = 0.02   # |run MAE delta| < this counts as TIE

DATABASE_URL = os.getenv("DATABASE_URL")


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


def evaluate(games, w):
    errors, biases, win_correct = [], [], 0
    for g in games:
        if g["actual_runs"] is None or g["avg_woba"] is None:
            continue
        pred_r = predict_runs(g["avg_woba"], g["avg_score"], g["opp_era"], g["opp_win_pct"], g["my_win_pct"], w)
        pred_w = predict_win(pred_r, g["opp_era"], g["opp_win_pct"], g.get("opp_lineup_woba"), w)
        errors.append(abs(pred_r - g["actual_runs"]))
        biases.append(pred_r - g["actual_runs"])
        if g["actual_win"] is not None:
            win_correct += int(pred_w == g["actual_win"])
    n = len(errors)
    if n == 0:
        return None
    return {
        "n":            n,
        "run_mae":      sum(errors) / n,
        "win_accuracy": win_correct / n,
        "run_bias":     sum(biases) / n,
    }


# ── DB connection ─────────────────────────────────────────────────────────────
conn = psycopg2.connect(DATABASE_URL)
cur  = conn.cursor()
today = date.today()

# ── Load the two most recent weight sets ──────────────────────────────────────
cur.execute("""
    SELECT id, effective_date, woba_run_scale, opp_era_scale, max_predicted_runs, score_boost
    FROM weight_history
    WHERE season = %s
    ORDER BY effective_date DESC, id DESC
    LIMIT 2
""", (SEASON,))
hist_rows = cur.fetchall()

if not hist_rows:
    print("weight_history is empty — nothing to compare. Run apply_weights.py first.")
    cur.close()
    conn.close()
    exit(0)

current = {
    "id":              hist_rows[0][0],
    "effective_date":  hist_rows[0][1],
    "woba_run_scale":  float(hist_rows[0][2]),
    "opp_era_scale":   float(hist_rows[0][3]),
    "max_predicted_runs": float(hist_rows[0][4]),
    "score_boost":     float(hist_rows[0][5]),
}

if len(hist_rows) < 2:
    print("Only one weight set in history — no previous to compare against.")
    print("Will record current performance only.")
    previous = None
else:
    previous = {
        "id":              hist_rows[1][0],
        "effective_date":  hist_rows[1][1],
        "woba_run_scale":  float(hist_rows[1][2]),
        "opp_era_scale":   float(hist_rows[1][3]),
        "max_predicted_runs": float(hist_rows[1][4]),
        "score_boost":     float(hist_rows[1][5]),
    }

print(f"Current weights (id={current['id']}, since {current['effective_date']}):")
print(f"  scale={current['woba_run_scale']}  era_scale={current['opp_era_scale']}  max={current['max_predicted_runs']}")
if previous:
    print(f"Previous weights (id={previous['id']}, since {previous['effective_date']}):")
    print(f"  scale={previous['woba_run_scale']}  era_scale={previous['opp_era_scale']}  max={previous['max_predicted_runs']}")

# ── Load games from today's backtest_results ──────────────────────────────────
cur.execute("""
    SELECT team, game_rows
    FROM backtest_results
    WHERE run_date = %s AND season = %s AND days_window = %s
""", (str(today), SEASON, DAYS_WINDOW))
rows = cur.fetchall()
print(f"\nLoaded {len(rows)} team rows for run_date={today}, window={DAYS_WINDOW}d")

if not rows:
    print("No backtest_results for today — run fetch_backtest_cache.py first. Exiting.")
    cur.close()
    conn.close()
    exit(0)

# Restrict comparison to games that occurred AFTER the current weights took effect,
# so we're measuring real forward performance, not retrofitted accuracy.
effective_cutoff = current["effective_date"]
games = []
included_all = 0
for team, game_rows_json in rows:
    data = game_rows_json if isinstance(game_rows_json, dict) else json.loads(game_rows_json)
    for g in (data.get("games") or []):
        included_all += 1
        # Only games on/after the current weights took effect
        try:
            g_date = date.fromisoformat(g["date"])
        except Exception:
            continue
        if g_date < effective_cutoff:
            continue
        actual = g.get("actual") or {}
        games.append({
            "actual_runs":     g.get("actual_runs"),
            "actual_win":      g.get("actual_win"),
            "avg_woba":        actual.get("predicted_woba"),
            "avg_score":       50,
            "opp_era":         float(g["opp_era"]) if g.get("opp_era") else LEAGUE_AVG_ERA,
            "opp_win_pct":     float(g["opp_win_pct"]) if g.get("opp_win_pct") else 0.500,
            "my_win_pct":      0.500,
            "opp_lineup_woba": None,
        })

print(f"  {included_all} total games, {len(games)} since current weights took effect ({effective_cutoff})")

if len(games) < 20:
    print(f"Too few games since weight change ({len(games)}) — need at least 20. Exiting.")
    cur.close()
    conn.close()
    exit(0)

# ── Evaluate both weight sets ─────────────────────────────────────────────────
cur_metrics = evaluate(games, current)
prev_metrics = evaluate(games, previous) if previous else None

print(f"\nCurrent weights on {cur_metrics['n']} games:")
print(f"  RunMAE={cur_metrics['run_mae']:.3f}  WinAcc={cur_metrics['win_accuracy']:.3f}  Bias={cur_metrics['run_bias']:+.3f}")

if prev_metrics:
    print(f"Previous weights on the same games (counterfactual):")
    print(f"  RunMAE={prev_metrics['run_mae']:.3f}  WinAcc={prev_metrics['win_accuracy']:.3f}  Bias={prev_metrics['run_bias']:+.3f}")

    mae_delta = cur_metrics["run_mae"] - prev_metrics["run_mae"]       # negative = current better
    win_delta = cur_metrics["win_accuracy"] - prev_metrics["win_accuracy"]  # positive = current better

    if abs(mae_delta) < TIE_TOLERANCE:
        verdict = "TIE"
    elif mae_delta < 0:
        verdict = "CURRENT_BETTER"
    else:
        verdict = "PREVIOUS_BETTER"

    print(f"\nVerdict: {verdict}")
    print(f"  Run MAE delta: {mae_delta:+.3f}  (negative = current better)")
    print(f"  Win Acc delta: {win_delta:+.3%}")

    notes = (
        f"{cur_metrics['n']} games since {effective_cutoff}. "
        f"Current RunMAE {cur_metrics['run_mae']:.3f} vs previous {prev_metrics['run_mae']:.3f} "
        f"(delta {mae_delta:+.3f}). "
        f"Current WinAcc {cur_metrics['win_accuracy']:.1%} vs previous {prev_metrics['win_accuracy']:.1%} "
        f"(delta {win_delta:+.1%})."
    )
else:
    verdict, mae_delta, win_delta, notes = "NO_CHANGE", None, None, "No previous weights to compare against."

# ── Upsert ───────────────────────────────────────────────────────────────────
cur.execute("""
    INSERT INTO weight_performance
    (run_date, season, window_days, games_evaluated,
     current_weight_id, current_run_mae, current_win_accuracy, current_run_bias,
     previous_weight_id, previous_run_mae, previous_win_accuracy, previous_run_bias,
     run_mae_delta, win_accuracy_delta,
     verdict, notes)
    VALUES (%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s, %s,%s, %s,%s)
    ON CONFLICT (run_date, window_days, season) DO UPDATE SET
        games_evaluated       = EXCLUDED.games_evaluated,
        current_weight_id     = EXCLUDED.current_weight_id,
        current_run_mae       = EXCLUDED.current_run_mae,
        current_win_accuracy  = EXCLUDED.current_win_accuracy,
        current_run_bias      = EXCLUDED.current_run_bias,
        previous_weight_id    = EXCLUDED.previous_weight_id,
        previous_run_mae      = EXCLUDED.previous_run_mae,
        previous_win_accuracy = EXCLUDED.previous_win_accuracy,
        previous_run_bias     = EXCLUDED.previous_run_bias,
        run_mae_delta         = EXCLUDED.run_mae_delta,
        win_accuracy_delta    = EXCLUDED.win_accuracy_delta,
        verdict               = EXCLUDED.verdict,
        notes                 = EXCLUDED.notes,
        created_at            = NOW()
""", (
    str(today), SEASON, DAYS_WINDOW, cur_metrics["n"],
    current["id"], round(cur_metrics["run_mae"], 3), round(cur_metrics["win_accuracy"], 4), round(cur_metrics["run_bias"], 3),
    previous["id"] if previous else None,
    round(prev_metrics["run_mae"], 3) if prev_metrics else None,
    round(prev_metrics["win_accuracy"], 4) if prev_metrics else None,
    round(prev_metrics["run_bias"], 3) if prev_metrics else None,
    round(mae_delta, 3) if mae_delta is not None else None,
    round(win_delta, 4) if win_delta is not None else None,
    verdict, notes,
))
conn.commit()
cur.close()
conn.close()
print(f"\nSaved to weight_performance: {verdict} for {today}")
