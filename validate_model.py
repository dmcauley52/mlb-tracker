"""
validate_model.py
Does the internal run model add any information over the market price?

Reads resolved rows from kalshi_tracker and reports, for each predictor
(model / kalshi / vegas / cycle):
  - hit rate, Brier score, log-loss
  - head-to-head accuracy on the games where the two picks disagree

Headline test (forecast-encompassing / Fair-Shiller regression):
  fit  P(home win) ~ b0 + b1*logit(kalshi) + b2*logit(model)
  If the market already contains everything the model knows, b2 collapses to
  ~0 and is not significant, while b1 stays near 1. Same test is run for
  model-vs-vegas.

Pure-Python logistic regression (Newton/IRLS) — no numpy/scipy needed.

Run:  python validate_model.py
Env:  DATABASE_URL  (required)
      SEASON        (optional, default = all seasons)
"""
import os, math
import psycopg2
from dotenv import load_dotenv

load_dotenv()

SEASON = os.getenv("SEASON")  # None = all seasons
EPS    = 0.02                  # prob clamp so logit() stays finite


# ── tiny stats helpers ─────────────────────────────────────────────────────
def clamp(p):
    return min(1 - EPS, max(EPS, p))

def logit(p):
    p = clamp(p)
    return math.log(p / (1 - p))

def norm_sf(z):
    """Two-sided p-value from a z-score via erf."""
    return 2 * (1 - 0.5 * (1 + math.erf(abs(z) / math.sqrt(2))))


def matinv(A):
    """Gauss-Jordan inverse of a small square matrix (list of lists)."""
    n = len(A)
    M = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            raise ValueError("singular matrix")
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        M[col] = [x / pv for x in M[col]]
        for r in range(n):
            if r == col:
                continue
            f = M[r][col]
            if f:
                M[r] = [a - f * b for a, b in zip(M[r], M[col])]
    return [row[n:] for row in M]


def logistic_fit(X, y, iters=100):
    """
    Newton-Raphson logistic regression.
    X: list of feature rows (WITHOUT intercept), y: list of 0/1.
    Returns (beta, se) including a leading intercept term.
    """
    Xd = [[1.0] + row for row in X]          # prepend intercept
    k  = len(Xd[0])
    beta = [0.0] * k
    for _ in range(iters):
        # gradient g = X^T (y - p);  Hessian H = X^T W X
        g = [0.0] * k
        H = [[0.0] * k for _ in range(k)]
        for xi, yi in zip(Xd, y):
            eta = sum(b * x for b, x in zip(beta, xi))
            p   = 1 / (1 + math.exp(-max(-30, min(30, eta))))
            w   = p * (1 - p)
            for a in range(k):
                g[a] += (yi - p) * xi[a]
                for b in range(k):
                    H[a][b] += w * xi[a] * xi[b]
        Hinv = matinv(H)
        step = [sum(Hinv[a][b] * g[b] for b in range(k)) for a in range(k)]
        beta = [ba + st for ba, st in zip(beta, step)]
        if max(abs(s) for s in step) < 1e-8:
            break
    cov = matinv(H)
    se  = [math.sqrt(max(cov[i][i], 0.0)) for i in range(k)]
    return beta, se


def brier(probs, ys):
    return sum((p - y) ** 2 for p, y in zip(probs, ys)) / len(ys)

def logloss(probs, ys):
    s = 0.0
    for p, y in zip(probs, ys):
        p = clamp(p)
        s += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return s / len(ys)


# ── load data ──────────────────────────────────────────────────────────────
def fetch_rows():
    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur  = conn.cursor()
    where = "actual_winner IS NOT NULL"
    params = []
    if SEASON:
        where += " AND season = %s"
        params.append(int(SEASON))
    cur.execute(f"""
        SELECT actual_winner,
               model_home_prob, model_pick, model_correct,
               kalshi_home_prob, kalshi_pick, kalshi_correct,
               vegas_home_prob,  vegas_pick,  vegas_correct,
               cycle_home_prob,  cycle_pick,  cycle_correct
        FROM kalshi_tracker
        WHERE {where}
        ORDER BY game_date
    """, params)
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close(); conn.close()
    return rows


# ── reporting ──────────────────────────────────────────────────────────────
def f(x):
    return "n/a" if x is None else f"{float(x):.3f}"

def summarize(name, rows, prob_key, pick_key, correct_key):
    """Hit rate / Brier / log-loss for one predictor."""
    picks = [r for r in rows if r[correct_key] is not None and r[pick_key]]
    n_pick = len(picks)
    acc = sum(1 for r in picks if r[correct_key]) / n_pick if n_pick else None

    scored = [(float(r[prob_key]), 1 if r["actual_winner"] == "home" else 0)
              for r in rows if r[prob_key] is not None]
    bs = brier([p for p, _ in scored], [y for _, y in scored]) if scored else None
    ll = logloss([p for p, _ in scored], [y for _, y in scored]) if scored else None
    print(f"  {name:<8} n={n_pick:<4} acc={f(acc)}   "
          f"Brier={f(bs)} (n={len(scored)})   logloss={f(ll)}")
    return acc, n_pick


def head_to_head(rows, a_pick, a_corr, b_pick, b_corr, label_a, label_b):
    sub = [r for r in rows
           if r[a_pick] and r[b_pick] and r[a_pick] != r[b_pick]
           and r[a_corr] is not None and r[b_corr] is not None]
    if not sub:
        print(f"  {label_a} vs {label_b}: no disagreement rows")
        return
    a_win = sum(1 for r in sub if r[a_corr])
    b_win = sum(1 for r in sub if r[b_corr])
    n = len(sub)
    print(f"  When {label_a} and {label_b} DISAGREE (n={n}):  "
          f"{label_a} right {a_win}/{n} ({a_win/n:.1%})   "
          f"{label_b} right {b_win}/{n} ({b_win/n:.1%})")


def encompass(rows, base_key, add_key, base_lbl, add_lbl):
    """Does `add` add info beyond `base`?  outcome ~ logit(base) + logit(add)."""
    data = [(float(r[base_key]), float(r[add_key]),
             1 if r["actual_winner"] == "home" else 0)
            for r in rows if r[base_key] is not None and r[add_key] is not None]
    if len(data) < 30:
        print(f"  {add_lbl} vs {base_lbl}: only {len(data)} rows — need ~30+, skipping")
        return
    X = [[logit(b), logit(a)] for b, a, _ in data]
    y = [yy for _, _, yy in data]
    try:
        beta, se = logistic_fit(X, y)
    except ValueError as e:
        print(f"  {add_lbl} vs {base_lbl}: fit failed ({e})")
        return
    names = ["intercept", f"logit({base_lbl})", f"logit({add_lbl})"]
    print(f"  outcome ~ logit({base_lbl}) + logit({add_lbl})   (n={len(data)})")
    for nm, b, s in zip(names, beta, se):
        z = b / s if s > 0 else 0.0
        star = " *" if norm_sf(z) < 0.05 else ""
        print(f"      {nm:<22} coef={b:+.3f}  se={s:.3f}  z={z:+.2f}  p={norm_sf(z):.3f}{star}")
    add_z = beta[2] / se[2] if se[2] > 0 else 0.0
    verdict = ("ADDS info" if norm_sf(add_z) < 0.05 and abs(beta[2]) > 0.1
               else "NO added info")
    print(f"      -> {add_lbl} {verdict} beyond {base_lbl} "
          f"(coef {beta[2]:+.3f}, p={norm_sf(add_z):.3f})\n")


def main():
    rows = fetch_rows()
    print(f"\nResolved games: {len(rows)}"
          + (f"  (season {SEASON})" if SEASON else "  (all seasons)") + "\n")
    if not rows:
        print("No resolved rows — nothing to validate.")
        return

    print("Per-predictor accuracy / calibration")
    print("-" * 60)
    summarize("model",  rows, "model_home_prob",  "model_pick",  "model_correct")
    summarize("kalshi", rows, "kalshi_home_prob", "kalshi_pick", "kalshi_correct")
    summarize("vegas",  rows, "vegas_home_prob",  "vegas_pick",  "vegas_correct")
    summarize("cycle",  rows, "cycle_home_prob",  "cycle_pick",  "cycle_correct")

    print("\nHead-to-head on disagreements")
    print("-" * 60)
    head_to_head(rows, "model_pick", "model_correct", "kalshi_pick", "kalshi_correct", "model", "kalshi")
    head_to_head(rows, "model_pick", "model_correct", "vegas_pick",  "vegas_correct",  "model", "vegas")
    head_to_head(rows, "cycle_pick", "cycle_correct", "kalshi_pick", "kalshi_correct", "cycle", "kalshi")

    print("\nForecast-encompassing test (the headline)")
    print("-" * 60)
    encompass(rows, "kalshi_home_prob", "model_home_prob", "kalshi", "model")
    encompass(rows, "vegas_home_prob",  "model_home_prob", "vegas",  "model")
    encompass(rows, "kalshi_home_prob", "cycle_home_prob", "kalshi", "cycle")

    print("Reading: if logit(model) coef is ~0 and p>0.05, the model carries no")
    print("information the market price doesn't already have. A logit(kalshi) coef")
    print("near 1.0 with model coef ~0 means the market fully encompasses the model.")


if __name__ == "__main__":
    main()
