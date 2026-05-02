"""
fetch_kalshi_outcomes.py
Two jobs in one:
  1. Fill in actual outcomes for yesterday's logged disagreements
  2. Log new disagreements from today's backtest_results vs Kalshi market prices

Run nightly AFTER fetch_backtest_cache.py.
Usage: python fetch_kalshi_outcomes.py
"""
import psycopg2, os, json, math, time, hmac, hashlib, base64, requests
from datetime import date, timedelta
from dotenv import load_dotenv
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.backends import default_backend

load_dotenv()

SEASON      = 2026
GAP_THRESH  = 0.10   # min gap to log
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
MONTHS      = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

# ── Kalshi auth ────────────────────────────────────────────────────────────
def kalshi_headers(method, path):
    key_pem = os.getenv("KALSHI_PRIVATE_KEY", "").replace("\\n", "\n")
    private_key = serialization.load_pem_private_key(key_pem.encode(), password=None, backend=default_backend())
    ts  = str(int(time.time() * 1000))
    msg = f"{ts}{method.upper()}/trade-api/v2{path}".encode()
    sig = private_key.sign(msg, padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=32), hashes.SHA256())
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
today     = date.today()
yesterday = today - timedelta(days=1)

# ── Job 1: Fill outcomes for yesterday's logged rows ──────────────────────
print("Job 1: filling outcomes for yesterday's games...")
cur.execute("""
    SELECT id, game_pk, home_team, away_team, model_pick, kalshi_pick, cycle_pick
    FROM kalshi_tracker
    WHERE game_date = %s AND actual_winner IS NULL AND game_pk IS NOT NULL
""", (yesterday,))
pending = cur.fetchall()
print(f"  {len(pending)} rows need outcomes")

for row_id, game_pk, home_team, away_team, model_pick, kalshi_pick, cycle_pick in pending:
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
                cycle_correct  = %s
            WHERE id = %s
        """, (
            actual_winner,
            model_pick  == actual_winner if model_pick  else None,
            kalshi_pick == actual_winner if kalshi_pick else None,
            cycle_pick  == actual_winner if cycle_pick  else None,
            row_id,
        ))
        print(f"  {home_team} vs {away_team}: {actual_winner} wins  model={'OK' if model_pick==actual_winner else 'WRONG'}  kalshi={'OK' if kalshi_pick==actual_winner else 'WRONG'}")
    except Exception as e:
        print(f"  ERR game {game_pk}: {e}")

conn.commit()

# ── Job 2: Log today's disagreements from backtest_results + Kalshi ───────
print("\nJob 2: logging today's disagreements...")

# Load today's backtest game rows for all teams
cur.execute("""
    SELECT team, game_rows FROM backtest_results
    WHERE run_date = %s AND season = %s
""", (today, SEASON))
bt_rows = cur.fetchall()
print(f"  {len(bt_rows)} team backtest rows")

# Fetch today's Kalshi KXMLBGAME markets
try:
    mk_data = kalshi_get("/markets?limit=200&series_ticker=KXMLBGAME&status=open")
    markets = mk_data.get("markets", [])
    ev_data = kalshi_get("/events?limit=100&series_ticker=KXMLBGAME&status=open")
    events  = ev_data.get("events", [])
    print(f"  {len(markets)} Kalshi markets, {len(events)} events")
except Exception as e:
    print(f"  Kalshi fetch failed: {e}")
    cur.close(); conn.close(); exit(1)

# Index markets by event ticker
mkt_by_event = {}
for m in markets:
    mkt_by_event.setdefault(m["event_ticker"], []).append(m)

# Team abbr map (same as kalshi.js)
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

def find_kalshi_market(home_team, away_team, game_date):
    home_abbrs = TEAM_ABBRS.get(home_team, [])
    away_abbrs = TEAM_ABBRS.get(away_team, [])
    if not home_abbrs or not away_abbrs:
        return None, None
    yr, mo, dy = str(game_date).split("-")
    date_prefix = yr[2:] + MONTHS[int(mo)-1] + dy
    for ev in events:
        ticker = ev["event_ticker"].upper()
        if not any(h.upper() in ticker for h in home_abbrs): continue
        if not any(a.upper() in ticker for a in away_abbrs): continue
        if date_prefix not in ticker: continue
        # Found event — find home team market
        mks = mkt_by_event.get(ev["event_ticker"], [])
        home_words = [w.lower() for w in home_team.split() if len(w) > 2]
        away_words = [w.lower() for w in away_team.split() if len(w) > 2]
        home_mkt = next((m for m in mks if any(w in (m.get("yes_sub_title","")).lower() for w in home_words)), None)
        if not home_mkt:
            home_mkt = mks[0] if mks else None
        if not home_mkt:
            return None, None
        yes_sub = (home_mkt.get("yes_sub_title","")).lower()
        yes_is_away = any(w in yes_sub for w in away_words) and not any(w in yes_sub for w in home_words)
        ask = float(home_mkt.get("yes_ask_dollars",0) or 0)
        bid = float(home_mkt.get("yes_bid_dollars",0) or 0)
        mid = (ask + bid) / 2 if ask and bid else (ask or bid)
        home_prob = round(1 - mid, 3) if yes_is_away else round(mid, 3)
        return home_prob, ev["event_ticker"]
    return None, None

# Deduplicate by game_pk (backtest has each game from both teams' perspectives)
logged_pks = set()
logged = 0

for team, game_rows_raw in bt_rows:
    data = game_rows_raw if isinstance(game_rows_raw, dict) else json.loads(game_rows_raw)
    games = data.get("games", [])
    for g in games:
        gdate = date.fromisoformat(g["date"]) if g.get("date") else today
        if gdate != today:
            continue
        game_pk = g.get("game_pk")
        if game_pk in logged_pks:
            continue

        # Determine home/away from game row
        is_home = g.get("is_home", True)
        if is_home:
            home_team, away_team = team, g.get("opponent","")
        else:
            home_team, away_team = g.get("opponent",""), team

        # Get our model's home win prob from the actual prediction
        act = g.get("actual") or {}
        model_home_prob = act.get("win_prob") if is_home else (1 - act.get("win_prob", 0.5) if act else None)
        if model_home_prob is None:
            continue
        model_home_prob = round(float(model_home_prob), 3)
        model_confidence = abs(model_home_prob - 0.5) * 2

        # Get Kalshi odds
        kalshi_home_prob, event_ticker = find_kalshi_market(home_team, away_team, gdate)
        if kalshi_home_prob is None:
            continue

        prob_gap = round(model_home_prob - kalshi_home_prob, 3)
        if abs(prob_gap) < GAP_THRESH:
            continue

        # Determine picks
        model_pick  = "home" if model_home_prob >= 0.5 else "away"
        kalshi_pick = "home" if kalshi_home_prob >= 0.5 else "away"
        signal_type = "strong_edge" if model_confidence >= 0.25 else "disagreement"

        try:
            cur.execute("""
                INSERT INTO kalshi_tracker
                (game_date, season, home_team, away_team, game_pk,
                 model_home_prob, model_away_prob, model_pick, model_confidence,
                 kalshi_home_prob, kalshi_away_prob, kalshi_pick,
                 prob_gap, signal_type)
                VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s, %s,%s)
                ON CONFLICT (game_pk, game_date) DO UPDATE SET
                    model_home_prob  = EXCLUDED.model_home_prob,
                    kalshi_home_prob = EXCLUDED.kalshi_home_prob,
                    prob_gap         = EXCLUDED.prob_gap,
                    signal_type      = EXCLUDED.signal_type
            """, (
                gdate, SEASON, home_team, away_team, game_pk,
                model_home_prob, round(1-model_home_prob,3), model_pick, round(model_confidence,3),
                kalshi_home_prob, round(1-kalshi_home_prob,3), kalshi_pick,
                prob_gap, signal_type,
            ))
            logged_pks.add(game_pk)
            logged += 1
            print(f"  Logged: {away_team} @ {home_team}  gap={prob_gap:+.2f}  signal={signal_type}")
        except Exception as e:
            print(f"  ERR logging {home_team}: {e}")

conn.commit()
cur.close()
conn.close()
print(f"\nDone: {logged} disagreements logged, {len(pending)} outcomes filled")
