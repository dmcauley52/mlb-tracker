"""
win_streak_histogram.py
Histogram of winning-streak lengths: how many wins in a row before a loss.
Queries game_results for season 2026, builds per-team win/loss sequences,
extracts all streaks, and plots the distribution.
"""
import os, collections
import psycopg2
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
from dotenv import load_dotenv

load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

# Fetch every completed game, ordered by team and date
cur.execute("""
    SELECT game_date, home_team, away_team, home_score, away_score
    FROM game_results
    WHERE season = 2026 AND home_score IS NOT NULL AND away_score IS NOT NULL
    ORDER BY game_date ASC
""")
rows = cur.fetchall()
cur.close(); conn.close()

# Build per-team win/loss lists  {"Team Name": [True, False, True, ...]}
team_results: dict[str, list[bool]] = collections.defaultdict(list)
for game_date, home, away, hs, aws in rows:
    team_results[home].append(hs > aws)   # home won?
    team_results[away].append(aws > hs)   # away won?

# Extract streak lengths: count wins in a row before each loss
streaks: list[int] = []
for team, results in team_results.items():
    run = 0
    for won in results:
        if won:
            run += 1
        else:
            # Loss ends the streak; only record if there were wins before it
            if run > 0:
                streaks.append(run)
            run = 0
    # Trailing streak with no following loss — skip (streak still ongoing)

total_streaks = len(streaks)
max_streak    = max(streaks)
mean_streak   = sum(streaks) / total_streaks

# Count frequency for each streak length
freq = collections.Counter(streaks)
xs   = list(range(1, max_streak + 1))
ys   = [freq.get(x, 0) for x in xs]

print(f"Total streak events : {total_streaks}")
print(f"Mean streak length  : {mean_streak:.2f}")
print(f"Max streak observed : {max_streak}")
print()
print("Streak  Count")
for x, y in zip(xs, ys):
    bar = "#" * y
    print(f"  {x:>3}    {y:>4}  {bar}")

# ── Plot ──────────────────────────────────────────────────────────────────────
fig, ax = plt.subplots(figsize=(10, 5))

bars = ax.bar(xs, ys, color="#3b82f6", edgecolor="white", linewidth=0.6)

# Annotate each bar with its count
for bar, count in zip(bars, ys):
    if count > 0:
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.4,
            str(count),
            ha="center", va="bottom", fontsize=8, color="#e2e8f0"
        )

ax.axvline(mean_streak, color="#f59e0b", linewidth=1.5, linestyle="--",
           label=f"Mean = {mean_streak:.2f}")

ax.set_facecolor("#0f172a")
fig.patch.set_facecolor("#0f172a")
ax.tick_params(colors="#94a3b8")
for spine in ax.spines.values():
    spine.set_edgecolor("#334155")

ax.set_xlabel("Winning streak length (games won before a loss)", color="#94a3b8", fontsize=11)
ax.set_ylabel("Number of streaks", color="#94a3b8", fontsize=11)
ax.set_title(
    f"MLB 2026 — Win Streaks Before a Loss\n"
    f"All 30 teams  ·  {total_streaks} streak events  ·  max = {max_streak}",
    color="#f1f5f9", fontsize=13, pad=14
)

ax.xaxis.set_major_locator(mticker.MultipleLocator(1))
ax.yaxis.set_major_locator(mticker.MultipleLocator(10))
ax.legend(facecolor="#1e293b", edgecolor="#334155", labelcolor="#f59e0b", fontsize=10)
ax.grid(axis="y", color="#1e293b", linewidth=0.8)

plt.tight_layout()
plt.savefig("win_streak_histogram.png", dpi=150, bbox_inches="tight")
print("\nSaved: win_streak_histogram.png")
plt.show()
