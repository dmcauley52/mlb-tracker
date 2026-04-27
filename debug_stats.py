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

# ── Test 2: Print full raw player_stat_data response ──────────────────
if qualified:
    pid = qualified[0]["id"]
    name = qualified[0]["name"]
    print(f"\n=== player_stat_data for {name} (id: {pid}) ===")

    raw = statsapi.player_stat_data(pid, group="hitting", type="gameLog")

    print("\n--- TOP LEVEL KEYS ---")
    print(list(raw.keys()))

    print("\n--- FULL RAW DUMP (first 3000 chars) ---")
    print(json.dumps(raw, indent=2)[:3000])
