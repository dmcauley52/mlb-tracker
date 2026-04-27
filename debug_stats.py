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

# ── Test 2: Try the statsapi wrapper with hydrate ─────────────────────
if qualified:
    pid = qualified[0]["id"]
    name = qualified[0]["name"]
    print(f"\n=== Game log for {name} (id: {pid}) ===")

    # Try direct URL call to see raw response
    raw = statsapi.get("stats", {
        "stats": "gameLog",
        "group": "hitting",
        "season": SEASON,
        "playerPool": "ALL",
        "playerId": pid,
    })

    splits2 = raw.get("stats", [{}])[0].get("splits", [])
    print(f"Splits returned: {len(splits2)}")

    if splits2:
        print("\n--- FULL RAW LAST GAME ENTRY ---")
        print(json.dumps(splits2[-1], indent=2))
    else:
        # Try the player_stat_data wrapper and print everything
        print("Trying player_stat_data wrapper...")
        raw2 = statsapi.player_stat_data(pid, group="hitting", type="gameLog")
        print("\n--- TOP LEVEL KEYS ---")
        print(list(raw2.keys()))
        print("\n--- FULL RAW RESPONSE ---")
        print(json.dumps(raw2, indent=2)[:3000])  # first 3000 chars
