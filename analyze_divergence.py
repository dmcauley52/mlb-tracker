"""
analyze_divergence.py
Is there a cross-market arbitrage edge — Kalshi mispriced vs the Vegas (sharp)
line — after fees?  This is "Option A": exploit price divergence, NOT predict
winners (the internal model has no edge — see validate_model.py).

Treats the Vegas (DraftKings) probability as the fair value. When Kalshi prices
a side cheaper than Vegas by >= EDGE, we "buy" 1 Kalshi YES contract on that
side and settle it against the actual outcome, net of Kalshi fees. Reports
realized ROI — the only thing that matters.

Run:  python analyze_divergence.py
Env:  DATABASE_URL  (required)
      EDGE   minimum vegas_prob - kalshi_price to place a bet (default 0.03)
      FEE    'kalshi' (default, 0.07*p*(1-p) per contract) | a flat number | '0'
      SEASON optional season filter
      MAX_SPREAD  keep only games with kalshi_spread <= this (liquidity data is
                  forward-only from 2026-06-29; setting any liquidity filter
                  drops rows without it)
      MIN_VOLUME  keep only games with kalshi_volume >= this
      MIN_OI      keep only games with kalshi_open_interest >= this
"""
import os, math, statistics
import psycopg2
from dotenv import load_dotenv

load_dotenv()

EDGE   = float(os.getenv("EDGE", "0.03"))
SEASON = os.getenv("SEASON")
FEE    = os.getenv("FEE", "kalshi")
MAX_SPREAD = os.getenv("MAX_SPREAD")
MIN_VOLUME = os.getenv("MIN_VOLUME")
MIN_OI     = os.getenv("MIN_OI")


def kalshi_fee(price):
    """Kalshi trading fee for 1 contract: round_up(0.07 * P * (1-P)) in dollars."""
    if FEE == "kalshi":
        return math.ceil(0.07 * price * (1 - price) * 100) / 100
    return float(FEE) if FEE not in ("", "0") else 0.0


def fetch():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur  = conn.cursor()
    where = ("kalshi_home_prob IS NOT NULL AND vegas_home_prob IS NOT NULL "
             "AND actual_winner IS NOT NULL")
    params = []
    if SEASON:
        where += " AND season = %s"; params.append(int(SEASON))
    if MAX_SPREAD is not None:
        where += " AND kalshi_spread <= %s"; params.append(float(MAX_SPREAD))
    if MIN_VOLUME is not None:
        where += " AND kalshi_volume >= %s"; params.append(float(MIN_VOLUME))
    if MIN_OI is not None:
        where += " AND kalshi_open_interest >= %s"; params.append(float(MIN_OI))
    cur.execute(f"""
        SELECT home_team, away_team, game_date,
               kalshi_home_prob, vegas_home_prob, actual_winner
        FROM kalshi_tracker WHERE {where} ORDER BY game_date
    """, params)
    rows = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    cur.close(); conn.close()
    return rows


def brier(pairs):
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def main():
    rows = fetch()
    if not rows:
        print("No both-priced resolved rows."); return
    liq = "".join(
        f"  {name}={val}" for name, val in
        (("MAX_SPREAD", MAX_SPREAD), ("MIN_VOLUME", MIN_VOLUME), ("MIN_OI", MIN_OI))
        if val is not None
    )
    print(f"\nBoth-priced resolved games: {len(rows)}"
          + (f"  (season {SEASON})" if SEASON else "")
          + f"\nEDGE={EDGE}  FEE={FEE}{liq}\n")

    # ── divergence distribution ──
    divs = [abs(float(r["kalshi_home_prob"]) - float(r["vegas_home_prob"])) for r in rows]
    print("Divergence |kalshi_home - vegas_home|")
    print("-" * 60)
    print(f"  mean={statistics.mean(divs):.3f}  median={statistics.median(divs):.3f}  max={max(divs):.3f}")
    for thr in (0.03, 0.05, 0.08, 0.10):
        print(f"  >= {thr:.2f}: {sum(1 for d in divs if d >= thr)} games")

    # ── calibration on the divergent subset: who is closer to truth? ──
    print("\nWho is closer to the outcome where they diverge >= EDGE")
    print("-" * 60)
    div_sub = [r for r in rows
               if abs(float(r["kalshi_home_prob"]) - float(r["vegas_home_prob"])) >= EDGE]
    if div_sub:
        kb = brier([(float(r["kalshi_home_prob"]), 1 if r["actual_winner"]=="home" else 0) for r in div_sub])
        vb = brier([(float(r["vegas_home_prob"]),  1 if r["actual_winner"]=="home" else 0) for r in div_sub])
        print(f"  n={len(div_sub)}   kalshi Brier={kb:.3f}   vegas Brier={vb:.3f}   "
              f"({'kalshi' if kb<vb else 'vegas'} closer)")
    else:
        print(f"  no games diverge >= {EDGE}")

    # ── EV backtest: buy the Kalshi side that's cheap vs Vegas, settle on outcome ──
    print(f"\nBacktest: buy 1 Kalshi contract when vegas_prob - kalshi_price >= {EDGE}")
    print("-" * 60)
    stake = pnl = 0.0
    bets = wins = 0
    for r in rows:
        kh = float(r["kalshi_home_prob"]); vh = float(r["vegas_home_prob"])
        home_won = r["actual_winner"] == "home"
        # consider both sides: home (price kh, fair vh) and away (price 1-kh, fair 1-vh)
        for price, fair, won in ((kh, vh, home_won), (1 - kh, 1 - vh, not home_won)):
            if fair - price >= EDGE:                 # Kalshi underpricing this side
                fee = kalshi_fee(price)
                payoff = (1 - price) if won else (-price)
                pnl += payoff - fee
                stake += price + fee                 # capital at risk
                bets += 1
                wins += 1 if won else 0
    if bets:
        print(f"  bets={bets}  win rate={wins/bets:.1%}  net P&L=${pnl:+.2f}  "
              f"staked=${stake:.2f}  ROI={pnl/stake:+.1%}")
        print(f"  (each contract settles $0-1; ROI is per dollar of price+fee risked)")
    else:
        print(f"  no qualifying bets at EDGE={EDGE} — markets too aligned at this threshold")

    print("\nReading: positive ROI that survives across EDGE thresholds and a full")
    print("season = a real arb edge. Tiny bet counts or ROI that flips sign with")
    print("EDGE = noise. Re-run with EDGE=0.05/0.08 and FEE=kalshi to stress it.")


if __name__ == "__main__":
    main()
