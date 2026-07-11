"""
fetch_game_predictions.py
Runs daily at ~5 PM ET (lineups typically posted by then).
For every MLB game today:
  1. Fetches official lineup via hydrate=lineups
  2. Pulls last-30-game PA distributions from player_gamelogs
  3. Runs 10,000-iteration Monte Carlo simulation
  4. Computes wOBA-model prediction (same formula as analytics.js _predictGame)
  5. Upserts both predictions into game_predictions table

Run:
  python fetch_game_predictions.py
  GAME_DATE=2026-05-06 python fetch_game_predictions.py  (override date)
"""
import psycopg2
import os
import json
import time
import urllib.request
from datetime import date, timedelta
from dotenv import load_dotenv
from model_config import (
    LEAGUE_AVG_ERA,
    LEAGUE_AVG_K9,
    SEASON,
)

load_dotenv()

GAME_DATE       = os.getenv("GAME_DATE", str(date.today()))
DATABASE_URL    = os.getenv("DATABASE_URL")
SIM_ITERATIONS  = 10_000

from model_weights import load_weights, FALLBACK_BACKTEST
from prediction_engine import (
    build_distributions,
    run_monte_carlo,
    compute_woba,
    predict_runs_woba,
)

SPOT_PA = [4.5, 4.4, 4.3, 4.2, 4.1, 4.0, 3.95, 3.9, 3.8]

# ── HTTP helper ───────────────────────────────────────────────────────────────
def mlb_get(path, retries=3):
    url = "https://statsapi.mlb.com/api/v1" + path
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(2)

# ── DB helpers ────────────────────────────────────────────────────────────────
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def fetch_player_games(cur, player_names, since_date):
    """Returns dict of player_name -> list of gamelog rows (last 30 games each)."""
    if not player_names:
        return {}
    cur.execute("""
        SELECT player_name, plate_appearances, at_bats, hits, doubles, triples,
               home_runs, walks, hit_by_pitch, strikeouts, sac_flies
        FROM player_gamelogs
        WHERE player_name = ANY(%s)
          AND game_date >= %s
        ORDER BY player_name, game_date DESC
    """, (list(player_names), since_date))
    rows = cur.fetchall()
    cols = ["player_name","plate_appearances","at_bats","hits","doubles","triples",
            "home_runs","walks","hit_by_pitch","strikeouts","sac_flies"]
    by_player = {}
    for row in rows:
        r = dict(zip(cols, row))
        name = r["player_name"]
        if name not in by_player:
            by_player[name] = []
        if len(by_player[name]) < 30:
            by_player[name].append(r)
    return by_player

def fetch_team_stats(cur, team_name):
    """Returns (win_pct, era) for a team from the cache."""
    cur.execute("""
        SELECT win_pct, season_era FROM team_stats_cache WHERE team_name = %s
    """, (team_name,))
    row = cur.fetchone()
    return (float(row[0]), float(row[1])) if row else (0.500, LEAGUE_AVG_ERA)

def fetch_pitcher_profile(cur, pitcher_name):
    """Returns (era, k9) for an SP from the cache."""
    cur.execute("""
        SELECT season_era, k_per_9 FROM pitcher_profiles WHERE player_name = %s
        ORDER BY fetched_date DESC LIMIT 1
    """, (pitcher_name,))
    row = cur.fetchone()
    if not row or row[0] is None or row[1] is None:
        return (LEAGUE_AVG_ERA, LEAGUE_AVG_K9)
    return (float(row[0]), float(row[1]))

# ── MLB API fetchers ──────────────────────────────────────────────────────────
def fetch_todays_games(game_date):
    data = mlb_get(f"/schedule?sportId=1&date={game_date}&hydrate=lineups,probablePitcher,teams")
    games = []
    for d in data.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") == "Final":
                continue  # already done — skip
            home = g["teams"]["home"]
            away = g["teams"]["away"]
            lineups = g.get("lineups", {})
            home_players = lineups.get("homePlayers", [])
            away_players = lineups.get("awayPlayers", [])
            if not home_players or not away_players:
                print(f"  SKIP {home['team']['name']} vs {away['team']['name']} — lineup not posted")
                continue
            home_sp = home.get("probablePitcher", {})
            away_sp = away.get("probablePitcher", {})
            games.append({
                "game_pk":       g["gamePk"],
                "home_team":     home["team"]["name"],
                "away_team":     away["team"]["name"],
                "home_team_id":  home["team"]["id"],
                "away_team_id":  away["team"]["id"],
                "home_lineup":   [p["fullName"] for p in home_players],
                "away_lineup":   [p["fullName"] for p in away_players],
                "home_sp_name":  home_sp.get("fullName"),
                "away_sp_name":  away_sp.get("fullName"),
            })
    return games

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"fetch_game_predictions.py — {GAME_DATE}")
    conn = get_conn()
    cur  = conn.cursor()

    w = load_weights(cur, "backtest", FALLBACK_BACKTEST)
    print(f"Loaded weights: woba_run_scale={w['woba_run_scale']} max_pred_runs={w['max_predicted_runs']} opp_era_scale={w['opp_era_scale']}")

    games = fetch_todays_games(GAME_DATE)
    print(f"Found {len(games)} games with lineups posted")

    since = str(date.fromisoformat(GAME_DATE) - timedelta(days=60))

    for g in games:
        print(f"\n{g['away_team']} @ {g['home_team']} (pk={g['game_pk']})")

        # Collect all player names for one DB query
        all_names = list(set(g["home_lineup"] + g["away_lineup"]))
        games_map = fetch_player_games(cur, all_names, since)

        home_db = sum(1 for n in g["home_lineup"] if n in games_map and len(games_map[n]) >= 10)
        away_db = sum(1 for n in g["away_lineup"] if n in games_map and len(games_map[n]) >= 10)
        print(f"  DB data: home {home_db}/9, away {away_db}/9")

        # SP profiles
        home_sp_era, home_sp_k9 = (fetch_pitcher_profile(cur, g["home_sp_name"])
                                    if g["home_sp_name"] else (LEAGUE_AVG_ERA, LEAGUE_AVG_K9))
        away_sp_era, away_sp_k9 = (fetch_pitcher_profile(cur, g["away_sp_name"])
                                    if g["away_sp_name"] else (LEAGUE_AVG_ERA, LEAGUE_AVG_K9))

        # Team win pcts
        home_win_pct, _ = fetch_team_stats(cur, g["home_team"])
        away_win_pct, _ = fetch_team_stats(cur, g["away_team"])

        # Build PA distributions (home batters face away SP, vice versa)
        home_dists = build_distributions(g["home_lineup"], games_map, away_sp_era, away_sp_k9)
        away_dists = build_distributions(g["away_lineup"], games_map, home_sp_era, home_sp_k9)

        # ── Monte Carlo ───────────────────────────────────────────────────────
        print(f"  Running {SIM_ITERATIONS:,} simulations…")
        mc = run_monte_carlo(home_dists, away_dists)
        print(f"  MC: {g['home_team']} {mc['home_mean']} ({mc['home_p10']}–{mc['home_p90']}) "
              f"vs {g['away_team']} {mc['away_mean']} ({mc['away_p10']}–{mc['away_p90']}) "
              f"home win {mc['home_win_prob']:.1%}")

        # ── wOBA model ────────────────────────────────────────────────────────
        home_wobas = [compute_woba(games_map.get(n, [])) for n in g["home_lineup"]]
        away_wobas = [compute_woba(games_map.get(n, [])) for n in g["away_lineup"]]
        home_lineup_woba = round(sum(home_wobas) / len(home_wobas), 4) if home_wobas else 0.310
        away_lineup_woba = round(sum(away_wobas) / len(away_wobas), 4) if away_wobas else 0.310

        home_woba_pred = predict_runs_woba(
            home_wobas, away_sp_era, home_win_pct, away_win_pct,
            opp_lineup_woba=away_lineup_woba, is_home=True, weights=w)
        away_woba_pred = predict_runs_woba(
            away_wobas, home_sp_era, away_win_pct, home_win_pct,
            opp_lineup_woba=home_lineup_woba, is_home=False, weights=w)
        print(f"  wOBA: {g['home_team']} {home_woba_pred['predicted_runs']} "
              f"vs {g['away_team']} {away_woba_pred['predicted_runs']}")

        # ── Upsert ────────────────────────────────────────────────────────────
        cur.execute("""
            INSERT INTO game_predictions (
                game_pk, game_date, season,
                home_team, away_team, home_team_id, away_team_id,
                home_lineup, away_lineup,
                home_sp_name, away_sp_name,
                home_sp_era, away_sp_era,
                home_win_pct, away_win_pct,
                mc_home_runs, mc_away_runs,
                mc_home_p10, mc_home_p90,
                mc_away_p10, mc_away_p90,
                mc_home_win_prob,
                mc_home_inn_means, mc_away_inn_means,
                woba_home_runs, woba_away_runs,
                woba_home_win_prob,
                home_lineup_woba, away_lineup_woba,
                home_db_coverage, away_db_coverage,
                predicted_at
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()
            )
            ON CONFLICT (game_pk) DO UPDATE SET
                home_lineup       = EXCLUDED.home_lineup,
                away_lineup       = EXCLUDED.away_lineup,
                mc_home_runs      = EXCLUDED.mc_home_runs,
                mc_away_runs      = EXCLUDED.mc_away_runs,
                mc_home_p10       = EXCLUDED.mc_home_p10,
                mc_home_p90       = EXCLUDED.mc_home_p90,
                mc_away_p10       = EXCLUDED.mc_away_p10,
                mc_away_p90       = EXCLUDED.mc_away_p90,
                mc_home_win_prob  = EXCLUDED.mc_home_win_prob,
                mc_home_inn_means = EXCLUDED.mc_home_inn_means,
                mc_away_inn_means = EXCLUDED.mc_away_inn_means,
                woba_home_runs    = EXCLUDED.woba_home_runs,
                woba_away_runs    = EXCLUDED.woba_away_runs,
                woba_home_win_prob= EXCLUDED.woba_home_win_prob,
                home_lineup_woba  = EXCLUDED.home_lineup_woba,
                away_lineup_woba  = EXCLUDED.away_lineup_woba,
                home_db_coverage  = EXCLUDED.home_db_coverage,
                away_db_coverage  = EXCLUDED.away_db_coverage,
                predicted_at      = NOW()
        """, (
            g["game_pk"], GAME_DATE, SEASON,
            g["home_team"], g["away_team"], g["home_team_id"], g["away_team_id"],
            json.dumps(g["home_lineup"]), json.dumps(g["away_lineup"]),
            g["home_sp_name"], g["away_sp_name"],
            home_sp_era, away_sp_era,
            home_win_pct, away_win_pct,
            mc["home_mean"], mc["away_mean"],
            mc["home_p10"], mc["home_p90"],
            mc["away_p10"], mc["away_p90"],
            mc["home_win_prob"],
            json.dumps(mc["home_inn_means"]), json.dumps(mc["away_inn_means"]),
            home_woba_pred["predicted_runs"], away_woba_pred["predicted_runs"],
            home_woba_pred["win_prob"],
            home_lineup_woba, away_lineup_woba,
            home_db / 9.0, away_db / 9.0,
        ))

    conn.commit()
    cur.close()
    conn.close()
    print(f"\nDone — {len(games)} games written.")

if __name__ == "__main__":
    main()
