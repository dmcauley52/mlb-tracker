"""
fetch_backtest_cache.py
Runs the prediction backtest for every team using only cached DB tables
(game_results, team_stats_cache, pitcher_profiles, player_gamelogs).
Zero MLB API calls. Upserts results into backtest_results.

Run AFTER: fetch_game_results, fetch_team_stats, fetch_pitcher_profiles.

Usage:
  python fetch_backtest_cache.py
  DAYS=14 python fetch_backtest_cache.py
"""
import psycopg2
import os
import json
from decimal import Decimal

class _DecimalEncoder(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, Decimal):
            return float(o)
        return super().default(o)
import math
from datetime import date, timedelta
from dotenv import load_dotenv
load_dotenv()

SEASON      = 2026
DAYS_WINDOW = int(os.getenv("DAYS", 21))
LEAGUE_AVG_ERA = 4.20

WOBA_WEIGHTS = {
    "bb": 0.690, "hbp": 0.722, "single": 0.888,
    "double": 1.271, "triple": 1.616, "hr": 2.101,
}

# Tunable model weights - keep in sync with analytics.js BACKTEST_WEIGHTS
WEIGHTS = {
    "woba_run_scale": 17.0,   # lowered from 18.5 — reduces systematic run inflation
    "max_predicted_runs": 7.5, # cap prevents 9-10R predictions that always flip to Win
    "score_boost":    0.08,
    "opp_era_scale":  0.75,  # raised from 0.55 so elite/bad starters matter more
    "spot_weights":   [1.15, 1.12, 1.10, 1.05, 1.02, 0.95, 0.90, 0.88, 0.83],
}

def spot_weighted_woba(wobas):
    """Spot-weighted avg wOBA: top-of-order PA weights, normalized so mean is preserved."""
    sw = WEIGHTS["spot_weights"]
    n  = len(wobas)
    if not n:
        return 0.310
    weights  = [sw[i] if i < len(sw) else 0.83 for i in range(n)]
    wt_sum   = sum(weights)
    return sum(w * v for w, v in zip(weights, wobas)) / wt_sum

def compute_woba(s):
    bb  = s.get("baseOnBalls", 0) or 0
    ibb = s.get("intentionalWalks", 0) or 0
    hbp = s.get("hitByPitch", 0) or 0
    h   = s.get("hits", 0) or 0
    dbl = s.get("doubles", 0) or 0
    trp = s.get("triples", 0) or 0
    hr  = s.get("homeRuns", 0) or 0
    ab  = s.get("atBats", 0) or 0
    sf  = s.get("sacFlies", 0) or s.get("sacrificeFlies", 0) or 0
    ubb = bb - ibb
    s1b = h - dbl - trp - hr
    num = (WOBA_WEIGHTS["bb"]*ubb + WOBA_WEIGHTS["hbp"]*hbp +
           WOBA_WEIGHTS["single"]*s1b + WOBA_WEIGHTS["double"]*dbl +
           WOBA_WEIGHTS["triple"]*trp + WOBA_WEIGHTS["hr"]*hr)
    den = ab + ubb + hbp + sf
    return round(num / den, 4) if den > 0 else None

def predict_game(avg_woba, avg_score, opp_era, opp_win_pct, opp_lineup_woba=None, my_win_pct=0.500):
    w = WEIGHTS
    era_factor      = max(0.80, min(1.30, float(opp_era) / LEAGUE_AVG_ERA))
    score_factor    = 1 + (avg_score - 50) / 99 * w["score_boost"]
    adj_era_factor  = era_factor * w["opp_era_scale"] + 1.0 * (1 - w["opp_era_scale"])
    # Fix 1: team quality discount - bad teams convert wOBA to runs less efficiently
    team_quality    = max(0.88, min(1.12, 1.0 + (float(my_win_pct) - 0.500) * 0.5))
    predicted_runs  = min(
        avg_woba * w["woba_run_scale"] * score_factor * adj_era_factor * team_quality,
        w["max_predicted_runs"]
    )
    opp_runs_win_pct = 4.65 + (float(opp_win_pct) - 0.500) * 13.0
    if opp_lineup_woba:
        opp_runs_est = float(opp_lineup_woba) * w["woba_run_scale"] * 0.6 + opp_runs_win_pct * 0.4
    else:
        opp_runs_est = opp_runs_win_pct
    run_diff        = predicted_runs - opp_runs_est
    # Fix 3: softened sigmoid (0.40 vs 0.55) - less confident on close run differentials
    win_prob        = 1 / (1 + math.exp(-run_diff * 0.40))
    return {
        "predicted_runs": round(predicted_runs, 2),
        "predicted_woba": round(avg_woba, 4),
        "predicted_win":  win_prob >= 0.50,
        "win_prob":       round(win_prob, 3),
        "opp_runs_est":   round(opp_runs_est, 2),
    }

# ── DB connection ─────────────────────────────────────────────────────────────
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()
today      = date.today()
start_date = today - timedelta(days=DAYS_WINDOW)

# ── Load team stats cache ─────────────────────────────────────────────────────
print("Loading team_stats_cache...")
cur.execute("SELECT team_name, team_id, season_era, win_pct, l10_win_pct FROM team_stats_cache WHERE season = %s", (SEASON,))
team_cache = {r[0]: {"id": r[1], "era": r[2] or LEAGUE_AVG_ERA, "win_pct": r[3] or 0.500, "l10_win_pct": r[4] or 0.500}
              for r in cur.fetchall()}
team_id_to_name = {v["id"]: k for k, v in team_cache.items()}

# ── Load pitcher profiles ─────────────────────────────────────────────────────
print("Loading pitcher_profiles...")
cur.execute("SELECT player_id, player_name, hand, season_era, season_whip, k_per_9, median_ip, era_rank_al, era_rank_nl FROM pitcher_profiles WHERE season = %s", (SEASON,))
pitcher_cache = {r[0]: {"name": r[1], "hand": r[2], "era": r[3], "whip": r[4], "k9": r[5], "median_ip": r[6], "era_rank_al": r[7], "era_rank_nl": r[8]}
                 for r in cur.fetchall()}

# ── Load roster: season avg wOBA + last-5 wOBA per player ────────────────────
print("Loading player_gamelogs for roster wOBA...")
cur.execute("""
    SELECT player_name, team,
           -- Fix 2: 60 pct recent (last 21 days) + 40 pct season - slumping players get a more accurate wOBA
           COALESCE(
               AVG(woba) FILTER (WHERE woba > 0 AND game_date >= current_date - 21) * 0.6
               + AVG(woba) FILTER (WHERE woba > 0) * 0.4,
               AVG(woba) FILTER (WHERE woba > 0)
           ) AS avg_woba,
           COUNT(*) AS games_played,
           -- Fix 3: last-5 avg wOBA for momentum score (mirrors JS trendScore component)
           AVG(woba) FILTER (WHERE woba > 0 AND game_date >= current_date - 7) AS last5_woba
    FROM player_gamelogs
    WHERE season = %s AND woba IS NOT NULL
    GROUP BY player_name, team
    HAVING COUNT(*) >= 5
""", (SEASON,))
roster_rows = cur.fetchall()

def momentum_score(avg_woba, last5_woba):
    """Mirrors JS trendScore: last-5 wOBA vs season mean, scaled 0-99.
    Uses wOBA midpoint of 0.315 matching the JS sigMid constant."""
    if not last5_woba or not avg_woba:
        return 50
    # JS: trendScore = min(25, max(0, round((last5Val / (sigMid * 1.4)) * 25)))
    # Scale to 0-99 by also factoring season level vs league avg (0.315)
    trend = min(25, max(0, (float(last5_woba) / (0.315 * 1.4)) * 25))
    level = min(25, max(0, (float(avg_woba) / (0.315 * 1.4)) * 25))
    return min(99, max(1, round(trend * 0.6 + level * 0.4 + 24)))  # +24 so avg player ~50

# name → {team, avg_woba, games_played, pred_score}
roster_map = {}
for name, team, avg_woba, gp, last5_woba in roster_rows:
    score = momentum_score(avg_woba, last5_woba)
    roster_map[name] = {
        "team": team,
        "avg_woba": float(avg_woba) if avg_woba else 0.310,
        "games_played": gp,
        "pred_score": score,
    }

# Group by team for lineup building
team_roster = {}
for name, info in roster_map.items():
    t = info["team"]
    if t not in team_roster:
        team_roster[t] = []
    team_roster[t].append({"name": name, "avg_woba": info["avg_woba"], "pred_score": info["pred_score"]})

# Sort each team's roster by wOBA desc (top-9 = suggested lineup)
for t in team_roster:
    team_roster[t].sort(key=lambda p: p["avg_woba"], reverse=True)

# ── Load game_results for the window ─────────────────────────────────────────
print(f"Loading game_results {start_date} to {today - timedelta(days=1)}...")
cur.execute("""
    SELECT game_pk, game_date,
           home_team, away_team, home_team_id, away_team_id,
           home_score, away_score,
           home_team_woba, away_team_woba,
           sp_home_id, sp_away_id,
           home_lineup, away_lineup
    FROM game_results
    WHERE game_date >= %s AND game_date < %s AND season = %s
    ORDER BY game_date
""", (start_date, today, SEASON))
games_raw = cur.fetchall()
print(f"  {len(games_raw)} games loaded")

# ── Build per-team game rows ──────────────────────────────────────────────────
# game_rows_by_team: team_name → [row, ...]
game_rows_by_team = {}

for row in games_raw:
    (game_pk, game_date, home_team, away_team, home_id, away_id,
     home_score, away_score, home_woba, away_woba,
     sp_home_id, sp_away_id, home_lineup_j, away_lineup_j) = row

    home_lineup = home_lineup_j if isinstance(home_lineup_j, list) else (json.loads(home_lineup_j) if home_lineup_j else [])
    away_lineup = away_lineup_j if isinstance(away_lineup_j, list) else (json.loads(away_lineup_j) if away_lineup_j else [])

    for is_home in [True, False]:
        my_team   = home_team if is_home else away_team
        opp_team  = away_team if is_home else home_team
        my_score  = home_score if is_home else away_score
        opp_score = away_score if is_home else home_score
        my_woba   = home_woba  if is_home else away_woba
        opp_woba  = away_woba  if is_home else home_woba
        my_lineup = home_lineup if is_home else away_lineup
        sp_id     = sp_away_id if is_home else sp_home_id  # opposing starter

        if my_score is None or opp_score is None:
            continue

        opp_info    = team_cache.get(opp_team, {})
        my_info     = team_cache.get(my_team, {})
        sp_info     = pitcher_cache.get(sp_id, {}) if sp_id else {}
        opp_era     = sp_info.get("era") or opp_info.get("era") or LEAGUE_AVG_ERA
        # Blend last-10 and season 50/50 - last-10 alone is too noisy over 10 games
        l10  = float(opp_info.get("l10_win_pct") or opp_info.get("win_pct") or 0.500)
        seas = float(opp_info.get("win_pct") or 0.500)
        opp_win_pct = (l10 * 0.5 + seas * 0.5) if opp_info.get("l10_win_pct") else seas
        my_l10  = float(my_info.get("l10_win_pct") or my_info.get("win_pct") or 0.500)
        my_seas = float(my_info.get("win_pct") or 0.500)
        my_win_pct = (my_l10 * 0.5 + my_seas * 0.5) if my_info.get("l10_win_pct") else my_seas

        # Actual lineup wOBA + momentum scores from roster map
        actual_players  = [roster_map.get(p["name"], {}) for p in my_lineup if p.get("name")]
        actual_wobas    = [pl.get("avg_woba", 0.310) for pl in actual_players]
        actual_scores   = [pl.get("pred_score", 50)  for pl in actual_players]
        avg_actual_woba = spot_weighted_woba(actual_wobas)
        avg_actual_score = sum(actual_scores) / len(actual_scores) if actual_scores else 50
        opp_woba_f = float(opp_woba) if opp_woba else None
        pred_actual = predict_game(avg_actual_woba, avg_actual_score, opp_era, opp_win_pct, opp_woba_f, my_win_pct)

        # Suggested lineup (top-9 by season wOBA, already sorted descending)
        sug9 = (team_roster.get(my_team) or [])[:9]
        sug_wobas  = [p["avg_woba"]   for p in sug9]
        sug_scores = [p.get("pred_score", 50) for p in sug9]
        avg_sug_woba  = spot_weighted_woba(sug_wobas)
        avg_sug_score = sum(sug_scores) / len(sug_scores) if sug_scores else 50
        pred_sug = predict_game(avg_sug_woba, avg_sug_score, opp_era, opp_win_pct, opp_woba_f, my_win_pct)

        sp_abbr = sp_info.get("name", "").split()[-1] if sp_info.get("name") else None

        game_row = {
            "date":          str(game_date),
            "game_pk":       game_pk,
            "opponent":      opp_team,
            "is_home":       is_home,
            "actual_win":    my_score > opp_score,
            "actual_runs":   my_score,
            "opp_runs":      opp_score,
            "actual_woba":   float(my_woba) if my_woba else None,
            "opp_era":       round(float(opp_era), 2),
            "opp_win_pct":   float(opp_win_pct),
            "sp":            {"name": sp_info.get("name"), "hand": sp_info.get("hand"), "era": sp_info.get("era")} if sp_info else None,
            "actual_lineup": [{"name": p["name"], "avg_woba": roster_map.get(p["name"], {}).get("avg_woba", 0.310)} for p in my_lineup],
            "actual": {
                "predicted_runs": pred_actual["predicted_runs"],
                "predicted_woba": pred_actual["predicted_woba"],
                "predicted_win":  pred_actual["predicted_win"],
                "win_prob":       pred_actual["win_prob"],
                "run_error":      round(abs(pred_actual["predicted_runs"] - my_score), 2),
                "woba_error":     round(abs(pred_actual["predicted_woba"] - float(my_woba)), 4) if my_woba else None,
            },
            "suggested": {
                "predicted_runs": pred_sug["predicted_runs"],
                "predicted_woba": pred_sug["predicted_woba"],
                "predicted_win":  pred_sug["predicted_win"],
                "win_prob":       pred_sug["win_prob"],
                "run_error":      round(abs(pred_sug["predicted_runs"] - my_score), 2),
                "woba_error":     round(abs(pred_sug["predicted_woba"] - float(my_woba)), 4) if my_woba else None,
            },
        }

        if my_team not in game_rows_by_team:
            game_rows_by_team[my_team] = []
        game_rows_by_team[my_team].append(game_row)

# ── Compute summaries and upsert ──────────────────────────────────────────────
print("Computing summaries and upserting backtest_results...")
saved = skipped = 0

for team, rows in game_rows_by_team.items():
    if not rows:
        skipped += 1
        continue

    actual_rows = [r for r in rows if r.get("actual")]
    sug_rows    = [r for r in rows if r.get("suggested")]

    def summarize(rlist, key):
        if not rlist:
            return None
        run_errors = [r[key]["run_error"] for r in rlist]
        win_correct = sum(1 for r in rlist if r[key]["predicted_win"] == r["actual_win"])
        woba_errors = [r[key]["woba_error"] for r in rlist if r[key].get("woba_error") is not None]
        return {
            "win_accuracy": round(win_correct / len(rlist), 3),
            "avg_run_mae":  round(sum(run_errors) / len(run_errors), 2),
            "avg_woba_mae": round(sum(woba_errors) / len(woba_errors), 4) if woba_errors else None,
            "games_eval":   len(rlist),
        }

    act_sum = summarize(actual_rows, "actual")
    sug_sum = summarize(sug_rows, "suggested")
    primary = act_sum or sug_sum

    try:
        cur.execute("""
            INSERT INTO backtest_results
            (team, run_date, days_window, season,
             games_eval, win_accuracy, avg_run_mae, avg_woba_mae,
             game_rows, created_at)
            VALUES (%s,%s,%s,%s, %s,%s,%s,%s, %s, NOW())
            ON CONFLICT (team, run_date, days_window) DO UPDATE SET
                games_eval   = EXCLUDED.games_eval,
                win_accuracy = EXCLUDED.win_accuracy,
                avg_run_mae  = EXCLUDED.avg_run_mae,
                avg_woba_mae = EXCLUDED.avg_woba_mae,
                game_rows    = EXCLUDED.game_rows,
                created_at   = NOW()
        """, (
            team, str(today), DAYS_WINDOW, SEASON,
            primary["games_eval"] if primary else 0,
            primary["win_accuracy"] if primary else None,
            primary["avg_run_mae"] if primary else None,
            primary["avg_woba_mae"] if primary else None,
            json.dumps({
                "games":           rows,
                "actualSummary":   act_sum,
                "suggestedSummary": sug_sum,
            }, cls=_DecimalEncoder),
        ))
        conn.commit()
        acc = f"{round(primary['win_accuracy']*100)}%" if primary else "-"
        print(f"  OK {team}: {len(rows)}g  W/L {acc}  RunMAE {primary['avg_run_mae'] if primary else '-'}")
        saved += 1
    except Exception as e:
        print(f"  ERR {team}: {e}")
        conn.rollback()
        skipped += 1

cur.close()
conn.close()
print(f"\nbacktest_results: {saved} teams saved, {skipped} skipped/errors")
