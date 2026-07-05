"""
pooled_acf.py
Population-level test for periodicity in hitter performance.

Motivation (see memory: project-dft-methodology-flaws): per-player DFT on a
15-game window can never detect a physiological cycle — per-game wOBA noise
(std ~0.17) swamps any plausible cycle amplitude (~0.015), and the DFT
"forecast" wraps around to the start of the window. The question that CAN be
answered with this data is population-level: does the average player's
performance autocorrelate at some calendar-day lag, after removing the
schedule effects we know about?

Method:
  1. Per player-season (>= MIN_GAMES games, COALESCE(PA, AB) >= 2 per game):
     residualize per-game wOBA by removing
       a. the player's PA-weighted season mean,
       b. a linear within-season trend (so slumps/ramp-ups don't read as
          long cycles),
       c. a global home/away offset (homestand/road-trip blocks create
          weekly schedule rhythm),
       d. a per-calendar-date league effect (weather, ball, league drift).
  2. Standardize residuals per player-season -> z-scores.
  3. Pooled ACF(d) = mean of z_i * z_j over ALL same-player game pairs whose
     game dates are exactly d calendar days apart, d = 1..MAX_LAG, pooled
     across every player-season.
  4. Permutation null: shuffle each player-season's residuals across that
     player's own game dates (dates fixed, values permuted — destroys any
     time structure, keeps the sampling pattern), re-apply detrend +
     standardization, recompute the pooled ACF. PERMS times.
     -> pointwise 95% band per lag, plus a family-wise band from the
        95th percentile of max|ACF| over lags 2..MAX_LAG (corrects for
        scanning 44 lags; lag 1 is reported separately as "persistence").

Reading the output:
  - Lag 1 above its band  -> day-to-day persistence (hot hand), not a cycle.
  - A bump at lag d clearing the FAMILY-WISE band -> genuine periodicity
    with period ~d days; worth building a signal around.
  - Everything inside the bands -> no detectable population-level cycle in
    this data; the hypothesis is answered.

Run:  python pooled_acf.py
Env:  DATABASE_URL (required)
      SEASON  (optional, restrict to one season)
      PERMS   (optional, default 200)
      MAX_LAG (optional, default 45)
"""
import os, math, random
from collections import defaultdict
import psycopg2
from dotenv import load_dotenv

load_dotenv()

SEASON    = os.getenv("SEASON")
PERMS     = int(os.getenv("PERMS", "200"))
MAX_LAG   = int(os.getenv("MAX_LAG", "45"))
MIN_GAMES = 30     # games per player-season to include
MIN_DATE_GAMES = 30  # player-games on a calendar date to estimate a date effect
SEED      = 20260705

random.seed(SEED)


# ── data loading ─────────────────────────────────────────────────────────────
def load_data(cur):
    where = "woba IS NOT NULL AND COALESCE(plate_appearances, at_bats) >= 2"
    params = []
    if SEASON:
        where += " AND season = %s"
        params.append(int(SEASON))
    cur.execute(f"""
        SELECT player_id, season, game_date, woba,
               COALESCE(plate_appearances, at_bats) AS pa, team
        FROM player_gamelogs
        WHERE {where} AND player_id IS NOT NULL
        ORDER BY player_id, season, game_date
    """, params)
    rows = cur.fetchall()

    cur.execute("SELECT game_date, home_team, away_team FROM game_results")
    home_map = {}   # (date, team_name) -> True (home) / False (away)
    for gdate, home, away in cur.fetchall():
        home_map[(gdate, home)] = True
        home_map[(gdate, away)] = False
    return rows, home_map


# ── residualization ──────────────────────────────────────────────────────────
def detrend(positions, values):
    """Remove OLS linear trend of values vs positions. Returns residuals."""
    n = len(values)
    mx = sum(positions) / n
    my = sum(values) / n
    sxx = sum((x - mx) ** 2 for x in positions)
    if sxx <= 0:
        return [v - my for v in values]
    sxy = sum((x - mx) * (y - my) for x, y in zip(positions, values))
    b = sxy / sxx
    return [y - (my + b * (x - mx)) for x, y in zip(positions, values)]


def standardize(values):
    """Z-score; returns None if degenerate."""
    n = len(values)
    m = sum(values) / n
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    if var < 1e-12:
        return None
    sd = math.sqrt(var)
    return [(v - m) / sd for v in values]


def build_series(rows, home_map):
    """
    Returns list of series dicts: {positions: [day offsets], resid: [values]}
    after player-mean, home/away and date-effect removal (detrend +
    standardize happen later, per pass, so permutations re-apply them).
    """
    by_series = defaultdict(list)   # (player_id, season) -> [(date, woba, pa, team)]
    for pid, season, gdate, woba, pa, team in rows:
        by_series[(pid, season)].append((gdate, float(woba), int(pa or 0), team))

    # a. player-season PA-weighted mean
    resid_rows = []   # (date, team, resid)  — flat, for offsets below
    series_games = {}
    for key, games in by_series.items():
        if len(games) < MIN_GAMES:
            continue
        wsum = sum(pa for _, _, pa, _ in games)
        if wsum <= 0:
            continue
        pmean = sum(w * pa for _, w, pa, _ in games) / wsum
        gl = [(gdate, w - pmean, team) for gdate, w, pa, team in games]
        series_games[key] = gl
        resid_rows.extend(gl)

    # b. global home/away offset (only for games matched in game_results)
    home_sum = home_n = away_sum = away_n = 0.0
    matched = 0
    for gdate, r, team in resid_rows:
        is_home = home_map.get((gdate, team))
        if is_home is None:
            continue
        matched += 1
        if is_home:
            home_sum += r; home_n += 1
        else:
            away_sum += r; away_n += 1
    home_off = home_sum / home_n if home_n else 0.0
    away_off = away_sum / away_n if away_n else 0.0
    match_rate = matched / len(resid_rows) if resid_rows else 0.0

    def ha_adjust(gdate, team, r):
        is_home = home_map.get((gdate, team))
        if is_home is None:
            return r
        return r - (home_off if is_home else away_off)

    # c. per-date league effect (computed after home/away removal)
    date_sum = defaultdict(float)
    date_n = defaultdict(int)
    for gdate, r, team in resid_rows:
        r2 = ha_adjust(gdate, team, r)
        date_sum[gdate] += r2
        date_n[gdate] += 1
    date_eff = {d: date_sum[d] / date_n[d]
                for d in date_sum if date_n[d] >= MIN_DATE_GAMES}

    series = []
    for key, gl in series_games.items():
        d0 = gl[0][0]
        positions, resid = [], []
        for gdate, r, team in gl:
            r2 = ha_adjust(gdate, team, r) - date_eff.get(gdate, 0.0)
            positions.append((gdate - d0).days)
            resid.append(r2)
        series.append({"positions": positions, "resid": resid})

    return series, {
        "n_series": len(series),
        "n_games": sum(len(s["resid"]) for s in series),
        "match_rate": match_rate,
        "home_off": home_off,
        "away_off": away_off,
        "n_date_effects": len(date_eff),
    }


# ── pooled ACF machinery ─────────────────────────────────────────────────────
def build_pairs(series):
    """
    Flatten all series into one z-slot array; enumerate same-player game
    pairs with calendar lag 1..MAX_LAG once. Returns (segments, pairs) where
    segments[s] = (start, positions, resid) and pairs = [(i, j, lag), ...]
    with i, j flat indices.
    """
    segments = []
    pairs = []
    start = 0
    for s in series:
        pos = s["positions"]
        segments.append((start, pos, s["resid"]))
        idx_by_pos = {p: start + i for i, p in enumerate(pos)}
        for i, p in enumerate(pos):
            for lag in range(1, MAX_LAG + 1):
                j = idx_by_pos.get(p + lag)
                if j is not None:
                    pairs.append((start + i, j, lag))
        start += len(pos)
    return segments, pairs, start


def fill_z(segments, z, permute):
    """Detrend + standardize each segment's residuals into flat z array."""
    for start, pos, resid in segments:
        vals = list(resid)
        if permute:
            random.shuffle(vals)
        vals = detrend(pos, vals)
        zs = standardize(vals)
        if zs is None:
            zs = [0.0] * len(vals)
        z[start:start + len(zs)] = zs


def pooled_acf(pairs, z):
    num = [0.0] * (MAX_LAG + 1)
    cnt = [0] * (MAX_LAG + 1)
    for i, j, lag in pairs:
        num[lag] += z[i] * z[j]
        cnt[lag] += 1
    return [num[d] / cnt[d] if cnt[d] else 0.0 for d in range(MAX_LAG + 1)], cnt


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    rows, home_map = load_data(cur)
    cur.close()
    conn.close()

    series, info = build_series(rows, home_map)
    print(f"\nPlayer-seasons: {info['n_series']}   games: {info['n_games']}"
          + (f"   (season {SEASON})" if SEASON else "   (all seasons)"))
    print(f"Home/away matched: {100*info['match_rate']:.1f}% of games  "
          f"(home offset {info['home_off']:+.4f}, away {info['away_off']:+.4f} wOBA)")
    print(f"Date effects estimated for {info['n_date_effects']} calendar dates")
    if info["n_series"] < 50:
        print("WARNING: fewer than 50 player-seasons — pooled bands will be wide.")

    segments, pairs, n_slots = build_pairs(series)
    print(f"Game pairs within {MAX_LAG}-day lag: {len(pairs)}")

    z = [0.0] * n_slots
    fill_z(segments, z, permute=False)
    acf, cnt = pooled_acf(pairs, z)

    # permutation null
    null_acf = [[] for _ in range(MAX_LAG + 1)]   # per-lag distributions
    null_max = []                                 # max |acf| over lags 2..MAX_LAG
    for p in range(PERMS):
        fill_z(segments, z, permute=True)
        pa, _ = pooled_acf(pairs, z)
        for d in range(1, MAX_LAG + 1):
            null_acf[d].append(pa[d])
        null_max.append(max(abs(pa[d]) for d in range(2, MAX_LAG + 1)))
        if (p + 1) % 25 == 0:
            print(f"  permutation {p + 1}/{PERMS}")

    def pct(sorted_vals, q):
        k = min(len(sorted_vals) - 1, max(0, int(round(q * (len(sorted_vals) - 1)))))
        return sorted_vals[k]

    null_max.sort()
    fw_band = pct(null_max, 0.95)   # family-wise |ACF| threshold, lags 2+

    print(f"\nFamily-wise 95% band (max |ACF| over lags 2-{MAX_LAG}, "
          f"{PERMS} permutations): ±{fw_band:.4f}")
    print(f"\n{'lag':>4} {'pairs':>8} {'ACF':>8}  {'null 2.5%':>9} {'null 97.5%':>10}  flags")
    print("-" * 55)
    pointwise_hits, familywise_hits = [], []
    for d in range(1, MAX_LAG + 1):
        dist = sorted(null_acf[d])
        lo, hi = pct(dist, 0.025), pct(dist, 0.975)
        flags = ""
        if acf[d] < lo or acf[d] > hi:
            flags += "*"
            pointwise_hits.append(d)
        if d >= 2 and abs(acf[d]) > fw_band:
            flags += "*"
            familywise_hits.append(d)
        mark = " <- weekly" if d % 7 == 0 else ""
        print(f"{d:>4} {cnt[d]:>8} {acf[d]:>+8.4f}  {lo:>+9.4f} {hi:>+10.4f}  {flags}{mark}")

    print("\nSummary")
    print("-" * 55)
    print(f"  Lag-1 (persistence / hot hand): ACF={acf[1]:+.4f} "
          f"{'OUTSIDE' if 1 in pointwise_hits else 'inside'} its pointwise band")
    if familywise_hits:
        print(f"  Lags clearing the FAMILY-WISE band: {familywise_hits}")
        print("  -> candidate periodicity; check whether these lags are schedule")
        print("     artifacts (series length, off-day pattern) before believing them.")
    else:
        print(f"  No lag in 2-{MAX_LAG} clears the family-wise band.")
        print("  -> no detectable population-level periodicity in hitter wOBA")
        print("     residuals at daily resolution with this dataset.")
    n_pw = len([d for d in pointwise_hits if d >= 2])
    print(f"  Pointwise-only excursions (lags 2+): {n_pw} of {MAX_LAG - 1} "
          f"(expect ~{0.05 * (MAX_LAG - 1):.1f} by chance)")


if __name__ == "__main__":
    main()
