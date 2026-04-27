import statsapi
import json
import urllib.request
from datetime import date, timedelta

SEASON = 2026
pid = 691740  # Daniel Susac

print(f"=== Direct MLB API call for player {pid} ===")

url = f"https://statsapi.mlb.com/api/v1/people/{pid}/stats?stats=gameLog&group=hitting&season={SEASON}"
print(f"URL: {url}\n")

with urllib.request.urlopen(url) as response:
    raw = json.loads(response.read().decode())

splits = raw.get("stats", [{}])[0].get("splits", [])
print(f"Total game splits returned: {len(splits)}")

if splits:
    print("\n--- FULL RAW LAST GAME ENTRY ---")
    print(json.dumps(splits[-1], indent=2))
