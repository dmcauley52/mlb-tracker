"""
validate_rolling_cycle.py
Tests a rolling-cycle phase prediction against the market baseline, using
the same forecast-encompassing methodology as validate_model.py (see HANDOFF.md).

Phase assignment mirrors analytics.js:866-869 (Surge / Cruise / Slide / Rebuild)
but uses a ROLLING window (WINDOW trailing games) so the cycle fit is always
fresh, not stale half-season data.

Per player:
  1. DFT on trailing WINDOW-game wOBA (variable window if <WINDOW games exist,
     provided >= MIN_GAMES).
  2. Keep all components with amplitude >= MIN_AMPLITUDE (variable count per player,
     same filter as analytics.js -- no hard cap so players with 4 strong cycles
     use all 4; players with 1 use 1).
  3. Reconstruct the blended signal. Evaluate at forecast position N+1 (not N,
     which is a degenerate DFT point where every integer-k component lands
     exactly back in phase).
  4. Phase = level (above/below own mean) x slope (rising/falling) quadrant:
       above mean + rising  -> Surge   +2
       above mean + falling -> Cruise  +1
       below mean + falling -> Slide   -1
       below mean + rising  -> Rebuild -2
     No detectable cycle (all amplitudes < MIN_AMPLITUDE) -> neutral  0
  5. Sum phase weights for the ACTUAL starting lineup (9 players from
     game_results.home_lineup, not season-average roster estimates).

Team score = sum of 9 available starters' weights.
Pick = team with higher total.

No pitchers -- dropped after validate_rolling_cycle.py pass 1.

Run:  python validate_rolling_cycle.py
Env:  DATABASE_URL (required), SEASON (optional, default = all seasons)
"""
import os, math
from collections import defaultdict
from bisect import bisect_left
import psycopg2
from dotenv import load_dotenv

from validate_model import logistic_fit, logit, norm_sf

load_dotenv()

SEASON       = os.getenv("SEASON")
WINDOW       = 15      # max trailing games per player, rolling
MIN_GAMES    = 10      # minimum to attempt a cycle fit
MIN_PERIOD   = 4       # games -- mirrors analytics.js / prediction_engine.py
MIN_AMPLITUDE= 0.005   # wOBA units -- mirrors analytics.js
MIN_BATTERS  = 5       # of 9 starters required; skip game if fewer have data

PHASE_WEIGHTS = {
    "Surge":   +2,
    "Cruise":  +1,
    "Slide":   -1,
    "Rebuild": -2,
}


# ── DFT + phase assignment ──────────────────────────────────────────────────
def _dft(signal):
    N = len(signal)
    re = [0.0] * N
    im = [0.0] * N
    for k in range(N):
        for n in range(N):
            angle = 2 * math.pi * k * n / N
            re[k] += signal[n] * math.cos(angle)
            im[k] -= signal[n] * math.sin(angle)
        re[k] /= N
        im[k] /= N
    return re, im


def _reconstruct_at(kept, N, x):
    """Reconstruct the cycle signal (mean-subtracted) at array position x."""
    v = 0.0
    for k, rek, imk in kept:
        angle = 2 * math.pi * k * x / N
        v += rek * math.cos(angle) - imk * math.sin(angle)
    return v


def player_phase_weight(values):
    """
    values: trailing per-game wOBA, oldest -> newest (len >= MIN_GAMES).
    Returns the player's phase weight (+2 / +1 / -1 / -2) or 0 if no
    detectable cycle meets the amplitude threshold.
    """
    N = len(values)
    mean = sum(values) / N
    signal = [v - mean for v in values]
    re, im = _dft(signal)

    kept = []
    for k in range(1, N // 2 + 1):
        amp = 2 * math.sqrt(re[k] ** 2 + im[k] ** 2)
        if N / k < MIN_PERIOD:     # period too short
            continue
        if amp < MIN_AMPLITUDE:    # not significant
            continue
        kept.append((k, re[k], im[k]))

    if not kept:
        return 0  # no detectable periodicity -> neutral

    # Evaluate reconstructed signal at forecast position N+1.
    # N+1 avoids the degenerate point N where cos(2πk·N/N)=cos(2πk)=1
    # for all integer k, collapsing all phase information to zero.
    level = _reconstruct_at(kept, N, N + 1)          # above/below own mean
    slope = _reconstruct_at(kept, N, N + 2) - _reconstruct_at(kept, N, N)

    if   level >  0 and slope >  0: return PHASE_WEIGHTS["Surge"]
    elif level >  0 and slope <= 0: return PHASE_WEIGHTS["Cruise"]
    elif level <= 0 and slope <= 0: return PHASE_WEIGHTS["Slide"]
    else:                           return PHASE_WEIGHTS["Rebuild"]


# ── data loading ─────────────────────────────────────────────────────────────
def load_data(cur):
    where = "actual_winner IS NOT NULL AND game_pk IS NOT NULL"
    params = []
    if SEASON:
        where += " AND season = %s"
        params.append(int(SEASON))

    cur.execute(f"""
        SELECT game_pk, game_date, season, home_team, away_team,
               actual_winner, kalshi_home_prob
        FROM kalshi_tracker
        WHERE {where}
        ORDER BY game_date
    """, params)
    games = cur.fetchall()

    cur.execute("""
        SELECT game_pk, home_lineup, away_lineup
        FROM game_results
        WHERE game_pk = ANY(%s)
    """, ([g[0] for g in games],))
    lineups = {}
    for game_pk, home_lu, away_lu in cur.fetchall():
        home_lu = home_lu if isinstance(home_lu, list) else []
        away_lu = away_lu if isinstance(away_lu, list) else []
        lineups[game_pk] = {
            "home_ids": [p["player_id"] for p in home_lu],
            "away_ids": [p["player_id"] for p in away_lu],
        }

    seasons = sorted({g[2] for g in games})
    cur.execute("""
        SELECT player_id, season, game_date, woba
        FROM player_gamelogs
        WHERE season = ANY(%s) AND at_bats >= 1 AND woba IS NOT NULL
              AND player_id IS NOT NULL
        ORDER BY player_id, season, game_date
    """, (seasons,))
    logs = defaultdict(list)   # (player_id, season) -> [(date, woba), ...]
    for player_id, season, gdate, woba in cur.fetchall():
        logs[(player_id, season)].append((gdate, float(woba)))

    return games, lineups, logs


def trailing_values(logs, player_id, season, game_date):
    """Return the last WINDOW wOBA values strictly before game_date, same season."""
    rows = logs.get((player_id, season))
    if not rows:
        return None
    dates = [d for d, _ in rows]
    idx = bisect_left(dates, game_date)
    trailing = rows[max(0, idx - WINDOW):idx]
    if len(trailing) < MIN_GAMES:
        return None
    return [v for _, v in trailing]


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    games, lineups, logs = load_data(cur)
    cur.close()
    conn.close()

    print(f"\nResolved games with market price: {len(games)}"
          + (f"  (season {SEASON})" if SEASON else "  (all seasons)"))
    print(f"Rolling window: {WINDOW} games  |  min games: {MIN_GAMES}  "
          f"|  min amplitude: {MIN_AMPLITUDE}  |  min batters: {MIN_BATTERS}/9")

    correct = 0
    n_picks = 0
    skipped = 0
    reg_rows = []   # (kalshi_home_prob, diff, actual_home_win)

    phase_counts = defaultdict(int)   # how often each phase weight appears

    for game_pk, game_date, season, home_team, away_team, actual_winner, khp in games:
        lu = lineups.get(game_pk)
        if not lu:
            skipped += 1
            continue

        def lineup_score(ids):
            weights = []
            for pid in ids:
                vals = trailing_values(logs, pid, season, game_date)
                if vals is not None:
                    w = player_phase_weight(vals)
                    weights.append(w)
                    phase_counts[w] += 1
            return weights

        home_w = lineup_score(lu["home_ids"])
        away_w = lineup_score(lu["away_ids"])

        if len(home_w) < MIN_BATTERS or len(away_w) < MIN_BATTERS:
            skipped += 1
            continue

        home_total = sum(home_w)
        away_total = sum(away_w)
        diff = home_total - away_total

        if diff != 0:
            n_picks += 1
            if (diff > 0) == (actual_winner == "home"):
                correct += 1

        if khp is not None:
            reg_rows.append((float(khp), diff, 1 if actual_winner == "home" else 0))

    print(f"Games evaluated: {n_picks + (len(games) - skipped - n_picks)}  "
          f"skipped: {skipped}  ties (diff=0): {len(games) - skipped - n_picks}")

    total_assignments = sum(phase_counts.values())
    print(f"\nPhase distribution across all player-game slots ({total_assignments} total):")
    for weight, label in [(+2,"Surge"), (+1,"Cruise"), (-1,"Slide"), (-2,"Rebuild"), (0,"Neutral (no cycle)")]:
        n = phase_counts[weight]
        print(f"  {label:<22} weight={weight:+d}  n={n}  ({100*n/total_assignments:.1f}%)")

    acc = correct / n_picks if n_picks else None
    print(f"\nPhase-cycle picks: n={n_picks}  accuracy={acc:.3f}" if acc else "\nNo picks made.")

    print("\nForecast-encompassing test")
    print("-" * 60)
    if len(reg_rows) < 30:
        print("  fewer than 30 usable rows -- skipping regression")
        return

    print(f"  outcome ~ logit(kalshi) + phase_cycle_diff   (n={len(reg_rows)})")
    X = [[logit(khp), diff] for khp, diff, _ in reg_rows]
    y = [yy for _, _, yy in reg_rows]
    beta, se = logistic_fit(X, y)
    names = ["intercept", "logit(kalshi)", "phase_cycle_diff"]
    for nm, b, s in zip(names, beta, se):
        z = b / s if s > 0 else 0.0
        star = " *" if norm_sf(z) < 0.05 else ""
        print(f"    {nm:<22} coef={b:+.4f}  se={s:.4f}  z={z:+.2f}  p={norm_sf(z):.3f}{star}")

    add_z = beta[2] / se[2] if se[2] > 0 else 0.0
    verdict = ("ADDS info" if norm_sf(add_z) < 0.05 and abs(beta[2]) > 0.005
               else "NO added info")
    print(f"\n  -> phase_cycle_diff {verdict} beyond kalshi "
          f"(coef {beta[2]:+.4f}, p={norm_sf(add_z):.3f})")
    print("\n  Reading: a positive significant coef means hot lineups (more Surge/Cruise)")
    print("  predict home wins even after the market is accounted for -- real edge.")
    print("  Near-zero or non-significant means phases don't beat the market.")


if __name__ == "__main__":
    main()
