"""
analyze_arb_quotes.py
Analysis of the arb_quotes logger (fetch_arb_quotes.py): answers the two
questions the logger was built for, plus the lead-lag question that decides
the next strategy.

  1. ARB: how often do true cross-venue arb windows open, how wide, and how
     much is executable at top-of-book depth?
  2. LEAD-LAG: when prices move, does Kalshi move first and Polymarket follow
     (stale-quote pickoff opportunity), or vice versa?

Data quality: rows where the Polymarket match was wrong (doubleheader bug,
fixed 2026-07-10 in fetch_arb_quotes.py) are detected and excluded — a pregame
ask <= $0.03 or a cross-venue mid divergence > $0.25 marks a mismatched or
resolved market, not a price.

Run:  python analyze_arb_quotes.py
Env:  DATABASE_URL (required)
"""
import os
import math
import statistics
import psycopg2
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

MAX_PAIR_GAP_H = 3.5   # consecutive-poll pairs wider than this are dropped
MOVE_THRESHOLD = 0.02  # |mid change| that counts as a "real" line move


def f(v):
    return float(v) if v is not None else None


def mid(bid, ask):
    return (bid + ask) / 2 if bid is not None and ask is not None else None


def kalshi_fee(price):
    return math.ceil(0.07 * price * (1 - price) * 100) / 100


def pearson(xs, ys):
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    return sxy / math.sqrt(sxx * syy)


def dist(label, vals, pos_note=False):
    if not vals:
        print(f"  {label}: no data")
        return
    q = statistics.quantiles(vals, n=100) if len(vals) >= 10 else None
    line = (f"  {label}: n={len(vals)}  mean={statistics.mean(vals):+.4f}  "
            f"median={statistics.median(vals):+.4f}  max={max(vals):+.4f}")
    if q:
        line += f"  p90={q[89]:+.4f}  p99={q[98]:+.4f}"
    if pos_note:
        npos = sum(1 for v in vals if v > 0)
        line += f"  >0: {npos} ({100*npos/len(vals):.1f}%)"
    print(line)


def main():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    cur.execute("""
        SELECT ts, game_pk, home_team, away_team, game_date, game_time_utc, started,
               k_home_bid, k_home_ask, k_home_ask_size,
               k_away_bid, k_away_ask, k_away_ask_size,
               p_home_bid, p_home_ask, p_home_ask_size,
               p_away_bid, p_away_ask, p_away_ask_size,
               margin_kh_pa, margin_ka_ph, margin_kalshi, margin_poly, best_margin
        FROM arb_quotes ORDER BY game_pk, ts
    """)
    rows = []
    for r in cur.fetchall():
        rows.append({
            "ts": r[0], "game_pk": r[1], "home": r[2], "away": r[3],
            "game_date": r[4], "game_time": r[5], "started": r[6],
            "kh_bid": f(r[7]), "kh_ask": f(r[8]), "kh_sz": f(r[9]),
            "ka_bid": f(r[10]), "ka_ask": f(r[11]), "ka_sz": f(r[12]),
            "ph_bid": f(r[13]), "ph_ask": f(r[14]), "ph_sz": f(r[15]),
            "pa_bid": f(r[16]), "pa_ask": f(r[17]), "pa_sz": f(r[18]),
            "m_kh_pa": f(r[19]), "m_ka_ph": f(r[20]),
            "m_kk": f(r[21]), "m_pp": f(r[22]), "best": f(r[23]),
        })
    conn.close()

    # Doubleheaders: before the 2026-07-10 matcher fix, every game after the
    # first of a doubleheader was matched to game 1's Polymarket market, so its
    # Poly quotes (and cross-venue margins) are phantoms — exclude those rows.
    starts = defaultdict(set)
    for r in rows:
        starts[(r["home"], r["away"], r["game_date"])].add((r["game_time"], r["game_pk"]))
    dh_later = set()
    for k, games in starts.items():
        if len(games) > 1:
            for _, gpk in sorted(games)[1:]:
                dh_later.add(gpk)

    for r in rows:
        r["k_mid"] = mid(r["kh_bid"], r["kh_ask"])   # home-team mid, Kalshi
        r["p_mid"] = mid(r["ph_bid"], r["ph_ask"])   # home-team mid, Polymarket

    # ── A. Coverage ─────────────────────────────────────────────────────────
    pre = [r for r in rows if not r["started"]]
    print("=== A. COVERAGE ===")
    print(f"  {len(rows)} rows | {len(set(r['ts'] for r in rows))} polls | "
          f"{len(set(r['game_pk'] for r in rows))} games | "
          f"{min(r['ts'] for r in rows):%Y-%m-%d} .. {max(r['ts'] for r in rows):%Y-%m-%d}")
    both = sum(1 for r in pre if r["k_mid"] is not None and r["p_mid"] is not None)
    print(f"  pregame rows: {len(pre)}  (both venues quoted: {both}, "
          f"kalshi-only: {sum(1 for r in pre if r['k_mid'] is not None and r['p_mid'] is None)}, "
          f"poly-only: {sum(1 for r in pre if r['k_mid'] is None and r['p_mid'] is not None)})")

    # ── B. Data quality ─────────────────────────────────────────────────────
    def suspect(r):
        if r["started"]:
            return False
        if r["game_pk"] in dh_later:
            return True
        for a in (r["ph_ask"], r["pa_ask"], r["kh_ask"], r["ka_ask"]):
            if a is not None and a <= 0.03:
                return True
        if r["k_mid"] is not None and r["p_mid"] is not None \
                and abs(r["k_mid"] - r["p_mid"]) > 0.25:
            return True
        return False

    bad = [r for r in pre if suspect(r)]
    clean = [r for r in pre if not suspect(r)]
    print("\n=== B. DATA QUALITY ===")
    print(f"  suspect pregame rows excluded: {len(bad)}")
    for r in bad:
        print(f"    {r['ts']:%m-%d %H:%M} {r['away']} @ {r['home']}  "
              f"k_mid={r['k_mid']}  p_ask h/a={r['ph_ask']}/{r['pa_ask']}  best={r['best']}")

    # ── C. Arb windows (clean pregame rows only) ────────────────────────────
    print("\n=== C. ARB WINDOWS (clean pregame) ===")
    dist("best_margin      ", [r["best"] for r in clean if r["best"] is not None], pos_note=True)
    dist("cross kh+pa      ", [r["m_kh_pa"] for r in clean if r["m_kh_pa"] is not None], pos_note=True)
    dist("cross ka+ph      ", [r["m_ka_ph"] for r in clean if r["m_ka_ph"] is not None], pos_note=True)
    dist("kalshi-only (fee)", [r["m_kk"] for r in clean if r["m_kk"] is not None], pos_note=True)
    dist("poly-only        ", [r["m_pp"] for r in clean if r["m_pp"] is not None], pos_note=True)
    for r in clean:
        if r["best"] is not None and r["best"] > 0:
            legs = []
            if r["m_kh_pa"] == r["best"]:
                legs = ["kh", "pa", min(x for x in (r["kh_sz"], r["pa_sz"]) if x is not None)]
            elif r["m_ka_ph"] == r["best"]:
                legs = ["ka", "ph", min(x for x in (r["ka_sz"], r["ph_sz"]) if x is not None)]
            depth = legs[2] if legs else None
            profit = f"${r['best'] * depth:,.0f} executable" if depth else "single-venue"
            print(f"    WINDOW {r['ts']:%m-%d %H:%M} {r['away']} @ {r['home']}  "
                  f"margin={r['best']:+.4f}  {profit}")

    # ── D. Venue spreads ────────────────────────────────────────────────────
    print("\n=== D. VENUE SPREADS (clean pregame, home side) ===")
    dist("kalshi bid-ask   ", [r["kh_ask"] - r["kh_bid"] for r in clean
                               if r["kh_ask"] is not None and r["kh_bid"] is not None])
    dist("poly   bid-ask   ", [r["ph_ask"] - r["ph_bid"] for r in clean
                               if r["ph_ask"] is not None and r["ph_bid"] is not None])

    # Snapshot mispricing: value each Poly ask against the Kalshi mid as "fair".
    # buy_p_home > 0 means Poly sells the home team below Kalshi's fair value.
    buy_p = []
    buy_k = []
    for r in clean:
        if r["k_mid"] is None:
            continue
        if r["ph_ask"] is not None:
            buy_p.append(r["k_mid"] - r["ph_ask"])
        if r["pa_ask"] is not None:
            buy_p.append((1 - r["k_mid"]) - r["pa_ask"])
        if r["p_mid"] is not None and r["kh_ask"] is not None:
            buy_k.append(r["p_mid"] - r["kh_ask"] - kalshi_fee(r["kh_ask"]))
        if r["p_mid"] is not None and r["ka_ask"] is not None:
            buy_k.append((1 - r["p_mid"]) - r["ka_ask"] - kalshi_fee(r["ka_ask"]))
    print("\n=== D2. CROSS-VENUE MISPRICING (edge vs other venue's mid) ===")
    dist("buy Poly @ K fair", buy_p, pos_note=True)
    dist("buy Kalshi @ P fair (after fee)", buy_k, pos_note=True)

    # ── E. Lead-lag ─────────────────────────────────────────────────────────
    # Consecutive clean pregame polls of the same game, gap <= MAX_PAIR_GAP_H.
    by_game = defaultdict(list)
    for r in clean:
        if r["k_mid"] is not None and r["p_mid"] is not None:
            by_game[r["game_pk"]].append(r)
    deltas = defaultdict(list)   # game_pk -> [(dK, dP)] per interval
    for gpk, seq in by_game.items():
        for a, b in zip(seq, seq[1:]):
            gap_h = (b["ts"] - a["ts"]).total_seconds() / 3600
            if gap_h <= MAX_PAIR_GAP_H:
                deltas[gpk].append((b["k_mid"] - a["k_mid"], b["p_mid"] - a["p_mid"]))

    same, k_lead, p_lead = [], [], []
    for gpk, ds in deltas.items():
        same.extend(ds)
        for (dk1, dp1), (dk2, dp2) in zip(ds, ds[1:]):
            k_lead.append((dk1, dp2))   # does K's move now predict P's next move?
            p_lead.append((dp1, dk2))   # does P's move now predict K's next move?

    print("\n=== E. LEAD-LAG (home-team mid changes between consecutive polls) ===")
    print(f"  usable intervals: {len(same)} across {len(deltas)} games")
    c0 = pearson([d[0] for d in same], [d[1] for d in same])
    ck = pearson([d[0] for d in k_lead], [d[1] for d in k_lead])
    cp = pearson([d[0] for d in p_lead], [d[1] for d in p_lead])
    print(f"  corr(dK_t, dP_t)   same interval : {c0 if c0 is None else f'{c0:+.3f}'}  (n={len(same)})")
    print(f"  corr(dK_t, dP_t+1) Kalshi leads  : {ck if ck is None else f'{ck:+.3f}'}  (n={len(k_lead)})")
    print(f"  corr(dP_t, dK_t+1) Poly leads    : {cp if cp is None else f'{cp:+.3f}'}  (n={len(p_lead)})")

    # Pickoff check: when one venue moved >= MOVE_THRESHOLD in an interval,
    # how much of that move did the other venue make over the same interval?
    for name, i, j in (("Kalshi moved", 0, 1), ("Poly moved", 1, 0)):
        moved = [(d[i], d[j]) for d in same if abs(d[i]) >= MOVE_THRESHOLD]
        if moved:
            agree = sum(1 for a, b in moved if a * b > 0)
            ratio = statistics.median(b / a for a, b in moved)
            print(f"  {name} >= {MOVE_THRESHOLD:.2f} ({len(moved)}x): other venue same "
                  f"direction {agree}/{len(moved)}, median follow-through {ratio:+.2f}x")
        else:
            print(f"  {name} >= {MOVE_THRESHOLD:.2f}: never in sample")


if __name__ == "__main__":
    main()
