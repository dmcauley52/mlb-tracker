"""
fetch_team_stats.py
Fetches season pitching ERA/WHIP + standings (win%, L10) for all 30 MLB teams
and upserts into team_stats_cache. Run nightly — one row per team, replaced daily.

Usage:
  python fetch_team_stats.py
"""
import psycopg2
import os
import json
import time
import urllib.request
from datetime import date
from dotenv import load_dotenv
load_dotenv()

SEASON = 2026

def mlb_get(path):
    url = "https://statsapi.mlb.com/api/v1" + path
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())

def f(val):
    try:
        v = float(val)
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()
today = str(date.today())

# ── 1. All teams list ─────────────────────────────────────────────────────────
print("Fetching teams list…")
teams_data = mlb_get(f"/teams?sportId=1&season={SEASON}")
teams = {t["id"]: {"name": t["name"], "abbreviation": t.get("abbreviation",""), "league_id": t.get("league",{}).get("id")}
         for t in teams_data.get("teams", [])}
print(f"  {len(teams)} teams found")

# ── 2. Standings (win%, L10) ──────────────────────────────────────────────────
print("Fetching standings…")
standings_data = mlb_get(f"/standings?leagueId=103,104&season={SEASON}&standingsTypes=regularSeason")

win_pct_map = {}    # team_id → season win%
l10_map     = {}    # team_id → L10 win%
wins_map    = {}
losses_map  = {}

for rec in standings_data.get("records", []):
    for tr in rec.get("teamRecords", []):
        tid   = tr["team"]["id"]
        w     = tr.get("wins", 0)
        l     = tr.get("losses", 0)
        wins_map[tid]   = w
        losses_map[tid] = l
        win_pct_map[tid] = round(w / (w + l), 3) if (w + l) > 0 else 0.500
        l10 = next((s for s in tr.get("records", {}).get("splitRecords", []) if s["type"] == "lastTen"), None)
        if l10 and (l10["wins"] + l10["losses"]) > 0:
            l10_map[tid] = round(l10["wins"] / (l10["wins"] + l10["losses"]), 3)
        else:
            l10_map[tid] = win_pct_map[tid]

# ── 3. Team pitching ERA/WHIP ─────────────────────────────────────────────────
print("Fetching team pitching stats…")
saved = errors = 0

for tid, info in teams.items():
    try:
        data  = mlb_get(f"/teams/{tid}/stats?stats=season&group=pitching&season={SEASON}&sportId=1")
        split = (data.get("stats") or [{}])[0].get("splits", [{}])[0]
        stat  = split.get("stat", {})

        era   = f(stat.get("era"))
        whip  = f(stat.get("whip"))

        cur.execute("""
            INSERT INTO team_stats_cache
            (team_id, team_name, abbreviation, league_id, season,
             season_era, season_whip, win_pct, l10_win_pct,
             wins, losses, fetched_date, updated_at)
            VALUES (%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s, NOW())
            ON CONFLICT (team_id) DO UPDATE SET
                season_era   = EXCLUDED.season_era,
                season_whip  = EXCLUDED.season_whip,
                win_pct      = EXCLUDED.win_pct,
                l10_win_pct  = EXCLUDED.l10_win_pct,
                wins         = EXCLUDED.wins,
                losses       = EXCLUDED.losses,
                fetched_date = EXCLUDED.fetched_date,
                updated_at   = NOW()
        """, (
            tid, info["name"], info["abbreviation"], info["league_id"], SEASON,
            era, whip,
            win_pct_map.get(tid), l10_map.get(tid),
            wins_map.get(tid), losses_map.get(tid),
            today,
        ))
        saved += 1
        time.sleep(0.15)
    except Exception as e:
        print(f"  ✗ {info['name']}: {e}")
        errors += 1

conn.commit()
cur.close()
conn.close()
print(f"team_stats_cache: {saved} upserted, {errors} errors")
