"""
fetch_game_results.py
Fetches completed game results (scores, lineups, team wOBA) from the MLB Stats API
and upserts into game_results.

Modes (set MODE env var or edit DEFAULT_MODE):
  nightly   — yesterday only
  backfill  — full season from BACKFILL_START_DATE

Usage:
  python fetch_game_results.py
  MODE=backfill python fetch_game_results.py
"""
import psycopg2
import os
import json
import time
import urllib.request
from datetime import date, timedelta
from dotenv import load_dotenv
load_dotenv()

SEASON             = int(os.getenv("SEASON", 2026))
DEFAULT_MODE       = os.getenv("MODE", "nightly")
BACKFILL_START_DATE = os.getenv("BACKFILL_START", f"{SEASON}-03-20")

WOBA_WEIGHTS = {
    "bb": 0.690, "hbp": 0.722, "single": 0.888,
    "double": 1.271, "triple": 1.616, "hr": 2.101,
}

def mlb_get(path):
    url = "https://statsapi.mlb.com/api/v1" + path
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())

def compute_woba(s):
    bb  = s.get("baseOnBalls", 0) or 0
    ibb = s.get("intentionalWalks", 0) or 0
    hbp = s.get("hitByPitch", 0) or 0
    h   = s.get("hits", 0) or 0
    dbl = s.get("doubles", 0) or 0
    trp = s.get("triples", 0) or 0
    hr  = s.get("homeRuns", 0) or 0
    ab  = s.get("atBats", 0) or 0
    sf  = s.get("sacFlies", 0) or s.get("sacrificeFlies", 0) or 0
    ubb = bb - ibb
    s1b = h - dbl - trp - hr
    num = (WOBA_WEIGHTS["bb"] * ubb + WOBA_WEIGHTS["hbp"] * hbp +
           WOBA_WEIGHTS["single"] * s1b + WOBA_WEIGHTS["double"] * dbl +
           WOBA_WEIGHTS["triple"] * trp + WOBA_WEIGHTS["hr"] * hr)
    den = ab + ubb + hbp + sf
    return round(num / den, 4) if den > 0 else None

def parse_lineup_woba(side):
    """Returns (team_woba, lineup_list) from a boxscore side dict."""
    agg = dict(baseOnBalls=0, intentionalWalks=0, hitByPitch=0, hits=0,
               doubles=0, triples=0, homeRuns=0, atBats=0, sacFlies=0)
    lineup = []
    for idx, pid in enumerate(side.get("battingOrder", [])):
        ps  = side.get("players", {}).get(f"ID{pid}", {})
        st  = ps.get("stats", {}).get("batting", {})
        info = ps.get("person", {})
        for k in agg:
            agg[k] += st.get(k, 0) or 0
        lineup.append({
            "spot":      idx + 1,
            "player_id": pid,
            "name":      info.get("fullName", f"Player {pid}"),
        })
    return compute_woba(agg), lineup

def fetch_schedule(start_date, end_date):
    path = (f"/schedule?sportId=1&startDate={start_date}&endDate={end_date}"
            f"&hydrate=linescore,probablePitcher,boxscore&gameType=R")
    return mlb_get(path)

def process_date_range(cur, start_str, end_str):
    print(f"  Fetching schedule {start_str} → {end_str}…")
    data = fetch_schedule(start_str, end_str)
    saved = skipped = errors = 0

    for d in data.get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") != "Final":
                continue
            game_pk   = g["gamePk"]
            game_date = d["date"]
            home      = g["teams"]["home"]
            away      = g["teams"]["away"]

            home_score = home.get("score")
            away_score = away.get("score")
            if home_score is None or away_score is None:
                skipped += 1
                continue

            sp_home_id   = (home.get("probablePitcher") or {}).get("id")
            sp_away_id   = (away.get("probablePitcher") or {}).get("id")
            sp_home_name = (home.get("probablePitcher") or {}).get("fullName")
            sp_away_name = (away.get("probablePitcher") or {}).get("fullName")

            # Box score lineups — hydrated inline
            home_woba = away_woba = None
            home_lineup = away_lineup = None
            try:
                box = mlb_get(f"/game/{game_pk}/boxscore")
                bt  = box.get("teams", {})
                if bt.get("home"):
                    home_woba, home_lineup = parse_lineup_woba(bt["home"])
                if bt.get("away"):
                    away_woba, away_lineup = parse_lineup_woba(bt["away"])
                time.sleep(0.2)
            except Exception as e:
                print(f"    ⚠ boxscore {game_pk}: {e}")

            try:
                cur.execute("""
                    INSERT INTO game_results
                    (game_pk, game_date, season,
                     home_team, away_team, home_team_id, away_team_id,
                     home_score, away_score,
                     home_team_woba, away_team_woba,
                     sp_home_id, sp_away_id, sp_home_name, sp_away_name,
                     home_lineup, away_lineup, updated_at)
                    VALUES (%s,%s,%s, %s,%s,%s,%s, %s,%s, %s,%s, %s,%s,%s,%s, %s,%s, NOW())
                    ON CONFLICT (game_pk) DO UPDATE SET
                        home_score      = EXCLUDED.home_score,
                        away_score      = EXCLUDED.away_score,
                        home_team_woba  = EXCLUDED.home_team_woba,
                        away_team_woba  = EXCLUDED.away_team_woba,
                        sp_home_id      = EXCLUDED.sp_home_id,
                        sp_away_id      = EXCLUDED.sp_away_id,
                        sp_home_name    = EXCLUDED.sp_home_name,
                        sp_away_name    = EXCLUDED.sp_away_name,
                        home_lineup     = EXCLUDED.home_lineup,
                        away_lineup     = EXCLUDED.away_lineup,
                        updated_at      = NOW()
                """, (
                    game_pk, game_date, SEASON,
                    home["team"]["name"], away["team"]["name"],
                    home["team"]["id"],   away["team"]["id"],
                    home_score, away_score,
                    home_woba, away_woba,
                    sp_home_id, sp_away_id, sp_home_name, sp_away_name,
                    json.dumps(home_lineup) if home_lineup else None,
                    json.dumps(away_lineup) if away_lineup else None,
                ))
                saved += 1
            except Exception as e:
                print(f"    ✗ game {game_pk}: {e}")
                errors += 1

    return saved, skipped, errors

# ── Main ──────────────────────────────────────────────────────────────────────
conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

if DEFAULT_MODE == "backfill":
    start = date.fromisoformat(BACKFILL_START_DATE)
    end   = date.today() - timedelta(days=1)
    # Process in 7-day chunks to stay within API limits
    total_saved = total_skipped = total_errors = 0
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(chunk_start + timedelta(days=6), end)
        s, sk, e = process_date_range(cur, str(chunk_start), str(chunk_end))
        conn.commit()
        total_saved += s; total_skipped += sk; total_errors += e
        print(f"  Chunk {chunk_start}–{chunk_end}: {s} saved, {sk} skipped, {e} errors")
        chunk_start = chunk_end + timedelta(days=1)
        time.sleep(1)
    print(f"\nBackfill complete: {total_saved} games saved, {total_skipped} skipped, {total_errors} errors")
else:
    yesterday = date.today() - timedelta(days=1)
    s, sk, e = process_date_range(cur, str(yesterday), str(yesterday))
    conn.commit()
    print(f"Nightly complete: {s} saved, {sk} skipped, {e} errors")

cur.close()
conn.close()
