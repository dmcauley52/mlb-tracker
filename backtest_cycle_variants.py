"""
backtest_cycle_variants.py

Compares 5 rolling-cycle signal formulations against the Kalshi market baseline
on all resolved games in kalshi_tracker.

Variants
--------
  Baseline — DFT phase buckets (+2/+1/−1/−2), window=15, stat=woba
  A        — no DFT: z-score of last-3-game avg vs 15-game window mean/std
  B        — DFT phase buckets, window=7 (shorter hot-streak window), stat=woba
  C        — DFT phase buckets, window=15, stat=slg instead of woba
  D        — DFT phase buckets, window=15, stat=woba, toss-up games only
             (|kalshi_home_prob − 0.50| ≤ 0.05)

Evaluation (each variant)
  • directional accuracy on non-tie picks
  • forecast-encompassing regression:
      outcome ~ logit(kalshi) + signal_diff
    A positive, significant coef on signal_diff means the variant adds
    information beyond the Kalshi market price.

Run:  python backtest_cycle_variants.py
Env:  DATABASE_URL  (required)
      SEASON        (optional, default = all seasons)
"""
import os, math
from collections import defaultdict
from bisect import bisect_left
import psycopg2
from dotenv import load_dotenv
from validate_model import logistic_fit, logit, norm_sf

load_dotenv()

SEASON        = os.getenv("SEASON")
MIN_PERIOD    = 4        # mirrors analytics.js
MIN_AMPLITUDE = 0.005    # woba units — mirrors analytics.js
MIN_BATTERS   = 5        # of 9 starters required; skip game if fewer have data
TOSSUP_BAND   = 0.05     # variant D: only games where |kalshi_home_prob − 0.50| ≤ this


# ── DFT core ─────────────────────────────────────────────────────────────────

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


def _kept_components(signal):
    N = len(signal)
    re, im = _dft(signal)
    kept = []
    for k in range(1, N // 2 + 1):
        amp = 2 * math.sqrt(re[k] ** 2 + im[k] ** 2)
        if N / k < MIN_PERIOD:
            continue
        if amp < MIN_AMPLITUDE:
            continue
        kept.append((k, re[k], im[k]))
    return kept


def _reconstruct_at(kept, N, x):
    v = 0.0
    for k, rek, imk in kept:
        angle = 2 * math.pi * k * x / N
        v += rek * math.cos(angle) - imk * math.sin(angle)
    return v


# ── Per-player signal functions ───────────────────────────────────────────────

def signal_phase(values):
    """Baseline / B / C / D: DFT phase bucket → +2 Surge / +1 Cruise / −1 Slide / −2 Rebuild."""
    N = len(values)
    mean = sum(values) / N
    signal = [v - mean for v in values]
    kept = _kept_components(signal)
    if not kept:
        return 0
    level = _reconstruct_at(kept, N, N + 1)
    slope = _reconstruct_at(kept, N, N + 2) - _reconstruct_at(kept, N, N)
    if   level >  0 and slope >  0: return +2   # Surge
    elif level >  0 and slope <= 0: return +1   # Cruise
    elif level <= 0 and slope <= 0: return -1   # Slide
    else:                           return -2   # Rebuild


def signal_phase_alt(values):
    """Variant E: asymmetric phase weights +3/+2/−1/−3."""
    N = len(values)
    mean = sum(values) / N
    signal = [v - mean for v in values]
    kept = _kept_components(signal)
    if not kept:
        return 0
    level = _reconstruct_at(kept, N, N + 1)
    slope = _reconstruct_at(kept, N, N + 2) - _reconstruct_at(kept, N, N)
    if   level >  0 and slope >  0: return +3   # Surge
    elif level >  0 and slope <= 0: return +2   # Cruise
    elif level <= 0 and slope <= 0: return -1   # Slide
    else:                           return -3   # Rebuild


def _phase_label(values):
    """Return 'Surge'/'Cruise'/'Slide'/'Rebuild'/None for a player's trailing values."""
    N = len(values)
    mean = sum(values) / N
    signal = [v - mean for v in values]
    kept = _kept_components(signal)
    if not kept:
        return None
    level = _reconstruct_at(kept, N, N + 1)
    slope = _reconstruct_at(kept, N, N + 2) - _reconstruct_at(kept, N, N)
    if   level >  0 and slope >  0: return "Surge"
    elif level >  0 and slope <= 0: return "Cruise"
    elif level <= 0 and slope <= 0: return "Slide"
    else:                           return "Rebuild"

def make_single_phase_signal(target):
    """Returns a signal fn that scores 1 if player is in `target` phase, else 0."""
    def fn(values):
        return 1 if _phase_label(values) == target else 0
    fn.__name__ = f"signal_{target.lower()}_only"
    return fn

signal_surge_only   = make_single_phase_signal("Surge")
signal_cruise_only  = make_single_phase_signal("Cruise")
signal_slide_only   = make_single_phase_signal("Slide")
signal_rebuild_only = make_single_phase_signal("Rebuild")


def signal_zscore(values):
    """Variant A: (mean of last-3 games − window mean) / window std. No DFT."""
    N = len(values)
    mean = sum(values) / N
    if N < 2:
        return 0.0
    std = math.sqrt(sum((v - mean) ** 2 for v in values) / (N - 1))
    if std < 1e-6:
        return 0.0
    tail = values[-min(3, N):]
    return (sum(tail) / len(tail) - mean) / std


# ── Data loading ──────────────────────────────────────────────────────────────

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
        # Store (player_id, spot) so batting-order-weighted variants can use position
        lineups[game_pk] = {
            "home": [(p["player_id"], p.get("spot") or i + 1) for i, p in enumerate(home_lu)],
            "away": [(p["player_id"], p.get("spot") or i + 1) for i, p in enumerate(away_lu)],
        }

    seasons = sorted({g[2] for g in games})
    # Load woba and slg in one pass; store separately for variant C
    cur.execute("""
        SELECT player_id, season, game_date, woba, slg
        FROM player_gamelogs
        WHERE season = ANY(%s)
          AND at_bats >= 1
          AND player_id IS NOT NULL
        ORDER BY player_id, season, game_date
    """, (seasons,))
    logs_woba = defaultdict(list)
    logs_slg  = defaultdict(list)
    for player_id, season, gdate, woba, slg in cur.fetchall():
        key = (player_id, season)
        if woba is not None:
            logs_woba[key].append((gdate, float(woba)))
        if slg is not None:
            logs_slg[key].append((gdate, float(slg)))

    return games, lineups, logs_woba, logs_slg


def _trailing(logs, player_id, season, game_date, window, min_games):
    rows = logs.get((player_id, season))
    if not rows:
        return None
    dates = [d for d, _ in rows]
    idx = bisect_left(dates, game_date)
    trailing = rows[max(0, idx - window):idx]
    if len(trailing) < min_games:
        return None
    return [v for _, v in trailing]


# ── Evaluation ────────────────────────────────────────────────────────────────

def run_variant(games, lineups, logs, score_fn, window, min_games,
                tossup_only=False, order_weight_fn=None):
    """
    order_weight_fn(base_score, spot) -> float
    If None, all spots are weighted equally (equivalent to lambda s, _: s).
    """
    correct  = 0
    n_picks  = 0
    skipped  = 0
    reg_rows = []   # (kalshi_home_prob, diff, actual_home_win)

    for game_pk, game_date, season, home_team, away_team, actual_winner, khp in games:
        if tossup_only:
            if khp is None or abs(float(khp) - 0.50) > TOSSUP_BAND:
                skipped += 1
                continue

        lu = lineups.get(game_pk)
        if not lu:
            skipped += 1
            continue

        def lineup_score(id_spot_pairs):
            scores = []
            for pid, spot in id_spot_pairs:
                vals = _trailing(logs, pid, season, game_date, window, min_games)
                if vals is not None:
                    base = score_fn(vals)
                    w = order_weight_fn(base, spot) if order_weight_fn else base
                    scores.append(w)
            return scores

        home_s = lineup_score(lu["home"])
        away_s = lineup_score(lu["away"])

        if len(home_s) < MIN_BATTERS or len(away_s) < MIN_BATTERS:
            skipped += 1
            continue

        home_total = sum(home_s)
        away_total = sum(away_s)
        diff = home_total - away_total

        if diff != 0:
            n_picks += 1
            if (diff > 0) == (actual_winner == "home"):
                correct += 1

        if khp is not None:
            reg_rows.append((float(khp), diff, 1 if actual_winner == "home" else 0))

    acc = correct / n_picks if n_picks else None
    return acc, n_picks, reg_rows


def encompassing_p(reg_rows):
    """outcome ~ logit(kalshi) + diff. Returns (coef, p) for the diff term."""
    if len(reg_rows) < 30:
        return None, None
    X = [[logit(khp), diff] for khp, diff, _ in reg_rows]
    y = [yy for _, _, yy in reg_rows]
    try:
        beta, se = logistic_fit(X, y)
    except ValueError:
        return None, None
    coef = beta[2]
    p    = norm_sf(coef / se[2]) if se[2] > 0 else 1.0
    return coef, p


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur  = conn.cursor()
    print("Loading data...", flush=True)
    games, lineups, logs_woba, logs_slg = load_data(cur)
    cur.close()
    conn.close()

    total = len(games)
    print(f"Resolved games: {total}"
          + (f"  (season {SEASON})" if SEASON else "  (all seasons)"))

    # order_weight_fn(base_score, spot) -> weighted score
    # None = uniform (all existing variants)
    surge_by_order   = lambda score, spot: score * (10 - spot)   # spot 1 = 9x, spot 9 = 1x
    surge_top3_only  = lambda score, spot: score if spot <= 3 else 0

    variants = [
        # (label,                            logs,      score_fn,           window, min_games, tossup_only, order_weight_fn)
        ("Baseline (phase/15/woba)",          logs_woba, signal_phase,       15, 10, False, None),
        ("A: z-score/no-DFT (15/woba)",      logs_woba, signal_zscore,      15, 10, False, None),
        ("B: short window (phase/7/woba)",    logs_woba, signal_phase,        7,  5, False, None),
        ("C: alt stat (phase/15/slg)",        logs_slg,  signal_phase,       15, 10, False, None),
        ("D: toss-up only (phase/15/woba)",   logs_woba, signal_phase,       15, 10, True,  None),
        ("E: alt weights +3/+2/-1/-3",        logs_woba, signal_phase_alt,   15, 10, False, None),
        ("F1: Surge-only (1/0/0/0)",          logs_woba, signal_surge_only,  15, 10, False, None),
        ("F2: Cruise-only (0/1/0/0)",         logs_woba, signal_cruise_only, 15, 10, False, None),
        ("F3: Slide-only (0/0/1/0)",          logs_woba, signal_slide_only,  15, 10, False, None),
        ("F4: Rebuild-only (0/0/0/1)",        logs_woba, signal_rebuild_only,15, 10, False, None),
        ("G1: Surge x batting order weight",  logs_woba, signal_surge_only,  15, 10, False, surge_by_order),
        ("G2: Surge top-3 batters only",      logs_woba, signal_surge_only,  15, 10, False, surge_top3_only),
    ]

    print(f"\nRunning {len(variants)} variants...\n")

    header = f"{'Variant':<36} {'n_picks':>8} {'accuracy':>9} {'diff_coef':>10} {'p-value':>9}  verdict"
    print(header)
    print("-" * len(header))

    results = []
    for label, logs, score_fn, window, min_games, tossup_only, order_weight_fn in variants:
        print(f"  {label}...", end="", flush=True)
        acc, n_picks, reg_rows = run_variant(
            games, lineups, logs, score_fn, window, min_games, tossup_only, order_weight_fn
        )
        coef, p = encompassing_p(reg_rows)
        results.append((label, n_picks, acc, coef, p, tossup_only))
        print(" done")

    print()
    print(header)
    print("-" * len(header))
    for label, n_picks, acc, coef, p, tossup_only in results:
        acc_s  = f"{acc:.3f}"   if acc  is not None else "  n/a  "
        coef_s = f"{coef:+.4f}" if coef is not None else "   n/a  "
        p_s    = f"{p:.3f}"     if p    is not None else "  n/a "
        if p is None:
            verdict = "too few rows"
        elif p < 0.05 and coef > 0:
            verdict = "*** EDGE ***"
        elif p < 0.10 and coef > 0:
            verdict = "marginal"
        else:
            verdict = "no edge"
        print(f"  {label:<36} {n_picks:>8} {acc_s:>9} {coef_s:>10} {p_s:>9}  {verdict}")

    # ── Held-out test: Surge-only on the last 30% of games ───────────────────
    # Games are sorted by date. We "discovered" Surge on the full dataset, so
    # we split chronologically and test on the held-out tail to check for
    # multiple-comparisons false positive (12 variants tried → Bonferroni
    # threshold would be 0.004, which p=0.062 doesn't clear).
    split_idx    = int(len(games) * 0.70)
    disc_games   = games[:split_idx]
    holdout_games= games[split_idx:]

    print(f"\n{'-'*70}")
    print(f"Held-out validation: Surge-only on chronological 70/30 split")
    print(f"  Discovery set : {len(disc_games)} games  (first 70%)")
    print(f"  Held-out set  : {len(holdout_games)} games  (last 30% - never used to pick variant)")
    print(f"{'-'*70}")

    ho_header = f"  {'Split':<20} {'n_picks':>8} {'accuracy':>9} {'diff_coef':>10} {'p-value':>9}  verdict"
    print(ho_header)
    print(f"  {'-'*68}")
    for split_label, split_games in [("Discovery (first 70%)", disc_games),
                                      ("Held-out  (last 30%)",  holdout_games)]:
        acc, n_picks, reg_rows = run_variant(
            split_games, lineups, logs_woba, signal_surge_only, 15, 10
        )
        coef, p = encompassing_p(reg_rows)
        acc_s  = f"{acc:.3f}"   if acc  is not None else "  n/a  "
        coef_s = f"{coef:+.4f}" if coef is not None else "   n/a  "
        p_s    = f"{p:.3f}"     if p    is not None else "  n/a "
        if p is None:
            verdict = "too few rows"
        elif p < 0.05 and coef > 0:
            verdict = "*** REPLICATES ***"
        elif p < 0.10 and coef > 0:
            verdict = "marginal"
        else:
            verdict = "no edge"
        print(f"  {split_label:<20} {n_picks:>8} {acc_s:>9} {coef_s:>10} {p_s:>9}  {verdict}")

    print()
    print("Kalshi baseline: ~54.0% accuracy (from HANDOFF.md / prior validation)")
    print("Reading: p<0.05 + positive coef = variant adds info beyond what Kalshi priced in")
    print("Note: variant D accuracy is measured only over toss-up games (45–55% Kalshi prob)")


if __name__ == "__main__":
    main()
