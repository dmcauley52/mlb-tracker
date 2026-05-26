"""
prediction_engine.py
Core MLB prediction algorithms. No I/O — pure computation only.
Imported by fetch_kalshi_outcomes, fetch_game_predictions, and predict_on_demand.
"""
import math
import random

from model_config import (
    LEAGUE_AVG_DIST,
    LEAGUE_AVG_ERA,
    LEAGUE_AVG_K9,
    OPP_RUNS_BASE,
    WIN_PCT_RUN_SCALE,
    WIN_PROB_SIGMOID_SCALE,
    WOBA_WEIGHTS,
)
from model_weights import FALLBACK_BACKTEST

# ── Cycle analysis constants (mirrors analytics.js) ──────────────────────────
MIN_PERIOD     = 4
MIN_AMPLITUDE  = 0.005
MAX_COMPONENTS = 5
FORECAST_DAYS  = 14


# ── DFT / cycle analysis ──────────────────────────────────────────────────────

def _dft(signal):
    """O(n²) DFT — returns (re[], im[]) each divided by N."""
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


def _reconstruct_at(components, N, positions):
    out = []
    for x in positions:
        v = 0.0
        for c in components:
            angle = 2 * math.pi * c['k'] * x / N
            v += c['re'] * math.cos(angle) - c['im'] * math.sin(angle)
        out.append(v)
    return out


def analyze_player_cycles(game_data):
    """
    game_data: list of dicts with 'avg' or 'woba' per game.
    Returns dict with forecastValues, mean, dominantCycles, N — or None if insufficient data.
    """
    N = len(game_data)
    if N < 10:
        return None
    raw = [g.get('woba') or g.get('avg', 0) for g in game_data]
    mean = sum(raw) / N
    signal = [v - mean for v in raw]
    re, im = _dft(signal)
    spectrum = []
    for k in range(1, N // 2 + 1):
        amp = 2 * math.sqrt(re[k]**2 + im[k]**2)
        period = N / k
        if period < MIN_PERIOD:
            continue
        spectrum.append({'k': k, 'amp': amp, 'period': period, 're': re[k], 'im': im[k]})
    spectrum.sort(key=lambda c: -c['amp'])
    kept = [c for c in spectrum if c['amp'] >= MIN_AMPLITUDE][:MAX_COMPONENTS]
    if not kept:
        return None
    components = [{'k': 0, 're': mean, 'im': 0.0}] + kept
    reconstructed = _reconstruct_at(components, N, list(range(N)))
    forecast_vals = _reconstruct_at(components, N, [N + i for i in range(FORECAST_DAYS)])
    dominant = [{'period': c['period'], 'amp': c['amp']} for c in kept]
    return {
        'mean': mean,
        'reconstructed': reconstructed,
        'forecastValues': forecast_vals,
        'dominantCycles': dominant,
        'N': N,
    }


def compute_player_score(game_data, season_ops=0.720):
    """
    Returns {'score': 0-99, 'tier': str, 'breakdown': dict}.
    Mirrors computePredictionScore in analytics.js (no matchup/splits component).
    """
    if not game_data or len(game_data) < 3:
        return {'score': 50, 'tier': 'neutral',
                'breakdown': {'phaseScore': 15, 'trendScore': 12, 'opsScore': 11, 'momentumScore': 0}}

    def sig(g):
        return g.get('woba') or g.get('avg', 0)

    # Phase score from DFT forecast direction (0-30)
    analysis = analyze_player_cycles(game_data)
    phase_score = 15
    if analysis and analysis['forecastValues']:
        f = analysis['forecastValues']
        trend3  = sum(f[0:3]) / 3
        trend3b = sum(f[1:4]) / 3
        slope   = trend3b - trend3
        level   = f[0] - analysis['mean']
        if   slope >  0.010: phase_score = 30
        elif slope >  0.004: phase_score = 26
        elif slope >  0:     phase_score = 20
        elif slope > -0.004: phase_score = 14
        elif slope > -0.010: phase_score = 8
        else:                phase_score = 4
        if level > 0.015:
            phase_score = min(30, phase_score + 4)

    # Trend score: last-5 avg vs wOBA midpoint (0-25)
    last5     = game_data[-5:]
    last5_val = sum(sig(g) for g in last5) / len(last5)
    sig_mid   = 0.315
    trend_score = min(25, max(0, round((last5_val / (sig_mid * 1.4)) * 25)))

    # OPS score (0-20)
    ops = float(season_ops or 0)
    if   ops >= 0.900: ops_score = 20
    elif ops >= 0.800: ops_score = 16
    elif ops >= 0.700: ops_score = 11
    elif ops >= 0.600: ops_score = 6
    else:              ops_score = 3

    # Momentum: 2nd half of last 10 vs 1st half (0-15)
    last10      = game_data[-10:]
    first_half  = sum(sig(g) for g in last10[:5]) / 5 if len(last10) >= 5 else last5_val
    second_half = sum(sig(g) for g in last10[5:]) / max(len(last10[5:]), 1)
    momentum_score = min(15, round((second_half - first_half) * 200)) if second_half > first_half else 0

    score = min(99, max(1, phase_score + trend_score + ops_score + momentum_score))
    tier  = 'hot' if score >= 75 else 'warm' if score >= 55 else 'neutral' if score >= 35 else 'cold'
    return {
        'score': score, 'tier': tier,
        'breakdown': {
            'phaseScore':    phase_score,
            'trendScore':    trend_score,
            'opsScore':      ops_score,
            'momentumScore': momentum_score,
            'matchupScore':  5,  # neutral default — no live SP data in Python path
        },
    }


def compute_cycle_edge(player_scores):
    """
    player_scores: list of dicts from compute_player_score.
    Returns {'score': 0-100, 'hotCount': int, 'totalCount': int} or None.
    Mirrors computeCycleEdge in analytics.js.
    """
    valid = [p for p in player_scores if p is not None]
    if not valid:
        return None
    n = len(valid)
    avg_phase   = sum(p['breakdown']['phaseScore']          for p in valid) / n
    avg_trend   = sum(p['breakdown']['trendScore']          for p in valid) / n
    avg_matchup = sum(p['breakdown'].get('matchupScore', 5) for p in valid) / n
    hot_count   = sum(1 for p in valid if p['tier'] in ('hot', 'warm'))
    phase_norm   = (avg_phase   / 30)  * 100
    trend_norm   = (avg_trend   / 25)  * 100
    heat_norm    = (hot_count   / n)   * 100
    matchup_norm = (avg_matchup / 10)  * 100
    raw = phase_norm * 0.40 + trend_norm * 0.30 + heat_norm * 0.20 + matchup_norm * 0.10
    return {'score': round(raw), 'hotCount': hot_count, 'totalCount': n}


def cycle_edge_prob(home_score, away_score):
    """Logistic sigmoid on score diff → home win probability. Mirrors JS."""
    diff = (home_score - away_score) / 100.0
    return round(1 / (1 + math.exp(-diff * 3.0)), 3)


# ── wOBA model ────────────────────────────────────────────────────────────────

def compute_woba(games):
    """
    games: list of DB gamelog row dicts (snake_case field names).
    Returns wOBA float, or 0.310 if insufficient data.
    """
    pa = bb = hbp = h = dbl = trp = hr = ab = sf = 0
    for g in games:
        ab  += g["at_bats"]      or 0
        h   += g["hits"]         or 0
        dbl += g["doubles"]      or 0
        trp += g["triples"]      or 0
        hr  += g["home_runs"]    or 0
        bb  += g["walks"]        or 0
        hbp += g["hit_by_pitch"] or 0
        sf  += g["sac_flies"]    or 0
    s1b = h - dbl - trp - hr
    num = (WOBA_WEIGHTS["bb"] * bb + WOBA_WEIGHTS["hbp"] * hbp +
           WOBA_WEIGHTS["single"] * s1b + WOBA_WEIGHTS["double"] * dbl +
           WOBA_WEIGHTS["triple"] * trp + WOBA_WEIGHTS["hr"] * hr)
    den = ab + bb + hbp + sf
    return round(num / den, 4) if den > 0 else 0.310


def predict_runs_woba(lineup_wobas, opp_era, my_win_pct, opp_win_pct,
                      opp_lineup_woba=None, is_home=False, park_factor=1.0,
                      weights=None):
    """
    Spot-weighted wOBA run/win model. weights defaults to FALLBACK_BACKTEST.
    Returns dict with predicted_runs, opp_runs_est, avg_woba, win_prob.
    """
    w            = weights or FALLBACK_BACKTEST
    spot_weights = w["spot_weights"]
    n            = len(lineup_wobas)
    raw_w        = spot_weights[:n]
    w_sum        = sum(raw_w)
    avg_woba     = (sum(wt * wo for wt, wo in zip(raw_w, lineup_wobas)) / w_sum
                    if w_sum > 0 else 0.310)

    era_factor   = max(0.80, min(1.30, opp_era / LEAGUE_AVG_ERA)) if opp_era else 1.0
    adj_era      = era_factor * w["opp_era_scale"] + 1.0 * (1 - w["opp_era_scale"])
    team_quality = max(0.88, min(1.12, 1.0 + (my_win_pct - 0.500) * 0.5))

    predicted = min(
        avg_woba * w["woba_run_scale"] * adj_era * team_quality * park_factor,
        w["max_predicted_runs"]
    )

    opp_runs_wpc = OPP_RUNS_BASE + (opp_win_pct - 0.500) * WIN_PCT_RUN_SCALE
    opp_runs_est = ((opp_lineup_woba * w["woba_run_scale"] * 0.6 + opp_runs_wpc * 0.4) * park_factor
                    if opp_lineup_woba else opp_runs_wpc * park_factor)

    run_diff = predicted - opp_runs_est
    win_prob = 1 / (1 + math.exp(-run_diff * WIN_PROB_SIGMOID_SCALE))
    return {
        "predicted_runs": round(predicted, 2),
        "opp_runs_est":   round(opp_runs_est, 2),
        "avg_woba":       round(avg_woba, 4),
        "win_prob":       round(win_prob, 3),
    }


def model_home_prob(home_woba, away_woba, home_win_pct, away_win_pct,
                    home_sp_era, away_sp_era, weights=None):
    """
    Simple two-team wOBA model → normalised home win probability.
    weights defaults to FALLBACK_BACKTEST.
    """
    w = weights or FALLBACK_BACKTEST

    def pred_runs(woba, my_wpc, opp_era):
        era_f  = max(0.80, min(1.30, opp_era / LEAGUE_AVG_ERA))
        adj    = era_f * w["opp_era_scale"] + 1.0 * (1 - w["opp_era_scale"])
        team_q = max(0.88, min(1.12, 1.0 + (my_wpc - 0.500) * 0.5))
        return min(woba * w["woba_run_scale"] * adj * team_q, w["max_predicted_runs"])

    hr    = pred_runs(home_woba, home_win_pct, away_sp_era)
    ar    = pred_runs(away_woba, away_win_pct, home_sp_era)
    opp_h = OPP_RUNS_BASE + (away_win_pct - 0.500) * WIN_PCT_RUN_SCALE
    opp_a = OPP_RUNS_BASE + (home_win_pct - 0.500) * WIN_PCT_RUN_SCALE
    rh    = 1 / (1 + math.exp(-(hr - opp_h) * WIN_PROB_SIGMOID_SCALE))
    ra    = 1 / (1 + math.exp(-(ar - opp_a) * WIN_PROB_SIGMOID_SCALE))
    s     = rh + ra
    return round(rh / s, 3) if s > 0 else 0.500


# ── PA distributions ──────────────────────────────────────────────────────────

def compute_distribution(games):
    """
    games: list of DB gamelog row dicts.
    Returns per-outcome probability dict, or None if no plate appearances.
    """
    pa = hr = trip = dbl = hit = bb_hbp = k = 0
    for g in games:
        pa     += g["plate_appearances"] or g["at_bats"] or 0
        hr     += g["home_runs"]  or 0
        trip   += g["triples"]    or 0
        dbl    += g["doubles"]    or 0
        hit    += g["hits"]       or 0
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
    k_mult    = min(2.0, max(0.5, sp_k9 / LEAGUE_AVG_K9))
    era_adj   = min(1.25, max(0.75, sp_era / LEAGUE_AVG_ERA))
    new_k     = min(0.40, dist["k"] * k_mult)
    k_delta   = new_k - dist["k"]
    hit_sum   = dist["hr"] + dist["trip"] + dist["dbl"] + dist["s1b"]
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
    for key in list(raw):
        raw[key] *= norm
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


# ── Monte Carlo simulation ────────────────────────────────────────────────────

def sample_pa(dist):
    r = random.random()
    c = dist["hr"]
    if r < c: return "hr"
    c += dist["trip"]
    if r < c: return "trip"
    c += dist["dbl"]
    if r < c: return "dbl"
    c += dist["s1b"]
    if r < c: return "s1b"
    c += dist["bb"]
    if r < c: return "bb"
    c += dist["k"]
    if r < c: return "k"
    return "out"


def simulate_half_inning(dists, start):
    outs = runs = 0
    b = [False, False, False]  # 1B, 2B, 3B occupied
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
    h_runs = a_runs = h_bat = a_bat = 0
    h_inn = []
    a_inn = []
    for _ in range(9):
        ar, a_bat = simulate_half_inning(away_dists, a_bat)
        hr, h_bat = simulate_half_inning(home_dists, h_bat)
        a_inn.append(ar); h_inn.append(hr)
        h_runs += hr; a_runs += ar
    return h_runs, a_runs, h_inn, a_inn


def run_monte_carlo(home_dists, away_dists, n=10_000):
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
        "home_median":    pct(home_totals, 0.5),
        "home_p10":       pct(home_totals, 0.1),
        "home_p90":       pct(home_totals, 0.9),
        "away_median":    pct(away_totals, 0.5),
        "away_p10":       pct(away_totals, 0.1),
        "away_p90":       pct(away_totals, 0.9),
        "home_win_prob":  round(home_wins / n, 3),
        "home_inn_means": [round(v / n, 3) for v in home_inn_acc],
        "away_inn_means": [round(v / n, 3) for v in away_inn_acc],
    }
