"""
rank_price_accuracy.py

Which price source is sharpest IN OUR OWN SAMPLE? Ranks every home-win probability
we log in kalshi_tracker — Kalshi, DraftKings (vegas_*), Pinnacle, our model, and
CycleEdge — by accuracy, Brier score, and log-loss against realized outcomes.

Two views, because Pinnacle capture started late and is thin:
  1. FULL   — each price scored on every resolved game where THAT price exists
              (n differs per source; best for the rich columns).
  2. COMMON — all sources scored on the same games where every price is present
              (apples-to-apples; n is capped by the sparsest column = Pinnacle).

Lower Brier / log-loss = sharper. Reuses the scoring helpers from validate_model.py.

Run:  python rank_price_accuracy.py
Env:  DATABASE_URL (required), SEASON (optional)
"""
import os
import psycopg2
from dotenv import load_dotenv

from validate_model import brier, logloss, clamp

load_dotenv()
SEASON = os.getenv("SEASON")

# (label, home-prob column) — order is display order, not a ranking
SOURCES = [
    ("Pinnacle",   "pinnacle_home_prob"),
    ("Kalshi",     "kalshi_home_prob"),
    ("DraftKings", "vegas_home_prob"),
    ("Model",      "model_home_prob"),
    ("CycleEdge",  "cycle_home_prob"),
]


def score(rows, col):
    """(n, accuracy, brier, logloss) for one price column over rows that have it."""
    data = [(float(r[col]), 1 if r["actual_winner"] == "home" else 0)
            for r in rows if r[col] is not None]
    if not data:
        return None
    probs = [p for p, _ in data]
    ys    = [y for _, y in data]
    # accuracy: pick home when prob >= 0.5
    correct = sum(1 for p, y in data if (p >= 0.5) == (y == 1))
    return len(data), correct / len(data), brier(probs, ys), logloss(probs, ys)


def rank_and_print(title, rows, cols):
    print("\n" + "=" * 66)
    print(title)
    print("=" * 66)
    results = []
    for label, col in SOURCES:
        if col not in cols:
            continue
        s = score(rows, col)
        if s:
            results.append((label, *s))
    if not results:
        print("  no data")
        return
    # sort by Brier (sharper first)
    results.sort(key=lambda r: r[3])
    print(f"{'source':<12}{'n':>6}{'acc':>9}{'Brier':>10}{'logloss':>10}   rank")
    print("-" * 66)
    for i, (label, n, acc, bs, ll) in enumerate(results, 1):
        print(f"{label:<12}{n:>6}{acc:>9.3f}{bs:>10.4f}{ll:>10.4f}   #{i}")
    print("\n(lower Brier / log-loss = sharper; accuracy is coin-flip-style hit rate)")


def main():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    where = "actual_winner IS NOT NULL"
    params = []
    if SEASON:
        where += " AND season = %s"
        params.append(int(SEASON))
    cur.execute(f"""
        SELECT actual_winner, pinnacle_home_prob, kalshi_home_prob,
               vegas_home_prob, model_home_prob, cycle_home_prob
        FROM kalshi_tracker
        WHERE {where}
        ORDER BY game_date
    """, params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close(); conn.close()

    print(f"Resolved games: {len(rows)}"
          + (f"  (season {SEASON})" if SEASON else "  (all seasons)"))

    # FULL view — each price on its own available games
    rank_and_print("FULL — each source on every game where it exists (n varies)",
                   rows, set(cols))

    # COMMON view — only games where EVERY listed source is present
    price_cols = [c for _, c in SOURCES if c in cols]
    common = [r for r in rows if all(r[c] is not None for c in price_cols)]
    rank_and_print(
        f"COMMON — same {len(common)} games where all sources present "
        f"(capped by Pinnacle)",
        common, set(cols))
    if len(common) < 50:
        print("\n  NOTE: common-subset n is small — Pinnacle capture is young. Treat")
        print("  the Pinnacle row as a preview; re-run as more nights accumulate.")


if __name__ == "__main__":
    main()
