# Handoff — Kalshi prediction vs. arbitrage investigation

_Last updated 2026-06-27. This note travels with the repo so work can resume on any machine._

## Why this exists
Started from an mlb-arb-bot roadmap flag: the upstream model that populates
`model_home_prob` in the `kalshi_tracker` table "adds no information over the
market price." That model lives in **this** repo (`fetch_kalshi_outcomes.py`).

## What we found (settled)
- **Internal run model: no edge.** `validate_model.py` on 747 resolved games —
  forecast-encompassing regression gives the model a coefficient ~0 (p=0.23)
  once the market price is included. Market (Kalshi/Vegas) fully encompasses it.
  Accuracy/Brier: kalshi 54.0%/0.246, vegas 54.0%/0.246, model 53.3%/0.251.
- **Cycle/FFT signal: also dead.** 46.9% accuracy (below a coin flip), negative
  encompassing coefficient. Not an edge.
- **When the model disagrees with the market, the market is right ~51%** — so the
  arb bot's `strong_edge` (largest `prob_gap`) is effectively an anti-signal.

**Conclusion:** outcome prediction (model tuning, cycle signals) is a dead end.
Don't re-litigate without new evidence.

## The only remaining edge avenue: cross-market arbitrage
Exploit Kalshi mispricing vs. the Vegas (DraftKings) sharp line — NOT predicting
winners. `analyze_divergence.py` runs a fee-aware EV backtest.

- Raw backtest shows +ROI (+44% at EDGE=0.03) **but it's not trustworthy yet**:
  only ~29 bets, dominated by phantom quotes (e.g. a game with Vegas 9% vs Kalshi
  50% — a stale/illiquid Kalshi price you could never actually fill).
- Kalshi and Vegas otherwise agree to ~1pp (mean |divergence| = 0.010).

## NEXT STEP (time-gated — nothing to do until data accrues)
1. `fetch_kalshi_outcomes.py` now logs `kalshi_spread`, `kalshi_volume`,
   `kalshi_open_interest` on the morning snapshot. **Forward-only** — existing
   rows are NULL; `kalshi_morning.yml` (14:00 UTC) fills them nightly.
2. After ~2 weeks of data, re-run `analyze_divergence.py` **filtered to tradeable
   games**: `kalshi_spread <= ~0.02` and a `kalshi_volume`/`open_interest` floor.
   This strips the phantom quotes.
3. If a real +ROI survives across EDGE thresholds (0.03 / 0.05 / 0.08) on the
   filtered set → arb has legs, consider paper-trading forward. If not → abandon.
4. Also audit `find_kalshi_prob` team-matching against the top-divergence rows
   (some large gaps may be wrong-market matches, not real mispricing).

## How to run
```
cp .env.example .env          # then fill DATABASE_URL (Supabase pooler URI)
python validate_model.py      # model vs market (env: SEASON optional)
python analyze_divergence.py  # arb backtest (env: EDGE, FEE, SEASON)
```
Scripts are pure-Python by design (no numpy/scipy installed). `.env` is gitignored
— copy DATABASE_URL from the sibling `mlb-arb-bot/.env` or Supabase dashboard.

## Key files
| File | Purpose |
|------|---------|
| `validate_model.py` | Model/cycle vs market: accuracy, Brier, encompassing regression |
| `analyze_divergence.py` | Kalshi-vs-Vegas divergence + fee-aware EV backtest |
| `fetch_kalshi_outcomes.py` | `find_kalshi_prob` returns {prob, spread, volume, open_interest}; morning insert logs liquidity |
| `migrate_kalshi_tracker.py` | Adds the 3 `kalshi_*` liquidity columns (idempotent) |
