import statsapi
from datetime import date, timedelta

SEASON = 2026
MIN_AT_BATS = 20

# ── Test 1: Qualified batters ──────────────────────────────────────────
print("=== Fetching ALL splits ===")
leaders = statsapi.get("stats", {
    "stats": "season",
    "group": "hitting",
    "season": SEASON,
    "playerPool": "ALL",
    "limit": 500,
})

splits = leaders.get("stats", [{}])[0].get("splits", [])
print(f"Total splits returned: {len(splits)}")

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

# ── Test 2: Print RAW game entry to see all available keys ────────────
if qualified:
    pid = qualified[0]["id"]
    name = qualified[0]["name"]
    print(f"\n=== Raw game log entry for {name} ===")
    stats = statsapi.player_stat_data(pid, group="hitting", type="gameLog")
    games = stats.get("stats", [])
    print(f"Total games in log: {len(games)}")

    if games:
        print("\n--- FULL RAW ENTRY (last game) ---")
        import json
        print(json.dumps(games[-1], indent=2))

        print("\n--- TOP LEVEL KEYS in each game entry ---")
        print(list(games[-1].keys()))
    else:
        print("⚠️  No games returned for this player")
else:
    print("⚠️  No qualified players — printing raw split sample:")
    import json
    if splits:
        print(json.dumps(splits[0], indent=2))
