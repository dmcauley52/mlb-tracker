"""
backtest_pitcher_cycles.py

Two questions, same DFT-cycle machinery used for the hitter rolling-cycle work
(validate_rolling_cycle.py / backtest_cycle_variants.py):

  PART 1 — Do a starting pitcher's game_score cycles predict his NEXT start being
           a Quality Start (IP >= 6.0 AND ER <= 3)?
           The honest test is not "does the cycle correlate with QS" but "does the
           cycle add anything beyond persistence (his trailing mean game_score)."
           A pitcher who's simply been good lately will post QS regardless of any
           cycle. So every signal is tested twice:
             (a) univariate      QS ~ signal
             (b) encompassing     QS ~ trailing_mean_gs + signal   <- the real test

  PART 2 — Does the starting pitcher's cycle add to the game-winning prediction,
           the way Surge hitters were tested? For each resolved kalshi game we take
           sp_home / sp_away cycle signals, form sp_diff = home - away, and run
             outcome ~ logit(kalshi) + sp_diff
           A positive, significant sp_diff coef = the hotter starter's cycle carries
           win information the market hasn't priced.

Signal is built on game_score (frontend Pitcher FFT's preferred stat), STARTS ONLY,
strictly before the target date, same season, most-recent WINDOW starts.

Run:  python backtest_pitcher_cycles.py
Env:  DATABASE_URL (required), SEASON (optional, default = all seasons)
"""
import os, math
from collections import defaultdict
from bisect import bisect_left
import psycopg2
from dotenv import load_dotenv

from validate_model import logistic_fit, logit, norm_sf

load_dotenv()

SEASON      = os.getenv("SEASON")
QS_MIN_IP   = float(os.getenv("QS_MIN_IP", "6.0"))  # quality-start IP floor
QS_MAX_ER   = int(os.getenv("QS_MAX_ER", "3"))      # quality-start ER ceiling
WINDOW      = 15     # max trailing starts per pitcher
MIN_GAMES   = 8      # minimum trailing starts to attempt a cycle fit
MIN_PERIOD  = 4      # games -- mirrors analytics.js / hitter work
AMP_FRAC    = 0.10   # keep DFT components with amp >= AMP_FRAC * signal std
                     # (relative floor: game_score scale ~15, not woba's 0.005)


# ── DFT core (identical math to the hitter scripts) ──────────────────────────
def _dft(signal):
    N = len(signal)
    re = [0.0] * N
    im = [0.0] * N
    for k in range(N):
        for n in range(N):
            angle = 2 * math.pi * k * n / N
            re[k] += signal[n] * math.cos(angle)
            im[k] -= signal[n] * math.sin(angle)
        re[k] /= N
        im[k] /= N
    return re, im


def _kept_components(signal, std):
    N = len(signal)
    re, im = _dft(signal)
    floor = AMP_FRAC * std
    kept = []
    for k in range(1, N // 2 + 1):
        amp = 2 * math.sqrt(re[k] ** 2 + im[k] ** 2)
        if N / k < MIN_PERIOD:
            continue
        if amp < floor:
            continue
        kept.append((k, re[k], im[k]))
    return kept


def _reconstruct_at(kept, N, x):
    v = 0.0
    for k, rek, imk in kept:
        angle = 2 * math.pi * k * x / N
        v += rek * math.cos(angle) - imk * math.sin(angle)
    return v


def _decompose(values):
    """Return (mean, kept_components, N) for a trailing game_score window."""
    N = len(values)
    mean = sum(values) / N
    signal = [v - mean for v in values]
    std = math.sqrt(sum(s * s for s in signal) / N) if N else 0.0
    kept = _kept_components(signal, std) if std > 1e-9 else []
    return mean, kept, N


# ── per-pitcher signals (all on trailing game_score) ─────────────────────────
def sig_phase(values):
    """DFT phase bucket: +2 Surge / +1 Cruise / -1 Slide / -2 Rebuild / 0 none."""
    _, kept, N = _decompose(values)
    if not kept:
        return 0
    level = _reconstruct_at(kept, N, N + 1)
    slope = _reconstruct_at(kept, N, N + 2) - _reconstruct_at(kept, N, N)
    if   level >  0 and slope >  0: return +2
    elif level >  0 and slope <= 0: return +1
    elif level <= 0 and slope <= 0: return -1
    else:                           return -2


def sig_surge(values):
    """1 if next-start phase is Surge (above own mean + rising), else 0."""
    return 1 if sig_phase(values) == 2 else 0


def sig_forecast(values):
    """Continuous forecast of next-start game_score deviation from trailing mean."""
    _, kept, N = _decompose(values)
    if not kept:
        return 0.0
    return _reconstruct_at(kept, N, N + 1)


def sig_forecast_level(values):
    """Absolute forecast game_score = trailing mean + cyclic deviation."""
    mean, kept, N = _decompose(values)
    dev = _reconstruct_at(kept, N, N + 1) if kept else 0.0
    return mean + dev


def sig_due(values):
    """'Pitcher looks due' — contrarian. Negative of the cyclic forecast: a pitcher
    the cycle forecasts BELOW his recent mean scores positive (he's 'due' to bounce).
    This is exactly -sig_forecast, isolated so the win test can point it the right way."""
    return -sig_forecast(values)


def sig_slide(values):
    """'Due' bucket: 1 if next-start phase is Slide (below own mean + falling), the
    quadrant with the highest dominant-start rate in the ER<=2 cut. Else 0."""
    return 1 if sig_phase(values) == -1 else 0


def trailing_mean(values):
    """Persistence baseline: plain mean of trailing game_score."""
    return sum(values) / len(values)


# ── data loading ─────────────────────────────────────────────────────────────
def load_starts(cur, seasons):
    """(player_id, season) -> sorted [(date, game_score, qs)] for STARTS only."""
    cur.execute("""
        SELECT player_id, season, game_date, game_score,
               innings_pitched, earned_runs
        FROM pitcher_gamelogs
        WHERE is_starter = TRUE
          AND game_score IS NOT NULL
          AND player_id IS NOT NULL
          AND season = ANY(%s)
        ORDER BY player_id, season, game_date
    """, (seasons,))
    logs = defaultdict(list)
    for pid, season, gdate, gs, ip, er in cur.fetchall():
        qs = 1 if (ip is not None and float(ip) >= QS_MIN_IP
                   and er is not None and er <= QS_MAX_ER) else 0
        logs[(pid, season)].append((gdate, float(gs), qs))
    return logs


def trailing(logs, pid, season, game_date):
    """Most-recent WINDOW starts strictly before game_date. -> list of game_score."""
    rows = logs.get((pid, season))
    if not rows:
        return None
    dates = [d for d, _, _ in rows]
    idx = bisect_left(dates, game_date)
    window = rows[max(0, idx - WINDOW):idx]
    if len(window) < MIN_GAMES:
        return None
    return [gs for _, gs, _ in window]


# ── PART 1: cycle -> quality start ───────────────────────────────────────────
SIGNALS = [
    ("phase (+2/+1/-1/-2)", sig_phase),
    ("surge-only (1/0)",    sig_surge),
    ("forecast deviation",  sig_forecast),
    ("forecast level",      sig_forecast_level),
    ("DUE (-forecast dev)", sig_due),
    ("DUE slide-only (1/0)",sig_slide),
]


def part1(logs):
    print("\n" + "=" * 74)
    print("PART 1 — Do game_score cycles predict the NEXT start being a Quality Start?")
    print(f"         (QS defined as IP >= {QS_MIN_IP:g} AND ER <= {QS_MAX_ER})")
    print("=" * 74)

    # Build the evaluation set: every start with >= MIN_GAMES trailing starts.
    # Row = (trailing_mean_gs, {signal_name: value}, qs)
    rows = []
    phase_qs = defaultdict(lambda: [0, 0])  # phase -> [qs, total]
    for (pid, season), starts in logs.items():
        dates = [d for d, _, _ in starts]
        for i in range(MIN_GAMES, len(starts)):
            window = starts[max(0, i - WINDOW):i]
            vals = [gs for _, gs, _ in window]
            qs = starts[i][2]
            tmean = trailing_mean(vals)
            sigs = {name: fn(vals) for name, fn in SIGNALS}
            rows.append((tmean, sigs, qs))
            phase_qs[sigs["phase (+2/+1/-1/-2)"]][0] += qs
            phase_qs[sigs["phase (+2/+1/-1/-2)"]][1] += 1

    n = len(rows)
    base_rate = sum(r[2] for r in rows) / n if n else 0
    print(f"\nEvaluable starts (>= {MIN_GAMES} prior starts): {n}")
    print(f"Overall QS rate in this set: {base_rate:.3f}")

    print("\nQS rate by DFT phase bucket (does the phase itself sort QS?):")
    labels = {2: "Surge  (above+rising)", 1: "Cruise (above+falling)",
              -1: "Slide  (below+falling)", -2: "Rebuild(below+rising)",
              0: "Neutral(no cycle)"}
    for ph in (2, 1, -1, -2, 0):
        qs, tot = phase_qs[ph]
        if tot:
            print(f"  {labels[ph]:<24} n={tot:<5} QS rate={qs/tot:.3f}  "
                  f"(lift {qs/tot - base_rate:+.3f})")

    print("\nLogistic tests per signal  (QS is the target)")
    print("-" * 74)
    print(f"{'signal':<22} {'(a) univariate':>22} {'(b) beyond persistence':>26}")
    print(f"{'':<22} {'coef':>10}{'p':>12} {'coef':>12}{'p':>14}")
    print("-" * 74)

    ys = [r[2] for r in rows]
    for name, _ in SIGNALS:
        xs = [r[1][name] for r in rows]
        # (a) univariate  QS ~ signal
        try:
            b, se = logistic_fit([[x] for x in xs], ys)
            ua_c, ua_p = b[1], norm_sf(b[1] / se[1]) if se[1] > 0 else 1.0
        except ValueError:
            ua_c, ua_p = float("nan"), float("nan")
        # (b) encompassing  QS ~ trailing_mean + signal
        try:
            X = [[r[0], r[1][name]] for r in rows]
            b, se = logistic_fit(X, ys)
            en_c, en_p = b[2], norm_sf(b[2] / se[2]) if se[2] > 0 else 1.0
        except ValueError:
            en_c, en_p = float("nan"), float("nan")
        star_a = " *" if ua_p < 0.05 else "  "
        star_b = " *" if en_p < 0.05 else "  "
        print(f"{name:<22} {ua_c:>+10.4f}{ua_p:>10.3f}{star_a} "
              f"{en_c:>+10.4f}{en_p:>10.3f}{star_b}")

    # For reference: persistence alone.
    try:
        b, se = logistic_fit([[r[0]] for r in rows], ys)
        print(f"\n  reference: persistence alone  QS ~ trailing_mean_gs  "
              f"coef={b[1]:+.4f}  p={norm_sf(b[1]/se[1]):.3f}")
    except ValueError:
        pass
    print("\nReading PART 1: column (b) is the one that matters. A signal only earns")
    print("its keep if its coef stays positive AND significant AFTER trailing mean")
    print("game_score is in the model. Otherwise the 'cycle' is just recent form.")


# ── PART 2: SP cycle -> game winner, beyond the market ───────────────────────
def load_games(cur):
    where = "actual_winner IS NOT NULL AND game_pk IS NOT NULL"
    params = []
    if SEASON:
        where += " AND season = %s"
        params.append(int(SEASON))
    cur.execute(f"""
        SELECT game_pk, game_date, season, actual_winner, kalshi_home_prob
        FROM kalshi_tracker
        WHERE {where}
        ORDER BY game_date
    """, params)
    games = cur.fetchall()

    cur.execute("""
        SELECT game_pk, sp_home_id, sp_away_id
        FROM game_results
        WHERE game_pk = ANY(%s)
    """, ([g[0] for g in games],))
    sp = {gp: (h, a) for gp, h, a in cur.fetchall()}
    return games, sp


def eval_signal(fn, games, logs, sp):
    """Run one signal over a set of games. Returns (acc, n_pick, usable, reg_rows)."""
    reg = []      # (kalshi_home_prob, sp_diff, home_win)
    n_pick = correct = usable = 0
    for game_pk, gdate, season, winner, khp in games:
        ids = sp.get(game_pk)
        if not ids or khp is None:
            continue
        hid, aid = ids
        hv = trailing(logs, hid, season, gdate) if hid else None
        av = trailing(logs, aid, season, gdate) if aid else None
        if hv is None or av is None:
            continue
        diff = fn(hv) - fn(av)
        usable += 1
        reg.append((float(khp), diff, 1 if winner == "home" else 0))
        if diff != 0:
            n_pick += 1
            if (diff > 0) == (winner == "home"):
                correct += 1
    acc = correct / n_pick if n_pick else float("nan")
    return acc, n_pick, usable, reg


def encompass_diff(reg):
    """outcome ~ logit(kalshi) + sp_diff -> (coef, p) for sp_diff, or (nan, nan)."""
    if len(reg) < 30:
        return float("nan"), float("nan")
    X = [[logit(k), d] for k, d, _ in reg]
    y = [yy for _, _, yy in reg]
    try:
        b, se = logistic_fit(X, y)
        return b[2], (norm_sf(b[2] / se[2]) if se[2] > 0 else 1.0)
    except ValueError:
        return float("nan"), float("nan")


def _verdict(coef, p):
    if p != p:  # nan
        return "n/a"
    if p < 0.05 and coef > 0:
        return "*** EDGE ***"
    if p < 0.10 and coef > 0:
        return "marginal"
    return "no edge"


def part2(logs, games, sp):
    print("\n" + "=" * 74)
    print("PART 2 — Does the starting pitcher's cycle add to the win prediction?")
    print("=" * 74)
    print(f"\nResolved games with market price: {len(games)}"
          + (f"  (season {SEASON})" if SEASON else "  (all seasons)"))

    for name, fn in SIGNALS:
        acc, n_pick, usable, reg = eval_signal(fn, games, logs, sp)
        if len(reg) < 30:
            print(f"\n  [{name}] only {len(reg)} usable games — skipping")
            continue
        coef, p = encompass_diff(reg)
        print(f"\n  [{name}]  usable={usable}  standalone picks n={n_pick} "
              f"acc={acc:.3f}")
        print(f"     outcome ~ logit(kalshi) + sp_diff : "
              f"coef={coef:+.4f}  p={p:.3f}  -> {_verdict(coef, p)}")

    print("\nReading PART 2: an EDGE means the hotter starter's cycle predicts wins")
    print("the market underprices. 'no edge' means Kalshi already reflects SP form.")

    # ── Held-out check for the DUE signal ─────────────────────────────────────
    # We DISCOVERED the mean-reversion pattern on the full data, so an in-sample
    # test of a signal built on it is circular. Split chronologically and test the
    # 'due' signal on the last 30% it never influenced.
    print("\n" + "-" * 74)
    print("Held-out validation: DUE (-forecast dev) on chronological 70/30 split")
    print("(guards against fitting the contrarian pattern we found in-sample)")
    print("-" * 74)
    split = int(len(games) * 0.70)
    for label, gset in [("Discovery (first 70%)", games[:split]),
                        ("Held-out  (last 30%)",  games[split:])]:
        acc, n_pick, usable, reg = eval_signal(sig_due, gset, logs, sp)
        coef, p = encompass_diff(reg)
        print(f"  {label:<22} usable={usable:<4} picks n={n_pick:<4} "
              f"acc={acc:.3f}  coef={coef:+.4f}  p={p:.3f}  -> {_verdict(coef, p)}")
    print("  A real 'due' edge stays positive & holds up on the held-out tail.")


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()

    seasons = [int(SEASON)] if SEASON else None
    # load_starts needs the season list for the ANY() filter; fetch all if None.
    if seasons is None:
        cur.execute("SELECT DISTINCT season FROM pitcher_gamelogs WHERE is_starter")
        seasons = [r[0] for r in cur.fetchall()]
    logs = load_starts(cur, seasons)

    print(f"Loaded {sum(len(v) for v in logs.values())} starter appearances "
          f"across {len(logs)} pitcher-seasons")
    print(f"Window: last {WINDOW} starts | min {MIN_GAMES} to fit | "
          f"period >= {MIN_PERIOD} | amp floor {AMP_FRAC}*std")

    part1(logs)

    games, sp = load_games(cur)
    part2(logs, games, sp)

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
