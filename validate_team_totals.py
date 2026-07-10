"""
validate_team_totals.py
Does the run model have skill on TOTALS markets (Kalshi KXMLBTEAMTOTAL team
run ladders, Polymarket game-total O/U ladders)?

Prior work (rank_price_accuracy.py) showed the model is ~2% behind the market
on WIN probability — but totals are a different instrument, and the model's
first-class output is predicted runs (lineup wOBA / Monte Carlo), not wins.

Uses game_predictions history (predictions frozen before first pitch, actuals
joined after): for each team-side observation, convert the predicted run mean
to over/under probabilities via a negative binomial (dispersion fitted from
residuals), then score against actuals with Brier at each market threshold.
Skill bar: beat climatology (same NB centred on the league-average mean).
Beating climatology is necessary but not sufficient to beat Kalshi's quotes —
market comparison needs the quote logger.

Run:  python validate_team_totals.py
Env:  DATABASE_URL (required)
"""
import os
import sys
import math
import statistics
import psycopg2

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from dotenv import load_dotenv

load_dotenv()

TEAM_THRESHOLDS = [1.5, 2.5, 3.5, 4.5, 5.5, 6.5]   # Kalshi team-total rungs
GAME_THRESHOLDS = [6.5, 7.5, 8.5, 9.5, 10.5]        # Polymarket game-total rungs


def nb_r(mean, var):
    """Method-of-moments negative-binomial dispersion r (None = Poisson-ish)."""
    return mean * mean / (var - mean) if var > mean else None


def nb_sf(threshold, mu, r):
    """P(X > threshold) for NB(mean=mu, dispersion=r); Poisson if r is None."""
    mu = max(mu, 0.05)
    k_max = int(threshold)  # P(X > t) = 1 - P(X <= floor(t))
    cdf = 0.0
    for k in range(k_max + 1):
        if r is None:
            logp = -mu + k * math.log(mu) - math.lgamma(k + 1)
        else:
            logp = (math.lgamma(k + r) - math.lgamma(r) - math.lgamma(k + 1)
                    + r * math.log(r / (r + mu)) + k * math.log(mu / (r + mu)))
        cdf += math.exp(logp)
    return max(0.0, 1.0 - cdf)


def brier(pairs):
    """pairs: [(prob, outcome01)]"""
    return sum((p - o) ** 2 for p, o in pairs) / len(pairs)


def recalibrate(obs):
    """In-sample OLS actual = a + b*pred — the GENEROUS upper bound on what a
    bias/scale fix could recover. Returns recalibrated obs."""
    preds = [p for p, _ in obs]
    actuals = [a for _, a in obs]
    mp, ma = statistics.mean(preds), statistics.mean(actuals)
    sxx = sum((p - mp) ** 2 for p in preds)
    b = sum((p - mp) * (a - ma) for p, a in obs) / sxx if sxx else 0.0
    a0 = ma - b * mp
    return [(a0 + b * p, a) for p, a in obs]


def eval_thresholds(label, obs, thresholds):
    """obs: [(predicted_mean, actual_int)]. Prints skill table per threshold."""
    preds = [p for p, _ in obs]
    actuals = [a for _, a in obs]
    clim_mu = statistics.mean(actuals)
    resid_var = sum((a - p) ** 2 for p, a in obs) / len(obs)
    r_model = nb_r(clim_mu, resid_var)
    r_clim = nb_r(clim_mu, statistics.variance(actuals))
    print(f"\n=== {label} (n={len(obs)}) ===")
    print(f"  pred mean={statistics.mean(preds):.2f}  actual mean={clim_mu:.2f}  "
          f"bias={statistics.mean(preds) - clim_mu:+.2f}")
    print(f"  MAE model={statistics.mean(abs(a - p) for p, a in obs):.3f}  "
          f"MAE climatology={statistics.mean(abs(a - clim_mu) for a in actuals):.3f}  "
          f"corr={pearson(preds, actuals):+.3f}")
    print(f"  NB dispersion: model-residual r={r_model and round(r_model, 1)}  "
          f"climatology r={r_clim and round(r_clim, 1)}")
    print(f"  {'thresh':>7} {'base':>6} {'Brier model':>12} {'Brier clim':>11} "
          f"{'skill':>7}  calibration (pred-quintile exceedance act vs mod)")
    for t in thresholds:
        model_pairs = [(nb_sf(t, p, r_model), 1 if a > t else 0) for p, a in obs]
        clim_p = nb_sf(t, clim_mu, r_clim)
        clim_pairs = [(clim_p, o) for _, o in model_pairs]
        bm, bc = brier(model_pairs), brier(clim_pairs)
        # calibration by predicted-mean quintile
        srt = sorted(zip(obs, model_pairs), key=lambda x: x[0][0])
        q = len(srt) // 5
        cal = []
        for i in range(5):
            chunk = srt[i * q:(i + 1) * q] if i < 4 else srt[4 * q:]
            act = statistics.mean(1 if a > t else 0 for (_, a), _ in chunk)
            mod = statistics.mean(mp for _, (mp, _) in chunk)
            cal.append(f"{act:.2f}/{mod:.2f}")
        print(f"  {t:>7} {statistics.mean(o for _, o in model_pairs):>6.3f} "
              f"{bm:>12.4f} {bc:>11.4f} {100 * (bc - bm) / bc:>+6.1f}%  {' '.join(cal)}")


def pearson(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    return sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0


def main():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("""
        SELECT mc_home_runs, mc_away_runs, woba_home_runs, woba_away_runs,
               actual_home_runs, actual_away_runs
        FROM game_predictions
        WHERE actual_home_runs IS NOT NULL AND actual_away_runs IS NOT NULL
    """)
    rows = cur.fetchall()
    conn.close()

    mc_team, woba_team, mc_game, woba_game = [], [], [], []
    for mh, ma, wh, wa, ah, aa in rows:
        if mh is not None and ma is not None:
            mc_team += [(float(mh), ah), (float(ma), aa)]
            mc_game.append((float(mh) + float(ma), ah + aa))
        if wh is not None and wa is not None:
            woba_team += [(float(wh), ah), (float(wa), aa)]
            woba_game.append((float(wh) + float(wa), ah + aa))

    print(f"{len(rows)} completed games with predictions")
    if mc_team:
        eval_thresholds("MC model — TEAM runs (Kalshi KXMLBTEAMTOTAL)", mc_team, TEAM_THRESHOLDS)
        eval_thresholds("MC model — GAME total (Polymarket O/U)", mc_game, GAME_THRESHOLDS)
        eval_thresholds("MC RECALIBRATED — TEAM runs", recalibrate(mc_team), TEAM_THRESHOLDS)
        eval_thresholds("MC RECALIBRATED — GAME total", recalibrate(mc_game), GAME_THRESHOLDS)
    if woba_team:
        eval_thresholds("wOBA model — TEAM runs", woba_team, TEAM_THRESHOLDS)
        eval_thresholds("wOBA model — GAME total", woba_game, GAME_THRESHOLDS)
        eval_thresholds("wOBA RECALIBRATED — TEAM runs", recalibrate(woba_team), TEAM_THRESHOLDS)
        eval_thresholds("wOBA RECALIBRATED — GAME total", recalibrate(woba_game), GAME_THRESHOLDS)


if __name__ == "__main__":
    main()
