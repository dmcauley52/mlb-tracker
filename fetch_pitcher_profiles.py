"""
fetch_pitcher_profiles.py
Builds season-level pitcher summaries (ERA, WHIP, K/9, hand, median IP, ERA rank)
and upserts into pitcher_profiles. Derives the pitcher list from pitcher_gamelogs
so no extra API call is needed to find who pitched.

Run nightly after fetch_pitcher_stats.py.

Usage:
  python fetch_pitcher_profiles.py
"""
import psycopg2
import os
import json
import time
import math
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

def ip_to_decimal(ip):
    parts = str(ip or "0").split(".")
    return (int(parts[0]) or 0) + (int(parts[1]) if len(parts) > 1 else 0) / 3

def decimal_to_ip(d):
    full   = int(d)
    thirds = round((d - full) * 3)
    return f"{full}.{thirds}"

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()
today = str(date.today())

# ── 1. Get all pitcher IDs that have logged games this season ─────────────────
print("Loading pitcher IDs from pitcher_gamelogs…")
cur.execute("""
    SELECT DISTINCT player_id, player_name
    FROM pitcher_gamelogs
    WHERE season = %s AND player_id IS NOT NULL
    ORDER BY player_name
""", (SEASON,))
pitchers = cur.fetchall()
print(f"  {len(pitchers)} pitchers found in DB")

# ── 2. Fetch ERA leaderboard for ranking ──────────────────────────────────────
print("Fetching ERA leaderboard for ranking…")
try:
    lb = mlb_get(f"/stats?stats=season&group=pitching&season={SEASON}"
                 f"&sportId=1&sortStat=era&order=asc&limit=400&gameType=R")
    al_rank, nl_rank = [], []
    for s in (lb.get("stats") or [{}])[0].get("splits", []):
        lid = s.get("team", {}).get("league", {}).get("id")
        pid = s.get("player", {}).get("id")
        if not pid:
            continue
        if lid == 103:
            al_rank.append(pid)
        elif lid == 104:
            nl_rank.append(pid)
except Exception as e:
    print(f"  ⚠ ERA leaderboard: {e}")
    al_rank, nl_rank = [], []

# ── 3. Fetch season stats + player info for each pitcher ─────────────────────
print(f"Fetching profiles for {len(pitchers)} pitchers…")
saved = skipped = errors = 0

for player_id, player_name in pitchers:
    try:
        responses = [
            mlb_get(f"/people/{player_id}?hydrate=currentTeam"),
            mlb_get(f"/people/{player_id}/stats?stats=season&group=pitching&season={SEASON}&gameType=R"),
            mlb_get(f"/people/{player_id}/stats?stats=gameLog&group=pitching&season={SEASON}&gameType=R"),
        ]
        person_data = responses[0]
        stats_data  = responses[1]
        logs_data   = responses[2]

        person = (person_data.get("people") or [{}])[0]
        hand   = (person.get("pitchHand") or {}).get("code", "?")
        team   = (person.get("currentTeam") or {})
        team_name = team.get("name")
        team_id   = team.get("id")
        league_id = (team.get("league") or {}).get("id")

        season_split = ((stats_data.get("stats") or [{}])[0].get("splits") or [{}])[0]
        stat = season_split.get("stat", {})

        era   = f(stat.get("era"))
        whip  = f(stat.get("whip"))
        k9    = f(stat.get("strikeoutsPer9Inn"))
        bb9   = f(stat.get("walksPer9Inn"))
        ip    = f(stat.get("inningsPitched"))
        gs    = int(stat.get("gamesStarted") or 0)

        if not era and not ip:
            skipped += 1
            continue

        # Median IP from starts only
        game_logs = ((logs_data.get("stats") or [{}])[0].get("splits") or [])
        start_ips = sorted([
            ip_to_decimal(g["stat"]["inningsPitched"])
            for g in game_logs
            if g.get("stat", {}).get("gamesStarted", 0) > 0
               and g.get("stat", {}).get("inningsPitched")
        ])
        median_ip = None
        if start_ips:
            mid = len(start_ips) // 2
            med = start_ips[mid] if len(start_ips) % 2 else (start_ips[mid-1] + start_ips[mid]) / 2
            median_ip = decimal_to_ip(med)

        era_rank_al = (al_rank.index(player_id) + 1) if player_id in al_rank else None
        era_rank_nl = (nl_rank.index(player_id) + 1) if player_id in nl_rank else None

        cur.execute("""
            INSERT INTO pitcher_profiles
            (player_id, player_name, team, team_id, league_id, hand, season,
             season_era, season_whip, k_per_9, bb_per_9,
             innings_pitched, games_started, median_ip,
             era_rank_al, era_rank_nl, fetched_date, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s, %s,%s,%s,%s, %s,%s,%s, %s,%s,%s, NOW())
            ON CONFLICT (player_id) DO UPDATE SET
                player_name    = EXCLUDED.player_name,
                team           = EXCLUDED.team,
                team_id        = EXCLUDED.team_id,
                league_id      = EXCLUDED.league_id,
                hand           = EXCLUDED.hand,
                season_era     = EXCLUDED.season_era,
                season_whip    = EXCLUDED.season_whip,
                k_per_9        = EXCLUDED.k_per_9,
                bb_per_9       = EXCLUDED.bb_per_9,
                innings_pitched= EXCLUDED.innings_pitched,
                games_started  = EXCLUDED.games_started,
                median_ip      = EXCLUDED.median_ip,
                era_rank_al    = EXCLUDED.era_rank_al,
                era_rank_nl    = EXCLUDED.era_rank_nl,
                fetched_date   = EXCLUDED.fetched_date,
                updated_at     = NOW()
        """, (
            player_id, player_name, team_name, team_id, league_id, hand, SEASON,
            era, whip, k9, bb9,
            ip, gs, median_ip,
            era_rank_al, era_rank_nl, today,
        ))
        saved += 1
        conn.commit()
        time.sleep(0.25)

    except Exception as e:
        print(f"  ✗ {player_name} ({player_id}): {e}")
        conn.rollback()
        errors += 1

cur.close()
conn.close()
print(f"pitcher_profiles: {saved} upserted, {skipped} skipped (no stats), {errors} errors")
