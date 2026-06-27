"""
sp_watcher.py
Polls MLB API for SP changes vs names stored in kalshi_tracker.
When a change is detected, fires predict_on_demand.yml via GitHub Actions dispatch.

Required env vars:
  DATABASE_URL    Postgres connection string
  GH_TOKEN        GitHub PAT or GITHUB_TOKEN with actions:write on this repo
  GH_REPO         owner/repo  (e.g. "dmcauley52/mlb-tracker")

Optional:
  GH_REF          Branch to dispatch against (default: main)
"""
import os
import requests
import psycopg2
from datetime import date
from dotenv import load_dotenv

from model_config import SEASON

load_dotenv()

GH_TOKEN = os.environ["GH_TOKEN"]
GH_REPO  = os.environ["GH_REPO"]
GH_REF   = os.getenv("GH_REF", "main")

DISPATCH_URL = (
    f"https://api.github.com/repos/{GH_REPO}"
    f"/actions/workflows/predict_on_demand.yml/dispatches"
)
GH_HEADERS = {
    "Authorization":        f"Bearer {GH_TOKEN}",
    "Accept":               "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


# ── helpers ────────────────────────────────────────────────────────────────────

def mlb_get(path):
    r = requests.get("https://statsapi.mlb.com/api/v1" + path, timeout=15)
    r.raise_for_status()
    return r.json()


def _trigger_dispatch(game_pk, home_sp=None, away_sp=None):
    payload = {
        "ref": GH_REF,
        "inputs": {
            "game_pk": str(game_pk),
            "home_sp": home_sp or "",
            "away_sp": away_sp or "",
        },
    }
    r = requests.post(DISPATCH_URL, json=payload, headers=GH_HEADERS, timeout=15)
    r.raise_for_status()
    print(f"    Dispatched predict_on_demand for pk={game_pk}"
          f"  home_sp={home_sp!r}  away_sp={away_sp!r}")


# ── main ───────────────────────────────────────────────────────────────────────

def main():
    conn = psycopg2.connect(os.environ["DATABASE_URL"])
    cur  = conn.cursor()

    # Ensure SP name columns exist (idempotent)
    for col in ("home_sp_name", "away_sp_name"):
        cur.execute(f"ALTER TABLE kalshi_tracker ADD COLUMN IF NOT EXISTS {col} TEXT")
    conn.commit()

    today = str(date.today())

    # Load today's unresolved tracked games
    cur.execute("""
        SELECT game_pk, home_team, away_team, home_sp_name, away_sp_name
        FROM kalshi_tracker
        WHERE game_date = %s AND actual_winner IS NULL AND game_pk IS NOT NULL
        ORDER BY game_time_utc ASC NULLS LAST
    """, (today,))
    tracked = {
        r[0]: {"home_team": r[1], "away_team": r[2], "home_sp": r[3], "away_sp": r[4]}
        for r in cur.fetchall()
    }

    if not tracked:
        print(f"No tracked games for {today} — nothing to watch.")
        cur.close(); conn.close()
        return

    print(f"Watching {len(tracked)} game(s) on {today} for SP changes...")

    # Fetch current probable pitchers from MLB API
    try:
        data = mlb_get(
            f"/schedule?sportId=1&date={today}&hydrate=probablePitcher,teams"
        )
    except Exception as e:
        print(f"ERR: MLB API fetch failed: {e}")
        cur.close(); conn.close()
        raise SystemExit(1)

    dispatched = 0
    for d in data.get("dates", []):
        for g in d.get("games", []):
            pk = g.get("gamePk")
            if pk not in tracked:
                continue
            if g.get("status", {}).get("abstractGameState") == "Final":
                continue

            current_home = g["teams"]["home"].get("probablePitcher", {}).get("fullName")
            current_away = g["teams"]["away"].get("probablePitcher", {}).get("fullName")
            stored       = tracked[pk]

            home_team = stored["home_team"]
            away_team = stored["away_team"]

            if stored["home_sp"] is None and stored["away_sp"] is None:
                # First time seeing this game — store baseline, no trigger
                cur.execute("""
                    UPDATE kalshi_tracker
                    SET home_sp_name = %s, away_sp_name = %s
                    WHERE game_pk = %s
                """, (current_home, current_away, pk))
                print(f"  Baseline stored  pk={pk}  "
                      f"{away_team} @ {home_team}: {current_away!r} vs {current_home!r}")
                continue

            home_scratched = (
                current_home
                and stored["home_sp"]
                and current_home != stored["home_sp"]
            )
            away_scratched = (
                current_away
                and stored["away_sp"]
                and current_away != stored["away_sp"]
            )

            if home_scratched or away_scratched:
                if home_scratched:
                    print(f"  SCRATCH {home_team}: {stored['home_sp']!r} → {current_home!r}")
                if away_scratched:
                    print(f"  SCRATCH {away_team}: {stored['away_sp']!r} → {current_away!r}")

                _trigger_dispatch(
                    pk,
                    home_sp=current_home if home_scratched else None,
                    away_sp=current_away if away_scratched else None,
                )
                dispatched += 1

                # Update stored names so the next poll doesn't re-trigger
                cur.execute("""
                    UPDATE kalshi_tracker
                    SET home_sp_name = COALESCE(%s, home_sp_name),
                        away_sp_name = COALESCE(%s, away_sp_name)
                    WHERE game_pk = %s
                """, (
                    current_home if home_scratched else None,
                    current_away if away_scratched else None,
                    pk,
                ))
            else:
                print(f"  No change  pk={pk}  "
                      f"{away_team} @ {home_team}: {current_away!r} vs {current_home!r}")

    conn.commit()
    cur.close()
    conn.close()
    print(f"Done — {dispatched} dispatch(es) triggered.")


if __name__ == "__main__":
    main()
