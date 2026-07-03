"""
backtest_bullpen_cycles.py

We don't know which reliever will pitch, but we DO know the whole pen. Question:
does one team's bullpen carrying more Surge (hot) or Due (mean-reversion) relievers
than the opponent's predict the winner — and does it add anything beyond the
starting pitcher signal (which on its own showed no edge, see
backtest_pitcher_cycles.py / [[project-pitcher-cycle-validation]])?

Approach
  Active pen as of a game date = relievers who (a) pitched in RELIEF for that team
  within the trailing ACTIVE_DAYS, and (b) have >= MIN_GAMES prior relief outings
  that season (enough to fit a cycle). Team match is exact (pitcher_gamelogs.team ==
  game_results.home_team).

  Per team, aggregate each reliever's cycle signal (built on relief game_score,
  same DFT phase machinery as the starter/hitter work) into pen-level numbers:
     surge_frac  — share of pen in Surge phase (above own mean + rising)
     due_frac    — share of pen in Slide phase (below own mean + falling = 'due')
     phase_mean  — mean phase weight (+2/+1/-1/-2); higher = hotter pen
     due_mean    — mean of -forecast_dev; higher = more 'due' pen
  Also a persistence control:
     form_mean   — mean trailing relief game_score (how well the pen has thrown lately)

  Diff = home_pen - away_pen. Tests per aggregate:
     standalone directional accuracy
     outcome ~ logit(kalshi) + pen_diff              (beats the market?)
  Then the headline:
     outcome ~ logit(kalshi) + sp_due_diff + pen_diff   (pen add beyond starter?)
  And a chronological 70/30 held-out check on the best pen aggregate.

Run:  python backtest_bullpen_cycles.py
Env:  DATABASE_URL (required), SEASON (optional, default = all seasons)
"""
import os
from collections import defaultdict
from datetime import timedelta
from bisect import bisect_left
import psycopg2
from dotenv import load_dotenv

from validate_model import logistic_fit, logit, norm_sf
from backtest_pitcher_cycles import (
    sig_surge, sig_slide, sig_phase, sig_due, trailing_mean,
    load_starts, trailing, WINDOW, MIN_GAMES,
)

load_dotenv()

SEASON      = os.getenv("SEASON")
ACTIVE_DAYS = 21    # a reliever counts as 'in the pen' if he pitched within this many days
MIN_PEN     = 4     # require >= this many qualifying relievers per team, else skip game


# ── data loading ─────────────────────────────────────────────────────────────
def load_relief(cur, seasons):
    """
    Returns:
      rlogs: (pid, season) -> sorted [(date, game_score, 0)]  (0 = qs placeholder,
             lets us reuse trailing() from backtest_pitcher_cycles unchanged)
      team_apps: team -> sorted list of (date, pid) relief appearances
    """
    cur.execute("""
        SELECT player_id, season, team, game_date, game_score
        FROM pitcher_gamelogs
        WHERE is_starter = FALSE
          AND game_score IS NOT NULL
          AND player_id IS NOT NULL
          AND team IS NOT NULL
          AND season = ANY(%s)
        ORDER BY player_id, season, game_date
    """, (seasons,))
    rlogs = defaultdict(list)
    team_apps = defaultdict(list)
    for pid, season, team, gdate, gs in cur.fetchall():
        rlogs[(pid, season)].append((gdate, float(gs), 0))
        team_apps[team].append((gdate, pid, season))
    for t in team_apps:
        team_apps[t].sort()
    return rlogs, team_apps


def load_games(cur):
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
        SELECT game_pk, sp_home_id, sp_away_id
        FROM game_results WHERE game_pk = ANY(%s)
    """, ([g[0] for g in games],))
    sp = {gp: (h, a) for gp, h, a in cur.fetchall()}
    return games, sp


# ── pen aggregation ──────────────────────────────────────────────────────────
def active_pen(team_apps, team, season, game_date):
    """Distinct reliever pids for `team` with a relief outing in
    [game_date - ACTIVE_DAYS, game_date), same season."""
    rows = team_apps.get(team)
    if not rows:
        return []
    lo = game_date - timedelta(days=ACTIVE_DAYS)
    dates = [d for d, _, _ in rows]
    i0 = bisect_left(dates, lo)
    i1 = bisect_left(dates, game_date)
    return list({pid for _, pid, s in rows[i0:i1] if s == season})


def pen_metrics(rlogs, team_apps, team, season, game_date):
    """Aggregate cycle signals over the active pen. Returns dict or None if too thin."""
    pids = active_pen(team_apps, team, season, game_date)
    surge = slide = 0
    phase_ws, due_vals, form_vals = [], [], []
    n = 0
    for pid in pids:
        vals = trailing(rlogs, pid, season, game_date)   # last WINDOW relief game_scores
        if vals is None:
            continue
        n += 1
        surge += sig_surge(vals)
        slide += sig_slide(vals)
        phase_ws.append(sig_phase(vals))
        due_vals.append(sig_due(vals))
        form_vals.append(trailing_mean(vals))
    if n < MIN_PEN:
        return None
    return {
        "surge_frac": surge / n,
        "due_frac":   slide / n,
        "phase_mean": sum(phase_ws) / n,
        "due_mean":   sum(due_vals) / n,
        "form_mean":  sum(form_vals) / n,
        "pen_size":   n,
    }


AGGS = ["surge_frac", "due_frac", "phase_mean", "due_mean"]


# ── regression helpers ───────────────────────────────────────────────────────
def fit_report(reg, extra_names):
    """reg rows = (kalshi, [extra features...], y). Returns list of (name, coef, p)
    for the extra features (skips intercept + logit(kalshi))."""
    if len(reg) < 30:
        return None
    X = [[logit(k)] + list(feats) for k, feats, _ in reg]
    y = [yy for _, _, yy in reg]
    try:
        b, se = logistic_fit(X, y)
    except ValueError:
        return None
    out = []
    for j, nm in enumerate(extra_names):
        idx = 2 + j   # 0 intercept, 1 logit(kalshi), then extras
        coef = b[idx]
        p = norm_sf(coef / se[idx]) if se[idx] > 0 else 1.0
        out.append((nm, coef, p))
    return out


def verdict(coef, p):
    if p < 0.05 and coef > 0:
        return "*** EDGE ***"
    if p < 0.10 and coef > 0:
        return "marginal"
    return "no edge"


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    seasons = [int(SEASON)] if SEASON else None
    if seasons is None:
        cur.execute("SELECT DISTINCT season FROM pitcher_gamelogs")
        seasons = [r[0] for r in cur.fetchall()]

    slogs = load_starts(cur, seasons)              # starter logs for SP control
    rlogs, team_apps = load_relief(cur, seasons)
    games, sp = load_games(cur)
    cur.close(); conn.close()

    print(f"Relief appearances: {sum(len(v) for v in rlogs.values())} across "
          f"{len(rlogs)} reliever-seasons")
    print(f"Active-pen window: {ACTIVE_DAYS}d | min {MIN_PEN} qualifying relievers/team "
          f"| cycle window {WINDOW} outings, min {MIN_GAMES}")
    print(f"Resolved games with market price: {len(games)}"
          + (f"  (season {SEASON})" if SEASON else "  (all seasons)"))

    # Precompute per-game: pen metrics for both sides + starter 'due' diff.
    # rows: (game_pk, khp, home_win, {agg: home-away diff}, sp_due_diff or None)
    rows = []
    n_pen = 0
    for game_pk, gdate, season, home, away, winner, khp in games:
        if khp is None:
            continue
        hp = pen_metrics(rlogs, team_apps, home, season, gdate)
        ap = pen_metrics(rlogs, team_apps, away, season, gdate)
        if hp is None or ap is None:
            continue
        n_pen += 1
        pen_diff = {a: hp[a] - ap[a] for a in AGGS}
        # starter 'due' diff (continuous -forecast dev), if both SPs have history
        sp_due = None
        ids = sp.get(game_pk)
        if ids and ids[0] and ids[1]:
            hv = trailing(slogs, ids[0], season, gdate)
            av = trailing(slogs, ids[1], season, gdate)
            if hv is not None and av is not None:
                sp_due = sig_due(hv) - sig_due(av)
        rows.append((game_pk, float(khp), 1 if winner == "home" else 0,
                     pen_diff, sp_due))

    print(f"Games with both pens qualifying: {n_pen}\n")

    # ── standalone + market-encompassing per aggregate ───────────────────────
    print("=" * 74)
    print("Bullpen aggregate vs market   (diff = home_pen - away_pen)")
    print("=" * 74)
    print(f"{'aggregate':<14} {'picks':>6} {'acc':>7} {'coef':>10} {'p':>8}  verdict")
    print("-" * 74)
    for a in AGGS:
        reg = [(khp, [pd[a]], y) for _, khp, y, pd, _ in rows]
        # standalone accuracy
        npick = corr = 0
        for _, khp, y, pd, _ in rows:
            d = pd[a]
            if d != 0:
                npick += 1
                if (d > 0) == (y == 1):
                    corr += 1
        acc = corr / npick if npick else float("nan")
        res = fit_report(reg, [a])
        if res is None:
            print(f"{a:<14} too few rows")
            continue
        _, coef, p = res[0]
        print(f"{a:<14} {npick:>6} {acc:>7.3f} {coef:>+10.4f} {p:>8.3f}  {verdict(coef, p)}")

    # ── headline: does the pen add beyond the starter? ───────────────────────
    print("\n" + "=" * 74)
    print("Does the bullpen add BEYOND the starter?  "
          "outcome ~ logit(kalshi) + sp_due_diff + pen_diff")
    print("=" * 74)
    with_sp = [(khp, y, pd, spd) for _, khp, y, pd, spd in rows if spd is not None]
    print(f"Games with both pens AND both starters qualifying: {len(with_sp)}")
    if len(with_sp) >= 30:
        for a in AGGS:
            reg = [(khp, [spd, pd[a]], y) for khp, y, pd, spd in with_sp]
            res = fit_report(reg, ["sp_due_diff", a])
            if res is None:
                continue
            (_, sc, sp_p), (_, pc, pp) = res
            print(f"\n  pen aggregate = {a}")
            print(f"    sp_due_diff   coef={sc:+.4f}  p={sp_p:.3f}")
            print(f"    {a:<12}  coef={pc:+.4f}  p={pp:.3f}  -> {verdict(pc, pp)}")

    # ── held-out split on the strongest pen aggregate (by |coef/p|) ──────────
    print("\n" + "=" * 74)
    print("Held-out validation (chronological 70/30) on each pen aggregate")
    print("guards against fitting an in-sample pattern")
    print("=" * 74)
    split = int(len(rows) * 0.70)
    disc, hold = rows[:split], rows[split:]
    print(f"  Discovery: {len(disc)} games | Held-out: {len(hold)} games\n")
    print(f"  {'aggregate':<14} {'split':<12} {'coef':>10} {'p':>8}  verdict")
    print("  " + "-" * 56)
    for a in AGGS:
        for lbl, gs in [("discovery", disc), ("held-out", hold)]:
            reg = [(khp, [pd[a]], y) for _, khp, y, pd, _ in gs]
            res = fit_report(reg, [a])
            if res is None:
                print(f"  {a:<14} {lbl:<12} too few rows")
                continue
            _, coef, p = res[0]
            print(f"  {a:<14} {lbl:<12} {coef:>+10.4f} {p:>8.3f}  {verdict(coef, p)}")
        print()

    print("Reading: a real bullpen edge (a) beats the market on its own, (b) keeps a")
    print("significant coef with sp_due_diff in the model, and (c) does NOT shrink on")
    print("the held-out tail. Anything that only shows up in-sample is noise.")


if __name__ == "__main__":
    main()
