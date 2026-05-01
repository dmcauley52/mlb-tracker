import psycopg2
import os
import time
import json
import urllib.request
import statsapi
from datetime import date, timedelta
from dotenv import load_dotenv
load_dotenv()

SEASON = 2026
MIN_AT_BATS = 20

WOBA_WEIGHTS = {"bb": 0.690, "hbp": 0.722, "single": 0.888, "double": 1.271, "triple": 1.616, "hr": 2.101}

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
    num = WOBA_WEIGHTS["bb"]*ubb + WOBA_WEIGHTS["hbp"]*hbp + WOBA_WEIGHTS["single"]*s1b + WOBA_WEIGHTS["double"]*dbl + WOBA_WEIGHTS["triple"]*trp + WOBA_WEIGHTS["hr"]*hr
    den = ab + ubb + hbp + sf
    return round(num / den, 4) if den > 0 else None

def n(s, key):
    v = s.get(key)
    return None if v is None else (int(v) if isinstance(v, (int, float)) and v == int(v) else v)

def f(s, key):
    try:
        return float(s.get(key)) or None
    except (TypeError, ValueError):
        return None

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur = conn.cursor()

yesterday = date.today() - timedelta(days=1)
yesterday_str = str(yesterday)

# ── Get qualified batters ──────────────────────────────────────────────
print("Fetching qualified batters...")
leaders = statsapi.get("stats", {
    "stats": "season",
    "group": "hitting",
    "season": SEASON,
    "playerPool": "ALL",
    "limit": 500,
})

splits = leaders.get("stats", [{}])[0].get("splits", [])
qualified = []
for entry in splits:
    ab = entry.get("stat", {}).get("atBats", 0)
    player = entry.get("player", {})
    if ab >= MIN_AT_BATS:
        qualified.append({
            "id": player.get("id"),
            "name": player.get("fullName")
        })

print(f"Fetching {yesterday_str} stats for {len(qualified)} players...")

# ── Fetch game logs via direct API call ───────────────────────────────
for player in qualified:
    pid = player["id"]
    name = player["name"]
    try:
        url = f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=hitting&season={SEASON}"
        with urllib.request.urlopen(url) as response:
            raw = json.loads(response.read().decode())

        game_splits = raw.get("stats", [{}])[0].get("splits", [])

        for game in game_splits:
            gdate = game.get("date")
            if gdate != yesterday_str:
                continue

            s = game.get("stat", {})
            team = game.get("team", {}).get("name", "")
            positions = game.get("positionsPlayed", [{}])
            position = positions[0].get("abbreviation", "") if positions else ""
            is_starter = bool(s.get("gamesStarted", 0))
            bo_raw = game.get("battingOrder")
            batting_order = int(bo_raw) // 100 if bo_raw else None
            if batting_order == 0: batting_order = None

            cur.execute("""
                INSERT INTO player_gamelogs
                (player_id, player_name, game_date, season, at_bats, hits,
                 home_runs, rbi, batting_avg, ops, team, position, is_starter,
                 woba, obp, slg, doubles, triples, walks, hit_by_pitch, sac_flies,
                 strikeouts, stolen_bases, plate_appearances, total_bases, runs, babip,
                 batting_order)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (player_id, game_date) DO UPDATE SET
                    is_starter       = EXCLUDED.is_starter,
                    woba             = EXCLUDED.woba,
                    obp              = EXCLUDED.obp,
                    slg              = EXCLUDED.slg,
                    doubles          = EXCLUDED.doubles,
                    triples          = EXCLUDED.triples,
                    walks            = EXCLUDED.walks,
                    hit_by_pitch     = EXCLUDED.hit_by_pitch,
                    sac_flies        = EXCLUDED.sac_flies,
                    strikeouts       = EXCLUDED.strikeouts,
                    stolen_bases     = EXCLUDED.stolen_bases,
                    plate_appearances= EXCLUDED.plate_appearances,
                    total_bases      = EXCLUDED.total_bases,
                    runs             = EXCLUDED.runs,
                    babip            = EXCLUDED.babip,
                    batting_order    = EXCLUDED.batting_order
            """, (
                pid, name, gdate, SEASON,
                s.get("atBats", 0), s.get("hits", 0),
                s.get("homeRuns", 0), s.get("rbi", 0),
                f(s, "avg") or 0, f(s, "ops") or 0,
                team, position, is_starter,
                compute_woba(s),
                f(s, "obp"), f(s, "slg"),
                n(s, "doubles"), n(s, "triples"),
                n(s, "baseOnBalls"), n(s, "hitByPitch"), n(s, "sacFlies"),
                n(s, "strikeOuts"), n(s, "stolenBases"), n(s, "plateAppearances"),
                n(s, "totalBases"), n(s, "runs"),
                f(s, "babip"),
                batting_order,
            ))

        conn.commit()
        time.sleep(0.3)

    except Exception as e:
        print(f"  ✗ {name}: {e}")
        conn.rollback()

cur.close()
conn.close()
print(f"Nightly update complete for {yesterday_str}")
