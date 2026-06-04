"""
streak_prediction_analysis.py
Analyzes model predictions for games that ended a 4+ game winning streak.
"""
import os, collections
import psycopg2
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
import numpy as np
from dotenv import load_dotenv

load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

# ── Build streak-ending game list ─────────────────────────────────────────────
cur.execute("""
    SELECT game_pk, game_date, home_team, away_team, home_score, away_score
    FROM game_results
    WHERE season = 2026 AND home_score IS NOT NULL AND away_score IS NOT NULL
    ORDER BY game_date ASC
""")
team_games = collections.defaultdict(list)
for game_pk, game_date, home, away, hs, aws in cur.fetchall():
    team_games[home].append((game_pk, game_date, away, "home", hs > aws))
    team_games[away].append((game_pk, game_date, home, "away", aws > hs))

streak_pks = {}
for team, games in team_games.items():
    run = 0
    for game_pk, game_date, opp, side, won in games:
        if won:
            run += 1
        else:
            if run >= 4:
                streak_pks[game_pk] = (team, run, side)
            run = 0

# ── Pull kalshi_tracker rows ──────────────────────────────────────────────────
pks = list(streak_pks.keys())
placeholders = ",".join(["%s"] * len(pks))
cur.execute(f"""
    SELECT game_pk, game_date, home_team, away_team,
           model_home_prob, model_pick, model_confidence, signal_type,
           kalshi_home_prob, kalshi_pick, vegas_home_prob, vegas_pick,
           prob_gap, actual_winner, model_correct, kalshi_correct, vegas_correct
    FROM kalshi_tracker
    WHERE game_pk = ANY(ARRAY[{placeholders}])
    ORDER BY game_date
""", pks)
cols = [d[0] for d in cur.description]
tracked_rows = cur.fetchall()
cur.close(); conn.close()

# ── Enrich each row ───────────────────────────────────────────────────────────
rows = []
for row in tracked_rows:
    r = dict(zip(cols, row))
    gp = r["game_pk"]
    streaking_team, streak_len, streaking_side = streak_pks[gp]

    if streaking_side == "home":
        m_sp  = float(r["model_home_prob"]  or 0.5)
        k_sp  = float(r["kalshi_home_prob"]) if r["kalshi_home_prob"] else None
        v_sp  = float(r["vegas_home_prob"])  if r["vegas_home_prob"]  else None
        backs = r["model_pick"] == "home"
    else:
        m_sp  = 1 - float(r["model_home_prob"]  or 0.5)
        k_sp  = (1 - float(r["kalshi_home_prob"])) if r["kalshi_home_prob"] else None
        v_sp  = (1 - float(r["vegas_home_prob"]))  if r["vegas_home_prob"]  else None
        backs = r["model_pick"] == "away"

    rows.append({
        **r,
        "streaking_team":     streaking_team,
        "streak_len":         streak_len,
        "streaking_side":     streaking_side,
        "model_backs_streak": backs,
        "model_sp":           round(m_sp, 3),
        "kalshi_sp":          round(k_sp, 3) if k_sp is not None else None,
        "vegas_sp":           round(v_sp, 3) if v_sp is not None else None,
        "model_vs_mkt_gap":   round(m_sp - k_sp, 3) if k_sp is not None else None,
    })

backed   = [r for r in rows if r["model_backs_streak"]]
not_back = [r for r in rows if not r["model_backs_streak"]]

# ── Figure ────────────────────────────────────────────────────────────────────
BG   = "#0f172a"
CARD = "#1e293b"
BLUE = "#3b82f6"
RED  = "#ef4444"
AMB  = "#f59e0b"
GRN  = "#22c55e"
MUTE = "#94a3b8"
WHT  = "#f1f5f9"

fig = plt.figure(figsize=(15, 10), facecolor=BG)
fig.suptitle(
    "Games Ending a 4+ Win Streak — Prediction Analysis  (2026 Season)",
    color=WHT, fontsize=14, fontweight="bold", y=0.98,
)
gs = gridspec.GridSpec(2, 3, figure=fig, hspace=0.48, wspace=0.38)

def ax_style(ax, title):
    ax.set_facecolor(CARD)
    for sp in ax.spines.values():
        sp.set_edgecolor("#334155")
    ax.tick_params(colors=MUTE, labelsize=8)
    ax.set_title(title, color=WHT, fontsize=10, pad=8)

# ── Panel 1: Backed vs Not-Backed accuracy ────────────────────────────────────
ax1 = fig.add_subplot(gs[0, 0])
ax_style(ax1, "Model Accuracy: Backed Streak vs Faded Streak")

b_c  = sum(1 for r in backed   if r["model_correct"])
nb_c = sum(1 for r in not_back if r["model_correct"])
cats   = ["Backed\nstreaker", "Faded\nstreaker"]
totals = [len(backed), len(not_back)]
rights = [b_c, nb_c]
wrongs = [len(backed) - b_c, len(not_back) - nb_c]

x = np.arange(2)
w = 0.55
ax1.bar(x, rights, w, label="Correct",   color=GRN,  edgecolor=BG)
ax1.bar(x, wrongs, w, bottom=rights,     label="Wrong", color=RED, edgecolor=BG)
for i, (n, t) in enumerate(zip(rights, totals)):
    pct = 100 * n / t if t else 0
    ax1.text(i, t + 0.5, f"{n}/{t}\n({pct:.0f}%)", ha="center",
             va="bottom", color=WHT, fontsize=9, fontweight="bold")

ax1.set_xticks(x); ax1.set_xticklabels(cats, color=MUTE, fontsize=9)
ax1.set_ylabel("Games", color=MUTE, fontsize=9)
ax1.legend(facecolor=CARD, edgecolor="#334155", labelcolor=MUTE, fontsize=8)
ax1.set_ylim(0, max(totals) * 1.25)
ax1.yaxis.set_tick_params(labelcolor=MUTE)

# ── Panel 2: Model vs Market prob for streaker (scatter) ──────────────────────
ax2 = fig.add_subplot(gs[0, 1])
ax_style(ax2, "Model vs Market Probability for Streak Team")

m_probs = [r["model_sp"] for r in backed if r["kalshi_sp"] is not None]
k_probs = [r["kalshi_sp"] for r in backed if r["kalshi_sp"] is not None]
ax2.scatter(k_probs, m_probs, color=BLUE, edgecolors=BG, s=55, zorder=3, label="Game")

lo, hi = 0.25, 0.90
ax2.plot([lo, hi], [lo, hi], "--", color="#475569", linewidth=1, label="Model = Market")
ax2.fill_between([lo, hi], [lo, lo], [hi, hi], alpha=0.04, color=BLUE)

# Annotate mean gap
mean_gap = np.mean([r["model_vs_mkt_gap"] for r in backed if r["model_vs_mkt_gap"] is not None])
ax2.text(0.97, 0.05, f"Avg model excess\nvs market: +{mean_gap:.3f}",
         transform=ax2.transAxes, ha="right", va="bottom",
         color=AMB, fontsize=8,
         bbox=dict(boxstyle="round,pad=0.3", facecolor=CARD, edgecolor="#334155"))

ax2.set_xlabel("Market (Kalshi) prob", color=MUTE, fontsize=9)
ax2.set_ylabel("Model prob", color=MUTE, fontsize=9)
ax2.legend(facecolor=CARD, edgecolor="#334155", labelcolor=MUTE, fontsize=8)
ax2.set_xlim(lo, hi); ax2.set_ylim(lo, hi)

# ── Panel 3: Model prob distribution (backed games, all wrong) ────────────────
ax3 = fig.add_subplot(gs[0, 2])
ax_style(ax3, "Model Confidence When Backing Streaker\n(0/27 correct)")

prob_vals = [r["model_sp"] for r in backed]
bins = np.arange(0.45, 0.95, 0.05)
ax3.hist(prob_vals, bins=bins, color=RED, edgecolor=BG, linewidth=0.5)
ax3.axvline(np.mean(prob_vals), color=AMB, linewidth=1.5, linestyle="--",
            label=f"Mean = {np.mean(prob_vals):.3f}")
ax3.set_xlabel("Model probability for streak team", color=MUTE, fontsize=9)
ax3.set_ylabel("Count", color=MUTE, fontsize=9)
ax3.legend(facecolor=CARD, edgecolor="#334155", labelcolor=MUTE, fontsize=8)
ax3.text(0.97, 0.90, "All 27 wrong", transform=ax3.transAxes,
         ha="right", va="top", color=RED, fontsize=10, fontweight="bold")

# ── Panel 4: Accuracy by streak length ───────────────────────────────────────
ax4 = fig.add_subplot(gs[1, 0])
ax_style(ax4, "Model Accuracy by Streak Length")

lengths = sorted(set(r["streak_len"] for r in rows))
g_total  = [sum(1 for r in rows if r["streak_len"] == l) for l in lengths]
g_right  = [sum(1 for r in rows if r["streak_len"] == l and r["model_correct"]) for l in lengths]
g_wrong  = [t - c for t, c in zip(g_total, g_right)]

x = np.arange(len(lengths))
ax4.bar(x, g_right, 0.6, label="Correct", color=GRN, edgecolor=BG)
ax4.bar(x, g_wrong, 0.6, bottom=g_right,  label="Wrong",   color=RED, edgecolor=BG)
for i, (t, c) in enumerate(zip(g_total, g_right)):
    pct = 100 * c / t
    ax4.text(i, t + 0.15, f"{pct:.0f}%", ha="center", va="bottom", color=MUTE, fontsize=8)

ax4.set_xticks(x); ax4.set_xticklabels([str(l) for l in lengths], color=MUTE)
ax4.set_xlabel("Winning streak length", color=MUTE, fontsize=9)
ax4.set_ylabel("Games", color=MUTE, fontsize=9)
ax4.legend(facecolor=CARD, edgecolor="#334155", labelcolor=MUTE, fontsize=8)

# ── Panel 5: Model vs market gap distribution ─────────────────────────────────
ax5 = fig.add_subplot(gs[1, 1])
ax_style(ax5, "Model Excess Over Market (streak-backed games)")

gaps = [r["model_vs_mkt_gap"] for r in backed if r["model_vs_mkt_gap"] is not None]
bins = np.arange(-0.15, 0.40, 0.05)
ax5.hist(gaps, bins=bins, color=BLUE, edgecolor=BG, linewidth=0.5)
ax5.axvline(0, color="#475569", linewidth=1, linestyle="--")
ax5.axvline(np.mean(gaps), color=AMB, linewidth=1.5, linestyle="--",
            label=f"Mean = +{np.mean(gaps):.3f}")
ax5.set_xlabel("Model prob − Kalshi prob (for streak team)", color=MUTE, fontsize=9)
ax5.set_ylabel("Count", color=MUTE, fontsize=9)
ax5.legend(facecolor=CARD, edgecolor="#334155", labelcolor=MUTE, fontsize=8)
ax5.text(0.97, 0.90, "Positive = model\nover-favors streak",
         transform=ax5.transAxes, ha="right", va="top", color=MUTE, fontsize=7)

# ── Panel 6: signal_type breakdown ───────────────────────────────────────────
ax6 = fig.add_subplot(gs[1, 2])
ax_style(ax6, "Signal Type — Backed Streak Games")

sig_counts = collections.Counter(r["signal_type"] for r in backed)
sig_correct = collections.defaultdict(int)
for r in backed:
    if r["model_correct"]:
        sig_correct[r["signal_type"]] += 1

sig_labels = list(sig_counts.keys())
sig_totals = [sig_counts[s] for s in sig_labels]
sig_rights = [sig_correct[s] for s in sig_labels]
sig_wrongs = [sig_counts[s] - sig_correct[s] for s in sig_labels]

colors_map = {"strong_edge": RED, "disagreement": "#7c3aed"}
bar_colors = [colors_map.get(s, BLUE) for s in sig_labels]

x = np.arange(len(sig_labels))
ax6.bar(x, sig_rights, 0.6, label="Correct", color=GRN, edgecolor=BG)
ax6.bar(x, sig_wrongs, 0.6, bottom=sig_rights, color=bar_colors, edgecolor=BG)
for i, (t, c) in enumerate(zip(sig_totals, sig_rights)):
    ax6.text(i, t + 0.2, f"0/{t}", ha="center", va="bottom", color=MUTE, fontsize=9)

nice = {"strong_edge": "Strong Edge", "disagreement": "Disagreement"}
ax6.set_xticks(x)
ax6.set_xticklabels([nice.get(s, s) for s in sig_labels], color=MUTE, fontsize=9)
ax6.set_ylabel("Games", color=MUTE, fontsize=9)
ax6.legend(facecolor=CARD, edgecolor="#334155", labelcolor=MUTE, fontsize=8)

# ── Footer note ───────────────────────────────────────────────────────────────
fig.text(
    0.5, 0.01,
    f"34 of 62 streak-ending games tracked in kalshi_tracker  ·  "
    f"Model backed streaker {len(backed)}/34 times  ·  "
    f"0/{len(backed)} correct when backing streak  ·  "
    f"{nb_c}/{len(not_back)} correct when fading streak",
    ha="center", va="bottom", color=MUTE, fontsize=8,
)

plt.savefig("streak_prediction_analysis.png", dpi=150, bbox_inches="tight", facecolor=BG)
print("Saved: streak_prediction_analysis.png")

# ── Print calibration summary ─────────────────────────────────────────────────
print(f"\n{'='*60}")
print(f"BACKED-STREAK GAMES  ({len(backed)} total, 0 correct)")
print(f"{'='*60}")
print(f"  Avg model prob  : {np.mean([r['model_sp']   for r in backed]):.3f}")
print(f"  Avg market prob : {np.mean([r['kalshi_sp']  for r in backed if r['kalshi_sp']]):.3f}")
print(f"  Avg model excess: +{np.mean(gaps):.3f}")
print(f"\nFADED-STREAK GAMES ({len(not_back)} total, {nb_c} correct = {100*nb_c/max(len(not_back),1):.0f}%)")
print(f"\nALL STREAK-ENDING GAMES: {sum(1 for r in rows if r['model_correct'])}/{len(rows)} = "
      f"{100*sum(1 for r in rows if r['model_correct'])/len(rows):.0f}%")

plt.show()
