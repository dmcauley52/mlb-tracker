"""
fetch_kalshi_outcomes.py
Modes via MODE env var:
  morning        (default) -- log today's games with model/cycle/kalshi/vegas picks
  pregame                  -- update rows for games starting in next 90 min
  outcomes                 -- fill actual outcomes for yesterday's logged rows
  backfill-cycle           -- backfill cycle_pick + cycle_correct for historical rows

Schedules:
  kalshi_morning.yml  14:00 UTC (10 AM ET)      -- MODE=morning
  kalshi_pregame.yml  hourly 16:00-02:00 UTC    -- MODE=pregame
  nightly.yml         09:00 UTC (5 AM ET)       -- MODE=outcomes
"""
import psycopg2, os, math, time, base64, requests
from datetime import date, datetime, timedelta, timezone
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding as asym_padding
from cryptography.hazmat.backends import default_backend
from model_config import (
    LEAGUE_AVG_ERA,
    OPP_RUNS_BASE,
    SEASON,
    WIN_PCT_RUN_SCALE,
    WIN_PROB_SIGMOID_SCALE,
)

load_dotenv()

MODE        = os.getenv("MODE", "morning")
GAP_THRESH  = 0.10
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
ODDS_API_KEY = os.getenv("ODDS_API_KEY", "")
MONTHS      = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
# WOBA_RUN_SCALE / MAX_RUNS loaded from model_weights table after DB connect.
from model_weights import load_weights, FALLBACK_BACKTEST
WOBA_RUN_SCALE  = FALLBACK_BACKTEST["woba_run_scale"]
MAX_RUNS        = FALLBACK_BACKTEST["max_predicted_runs"]

# ── Cycle scoring constants (mirrors analytics.js) ────────────────────────
MIN_PERIOD      = 4
MIN_AMPLITUDE   = 0.005
MAX_COMPONENTS  = 5
FORECAST_DAYS   = 14

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
    game_data: list of dicts with 'avg' (batting avg or wOBA per game).
    Returns dict with forecastValues, mean, dominantCycles, or None if insufficient.
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
    total_power = sum(c['amp']**2 for c in kept) or 1.0
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
    Returns {'score': 0-99, 'tier': str, 'breakdown': dict} mirroring
    computePredictionScore in analytics.js (no matchup/splits component).
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

    # Trend score: last-5 avg vs midpoint (0-25)
    last5     = game_data[-5:]
    last5_val = sum(sig(g) for g in last5) / len(last5)
    sig_mid   = 0.315  # wOBA midpoint
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
            'matchupScore':  5,  # neutral default (no SP data in Python)
        },
    }

def compute_cycle_edge(player_scores):
    """
    player_scores: list of score dicts from compute_player_score.
    Returns {'score': 0-100, 'hotCount': int, 'totalCount': int} or None.
    Mirrors computeCycleEdge in analytics.js.
    """
    valid = [p for p in player_scores if p is not None]
    if not valid:
        return None
    n = len(valid)
    avg_phase   = sum(p['breakdown']['phaseScore']    for p in valid) / n
    avg_trend   = sum(p['breakdown']['trendScore']    for p in valid) / n
    avg_matchup = sum(p['breakdown'].get('matchupScore', 5) for p in valid) / n
    hot_count   = sum(1 for p in valid if p['tier'] in ('hot', 'warm'))
    phase_norm   = (avg_phase   / 30)  * 100
    trend_norm   = (avg_trend   / 25)  * 100
    heat_norm    = (hot_count   / n)   * 100
    matchup_norm = (avg_matchup / 10)  * 100
    raw = phase_norm * 0.40 + trend_norm * 0.30 + heat_norm * 0.20 + matchup_norm * 0.10
    return {'score': round(raw), 'hotCount': hot_count, 'totalCount': n}

def cycle_edge_prob(home_score, away_score):
    """Logistic sigmoid on score diff → home win probability (mirrors JS)."""
    diff = (home_score - away_score) / 100.0
    raw  = 1 / (1 + math.exp(-diff * 3.0))
    return round(raw, 3)

# ── Kalshi auth ────────────────────────────────────────────────────────────
def _load_private_key():
    key_pem = os.getenv("KALSHI_PRIVATE_KEY", "")
    if "\\n" in key_pem:
        key_pem = key_pem.replace("\\n", "\n")
    if len(key_pem) < 100:
        try:
            with open("kalshi_private.pem") as f:
                key_pem = f.read()
        except FileNotFoundError:
            pass
    return serialization.load_pem_private_key(key_pem.encode(), password=None, backend=default_backend())

def kalshi_headers(method, path):
    private_key = _load_private_key()
    ts  = str(int(time.time() * 1000))
    msg = f"{ts}{method.upper()}/trade-api/v2{path}".encode()
    sig = private_key.sign(msg, asym_padding.PSS(mgf=asym_padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
    return {
        "Content-Type":            "application/json",
        "KALSHI-ACCESS-KEY":       os.getenv("KALSHI_API_KEY_ID", ""),
        "KALSHI-ACCESS-TIMESTAMP": ts,
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(sig).decode(),
    }

def kalshi_get(path):
    r = requests.get(KALSHI_BASE + path, headers=kalshi_headers("GET", path))
    r.raise_for_status()
    return r.json()

# ── DB ─────────────────────────────────────────────────────────────────────
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()
_w = load_weights(cur, "backtest", FALLBACK_BACKTEST)
WOBA_RUN_SCALE = _w["woba_run_scale"]
MAX_RUNS       = _w["max_predicted_runs"]
print(f"Loaded weights: woba_run_scale={WOBA_RUN_SCALE} max_runs={MAX_RUNS}")
today     = date.today()
tomorrow  = today + timedelta(days=1)
yesterday = today - timedelta(days=1)
now_utc   = datetime.now(timezone.utc)

print(f"Mode: {MODE}  Date: {today}")

# ── Team abbr map ──────────────────────────────────────────────────────────
TEAM_ABBRS = {
    "Arizona Diamondbacks":   ["AZ","ARI"],   "Atlanta Braves":         ["ATL"],
    "Baltimore Orioles":      ["BAL"],        "Boston Red Sox":         ["BOS"],
    "Chicago Cubs":           ["CHC"],        "Chicago White Sox":      ["CWS"],
    "Cincinnati Reds":        ["CIN"],        "Cleveland Guardians":    ["CLE"],
    "Colorado Rockies":       ["COL"],        "Detroit Tigers":         ["DET"],
    "Houston Astros":         ["HOU"],        "Kansas City Royals":     ["KC","KCR"],
    "Los Angeles Angels":     ["LAA"],        "Los Angeles Dodgers":    ["LAD"],
    "Miami Marlins":          ["MIA"],        "Milwaukee Brewers":      ["MIL"],
    "Minnesota Twins":        ["MIN"],        "New York Mets":          ["NYM"],
    "New York Yankees":       ["NYY"],        "Athletics":              ["ATH","OAK"],
    "Philadelphia Phillies":  ["PHI"],        "Pittsburgh Pirates":     ["PIT"],
    "San Diego Padres":       ["SD","SDP"],   "San Francisco Giants":   ["SF","SFG"],
    "Seattle Mariners":       ["SEA"],        "St. Louis Cardinals":    ["STL"],
    "Tampa Bay Rays":         ["TB","TBR"],   "Texas Rangers":          ["TEX"],
    "Toronto Blue Jays":      ["TOR"],        "Washington Nationals":   ["WSH","WAS"],
}

# ── Kalshi market lookup ───────────────────────────────────────────────────
def find_kalshi_prob(home_team, away_team, game_date, events, mkt_by_event):
    home_abbrs = TEAM_ABBRS.get(home_team, [])
    away_abbrs = TEAM_ABBRS.get(away_team, [])
    if not home_abbrs or not away_abbrs:
        return None
    yr, mo, dy = str(game_date).split("-")
    date_prefix = yr[2:] + MONTHS[int(mo)-1] + dy
    for ev in events:
        ticker = ev["event_ticker"].upper()
        if not any(h.upper() in ticker for h in home_abbrs):
            continue
        if not any(a.upper() in ticker for a in away_abbrs):
            continue
        if date_prefix not in ticker:
            continue
        mks = mkt_by_event.get(ev["event_ticker"], [])
        home_words = [w.lower() for w in home_team.split() if len(w) > 2]
        away_words = [w.lower() for w in away_team.split() if len(w) > 2]
        home_mkt = next(
            (m for m in mks if any(w in (m.get("yes_sub_title","")).lower() for w in home_words)),
            mks[0] if mks else None
        )
        if not home_mkt:
            return None
        yes_sub    = (home_mkt.get("yes_sub_title","")).lower()
        yes_is_away = (any(w in yes_sub for w in away_words)
                       and not any(w in yes_sub for w in home_words))
        ask = float(home_mkt.get("yes_ask_dollars") or 0)
        bid = float(home_mkt.get("yes_bid_dollars") or 0)
        mid = (ask + bid) / 2 if ask and bid else (ask or bid)
        return round(1 - mid, 3) if yes_is_away else round(mid, 3)
    return None

# ── Run model ─────────────────────────────────────────────────────────────
def model_home_prob(home_woba, away_woba, home_win_pct, away_win_pct, home_sp_era, away_sp_era):
    def pred_runs(woba, my_wpc, opp_era):
        era_f  = max(0.80, min(1.30, opp_era / LEAGUE_AVG_ERA))
        adj    = era_f * _w["opp_era_scale"] + 1.0 * (1 - _w["opp_era_scale"])
        team_q = max(0.88, min(1.12, 1.0 + (my_wpc - 0.500) * 0.5))
        return min(woba * WOBA_RUN_SCALE * adj * team_q, MAX_RUNS)
    hr    = pred_runs(home_woba, home_win_pct, away_sp_era)
    ar    = pred_runs(away_woba, away_win_pct, home_sp_era)
    opp_h = OPP_RUNS_BASE + (away_win_pct - 0.500) * WIN_PCT_RUN_SCALE
    opp_a = OPP_RUNS_BASE + (home_win_pct - 0.500) * WIN_PCT_RUN_SCALE
    rh    = 1 / (1 + math.exp(-(hr - opp_h) * WIN_PROB_SIGMOID_SCALE))
    ra    = 1 / (1 + math.exp(-(ar - opp_a) * WIN_PROB_SIGMOID_SCALE))
    s     = rh + ra
    return round(rh / s, 3) if s > 0 else 0.500

# ── The Odds API ──────────────────────────────────────────────────────────
def fetch_vegas_odds():
    """Returns {(home_team, away_team): home_prob} using DraftKings h2h lines."""
    if not ODDS_API_KEY:
        return {}
    try:
        r = requests.get(
            "https://api.the-odds-api.com/v4/sports/baseball_mlb/odds",
            params={
                "apiKey":   ODDS_API_KEY,
                "regions":  "us",
                "markets":  "h2h",
                "bookmakers": "draftkings",
                "oddsFormat": "american",
            },
            timeout=10,
        )
        r.raise_for_status()
        result = {}
        for game in r.json():
            home = game.get("home_team", "")
            away = game.get("away_team", "")
            for bm in game.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    if mkt.get("key") != "h2h":
                        continue
                    probs = {}
                    for outcome in mkt.get("outcomes", []):
                        american = outcome.get("price", 0)
                        if american >= 100:
                            p = 100 / (american + 100)
                        else:
                            p = abs(american) / (abs(american) + 100)
                        probs[outcome["name"]] = p
                    total = sum(probs.values())
                    if total > 0:
                        home_prob = round(probs.get(home, 0) / total, 3)
                        result[(home, away)] = home_prob
        print(f"  Vegas odds: {len(result)} games")
        return result
    except Exception as e:
        print(f"  Odds API fetch failed: {e}")
        return {}

def find_vegas_prob(home_team, away_team, vegas_odds):
    """Match full team names to Odds API entries (fuzzy last-word match)."""
    direct = vegas_odds.get((home_team, away_team))
    if direct is not None:
        return direct
    home_last = home_team.split()[-1].lower()
    away_last = away_team.split()[-1].lower()
    for (h, a), prob in vegas_odds.items():
        if home_last in h.lower() and away_last in a.lower():
            return prob
    return None

# ════════════════════════════════════════════════════════════════════════════
# OUTCOMES — fill actual results for yesterday's logged rows
# ════════════════════════════════════════════════════════════════════════════
if MODE in ("outcomes", "morning", "nightly"):
    print("Filling outcomes for yesterday's games...")
    cur.execute("""
        SELECT id, game_pk, home_team, away_team, model_pick, kalshi_pick, cycle_pick, vegas_pick
        FROM kalshi_tracker
        WHERE game_date = %s AND actual_winner IS NULL AND game_pk IS NOT NULL
    """, (yesterday,))
    pending = cur.fetchall()
    print(f"  {len(pending)} rows need outcomes")
    for row_id, game_pk, home_team, away_team, model_pick, kalshi_pick, cycle_pick, vegas_pick in pending:
        try:
            r = requests.get(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/linescore")
            d = r.json()
            home_score = d.get("teams", {}).get("home", {}).get("runs")
            away_score = d.get("teams", {}).get("away", {}).get("runs")
            if home_score is None or away_score is None:
                continue
            actual_winner = "home" if home_score > away_score else "away"
            cur.execute("""
                UPDATE kalshi_tracker SET
                    actual_winner  = %s,
                    model_correct  = %s,
                    kalshi_correct = %s,
                    cycle_correct  = %s,
                    vegas_correct  = %s
                WHERE id = %s
            """, (
                actual_winner,
                model_pick  == actual_winner if model_pick  else None,
                kalshi_pick == actual_winner if kalshi_pick else None,
                cycle_pick  == actual_winner if cycle_pick  else None,
                vegas_pick  == actual_winner if vegas_pick  else None,
                row_id,
            ))
            print(f"  {home_team} vs {away_team}: {actual_winner}  model={'OK' if model_pick==actual_winner else 'WRONG'}")
        except Exception as e:
            print(f"  ERR game {game_pk}: {e}")
    conn.commit()

# ════════════════════════════════════════════════════════════════════════════
# BACKFILL-CYCLE — compute cycle_pick + cycle_correct for historical rows
# ════════════════════════════════════════════════════════════════════════════
if MODE == "backfill-cycle":
    print("Backfilling cycle_pick for historical rows...")

    # Load player gamelogs for cycle scoring
    cur.execute("""
        SELECT player_name, team, game_date, batting_avg, woba, ops
        FROM player_gamelogs
        WHERE season = %s AND at_bats >= 1
        ORDER BY player_name, game_date ASC
    """, (SEASON,))
    _bf_logs = {}
    for name, team, gdate, avg, woba, ops in cur.fetchall():
        _bf_logs.setdefault(name, {'team': team, 'games': []})
        _bf_logs[name]['games'].append({
            'avg':  float(avg  or 0),
            'woba': float(woba or 0) if woba else None,
        })

    cur.execute("""
        SELECT DISTINCT ON (player_name) player_name, ops
        FROM player_gamelogs
        WHERE season = %s AND ops IS NOT NULL
        ORDER BY player_name, game_date DESC
    """, (SEASON,))
    _bf_ops = {r[0]: float(r[1]) for r in cur.fetchall() if r[1]}

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
    _bf_woba = {r[0]: float(r[1]) for r in cur.fetchall() if r[1]}

    def _bf_cycle_pick(team_name):
        players = [(n, _bf_woba.get(n, 0))
                   for n, info in _bf_logs.items()
                   if info['team'] == team_name and n in _bf_woba]
        players.sort(key=lambda x: -x[1])
        top9 = [n for n, _ in players[:9]]
        if len(top9) < 3:
            return None
        scores = []
        for name in top9:
            info = _bf_logs.get(name)
            if not info:
                continue
            games = info['games'][-40:]
            ops   = _bf_ops.get(name, 0.720)
            scores.append(compute_player_score(games, ops))
        return compute_cycle_edge(scores)

    # Fetch rows missing cycle_pick
    cur.execute("""
        SELECT id, home_team, away_team, actual_winner
        FROM kalshi_tracker
        WHERE cycle_pick IS NULL
        ORDER BY game_date ASC
    """)
    rows = cur.fetchall()
    print(f"  {len(rows)} rows need cycle_pick")

    updated = 0
    for row_id, home_team, away_team, actual_winner in rows:
        home_ce = _bf_cycle_pick(home_team)
        away_ce = _bf_cycle_pick(away_team)
        if not home_ce or not away_ce:
            continue
        ce_prob = cycle_edge_prob(home_ce['score'], away_ce['score'])
        cpick   = "home" if ce_prob >= 0.5 else "away"
        ccorrect = (cpick == actual_winner) if actual_winner else None
        cur.execute("""
            UPDATE kalshi_tracker
            SET cycle_pick    = %s,
                cycle_correct = %s
            WHERE id = %s
        """, (cpick, ccorrect, row_id))
        updated += 1
        print(f"  {away_team} @ {home_team}: cycle={cpick}  correct={ccorrect}")

    conn.commit()
    print(f"  {updated} rows updated")

# ════════════════════════════════════════════════════════════════════════════
# MORNING + PREGAME — fetch schedule, run model, compare Kalshi
# ════════════════════════════════════════════════════════════════════════════
if MODE in ("morning", "pregame"):
    print(f"\nFetching team stats + schedule...")

    # Team stats
    cur.execute("""
        SELECT team_name, team_id, win_pct, l10_win_pct, season_era
        FROM team_stats_cache WHERE season = %s
    """, (SEASON,))
    team_stats = {
        r[0]: {"id": r[1], "win_pct": float(r[2] or 0.500),
               "l10_win_pct": float(r[3] or 0.500), "era": float(r[4] or 4.20)}
        for r in cur.fetchall()
    }
    team_by_id = {v["id"]: k for k, v in team_stats.items()}

    # Player wOBA (recent-weighted)
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

    # Team avg wOBA from top-9
    cur.execute("""
        SELECT player_name, team FROM player_gamelogs
        WHERE season = %s GROUP BY player_name, team
    """, (SEASON,))
    team_roster_woba = {}
    for name, team in cur.fetchall():
        if name in player_woba:
            team_roster_woba.setdefault(team, []).append(player_woba[name])

    def team_avg_woba(team_name):
        wobas = sorted(team_roster_woba.get(team_name, []), reverse=True)[:9]
        return round(sum(wobas) / len(wobas), 4) if wobas else 0.320

    # Player gamelogs for cycle scoring (last 40 games, ordered asc)
    cur.execute("""
        SELECT player_name, team, game_date, batting_avg, woba, ops
        FROM player_gamelogs
        WHERE season = %s AND at_bats >= 1
        ORDER BY player_name, game_date ASC
    """, (SEASON,))
    player_gamelogs_raw = {}
    for name, team, gdate, avg, woba, ops in cur.fetchall():
        player_gamelogs_raw.setdefault(name, {'team': team, 'games': [], 'ops': ops})
        player_gamelogs_raw[name]['games'].append({
            'avg':  float(avg  or 0),
            'woba': float(woba or 0) if woba else None,
        })
    # Keep last 40 games per player and store latest OPS
    cur.execute("""
        SELECT DISTINCT ON (player_name) player_name, ops
        FROM player_gamelogs
        WHERE season = %s AND ops IS NOT NULL
        ORDER BY player_name, game_date DESC
    """, (SEASON,))
    player_ops = {r[0]: float(r[1]) for r in cur.fetchall() if r[1]}

    def team_cycle_pick(team_name):
        """Compute CycleEdge score for a team's top-9 players and return pick dict."""
        # Get top-9 players by avg wOBA for this team
        players = [(name, player_woba.get(name, 0))
                   for name, info in player_gamelogs_raw.items()
                   if info['team'] == team_name and name in player_woba]
        players.sort(key=lambda x: -x[1])
        top9 = [name for name, _ in players[:9]]
        if len(top9) < 3:
            return None
        scores = []
        for name in top9:
            info = player_gamelogs_raw.get(name)
            if not info:
                continue
            games = info['games'][-40:]  # last 40 games
            ops   = player_ops.get(name, 0.720)
            scores.append(compute_player_score(games, ops))
        return compute_cycle_edge(scores)

    # MLB schedule — today + tomorrow
    sched_r = requests.get(
        f"https://statsapi.mlb.com/api/v1/schedule?sportId=1"
        f"&startDate={today}&endDate={tomorrow}&hydrate=probablePitcher,teams"
    )
    mlb_games = []
    for d in (sched_r.json().get("dates") or []):
        game_date_str = d.get("date", str(today))
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") == "Final":
                continue
            home_id   = g["teams"]["home"]["team"]["id"]
            away_id   = g["teams"]["away"]["team"]["id"]
            home_name = team_by_id.get(home_id) or g["teams"]["home"]["team"]["name"]
            away_name = team_by_id.get(away_id) or g["teams"]["away"]["team"]["name"]
            home_sp_era = team_stats.get(home_name, {}).get("era", LEAGUE_AVG_ERA)
            away_sp_era = team_stats.get(away_name, {}).get("era", LEAGUE_AVG_ERA)
            for side, attr in [("home", "home_sp_era"), ("away", "away_sp_era")]:
                sp = g["teams"][side].get("probablePitcher")
                if sp:
                    try:
                        sr = requests.get(
                            f"https://statsapi.mlb.com/api/v1/people/{sp['id']}/stats"
                            f"?stats=season&group=pitching&season={SEASON}"
                        )
                        splits = sr.json().get("stats", [{}])[0].get("splits", [])
                        if splits:
                            era = float(splits[0].get("stat", {}).get("era", LEAGUE_AVG_ERA))
                            if attr == "home_sp_era":
                                home_sp_era = era
                            else:
                                away_sp_era = era
                    except:
                        pass

            # Try boxscore for confirmed lineup wOBAs
            lineup_source     = "estimated"
            home_lineup_woba  = team_avg_woba(home_name)
            away_lineup_woba  = team_avg_woba(away_name)
            try:
                bs = requests.get(
                    f"https://statsapi.mlb.com/api/v1/game/{g['gamePk']}/boxscore"
                ).json()
                for side, tname in [("home", home_name), ("away", away_name)]:
                    bside = bs.get("teams", {}).get(side, {})
                    order = bside.get("battingOrder", [])
                    if len(order) >= 9:
                        lineup_source = "actual"
                        pdata = bside.get("players", {})
                        wobas = [
                            player_woba.get(
                                pdata.get(f"ID{pid}", {}).get("person", {}).get("fullName", ""),
                                0.320
                            )
                            for pid in order[:9]
                        ]
                        avg = round(sum(wobas) / len(wobas), 4)
                        if side == "home":
                            home_lineup_woba = avg
                        else:
                            away_lineup_woba = avg
            except:
                pass

            mlb_games.append({
                "game_pk":          g["gamePk"],
                "game_date":        game_date_str,
                "home_team":        home_name,
                "away_team":        away_name,
                "home_sp_era":      home_sp_era,
                "away_sp_era":      away_sp_era,
                "home_lineup_woba": home_lineup_woba,
                "away_lineup_woba": away_lineup_woba,
                "lineup_source":    lineup_source,
                "game_time_utc":    g.get("gameDate"),
            })
    print(f"  {len(mlb_games)} games (today + tomorrow)")

    # Fetch Vegas odds (morning only — one API call covers all games)
    vegas_odds = fetch_vegas_odds() if MODE == "morning" else {}

    # Fetch Kalshi markets
    try:
        mk_data = kalshi_get("/markets?limit=200&series_ticker=KXMLBGAME&status=open")
        ev_data = kalshi_get("/events?limit=100&series_ticker=KXMLBGAME&status=open")
        markets = mk_data.get("markets", [])
        events  = ev_data.get("events", [])
        print(f"  {len(markets)} Kalshi markets, {len(events)} events")
    except Exception as e:
        print(f"  Kalshi fetch failed: {e}")
        cur.close(); conn.close(); exit(1)

    mkt_by_event = {}
    for m in markets:
        mkt_by_event.setdefault(m["event_ticker"], []).append(m)

    logged = 0
    for g in mlb_games:
        game_pk   = g["game_pk"]
        game_date = g["game_date"]
        home_team = g["home_team"]
        away_team = g["away_team"]

        ht = team_stats.get(home_team, {})
        at = team_stats.get(away_team, {})
        hwp = ht.get("l10_win_pct", 0.5) * 0.5 + ht.get("win_pct", 0.5) * 0.5
        awp = at.get("l10_win_pct", 0.5) * 0.5 + at.get("win_pct", 0.5) * 0.5

        mhp  = model_home_prob(g["home_lineup_woba"], g["away_lineup_woba"],
                               hwp, awp, g["home_sp_era"], g["away_sp_era"])
        conf = round(abs(mhp - 0.5) * 2, 3)
        pick = "home" if mhp >= 0.5 else "away"
        sig  = "strong_edge" if conf >= 0.25 else "disagreement"

        khp = find_kalshi_prob(home_team, away_team, game_date, events, mkt_by_event)
        gap = round(mhp - khp, 3) if khp is not None else None

        # Cycle edge pick
        home_ce = team_cycle_pick(home_team)
        away_ce = team_cycle_pick(away_team)
        if home_ce and away_ce:
            ce_prob        = cycle_edge_prob(home_ce['score'], away_ce['score'])
            cpick          = "home" if ce_prob >= 0.5 else "away"
            cycle_home_sc  = home_ce['score']
            cycle_away_sc  = away_ce['score']
            cycle_home_p   = ce_prob
        else:
            cpick = None
            cycle_home_sc = cycle_away_sc = cycle_home_p = None

        # ── MORNING: insert row for every game (need Vegas for all games) ──
        if MODE == "morning":
            vhp = find_vegas_prob(home_team, away_team, vegas_odds)
            vap   = round(1 - vhp, 3) if vhp is not None else None
            vpick = ("home" if vhp >= 0.5 else "away") if vhp is not None else None
            kap   = round(1 - khp, 3) if khp is not None else None
            kpick = ("home" if khp >= 0.5 else "away") if khp is not None else None
            try:
                cur.execute("""
                    INSERT INTO kalshi_tracker
                    (game_date, season, home_team, away_team, game_pk,
                     model_home_prob, model_away_prob, model_pick, model_confidence,
                     kalshi_home_prob, kalshi_away_prob, kalshi_pick,
                     vegas_home_prob, vegas_away_prob, vegas_pick,
                     cycle_pick, cycle_home_score, cycle_away_score, cycle_home_prob, cycle_away_prob,
                     prob_gap, signal_type, lineup_source, game_time_utc)
                    VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s,%s,%s, %s,%s,%s,%s)
                    ON CONFLICT (game_pk, game_date) DO UPDATE SET
                        model_home_prob   = EXCLUDED.model_home_prob,
                        model_away_prob   = EXCLUDED.model_away_prob,
                        kalshi_home_prob  = COALESCE(EXCLUDED.kalshi_home_prob, kalshi_tracker.kalshi_home_prob),
                        kalshi_away_prob  = COALESCE(EXCLUDED.kalshi_away_prob, kalshi_tracker.kalshi_away_prob),
                        kalshi_pick       = COALESCE(EXCLUDED.kalshi_pick,      kalshi_tracker.kalshi_pick),
                        vegas_home_prob   = COALESCE(EXCLUDED.vegas_home_prob,  kalshi_tracker.vegas_home_prob),
                        vegas_away_prob   = COALESCE(EXCLUDED.vegas_away_prob,  kalshi_tracker.vegas_away_prob),
                        vegas_pick        = COALESCE(EXCLUDED.vegas_pick,       kalshi_tracker.vegas_pick),
                        cycle_pick        = COALESCE(EXCLUDED.cycle_pick,       kalshi_tracker.cycle_pick),
                        cycle_home_score  = COALESCE(EXCLUDED.cycle_home_score, kalshi_tracker.cycle_home_score),
                        cycle_away_score  = COALESCE(EXCLUDED.cycle_away_score, kalshi_tracker.cycle_away_score),
                        cycle_home_prob   = COALESCE(EXCLUDED.cycle_home_prob,  kalshi_tracker.cycle_home_prob),
                        cycle_away_prob   = COALESCE(EXCLUDED.cycle_away_prob,  kalshi_tracker.cycle_away_prob),
                        prob_gap          = EXCLUDED.prob_gap,
                        signal_type       = EXCLUDED.signal_type,
                        lineup_source     = EXCLUDED.lineup_source
                """, (
                    game_date, SEASON, home_team, away_team, game_pk,
                    mhp, round(1-mhp,3), pick, conf,
                    khp, kap, kpick,
                    vhp, vap, vpick,
                    cpick, cycle_home_sc, cycle_away_sc, cycle_home_p,
                    round(1 - cycle_home_p, 3) if cycle_home_p is not None else None,
                    gap, sig, g["lineup_source"], g["game_time_utc"],
                ))
                logged += 1
                kalshi_str = f"kalshi={khp}" if khp is not None else "kalshi=n/a"
                vegas_str  = f"vegas={vhp}"  if vhp is not None else "vegas=n/a"
                cycle_str  = f"cycle={cpick}" if cpick is not None else "cycle=n/a"
                print(f"  Morning: {away_team} @ {home_team}  {kalshi_str}  {vegas_str}  {cycle_str}")
            except Exception as e:
                print(f"  ERR {home_team}: {e}")

        # ── PREGAME: update row for games starting in next 90 min with confirmed lineup ──
        elif MODE == "pregame":
            gtime = g.get("game_time_utc")
            if not gtime:
                continue
            try:
                game_dt = datetime.fromisoformat(gtime.replace("Z", "+00:00"))
                mins    = (game_dt - now_utc).total_seconds() / 60
                if not (0 <= mins <= 90):
                    continue
            except:
                continue
            if g["lineup_source"] != "actual":
                continue
            try:
                cur.execute("""
                    UPDATE kalshi_tracker SET
                        pregame_model_home_prob  = %s,
                        pregame_model_away_prob  = %s,
                        pregame_kalshi_home_prob = %s,
                        pregame_kalshi_away_prob = %s,
                        pregame_model_pick       = %s,
                        pregame_signal_type      = %s,
                        pregame_logged_at        = NOW(),
                        lineup_source            = %s
                    WHERE game_pk = %s AND game_date = %s
                """, (
                    mhp, round(1-mhp,3),
                    khp, round(1-khp,3) if khp is not None else None,
                    pick, sig, g["lineup_source"],
                    game_pk, game_date,
                ))
                if cur.rowcount:
                    logged += 1
                    gap_str = f"gap={gap:+.2f}" if gap is not None else "gap=n/a"
                    print(f"  Pre-game: {away_team} @ {home_team}  model={mhp} kalshi={khp}  {gap_str}")
            except Exception as e:
                print(f"  ERR pregame {home_team}: {e}")

    conn.commit()
    print(f"  {logged} rows {'logged' if MODE=='morning' else 'updated'}")

cur.close()
conn.close()
print("Done.")
