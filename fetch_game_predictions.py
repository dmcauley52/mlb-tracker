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
import math
import random
import time
import urllib.request
from datetime import date, timedelta
from dotenv import load_dotenv
from model_config import (
    LEAGUE_AVG_DIST,
    LEAGUE_AVG_ERA,
    LEAGUE_AVG_K9,
    OPP_RUNS_BASE,
    SEASON,
    WIN_PCT_RUN_SCALE,
    WIN_PROB_SIGMOID_SCALE,
    WOBA_WEIGHTS,
)

load_dotenv()

GAME_DATE       = os.getenv("GAME_DATE", str(date.today()))
DATABASE_URL    = os.getenv("DATABASE_URL")
SIM_ITERATIONS  = 10_000

# ── Model constants — overwritten from model_weights table in main() ─────────
from model_weights import load_weights, FALLBACK_BACKTEST
WOBA_RUN_SCALE   = FALLBACK_BACKTEST["woba_run_scale"]
MAX_PRED_RUNS    = FALLBACK_BACKTEST["max_predicted_runs"]
SCORE_BOOST      = FALLBACK_BACKTEST["score_boost"]
OPP_ERA_SCALE    = FALLBACK_BACKTEST["opp_era_scale"]
SPOT_WEIGHTS     = FALLBACK_BACKTEST["spot_weights"]
SPOT_PA          = [4.5,  4.4,  4.3,  4.2,  4.1,  4.0,  3.95, 3.9,  3.8]

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

# ── PA distribution helpers ───────────────────────────────────────────────────
def compute_distribution(games):
    pa = hr = trip = dbl = hit = bb_hbp = k = 0
    for g in games:
        pa     += g["plate_appearances"] or g["at_bats"] or 0
        hr     += g["home_runs"] or 0
        trip   += g["triples"] or 0
        dbl    += g["doubles"] or 0
        hit    += g["hits"] or 0
        bb_hbp += (g["walks"] or 0) + (g["hit_by_pitch"] or 0)
        k      += g["strikeouts"] or 0
    if pa < 1:
        return None
    p1b = max(0, (hit - dbl - trip - hr) / pa)
    p_bb = bb_hbp / pa
    p_k  = k / pa
    p_hr = hr / pa
    p_tr = trip / pa
    p_db = dbl / pa
    return {"hr": p_hr, "trip": p_tr, "dbl": p_db, "s1b": p1b,
            "bb": p_bb, "k": p_k,
            "out": max(0, 1 - p_hr - p_tr - p_db - p1b - p_bb - p_k)}

def apply_sp_adjustment(dist, sp_era, sp_k9):
    k_mult   = min(2.0, max(0.5, sp_k9 / LEAGUE_AVG_K9))
    era_adj  = min(1.25, max(0.75, sp_era / LEAGUE_AVG_ERA))
    new_k    = min(0.40, dist["k"] * k_mult)
    k_delta  = new_k - dist["k"]
    hit_sum  = dist["hr"] + dist["trip"] + dist["dbl"] + dist["s1b"]
    hit_scale = max(0.5, 1 - k_delta / max(hit_sum, 0.01)) if hit_sum > 0 else 1
    raw = {
        "hr":   dist["hr"]   * hit_scale * era_adj,
        "trip": dist["trip"] * hit_scale * era_adj,
        "dbl":  dist["dbl"]  * hit_scale * era_adj,
        "s1b":  dist["s1b"]  * hit_scale * era_adj,
        "bb":   dist["bb"]   * era_adj,
        "k":    new_k,
    }
    pos_sum = sum(raw.values())
    norm = 0.90 / pos_sum if pos_sum > 0.90 else 1.0
    for k in list(raw):
        raw[k] *= norm
    raw["out"] = max(0.10, 1 - sum(raw.values()))
    return raw

def build_distributions(player_names, games_map, sp_era=None, sp_k9=None):
    dists = []
    for i in range(9):
        name = player_names[i] if i < len(player_names) else None
        dist = None
        if name and name in games_map and len(games_map[name]) >= 10:
            dist = compute_distribution(games_map[name])
        if dist is None:
            dist = dict(LEAGUE_AVG_DIST)
        if sp_era is not None and sp_k9 is not None:
            dist = apply_sp_adjustment(dist, sp_era, sp_k9)
        dists.append(dist)
    return dists

# ── Monte Carlo sim ───────────────────────────────────────────────────────────
def sample_pa(dist):
    r = random.random()
    c = dist["hr"];
    if r < c: return "hr"
    c += dist["trip"];
    if r < c: return "trip"
    c += dist["dbl"];
    if r < c: return "dbl"
    c += dist["s1b"];
    if r < c: return "s1b"
    c += dist["bb"];
    if r < c: return "bb"
    c += dist["k"];
    if r < c: return "k"
    return "out"

def simulate_half_inning(dists, start):
    outs = runs = 0
    b = [False, False, False]  # 1B, 2B, 3B
    bIdx = start
    while outs < 3:
        outcome = sample_pa(dists[bIdx % 9])
        bIdx += 1
        if outcome == "hr":
            runs += 1 + sum(b); b = [False, False, False]
        elif outcome == "trip":
            runs += sum(b); b = [False, False, True]
        elif outcome == "dbl":
            runs += (1 if b[1] else 0) + (1 if b[2] else 0)
            b = [False, False, b[0]]
        elif outcome == "s1b":
            runs += (1 if b[2] else 0) + (1 if b[1] and random.random() < 0.60 else 0)
            b = [True, b[0] and not b[1], False]
        elif outcome == "bb":
            if b[0] and b[1] and b[2]: runs += 1
            elif b[0] and b[1]: b = [True, True, True]
            elif b[0]: b = [True, True, b[2]]
            else: b[0] = True
        elif outcome == "k":
            outs += 1
        else:
            if b[0] and outs < 2 and random.random() < 0.15:
                outs += 2; b[0] = False
            else:
                outs += 1
    return runs, bIdx % 9

def simulate_game(home_dists, away_dists):
    h_runs = a_runs = 0
    h_bat = a_bat = 0
    h_inn = []
    a_inn = []
    for _ in range(9):
        ar, a_bat = simulate_half_inning(away_dists, a_bat)
        hr, h_bat = simulate_half_inning(home_dists, h_bat)
        a_inn.append(ar); h_inn.append(hr)
        h_runs += hr; a_runs += ar
    return h_runs, a_runs, h_inn, a_inn

def run_monte_carlo(home_dists, away_dists, n=SIM_ITERATIONS):
    home_totals = []
    away_totals = []
    home_inn_acc = [0.0] * 9
    away_inn_acc = [0.0] * 9
    home_wins = 0
    for _ in range(n):
        hr, ar, hi, ai = simulate_game(home_dists, away_dists)
        if hr > ar:
            home_wins += 1
        home_totals.append(hr); away_totals.append(ar)
        for i in range(9):
            home_inn_acc[i] += hi[i]; away_inn_acc[i] += ai[i]
    home_totals.sort(); away_totals.sort()
    pct = lambda arr, p: arr[int(len(arr) * p)]
    return {
        "home_median": pct(home_totals, 0.5),
        "home_p10":    pct(home_totals, 0.1),
        "home_p90":    pct(home_totals, 0.9),
        "away_median": pct(away_totals, 0.5),
        "away_p10":    pct(away_totals, 0.1),
        "away_p90":    pct(away_totals, 0.9),
        "home_win_prob": round(home_wins / n, 3),
        "home_inn_means": [round(v / n, 3) for v in home_inn_acc],
        "away_inn_means": [round(v / n, 3) for v in away_inn_acc],
    }

# ── wOBA model (mirrors analytics.js _predictGame) ───────────────────────────
def compute_woba(games):
    pa = bb = hbp = h = dbl = trp = hr = ab = sf = 0
    for g in games:
        ab  += g["at_bats"] or 0
        h   += g["hits"] or 0
        dbl += g["doubles"] or 0
        trp += g["triples"] or 0
        hr  += g["home_runs"] or 0
        bb  += g["walks"] or 0
        hbp += g["hit_by_pitch"] or 0
        sf  += g["sac_flies"] or 0
    s1b = h - dbl - trp - hr
    num = (WOBA_WEIGHTS["bb"] * bb + WOBA_WEIGHTS["hbp"] * hbp +
           WOBA_WEIGHTS["single"] * s1b + WOBA_WEIGHTS["double"] * dbl +
           WOBA_WEIGHTS["triple"] * trp + WOBA_WEIGHTS["hr"] * hr)
    den = ab + bb + hbp + sf
    return round(num / den, 4) if den > 0 else 0.310

def predict_runs_woba(lineup_wobas, opp_era, my_win_pct, opp_win_pct,
                      opp_lineup_woba=None, is_home=False, park_factor=1.0):
    n = len(lineup_wobas)
    raw_w = SPOT_WEIGHTS[:n]
    w_sum = sum(raw_w)
    avg_woba = (sum(w * wo for w, wo in zip(raw_w, lineup_wobas)) / w_sum
                if w_sum > 0 else 0.310)

    era_factor  = max(0.80, min(1.30, opp_era / LEAGUE_AVG_ERA)) if opp_era else 1.0
    adj_era     = era_factor * OPP_ERA_SCALE + 1.0 * (1 - OPP_ERA_SCALE)
    team_quality = max(0.88, min(1.12, 1.0 + (my_win_pct - 0.500) * 0.5))

    predicted = min(
        avg_woba * WOBA_RUN_SCALE * adj_era * team_quality * park_factor,
        MAX_PRED_RUNS
    )

    opp_runs_wpc = OPP_RUNS_BASE + (opp_win_pct - 0.500) * WIN_PCT_RUN_SCALE
    opp_runs_est = ((opp_lineup_woba * WOBA_RUN_SCALE * 0.6 + opp_runs_wpc * 0.4) * park_factor
                    if opp_lineup_woba else opp_runs_wpc * park_factor)

    run_diff = predicted - opp_runs_est
    win_prob = 1 / (1 + math.exp(-run_diff * WIN_PROB_SIGMOID_SCALE))

    return {
        "predicted_runs": round(predicted, 2),
        "opp_runs_est":   round(opp_runs_est, 2),
        "avg_woba":       round(avg_woba, 4),
        "win_prob":       round(win_prob, 3),
    }

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

    global WOBA_RUN_SCALE, MAX_PRED_RUNS, SCORE_BOOST, OPP_ERA_SCALE, SPOT_WEIGHTS
    w = load_weights(cur, "backtest", FALLBACK_BACKTEST)
    WOBA_RUN_SCALE = w["woba_run_scale"]
    MAX_PRED_RUNS  = w["max_predicted_runs"]
    SCORE_BOOST    = w["score_boost"]
    OPP_ERA_SCALE  = w["opp_era_scale"]
    SPOT_WEIGHTS   = w["spot_weights"]
    print(f"Loaded weights: woba_run_scale={WOBA_RUN_SCALE} max_pred_runs={MAX_PRED_RUNS} opp_era_scale={OPP_ERA_SCALE}")

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
        print(f"  MC: {g['home_team']} {mc['home_median']} ({mc['home_p10']}–{mc['home_p90']}) "
              f"vs {g['away_team']} {mc['away_median']} ({mc['away_p10']}–{mc['away_p90']}) "
              f"home win {mc['home_win_prob']:.1%}")

        # ── wOBA model ────────────────────────────────────────────────────────
        home_wobas = [compute_woba(games_map.get(n, [])) for n in g["home_lineup"]]
        away_wobas = [compute_woba(games_map.get(n, [])) for n in g["away_lineup"]]
        home_lineup_woba = round(sum(home_wobas) / len(home_wobas), 4) if home_wobas else 0.310
        away_lineup_woba = round(sum(away_wobas) / len(away_wobas), 4) if away_wobas else 0.310

        home_woba_pred = predict_runs_woba(
            home_wobas, away_sp_era, home_win_pct, away_win_pct,
            opp_lineup_woba=away_lineup_woba, is_home=True)
        away_woba_pred = predict_runs_woba(
            away_wobas, home_sp_era, away_win_pct, home_win_pct,
            opp_lineup_woba=home_lineup_woba, is_home=False)
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
            mc["home_median"], mc["away_median"],
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
