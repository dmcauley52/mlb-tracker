"""
fetch_prediction_outcomes.py
Runs nightly at 9 AM UTC (after games are final).
Fills in actual scores for any game_predictions rows that are missing outcomes.
Also computes model errors for backtest comparison.

Usage:
  python fetch_prediction_outcomes.py
"""
import psycopg2
import os
import json
import urllib.request
import time
from datetime import date, timedelta
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
SEASON       = 2026

def mlb_get(path, retries=3):
    url = "https://statsapi.mlb.com/api/v1" + path
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(2)

def main():
    print("fetch_prediction_outcomes.py")
    conn = psycopg2.connect(DATABASE_URL)
    cur  = conn.cursor()

    # Find predictions that don't have actual scores yet
    cur.execute("""
        SELECT game_pk, game_date,
               mc_home_runs, mc_away_runs, woba_home_runs, woba_away_runs,
               mc_home_win_prob, woba_home_win_prob
        FROM game_predictions
        WHERE actual_home_runs IS NULL
          AND game_date < CURRENT_DATE
          AND season = %s
        ORDER BY game_date DESC
        LIMIT 100
    """, (SEASON,))
    pending = cur.fetchall()
    print(f"Found {len(pending)} games needing outcomes")

    updated = 0
    for row in pending:
        (game_pk, game_date,
         mc_home, mc_away, woba_home, woba_away,
         mc_win_prob, woba_win_prob) = row
        try:
            data = mlb_get(f"/game/{game_pk}/linescore")
            home_score = data.get("teams", {}).get("home", {}).get("runs")
            away_score = data.get("teams", {}).get("away", {}).get("runs")
            if home_score is None or away_score is None:
                continue

            actual_home_win = home_score > away_score

            # Model errors
            mc_home_err   = round(abs(mc_home   - home_score), 2) if mc_home   is not None else None
            mc_away_err   = round(abs(mc_away   - away_score), 2) if mc_away   is not None else None
            woba_home_err = round(abs(woba_home - home_score), 2) if woba_home is not None else None
            woba_away_err = round(abs(woba_away - away_score), 2) if woba_away is not None else None

            mc_win_correct   = (actual_home_win == (mc_win_prob   >= 0.50)) if mc_win_prob   is not None else None
            woba_win_correct = (actual_home_win == (woba_win_prob >= 0.50)) if woba_win_prob is not None else None

            cur.execute("""
                UPDATE game_predictions SET
                    actual_home_runs   = %s,
                    actual_away_runs   = %s,
                    actual_home_win    = %s,
                    mc_home_run_err    = %s,
                    mc_away_run_err    = %s,
                    woba_home_run_err  = %s,
                    woba_away_run_err  = %s,
                    mc_win_correct     = %s,
                    woba_win_correct   = %s,
                    outcomes_fetched_at = NOW()
                WHERE game_pk = %s
            """, (
                home_score, away_score, actual_home_win,
                mc_home_err, mc_away_err, woba_home_err, woba_away_err,
                mc_win_correct, woba_win_correct,
                game_pk,
            ))
            updated += 1
            print(f"  {game_pk} {game_date}: home {home_score}-{away_score} away | "
                  f"MC err {mc_home_err}/{mc_away_err} wOBA err {woba_home_err}/{woba_away_err}")
            time.sleep(0.3)
        except Exception as e:
            print(f"  SKIP {game_pk}: {e}")

    conn.commit()
    cur.close()
    conn.close()
    print(f"\nDone — {updated} games updated.")

if __name__ == "__main__":
    main()
