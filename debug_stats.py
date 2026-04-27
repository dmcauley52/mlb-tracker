import statsapi
from datetime import date, timedelta

SEASON = 2026
MIN_AT_BATS = 20

# ── Test 1: See if any qualified batters come back ─────────────────────
print("=== Fetching ALL splits and filtering by AT BATS ===")
leaders = statsapi.get("stats", {
    "stats": "season",
    "group": "hitting",
    "season": SEASON,
    "playerPool": "ALL",
    "limit": 500,
})

splits = leaders.get("stats", [{}])[0].get("splits", [])
print(f"Total splits returned by API: {len(splits)}")

qualified = []
for entry in splits:
    ab = entry.get("stat", {}).get("atBats", 0)
    player = entry.get("player", {})
    if ab >= MIN_AT_BATS:
        qualified.append({
            "id": player.get("id"),
            "name": player.get("fullName"),
            "ab": ab
        })

print(f"Players with {MIN_AT_BATS}+ AB: {len(qualified)}")
print("\nFirst 10 qualified players:")
for p in qualified[:10]:
    print(f"  {p['name']}: {p['ab']} AB")

# ── Test 2: Check yesterday's date format ─────────────────────────────
yesterday = date.today() - timedelta(days=1)
print(f"\n=== Yesterday's date string ===")
print(f"  Python date: {yesterday}")
print(f"  As string:   {str(yesterday)}")

# ── Test 3: Pull one known player's game log and print raw dates ───────
if qualified:
    pid = qualified[0]["id"]
    name = qualified[0]["name"]
    print(f"\n=== Game log dates for {name} ===")
    stats = statsapi.player_stat_data(pid, group="hitting", type="gameLog")
    games = stats.get("stats", [])
    print(f"Total games returned: {len(games)}")
    for game in games[-5:]:
        print(f"  date: '{game.get('date')}' | AB: {game['stats'].get('atBats')}")
else:
    print("\n⚠️  No qualified players found — API may not have 2026 data yet")
    print("Trying with MIN_AT_BATS = 1...")
    for entry in splits[:5]:
        ab = entry.get("stat", {}).get("atBats", 0)
        name = entry.get("player", {}).get("fullName", "Unknown")
        print(f"  {name}: {ab} AB")
