import statsapi
import json
from datetime import date, timedelta

SEASON = 2026
MIN_AT_BATS = 20

# ── Test 1: Qualified batters ──────────────────────────────────────────
print("=== Fetching qualified batters ===")
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
            "name": player.get("fullName"),
            "ab": ab
        })

print(f"Players with {MIN_AT_BATS}+ AB: {len(qualified)}")

# ── Test 2: Call API directly for game log with dates ─────────────────
if qualified:
    pid = qualified[0]["id"]
    name = qualified[0]["name"]
    print(f"\n=== Direct API game log for {name} (id: {pid}) ===")

    # Call the API directly instead of using the wrapper
    raw = statsapi.get("person_stats", {
        "personId": pid,
        "stats": "gameLog",
        "group": "hitting",
        "season": SEASON,
    })

    print("\n--- TOP LEVEL KEYS ---")
    print(list(raw.keys()))

    splits2 = raw.get("stats", [{}])[0].get("splits", [])
    print(f"\nTotal game splits: {len(splits2)}")

    if splits2:
        print("\n--- FULL RAW LAST GAME ENTRY ---")
        print(json.dumps(splits2[-1], indent=2))
    else:
        print("⚠️  No splits returned")
