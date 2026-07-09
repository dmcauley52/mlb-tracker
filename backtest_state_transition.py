"""
backtest_state_transition.py

The state-transition counterpart to the (now-dead) cycle work. Instead of asking
"what phase of a fixed cycle is a team in", it asks "what discrete recent-history
STATE is a team in, and does that state carry win information the baseline doesn't
already have." This is the physiologically-defensible direction: the endocrinology
points at reactive/state-driven dynamics (winner effect, fatigue, travel), not fixed
periodicity — see the cycle nulls in pooled_acf.py / validate_rolling_cycle.py /
backtest_pitcher_cycles.py.

Honest-test structure, identical in spirit to backtest_pitcher_cycles.py PART 2:
a raw "streak predicts winning" correlation is worthless because good teams both
win and streak. So every state feature is tested as an ENCOMPASSING coefficient on
top of a quality baseline:

  TIER B (gold standard, small N):  win ~ logit(kalshi_home_prob) + state_diff
      The market already prices team quality AND the visible streak. A significant
      state_diff coef = information the market underprices. Data-limited to resolved
      kalshi_tracker rows.

  TIER A (large N, weaker control):  win ~ winpct_diff + state_diff
      Uses full game_results with each team's AS-OF-DATE trailing win% as the quality
      control (no market, no leakage). Bigger sample, cruder baseline.

state_diff = feature(home entering the game) - feature(away entering the game), so a
positive coef means "more of this state on the home side -> home wins more, beyond
the baseline." Agreement across the two tiers is the signal.

All state is built STRICTLY from games before the target date, within the same season.

Run:  python backtest_state_transition.py
Env:  DATABASE_URL (required), SEASON (optional, default = all seasons)
"""
import os
from collections import defaultdict
from bisect import bisect_left
import psycopg2
from dotenv import load_dotenv

from validate_model import logistic_fit, logit, norm_sf

load_dotenv()

SEASON        = os.getenv("SEASON")
MIN_PRIOR     = 10   # min prior games this season before a team is evaluable
REST_CAP      = 6    # cap rest_days so a season-opener gap can't dominate
BOUNCE_STREAK = 4    # a loss that ends a >= this win streak = "bounce-back" state

# ── venue timezone map: team -> standard UTC offset (hours) ───────────────────
# Geographic standard offsets. DST shifts every venue equally EXCEPT Arizona (no
# DST); during the season AZ effectively aligns with Pacific, so its relative gap
# is at most 1h off here. Athletics play in Sacramento (Pacific) in 2025-26.
# Eastward travel = arriving at a HIGHER offset (e.g. Pacific -8 -> Eastern -5 =
# +3) = phase advance = the hard direction.
TZ = {
    # Eastern (-5)
    "Baltimore Orioles": -5, "Boston Red Sox": -5, "New York Yankees": -5,
    "New York Mets": -5, "Philadelphia Phillies": -5, "Pittsburgh Pirates": -5,
    "Washington Nationals": -5, "Atlanta Braves": -5, "Miami Marlins": -5,
    "Tampa Bay Rays": -5, "Toronto Blue Jays": -5, "Cleveland Guardians": -5,
    "Cincinnati Reds": -5, "Detroit Tigers": -5,
    # Central (-6)
    "Chicago White Sox": -6, "Chicago Cubs": -6, "Kansas City Royals": -6,
    "Minnesota Twins": -6, "Milwaukee Brewers": -6, "St. Louis Cardinals": -6,
    "Houston Astros": -6, "Texas Rangers": -6,
    # Mountain (-7)
    "Colorado Rockies": -7, "Arizona Diamondbacks": -7,
    # Pacific (-8)
    "Los Angeles Dodgers": -8, "Los Angeles Angels": -8, "San Diego Padres": -8,
    "San Francisco Giants": -8, "Seattle Mariners": -8, "Athletics": -8,
}


# ── per-team season sequence ─────────────────────────────────────────────────
def load_sequences(cur, seasons):
    """(team, season) -> sorted [(date, won:bool, margin:int, venue_tz)] from team's
    POV. venue_tz is the game's venue UTC offset (= home team's tz), same for both
    participants. margin is signed from this team's perspective.
    """
    where = "home_score IS NOT NULL AND away_score IS NOT NULL"
    params = []
    if seasons is not None:
        where += " AND season = ANY(%s)"
        params.append(seasons)
    cur.execute(f"""
        SELECT game_date, season, home_team, away_team, home_score, away_score
        FROM game_results
        WHERE {where}
        ORDER BY game_date ASC
    """, params)
    seqs = defaultdict(list)
    for gdate, season, home, away, hs, aws in cur.fetchall():
        vtz = TZ.get(home)  # venue = home team's timezone
        seqs[(home, season)].append((gdate, hs > aws, hs - aws, vtz))
        seqs[(away, season)].append((gdate, aws > hs, aws - hs, vtz))
    return seqs


# ── state features entering a game ───────────────────────────────────────────
def team_state(seqs, team, season, game_date):
    """Dict of state features for `team` entering the game on game_date, using only
    strictly-prior games this season. None if < MIN_PRIOR prior games."""
    seq = seqs.get((team, season))
    if not seq:
        return None
    dates = [d for d, _, _, _ in seq]
    idx = bisect_left(dates, game_date)
    prior = seq[:idx]
    if len(prior) < MIN_PRIOR:
        return None

    winpct = sum(1 for _, w, _, _ in prior if w) / len(prior)
    last_date, last_won, last_margin, last_venue_tz = prior[-1]

    # trailing win/loss streak entering the game
    win_streak = loss_streak = 0
    for _, w, _, _ in reversed(prior):
        if w:
            if loss_streak:
                break
            win_streak += 1
        else:
            if win_streak:
                break
            loss_streak += 1
    streak_signed = win_streak if win_streak else -loss_streak

    # bounce-back: last game was a loss that ended a >= BOUNCE_STREAK win run
    post_streak_loss = 0
    if not last_won:
        run = 0
        for _, w, _, _ in reversed(prior[:-1]):
            if w:
                run += 1
            else:
                break
        if run >= BOUNCE_STREAK:
            post_streak_loss = 1

    rest_days = min((game_date - last_date).days, REST_CAP)

    return {
        "winpct":           winpct,
        "streak_signed":    streak_signed,
        "win_streak":       win_streak,
        "rest_days":        rest_days,
        "last_margin":      last_margin,
        "post_streak_loss": post_streak_loss,
        "prev_venue_tz":    last_venue_tz,  # for travel/timezone features
    }


# Direct per-team features (value lives in the state dict, diffed home-away).
FEATURES = [
    ("streak_signed    (net W+/L- entering)", "streak_signed"),
    ("win_streak       (active win-run len)", "win_streak"),
    ("rest_days        (days since last game)", "rest_days"),
    ("last_margin      (signed prev-game margin)", "last_margin"),
    ("post_streak_loss (bounce-back / fade-streak)", "post_streak_loss"),
]

# Travel/timezone features (computed in build_records from the CURRENT venue and
# each team's previous venue — a phase-shift/jet-lag proxy).
TZ_FEATURES = [
    ("tz_shift         (signed venue shift, +=east)", "tz_shift"),
    ("tz_jump          (abs venue shift)", "tz_jump"),
    ("east_travel      (eastward shift only, hard dir)", "east_travel"),
]

ALL_FEATURES = FEATURES + TZ_FEATURES


# ── shared record builder: one row per usable game ───────────────────────────
def build_records(games, seqs):
    """games: iterable of (game_date, season, home, away, base, home_win) where base
    is either kalshi_home_prob (Tier B) or None (Tier A fills winpct_diff itself).
    Returns list of (game_date, base, home_win, {feature: diff})."""
    recs = []
    for gdate, season, home, away, base, home_win in games:
        hs = team_state(seqs, home, season, gdate)
        as_ = team_state(seqs, away, season, gdate)
        if hs is None or as_ is None:
            continue
        diffs = {key: hs[key] - as_[key] for _, key in FEATURES}
        diffs["winpct"] = hs["winpct"] - as_["winpct"]

        # travel/timezone: shift from each team's previous venue to THIS venue.
        cur_tz = TZ.get(home)
        if cur_tz is not None and hs["prev_venue_tz"] is not None \
                and as_["prev_venue_tz"] is not None:
            shift_h = cur_tz - hs["prev_venue_tz"]   # ~0 unless home just got back
            shift_a = cur_tz - as_["prev_venue_tz"]  # the visitor's trip
            diffs["tz_shift"]    = shift_h - shift_a
            diffs["tz_jump"]     = abs(shift_h) - abs(shift_a)
            diffs["east_travel"] = max(0, shift_h) - max(0, shift_a)
        else:
            diffs["tz_shift"] = diffs["tz_jump"] = diffs["east_travel"] = 0

        recs.append((gdate, base, home_win, diffs))
    return recs


# ── encompassing test for one feature ────────────────────────────────────────
def encompass(recs, key, base_kind):
    """Fit  home_win ~ baseline + state_diff  and return the state coef/p.

    base_kind == 'kalshi': baseline column is logit(kalshi_home_prob)  (Tier B)
    base_kind == 'winpct': baseline column is winpct_diff              (Tier A)
    """
    X, y = [], []
    for _, base, home_win, diffs in recs:
        if base_kind == "kalshi":
            if base is None:
                continue
            b = logit(float(base))
        else:
            b = diffs["winpct"]
        X.append([b, diffs[key]])
        y.append(home_win)
    if len(y) < 30:
        return None
    try:
        beta, se = logistic_fit(X, y)
    except ValueError:
        return None
    coef, s = beta[2], se[2]
    p = norm_sf(coef / s) if s > 0 else 1.0

    # standalone directional accuracy on games where the feature separates the teams
    n_pick = correct = 0
    for _, base, home_win, diffs in recs:
        if base_kind == "kalshi" and base is None:
            continue
        d = diffs[key]
        if d != 0:
            n_pick += 1
            if (d > 0) == (home_win == 1):
                correct += 1
    acc = correct / n_pick if n_pick else float("nan")
    return {"coef": coef, "se": s, "p": p, "n": len(y),
            "acc": acc, "n_pick": n_pick, "base_coef": beta[1]}


def verdict(r):
    if r is None:
        return "n/a"
    if r["p"] < 0.05 and abs(r["coef"]) > 1e-6:
        return "*** ADDS INFO ***"
    if r["p"] < 0.10:
        return "marginal"
    return "no edge"


def run_tier(label, recs, base_kind):
    print("\n" + "=" * 78)
    print(label)
    print("=" * 78)
    usable = sum(1 for _, base, _, _ in recs
                 if not (base_kind == "kalshi" and base is None))
    print(f"Usable games: {usable}   (baseline = "
          f"{'logit(kalshi)' if base_kind == 'kalshi' else 'as-of-date winpct_diff'})")
    print("-" * 78)
    print(f"{'state feature':<44}{'coef':>9}{'p':>8}  standalone     verdict")
    print("-" * 78)
    for label_f, key in ALL_FEATURES:
        r = encompass(recs, key, base_kind)
        if r is None:
            print(f"{label_f:<44}{'--':>9}{'--':>8}  too few rows")
            continue
        sa = f"{r['acc']:.3f} (n={r['n_pick']})"
        print(f"{label_f:<44}{r['coef']:>+9.4f}{r['p']:>8.3f}  {sa:<14} {verdict(r)}")
    return usable


# ── held-out guard on the standout feature ───────────────────────────────────
def held_out(recs, key, base_kind, feat_label):
    print("\n" + "-" * 78)
    print(f"Held-out validation: {feat_label} on chronological 70/30 split")
    print("(guards against reading an in-sample fluke as signal)")
    print("-" * 78)
    ordered = sorted(recs, key=lambda r: r[0])
    split = int(len(ordered) * 0.70)
    for name, sub in [("Discovery (first 70%)", ordered[:split]),
                      ("Held-out  (last 30%)", ordered[split:])]:
        r = encompass(sub, key, base_kind)
        if r is None:
            print(f"  {name:<22} too few rows")
            continue
        print(f"  {name:<22} n={r['n']:<5} coef={r['coef']:+.4f}  "
              f"p={r['p']:.3f}  standalone={r['acc']:.3f}  -> {verdict(r)}")
    print("  A real edge keeps the same sign and holds up on the held-out tail.")


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    seasons = [int(SEASON)] if SEASON else None
    if seasons is None:
        cur.execute("SELECT DISTINCT season FROM game_results "
                    "WHERE home_score IS NOT NULL")
        seasons = [r[0] for r in cur.fetchall()]

    seqs = load_sequences(cur, seasons)
    print(f"Loaded {sum(len(v) for v in seqs.values())} team-game rows across "
          f"{len(seqs)} team-seasons")
    print(f"Config: min {MIN_PRIOR} prior games | rest cap {REST_CAP}d | "
          f"bounce-back = loss ending {BOUNCE_STREAK}+ win streak")

    # ── TIER A: all completed games, as-of-date winpct control ───────────────
    where_a = "home_score IS NOT NULL AND away_score IS NOT NULL AND season = ANY(%s)"
    cur.execute(f"""
        SELECT game_date, season, home_team, away_team, home_score, away_score
        FROM game_results
        WHERE {where_a}
        ORDER BY game_date
    """, (seasons,))
    games_a = [(gd, se, h, a, None, 1 if hs > aws else 0)
               for gd, se, h, a, hs, aws in cur.fetchall()]
    recs_a = build_records(games_a, seqs)
    run_tier("TIER A — win ~ winpct_diff + state_diff   (large N, market-free)",
             recs_a, "winpct")

    # ── TIER B: resolved kalshi_tracker games, market baseline ───────────────
    where_b = "actual_winner IS NOT NULL AND kalshi_home_prob IS NOT NULL"
    params_b = []
    if SEASON:
        where_b += " AND season = %s"
        params_b.append(int(SEASON))
    cur.execute(f"""
        SELECT game_date, season, home_team, away_team,
               kalshi_home_prob, actual_winner
        FROM kalshi_tracker
        WHERE {where_b}
        ORDER BY game_date
    """, params_b)
    games_b = [(gd, se, h, a, khp, 1 if win == "home" else 0)
               for gd, se, h, a, khp, win in cur.fetchall()]
    recs_b = build_records(games_b, seqs)
    usable_b = run_tier(
        "TIER B — win ~ logit(kalshi) + state_diff   (gold standard, small N)",
        recs_b, "kalshi")

    # Held-out check on the bounce-back state (the a-priori favourite, per the
    # 0/27 fade-the-streak result in streak_prediction_analysis.py) and on the
    # eastward-travel state (the sharpest circadian-disruption proxy).
    if usable_b >= 40:
        held_out(recs_b, "post_streak_loss", "kalshi",
                 "post_streak_loss (Tier B)")
        held_out(recs_b, "east_travel", "kalshi", "east_travel (Tier B)")

    print("\nReading: a feature only earns its keep if its coef is significant AND")
    print("same-signed in BOTH tiers, and survives the held-out split. Otherwise the")
    print("'state effect' is already inside team quality / the market price.")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
