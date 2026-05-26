"""
predict_on_demand.py
On-demand (re-)prediction for a single game. Upserts kalshi_tracker.
Triggered by webhook (GitHub Actions workflow_dispatch) or run directly.

Required env var:
  GAME_PK         MLB game PK

Optional env vars (SP overrides — set when a starter scratches):
  HOME_SP         New home starter full name
  AWAY_SP         New away starter full name

If SP override is omitted, fetches probablePitcher from MLB API.
If SP name not in pitcher_profiles, falls back to league-average ERA/K9 with a warning.
"""
import os
import requests
import psycopg2
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv

from model_config import LEAGUE_AVG_ERA, LEAGUE_AVG_K9, SEASON
from model_weights import load_weights, FALLBACK_BACKTEST
from prediction_engine import (
    compute_player_score,
    compute_cycle_edge,
    cycle_edge_prob,
    model_home_prob,
)

load_dotenv()

GAME_PK = int(os.environ["GAME_PK"])
HOME_SP = os.getenv("HOME_SP", "").strip() or None
AWAY_SP = os.getenv("AWAY_SP", "").strip() or None


# ── MLB API ────────────────────────────────────────────────────────────────────

def mlb_get(path):
    r = requests.get("https://statsapi.mlb.com/api/v1" + path, timeout=15)
    r.raise_for_status()
    return r.json()


# ── Data helpers ───────────────────────────────────────────────────────────────

def _lookup_sp(cur, name):
    """Returns (era, k9, known). known=False when name not in pitcher_profiles."""
    if not name:
        return LEAGUE_AVG_ERA, LEAGUE_AVG_K9, False
    cur.execute("""
        SELECT season_era, k_per_9 FROM pitcher_profiles
        WHERE player_name = %s ORDER BY fetched_date DESC LIMIT 1
    """, (name,))
    row = cur.fetchone()
    if row and row[0] is not None and row[1] is not None:
        return float(row[0]), float(row[1]), True
    print(f"  WARN: '{name}' not in pitcher_profiles — using league average ERA/K9")
    return LEAGUE_AVG_ERA, LEAGUE_AVG_K9, False


def _fetch_sp_from_api(game_pk):
    """Fetch probable pitchers from MLB API. Returns (home_name, away_name)."""
    try:
        data = mlb_get(f"/schedule?sportId=1&gamePk={game_pk}&hydrate=probablePitcher,teams")
        for d in data.get("dates", []):
            for g in d.get("games", []):
                if g.get("gamePk") == game_pk:
                    home = g["teams"]["home"].get("probablePitcher", {}).get("fullName")
                    away = g["teams"]["away"].get("probablePitcher", {}).get("fullName")
                    return home, away
    except Exception as e:
        print(f"  WARN: MLB API SP fetch failed: {e}")
    return None, None


def _fetch_lineup_wobas(game_pk, player_woba):
    """
    Try to pull confirmed batting orders from boxscore.
    Returns (home_wobas, away_wobas, lineup_source).
    """
    try:
        bs = mlb_get(f"/game/{game_pk}/boxscore")
        home_order   = bs.get("teams", {}).get("home", {}).get("battingOrder", [])
        away_order   = bs.get("teams", {}).get("away", {}).get("battingOrder", [])
        home_players = bs.get("teams", {}).get("home", {}).get("players", {})
        away_players = bs.get("teams", {}).get("away", {}).get("players", {})
        if len(home_order) >= 9 and len(away_order) >= 9:
            def wobas_for(order, pdata):
                return [player_woba.get(
                    pdata.get(f"ID{pid}", {}).get("person", {}).get("fullName", ""), 0.320
                ) for pid in order[:9]]
            return wobas_for(home_order, home_players), wobas_for(away_order, away_players), "actual"
    except Exception as e:
        print(f"  Boxscore unavailable ({e}) — using estimated lineup")
    return None, None, "estimated"


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur  = conn.cursor()

    # Ensure predicted_at column exists (idempotent — safe to run every time)
    cur.execute("""
        ALTER TABLE kalshi_tracker
        ADD COLUMN IF NOT EXISTS predicted_at TIMESTAMPTZ
    """)
    conn.commit()

    # Load tunable weights
    w = load_weights(cur, "backtest", FALLBACK_BACKTEST)
    print(f"Loaded weights: woba_run_scale={w['woba_run_scale']} opp_era_scale={w['opp_era_scale']}")

    # ── 1. Game info ─────────────────────────────────────────────────────────────
    cur.execute("""
        SELECT home_team, away_team, game_date
        FROM kalshi_tracker WHERE game_pk = %s LIMIT 1
    """, (GAME_PK,))
    row = cur.fetchone()

    if row:
        home_team, away_team, game_date = row
        print(f"Found existing row: {away_team} @ {home_team} on {game_date}")
    else:
        print(f"game_pk {GAME_PK} not in kalshi_tracker — fetching from MLB API")
        try:
            data = mlb_get(f"/schedule?sportId=1&gamePk={GAME_PK}&hydrate=teams")
            g    = data["dates"][0]["games"][0]
            home_team  = g["teams"]["home"]["team"]["name"]
            away_team  = g["teams"]["away"]["team"]["name"]
            game_date  = g["gameDate"][:10]
        except Exception as e:
            print(f"ERR: Cannot resolve game_pk {GAME_PK}: {e}")
            cur.close(); conn.close()
            raise SystemExit(1)

    # ── 2. SP resolution ─────────────────────────────────────────────────────────
    home_sp_name = HOME_SP
    away_sp_name = AWAY_SP
    if not home_sp_name or not away_sp_name:
        api_home, api_away = _fetch_sp_from_api(GAME_PK)
        if not home_sp_name:
            home_sp_name = api_home
        if not away_sp_name:
            away_sp_name = api_away

    home_sp_era, home_sp_k9, home_sp_known = _lookup_sp(cur, home_sp_name)
    away_sp_era, away_sp_k9, away_sp_known = _lookup_sp(cur, away_sp_name)
    print(f"  Home SP: {home_sp_name or 'TBD'}  ERA={home_sp_era}  known={home_sp_known}")
    print(f"  Away SP: {away_sp_name or 'TBD'}  ERA={away_sp_era}  known={away_sp_known}")

    # ── 3. Team win percentages ───────────────────────────────────────────────────
    cur.execute("""
        SELECT team_name, win_pct, l10_win_pct
        FROM team_stats_cache
        WHERE season = %s AND team_name = ANY(%s)
    """, (SEASON, [home_team, away_team]))
    team_stats = {r[0]: {"win_pct": float(r[1] or 0.500), "l10_win_pct": float(r[2] or 0.500)}
                  for r in cur.fetchall()}
    ht  = team_stats.get(home_team, {"win_pct": 0.500, "l10_win_pct": 0.500})
    at  = team_stats.get(away_team, {"win_pct": 0.500, "l10_win_pct": 0.500})
    hwp = ht["l10_win_pct"] * 0.5 + ht["win_pct"] * 0.5
    awp = at["l10_win_pct"] * 0.5 + at["win_pct"] * 0.5

    # ── 4. Player wOBA (recent-weighted season aggregate) ────────────────────────
    cur.execute("""
        SELECT player_name,
               COALESCE(
                   AVG(woba) FILTER (WHERE woba > 0 AND game_date >= current_date - 21) * 0.6
                   + AVG(woba) FILTER (WHERE woba > 0) * 0.4,
                   AVG(woba) FILTER (WHERE woba > 0)
               ) AS avg_woba
        FROM player_gamelogs
        WHERE season = %s AND woba IS NOT NULL
        GROUP BY player_name HAVING COUNT(*) >= 5
    """, (SEASON,))
    player_woba = {r[0]: float(r[1]) for r in cur.fetchall() if r[1]}

    # ── 5. Lineup wOBAs ──────────────────────────────────────────────────────────
    home_wobas, away_wobas, lineup_source = _fetch_lineup_wobas(GAME_PK, player_woba)

    if not home_wobas or not away_wobas:
        # Estimated: top-9 by wOBA per team
        cur.execute("""
            SELECT player_name, team FROM player_gamelogs
            WHERE season = %s GROUP BY player_name, team
        """, (SEASON,))
        team_roster: dict = {}
        for name, team in cur.fetchall():
            if name in player_woba:
                team_roster.setdefault(team, []).append((name, player_woba[name]))

        def top9(team_name):
            ranked = sorted(team_roster.get(team_name, []), key=lambda x: -x[1])[:9]
            return [woba for _, woba in ranked] or [0.320]

        home_wobas  = top9(home_team)
        away_wobas  = top9(away_team)
        lineup_source = "estimated"

    # ── 6. wOBA model ────────────────────────────────────────────────────────────
    home_avg_woba = round(sum(home_wobas) / len(home_wobas), 4)
    away_avg_woba = round(sum(away_wobas) / len(away_wobas), 4)
    mhp  = model_home_prob(home_avg_woba, away_avg_woba, hwp, awp,
                           home_sp_era, away_sp_era, weights=w)
    conf = round(abs(mhp - 0.5) * 2, 3)
    pick = "home" if mhp >= 0.5 else "away"
    sig  = "strong_edge" if conf >= 0.25 else "disagreement"
    print(f"  wOBA model: home_prob={mhp}  pick={pick}  conf={conf}  lineup={lineup_source}")

    # ── 7. Cycle edge ─────────────────────────────────────────────────────────────
    # Load gamelogs only for the two teams' top-9 players (not entire season)
    all_cycle_names = list(set(
        [n for n, _ in sorted(
            [(nm, player_woba.get(nm, 0)) for nm in player_woba],
            key=lambda x: -x[1]
        ) if True]  # resolved per-team below
    ))

    cur.execute("""
        SELECT player_name, team FROM player_gamelogs
        WHERE season = %s GROUP BY player_name, team
    """, (SEASON,))
    team_roster_all: dict = {}
    for name, team in cur.fetchall():
        if name in player_woba:
            team_roster_all.setdefault(team, []).append((name, player_woba[name]))

    def top9_names(team_name):
        ranked = sorted(team_roster_all.get(team_name, []), key=lambda x: -x[1])
        return [n for n, _ in ranked[:9]]

    cycle_names = list(set(top9_names(home_team) + top9_names(away_team)))

    cur.execute("""
        SELECT player_name, game_date, batting_avg, woba
        FROM player_gamelogs
        WHERE player_name = ANY(%s) AND season = %s AND at_bats >= 1
        ORDER BY player_name, game_date ASC
    """, (cycle_names, SEASON))
    player_logs: dict = {}
    for name, gdate, avg, woba in cur.fetchall():
        player_logs.setdefault(name, []).append({
            'avg':  float(avg  or 0),
            'woba': float(woba or 0) if woba else None,
        })

    cur.execute("""
        SELECT DISTINCT ON (player_name) player_name, ops
        FROM player_gamelogs
        WHERE player_name = ANY(%s) AND season = %s AND ops IS NOT NULL
        ORDER BY player_name, game_date DESC
    """, (cycle_names, SEASON))
    player_ops = {r[0]: float(r[1]) for r in cur.fetchall() if r[1]}

    def team_cycle(team_name):
        names = top9_names(team_name)
        if len(names) < 3:
            return None
        scores = [
            compute_player_score(player_logs.get(n, [])[-40:], player_ops.get(n, 0.720))
            for n in names if n in player_logs
        ]
        return compute_cycle_edge(scores) if scores else None

    home_ce = team_cycle(home_team)
    away_ce = team_cycle(away_team)
    if home_ce and away_ce:
        ce_prob       = cycle_edge_prob(home_ce['score'], away_ce['score'])
        cpick         = "home" if ce_prob >= 0.5 else "away"
        cycle_home_sc = home_ce['score']
        cycle_away_sc = away_ce['score']
        cycle_home_p  = ce_prob
    else:
        cpick = cycle_home_sc = cycle_away_sc = cycle_home_p = None
    print(f"  Cycle edge: home={cycle_home_sc} away={cycle_away_sc} pick={cpick}")

    # ── 8. Upsert — only overwrites if this prediction is newer ──────────────────
    cur.execute("""
        INSERT INTO kalshi_tracker
            (game_date, season, home_team, away_team, game_pk,
             model_home_prob, model_away_prob, model_pick, model_confidence,
             cycle_pick, cycle_home_score, cycle_away_score,
             cycle_home_prob, cycle_away_prob,
             signal_type, lineup_source, predicted_at)
        VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s,%s,%s, %s,%s, NOW())
        ON CONFLICT (game_pk, game_date) DO UPDATE SET
            model_home_prob  = EXCLUDED.model_home_prob,
            model_away_prob  = EXCLUDED.model_away_prob,
            model_pick       = EXCLUDED.model_pick,
            model_confidence = EXCLUDED.model_confidence,
            cycle_pick        = COALESCE(EXCLUDED.cycle_pick,       kalshi_tracker.cycle_pick),
            cycle_home_score  = COALESCE(EXCLUDED.cycle_home_score, kalshi_tracker.cycle_home_score),
            cycle_away_score  = COALESCE(EXCLUDED.cycle_away_score, kalshi_tracker.cycle_away_score),
            cycle_home_prob   = COALESCE(EXCLUDED.cycle_home_prob,  kalshi_tracker.cycle_home_prob),
            cycle_away_prob   = COALESCE(EXCLUDED.cycle_away_prob,  kalshi_tracker.cycle_away_prob),
            signal_type       = EXCLUDED.signal_type,
            lineup_source     = EXCLUDED.lineup_source,
            predicted_at      = EXCLUDED.predicted_at
        WHERE kalshi_tracker.predicted_at IS NULL
           OR kalshi_tracker.predicted_at < EXCLUDED.predicted_at
    """, (
        game_date, SEASON, home_team, away_team, GAME_PK,
        mhp, round(1 - mhp, 3), pick, conf,
        cpick, cycle_home_sc, cycle_away_sc,
        cycle_home_p, round(1 - cycle_home_p, 3) if cycle_home_p is not None else None,
        sig, lineup_source,
    ))
    updated = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if updated:
        print(f"\nDone — kalshi_tracker updated")
    else:
        print(f"\nSkipped — existing prediction was newer (no-op)")
    print(f"  {away_team} @ {home_team}: model={mhp}  pick={pick}  cycle_pick={cpick}")
    if HOME_SP:
        print(f"  Home SP override: {HOME_SP}  known={home_sp_known}")
    if AWAY_SP:
        print(f"  Away SP override: {AWAY_SP}  known={away_sp_known}")


if __name__ == "__main__":
    main()
