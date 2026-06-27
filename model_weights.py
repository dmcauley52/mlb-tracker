"""
model_weights.py
Shared helper for loading tunable model weights from the DB.

Two named weight sets: 'live' (used by analytics.js in the browser) and
'backtest' (used by Python scripts). They can drift intentionally — keep
fallbacks in source so a DB outage doesn't break nightly jobs.
"""

FALLBACK_BACKTEST = {
    "woba_run_scale":     12.0,
    "max_predicted_runs":  7.0,
    "score_boost":         0.08,
    "opp_era_scale":       0.85,
    "spot_weights":        [1.15, 1.12, 1.10, 1.05, 1.02, 0.95, 0.90, 0.88, 0.83],
    "l10_cap":             0.07,   # max deviation from season win% (streaks < 4)
    "win_pct_run_scale":  13.0,   # run-baseline leverage on win% difference
    "streak_cap_4":        0.03,   # tighter cap once streak >= streak_med_start
    "streak_cap_6":        0.00,   # ignore L10 once streak >= streak_high_start
    "streak_med_start":    3,      # game 4 of a streak (3 wins already)
    "streak_high_start":   6,      # game 7 of a streak (6 wins already)
    "era_floor":           0.60,   # min era_factor (was hardcoded 0.80 — clipped all ERA < 3.36)
    "era_ceil":            1.30,   # max era_factor
}

FALLBACK_LIVE = dict(FALLBACK_BACKTEST)


def load_weights(cur, name, fallback):
    cur.execute("""
        SELECT woba_run_scale, max_predicted_runs, score_boost,
               opp_era_scale, spot_weights, l10_cap, win_pct_run_scale,
               streak_cap_4, streak_cap_6, era_floor, era_ceil,
               streak_med_start, streak_high_start
        FROM model_weights WHERE weight_set_name = %s
    """, (name,))
    row = cur.fetchone()
    if not row:
        print(f"WARN: model_weights['{name}'] missing — using hardcoded fallback")
        return dict(fallback)
    return {
        "woba_run_scale":     float(row[0]),
        "max_predicted_runs": float(row[1]),
        "score_boost":        float(row[2]),
        "opp_era_scale":      float(row[3]),
        "spot_weights":       row[4],
        "l10_cap":            float(row[5]) if row[5] is not None else fallback.get("l10_cap",            0.07),
        "win_pct_run_scale":  float(row[6]) if row[6] is not None else fallback.get("win_pct_run_scale", 13.0),
        "streak_cap_4":       float(row[7]) if row[7] is not None else fallback.get("streak_cap_4",  0.03),
        "streak_cap_6":       float(row[8]) if row[8] is not None else fallback.get("streak_cap_6",  0.00),
        "era_floor":          float(row[9])  if row[9]  is not None else fallback.get("era_floor",        0.60),
        "era_ceil":           float(row[10]) if row[10] is not None else fallback.get("era_ceil",         1.30),
        "streak_med_start":   int(row[11])   if row[11] is not None else fallback.get("streak_med_start",    3),
        "streak_high_start":  int(row[12])   if row[12] is not None else fallback.get("streak_high_start",   6),
    }


def blend_win_pct(season_wp, l10_wp, l10_cap,
                  streak=0, streak_cap_4=0.03, streak_cap_6=0.00,
                  streak_med_start=3, streak_high_start=6):
    """
    Blend season and L10 win%, capping how far the result can deviate from
    season_wp based on the team's current win streak.

      streak < streak_med_start  : cap = l10_cap      (default: streaks 0-2, game ≤3)
      streak_med_start ≤ streak  : cap = streak_cap_4  (default: game 4+, 0.03)
      streak_high_start ≤ streak : cap = streak_cap_6  (default: game 7+, 0.00 — ignore L10)
    """
    if streak >= streak_high_start:
        cap = streak_cap_6
    elif streak >= streak_med_start:
        cap = streak_cap_4
    else:
        cap = l10_cap
    raw = 0.5 * season_wp + 0.5 * l10_wp
    return max(season_wp - cap, min(season_wp + cap, raw))
