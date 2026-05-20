"""
backtest_season_split.py
Full-season H1/H2 split backtest.

Trains on H1 stats (game_date < split_date), evaluates predictions on every
H2 game (game_date >= split_date). Zero MLB API calls — all from DB.

Usage (Windows):
  set SEASON=2025
  set SPLIT_DATE=2025-07-14
  python backtest_season_split.py

Optional:
  set OUTPUT=results_2025.json   (writes per-game detail to JSON)

Prerequisites — must have backfilled 2025 data first:
  set SEASON=2025 && python backfill_stats.py
  set SEASON=2025 && python backfill_pitcher_stats.py
  set SEASON=2025 && set MODE=backfill && set BACKFILL_START=2025-03-27 && python fetch_game_results.py
"""
import psycopg2
import os
import json
import math
from decimal import Decimal
from datetime import date, datetime
from dotenv import load_dotenv
from model_config import (
    LEAGUE_AVG_ERA, OPP_RUNS_BASE, SEASON as DEFAULT_SEASON,
    WIN_PCT_RUN_SCALE, WIN_PROB_SIGMOID_SCALE, WOBA_WEIGHTS,
)
from model_weights import load_weights, FALLBACK_BACKTEST

load_dotenv()

SEASON     = int(os.getenv("SEASON", DEFAULT_SEASON))
SPLIT_DATE = os.getenv("SPLIT_DATE", f"{SEASON}-07-14")
OUTPUT     = os.getenv("OUTPUT", "")

split_dt = datetime.strptime(SPLIT_DATE, "%Y-%m-%d").date()
print(f"Season: {SEASON}  |  Split date: {split_dt}")
print(f"  H1 (training) : {SEASON}-03-20 → {split_dt - __import__('datetime').timedelta(days=1)}")
print(f"  H2 (test)     : {split_dt} → end of season")

# ── DB connection ─────────────────────────────────────────────────────────────
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

WEIGHTS = load_weights(cur, "backtest", FALLBACK_BACKTEST)
print(f"\nWeights: woba_run_scale={WEIGHTS['woba_run_scale']}  "
      f"max_runs={WEIGHTS['max_predicted_runs']}  "
      f"era_scale={WEIGHTS['opp_era_scale']}")


# ── Helpers ───────────────────────────────────────────────────────────────────
def spot_weighted_woba(wobas):
    sw = WEIGHTS["spot_weights"]
    n  = len(wobas)
    if not n:
        return 0.310
    weights = [sw[i] if i < len(sw) else 0.83 for i in range(n)]
    wt_sum  = sum(weights)
    return sum(w * v for w, v in zip(weights, wobas)) / wt_sum


def momentum_score(avg_woba, last21_woba):
    if not last21_woba or not avg_woba:
        return 50
    trend = min(25, max(0, (float(last21_woba) / (0.315 * 1.4)) * 25))
    level = min(25, max(0, (float(avg_woba)    / (0.315 * 1.4)) * 25))
    return min(99, max(1, round(trend * 0.6 + level * 0.4 + 24)))


def predict_game(avg_woba, avg_score, opp_era, opp_win_pct,
                 opp_lineup_woba=None, my_win_pct=0.500):
    w             = WEIGHTS
    era_factor    = max(0.80, min(1.30, float(opp_era) / LEAGUE_AVG_ERA))
    score_factor  = 1 + (avg_score - 50) / 99 * w["score_boost"]
    adj_era       = era_factor * w["opp_era_scale"] + 1.0 * (1 - w["opp_era_scale"])
    team_quality  = max(0.88, min(1.12, 1.0 + (float(my_win_pct) - 0.500) * 0.5))
    pred_runs     = min(avg_woba * w["woba_run_scale"] * score_factor * adj_era * team_quality,
                        w["max_predicted_runs"])
    opp_runs_base = OPP_RUNS_BASE + (float(opp_win_pct) - 0.500) * WIN_PCT_RUN_SCALE
    if opp_lineup_woba:
        opp_runs = float(opp_lineup_woba) * w["woba_run_scale"] * 0.6 + opp_runs_base * 0.4
    else:
        opp_runs = opp_runs_base
    run_diff  = pred_runs - opp_runs
    win_prob  = 1 / (1 + math.exp(-run_diff * WIN_PROB_SIGMOID_SCALE))
    return {
        "predicted_runs": round(pred_runs, 2),
        "predicted_woba": round(avg_woba, 4),
        "predicted_win":  win_prob >= 0.50,
        "win_prob":       round(win_prob, 3),
        "opp_runs_est":   round(opp_runs, 2),
    }


# ── H1 roster: player wOBA from games before split_date ──────────────────────
print("\nLoading H1 roster stats (player_gamelogs)...")
cur.execute("""
    SELECT player_name, team,
           COALESCE(
               AVG(woba) FILTER (WHERE woba > 0 AND game_date >= %s - interval '21 days') * 0.6
               + AVG(woba) FILTER (WHERE woba > 0) * 0.4,
               AVG(woba) FILTER (WHERE woba > 0)
           ) AS avg_woba,
           COUNT(*) AS games_played,
           AVG(woba) FILTER (WHERE woba > 0 AND game_date >= %s - interval '21 days') AS last21_woba
    FROM player_gamelogs
    WHERE season = %s AND game_date < %s AND woba IS NOT NULL
    GROUP BY player_name, team
    HAVING COUNT(*) >= 5
""", (split_dt, split_dt, SEASON, split_dt))
roster_rows = cur.fetchall()
print(f"  {len(roster_rows)} players with 5+ H1 games")

roster_map = {}
for name, team, avg_woba, gp, last21_woba in roster_rows:
    score = momentum_score(avg_woba, last21_woba)
    roster_map[name] = {
        "team":       team,
        "avg_woba":   float(avg_woba) if avg_woba else 0.310,
        "games":      gp,
        "pred_score": score,
    }

team_roster = {}
for name, info in roster_map.items():
    t = info["team"]
    team_roster.setdefault(t, []).append({
        "name": name, "avg_woba": info["avg_woba"], "pred_score": info["pred_score"]
    })
for t in team_roster:
    team_roster[t].sort(key=lambda p: p["avg_woba"], reverse=True)


# ── H1 team win% from game_results before split_date ─────────────────────────
print("Computing H1 team win%...")
cur.execute("""
    SELECT team,
           SUM(wins)::float / NULLIF(SUM(games), 0) AS win_pct,
           SUM(wins) AS wins, SUM(games) AS games
    FROM (
        SELECT home_team AS team,
               SUM(CASE WHEN home_score > away_score THEN 1 ELSE 0 END) AS wins,
               COUNT(*) AS games
        FROM game_results
        WHERE season = %s AND game_date < %s
        GROUP BY home_team
        UNION ALL
        SELECT away_team,
               SUM(CASE WHEN away_score > home_score THEN 1 ELSE 0 END),
               COUNT(*)
        FROM game_results
        WHERE season = %s AND game_date < %s
        GROUP BY away_team
    ) t
    GROUP BY team
""", (SEASON, split_dt, SEASON, split_dt))
team_h1 = {}
for team, win_pct, wins, games in cur.fetchall():
    team_h1[team] = {"win_pct": float(win_pct) if win_pct else 0.500,
                     "wins": wins, "games": games}
print(f"  {len(team_h1)} teams with H1 records")


# ── H1 pitcher ERA per pitcher from pitcher_gamelogs before split_date ────────
print("Computing H1 pitcher ERA (pitcher_gamelogs)...")
cur.execute("""
    SELECT player_id, player_name,
           SUM(earned_runs) * 9.0 / NULLIF(SUM(innings_pitched), 0) AS era
    FROM pitcher_gamelogs
    WHERE season = %s AND game_date < %s AND is_starter = true
    GROUP BY player_id, player_name
    HAVING SUM(innings_pitched) >= 10
""", (SEASON, split_dt))
pitcher_h1 = {}
for pid, name, era in cur.fetchall():
    pitcher_h1[pid] = {"name": name, "era": float(era) if era else LEAGUE_AVG_ERA}
print(f"  {len(pitcher_h1)} starters with 10+ H1 IP")

# H1 team ERA as fallback (avg of starters' ERAs per team)
cur.execute("""
    SELECT team,
           SUM(earned_runs) * 9.0 / NULLIF(SUM(innings_pitched), 0) AS team_era
    FROM pitcher_gamelogs
    WHERE season = %s AND game_date < %s AND is_starter = true
    GROUP BY team
    HAVING SUM(innings_pitched) >= 20
""", (SEASON, split_dt))
team_era_h1 = {row[0]: float(row[1]) if row[1] else LEAGUE_AVG_ERA for row in cur.fetchall()}


# ── H2 games to evaluate ──────────────────────────────────────────────────────
print(f"\nLoading H2 games (game_date >= {split_dt})...")
cur.execute("""
    SELECT game_pk, game_date,
           home_team, away_team, home_team_id, away_team_id,
           home_score, away_score,
           home_team_woba, away_team_woba,
           sp_home_id, sp_away_id,
           home_lineup, away_lineup
    FROM game_results
    WHERE game_date >= %s AND season = %s
    ORDER BY game_date
""", (split_dt, SEASON))
h2_games = cur.fetchall()
print(f"  {len(h2_games)} H2 games loaded")

cur.close()
conn.close()

if not h2_games:
    print("\nNo H2 games found — run the backfill first:")
    print("  set SEASON=2025 && python backfill_stats.py")
    print("  set SEASON=2025 && python backfill_pitcher_stats.py")
    print("  set SEASON=2025 && set MODE=backfill && set BACKFILL_START=2025-03-27 && python fetch_game_results.py")
    raise SystemExit(1)


# ── Predict each H2 game ──────────────────────────────────────────────────────
print("\nRunning predictions on H2 games...")

game_rows_by_team = {}
games_no_roster = 0

for row in h2_games:
    (game_pk, game_date, home_team, away_team, home_id, away_id,
     home_score, away_score, home_woba, away_woba,
     sp_home_id, sp_away_id, home_lineup_j, away_lineup_j) = row

    if home_score is None or away_score is None:
        continue

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
        sp_id     = sp_away_id if is_home else sp_home_id

        opp_h1      = team_h1.get(opp_team, {})
        my_h1       = team_h1.get(my_team, {})
        sp_info     = pitcher_h1.get(sp_id, {}) if sp_id else {}
        opp_era     = sp_info.get("era") or team_era_h1.get(opp_team) or LEAGUE_AVG_ERA
        opp_win_pct = opp_h1.get("win_pct", 0.500)
        my_win_pct  = my_h1.get("win_pct", 0.500)

        # Actual lineup wOBA + momentum score from H1 roster
        actual_players = [roster_map.get(p["name"], {}) for p in my_lineup if p.get("name")]
        actual_wobas   = [pl.get("avg_woba", 0.310) for pl in actual_players]
        actual_scores  = [pl.get("pred_score", 50)  for pl in actual_players]

        if not actual_players:
            games_no_roster += 1

        avg_actual_woba  = spot_weighted_woba(actual_wobas) if actual_wobas else 0.310
        avg_actual_score = sum(actual_scores) / len(actual_scores) if actual_scores else 50
        opp_woba_f = float(opp_woba) if opp_woba else None
        pred = predict_game(avg_actual_woba, avg_actual_score, opp_era, opp_win_pct, opp_woba_f, my_win_pct)

        # Suggested: top-9 H1 wOBA players
        sug9       = (team_roster.get(my_team) or [])[:9]
        sug_wobas  = [p["avg_woba"]   for p in sug9]
        sug_scores = [p["pred_score"] for p in sug9]
        avg_sug_woba  = spot_weighted_woba(sug_wobas) if sug_wobas else 0.310
        avg_sug_score = sum(sug_scores) / len(sug_scores) if sug_scores else 50
        pred_sug = predict_game(avg_sug_woba, avg_sug_score, opp_era, opp_win_pct, opp_woba_f, my_win_pct)

        game_row = {
            "date":       str(game_date),
            "game_pk":    game_pk,
            "opponent":   opp_team,
            "is_home":    is_home,
            "actual_win": my_score > opp_score,
            "actual_runs": my_score,
            "opp_runs":   opp_score,
            "opp_era":    round(float(opp_era), 2),
            "opp_win_pct": float(opp_win_pct),
            "lineup_size": len(actual_players),
            "actual": {
                "predicted_runs": pred["predicted_runs"],
                "predicted_win":  pred["predicted_win"],
                "win_prob":       pred["win_prob"],
                "run_error":      round(abs(pred["predicted_runs"] - my_score), 2),
            },
            "suggested": {
                "predicted_runs": pred_sug["predicted_runs"],
                "predicted_win":  pred_sug["predicted_win"],
                "win_prob":       pred_sug["win_prob"],
                "run_error":      round(abs(pred_sug["predicted_runs"] - my_score), 2),
            },
        }
        game_rows_by_team.setdefault(my_team, []).append(game_row)

print(f"  {games_no_roster} game-sides had no roster match (lineup names not in H1 gamelogs)")


# ── Summarize ─────────────────────────────────────────────────────────────────
def summarize(rows, key):
    if not rows:
        return None
    correct = sum(1 for r in rows if r[key]["predicted_win"] == r["actual_win"])
    run_errs = [r[key]["run_error"] for r in rows]
    return {
        "games":        len(rows),
        "win_accuracy": round(correct / len(rows), 3),
        "win_correct":  correct,
        "avg_run_mae":  round(sum(run_errs) / len(run_errs), 2),
    }

print("\n" + "=" * 70)
print(f"{'TEAM':<26} {'GAMES':>5}  {'ACT W/L%':>8}  {'ACT RunMAE':>10}  {'SUG W/L%':>8}  {'SUG RunMAE':>10}")
print("-" * 70)

all_act_rows = []
all_sug_rows = []
team_summaries = {}

for team in sorted(game_rows_by_team.keys()):
    rows = game_rows_by_team[team]
    act  = summarize(rows, "actual")
    sug  = summarize(rows, "suggested")
    team_summaries[team] = {"actual": act, "suggested": sug, "games": rows}
    all_act_rows.extend(rows)
    all_sug_rows.extend(rows)

    act_pct = f"{act['win_accuracy']*100:.1f}%" if act else "-"
    act_mae = f"{act['avg_run_mae']:.2f}"        if act else "-"
    sug_pct = f"{sug['win_accuracy']*100:.1f}%" if sug else "-"
    sug_mae = f"{sug['avg_run_mae']:.2f}"        if sug else "-"
    print(f"{team:<26} {len(rows):>5}  {act_pct:>8}  {act_mae:>10}  {sug_pct:>8}  {sug_mae:>10}")

overall_act = summarize(all_act_rows, "actual")
overall_sug = summarize(all_sug_rows, "suggested")
print("=" * 70)
print(f"{'OVERALL':<26} {overall_act['games']:>5}  "
      f"{overall_act['win_accuracy']*100:.1f}%  "
      f"{' ':>2}{overall_act['avg_run_mae']:>8.2f}  "
      f"{overall_sug['win_accuracy']*100:.1f}%  "
      f"{' ':>2}{overall_sug['avg_run_mae']:>8.2f}")

print(f"\nSummary")
print(f"  Actual lineup  — W/L accuracy: {overall_act['win_accuracy']*100:.1f}%  "
      f"Run MAE: {overall_act['avg_run_mae']:.2f}")
print(f"  Suggested top-9 — W/L accuracy: {overall_sug['win_accuracy']*100:.1f}%  "
      f"Run MAE: {overall_sug['avg_run_mae']:.2f}")
print(f"  Total H2 game-sides evaluated: {overall_act['games']}")
print(f"  Training window: H1 {SEASON} (games before {split_dt})")


# ── Optional JSON output ──────────────────────────────────────────────────────
if OUTPUT:
    class _Enc(json.JSONEncoder):
        def default(self, o):
            if isinstance(o, (date, datetime)): return str(o)
            if isinstance(o, Decimal):          return float(o)
            return super().default(o)

    payload = {
        "season": SEASON,
        "split_date": str(split_dt),
        "overall_actual":    overall_act,
        "overall_suggested": overall_sug,
        "by_team": {
            team: {"actual": v["actual"], "suggested": v["suggested"]}
            for team, v in team_summaries.items()
        },
        "game_rows_by_team": {
            team: v["games"] for team, v in team_summaries.items()
        },
    }
    with open(OUTPUT, "w") as f:
        json.dump(payload, f, cls=_Enc, indent=2)
    print(f"\nDetailed results written to {OUTPUT}")
