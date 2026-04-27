import statsapi

SEASON = 2026

# ── Test 1: See if any qualified batters come back ─────────────────────
print("=== Qualified Batters ===")
leaders = statsapi.get("stats", {
    "stats": "season",
    "group": "hitting",
    "season": SEASON,
    "playerPool": "ALL",
    "limit": 20,
    "fields": "stats,splits,stat,atBats,player,id,fullName"
})

splits = leaders.get("stats", [{}])[0].get("splits", [])
print(f"Total splits returned: {len(splits)}")
for entry in splits[:5]:
    ab = entry.get("stat", {}).get("atBats", 0)
    name = entry.get("player", {}).get("fullName", "Unknown")
    print(f"  {name}: {ab} AB")

# ── Test 2: Check what yesterday's date looks like ────────────────────
from datetime import date, timedelta
yesterday = date.today() - timedelta(days=1)
print(f"\n=== Yesterday's date string ===")
print(f"  {yesterday}")

# ── Test 3: Pull one known player's game log and print raw dates ───────
print("\n=== Sample game log dates (Aaron Judge) ===")
stats = statsapi.player_stat_data(592450, group="hitting", type="gameLog")
games = stats.get("stats", [])
print(f"Total games returned: {len(games)}")
for game in games[-5:]:  # last 5 games
    print(f"  date: '{game.get('date')}' | AB: {game['stats'].get('atBats')}")
