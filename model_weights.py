"""
model_weights.py
Shared helper for loading tunable model weights from the DB.

Two named weight sets: 'live' (used by analytics.js in the browser) and
'backtest' (used by Python scripts). They can drift intentionally — keep
fallbacks in source so a DB outage doesn't break nightly jobs.
"""

FALLBACK_BACKTEST = {
    "woba_run_scale":    12.0,
    "max_predicted_runs": 7.0,
    "score_boost":        0.08,
    "opp_era_scale":      0.85,
    "spot_weights":       [1.15, 1.12, 1.10, 1.05, 1.02, 0.95, 0.90, 0.88, 0.83],
}

FALLBACK_LIVE = dict(FALLBACK_BACKTEST)


def load_weights(cur, name, fallback):
    cur.execute("""
        SELECT woba_run_scale, max_predicted_runs, score_boost,
               opp_era_scale, spot_weights
        FROM model_weights WHERE weight_set_name = %s
    """, (name,))
    row = cur.fetchone()
    if not row:
        print(f"WARN: model_weights['{name}'] missing — using hardcoded fallback")
        return dict(fallback)
    return {
        "woba_run_scale":    float(row[0]),
        "max_predicted_runs": float(row[1]),
        "score_boost":       float(row[2]),
        "opp_era_scale":     float(row[3]),
        "spot_weights":      row[4],
    }
