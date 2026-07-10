"""
fetch_arb_quotes.py
Cross-venue arb-window logger: one poll of executable top-of-book quotes on
Kalshi and Polymarket MLB winner markets, written to arb_quotes (append-only).

Purpose: MEASUREMENT, not execution. A true arbitrage exists when buying one
team's YES on venue A and the other team's YES on venue B costs < $1 after
fees. Divergence snapshots can't establish this — both legs must be fillable
at the same moment — so this script logs synchronized asks + depth every
~20 min (arb_quotes.yml) and the analysis later counts how often net-positive
windows actually open, how wide, and how long they persist.

Margins logged per $1 contract pair (negative = no window):
  margin_kh_pa  = 1 - k_home_ask - p_away_ask - kalshi_fee(k_home_ask)
  margin_ka_ph  = 1 - k_away_ask - p_home_ask - kalshi_fee(k_away_ask)
  margin_kalshi = 1 - k_home_ask - k_away_ask - fee(both)   (single-venue)
  margin_poly   = 1 - p_home_ask - p_away_ask               (no Poly fee)

Run:  python fetch_arb_quotes.py     (one poll; cron does the repetition)
Env:  DATABASE_URL (required)
"""
import os, math, time, json, requests
from datetime import datetime, timedelta, timezone
import psycopg2
from dotenv import load_dotenv

load_dotenv()

KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
GAMMA_URL   = "https://gamma-api.polymarket.com"
CLOB_URL    = "https://clob.polymarket.com"
MONTHS      = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]

now_utc = datetime.now(timezone.utc)

# Kalshi event tickers embed team abbreviations — same map as
# fetch_kalshi_outcomes.py (kept local; that module runs its pipeline at import).
TEAM_ABBRS = {
    "Arizona Diamondbacks":   ["AZ","ARI"],   "Atlanta Braves":         ["ATL"],
    "Baltimore Orioles":      ["BAL"],        "Boston Red Sox":         ["BOS"],
    "Chicago Cubs":           ["CHC"],        "Chicago White Sox":      ["CWS"],
    "Cincinnati Reds":        ["CIN"],        "Cleveland Guardians":    ["CLE"],
    "Colorado Rockies":       ["COL"],        "Detroit Tigers":         ["DET"],
    "Houston Astros":         ["HOU"],        "Kansas City Royals":     ["KC","KCR"],
    "Los Angeles Angels":     ["LAA"],        "Los Angeles Dodgers":    ["LAD"],
    "Miami Marlins":          ["MIA"],        "Milwaukee Brewers":      ["MIL"],
    "Minnesota Twins":        ["MIN"],        "New York Mets":          ["NYM"],
    "New York Yankees":       ["NYY"],        "Athletics":              ["ATH","OAK"],
    "Philadelphia Phillies":  ["PHI"],        "Pittsburgh Pirates":     ["PIT"],
    "San Diego Padres":       ["SD","SDP"],   "San Francisco Giants":   ["SF","SFG"],
    "Seattle Mariners":       ["SEA"],        "St. Louis Cardinals":    ["STL"],
    "Tampa Bay Rays":         ["TB","TBR"],   "Texas Rangers":          ["TEX"],
    "Toronto Blue Jays":      ["TOR"],        "Washington Nationals":   ["WSH","WAS"],
}


def kalshi_fee(price):
    """Kalshi trading fee for 1 contract: round_up(0.07 * P * (1-P)) in dollars."""
    return math.ceil(0.07 * price * (1 - price) * 100) / 100


def et_date(dt_utc):
    """US-Eastern calendar date; fixed -4h is safe for MLB start times."""
    return (dt_utc - timedelta(hours=4)).strftime("%Y-%m-%d")


# ── MLB schedule ─────────────────────────────────────────────────────────────
def mlb_games():
    """Non-final games on today's (ET) schedule date."""
    sched_date = et_date(now_utc)
    r = requests.get(
        f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={sched_date}",
        timeout=15,
    )
    r.raise_for_status()
    games = []
    for d in r.json().get("dates", []):
        for g in d.get("games", []):
            if g.get("status", {}).get("abstractGameState") == "Final":
                continue
            gt = g.get("gameDate", "")
            try:
                game_dt = datetime.fromisoformat(gt.replace("Z", "+00:00"))
            except ValueError:
                continue
            games.append({
                "game_pk":   g["gamePk"],
                "game_date": g.get("officialDate") or d["date"],
                "home":      g["teams"]["home"]["team"]["name"],
                "away":      g["teams"]["away"]["team"]["name"],
                "game_dt":   game_dt,
            })
    return games


# ── Kalshi ───────────────────────────────────────────────────────────────────
def kalshi_data():
    mk = requests.get(f"{KALSHI_BASE}/markets?limit=200&series_ticker=KXMLBGAME&status=open", timeout=15)
    ev = requests.get(f"{KALSHI_BASE}/events?limit=100&series_ticker=KXMLBGAME&status=open", timeout=15)
    mk.raise_for_status(); ev.raise_for_status()
    mkt_by_event = {}
    for m in mk.json().get("markets", []):
        mkt_by_event.setdefault(m["event_ticker"], []).append(m)
    return ev.json().get("events", []), mkt_by_event


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def kalshi_quotes(home_team, away_team, game_date, game_dt, events, mkt_by_event):
    """Top-of-book YES quotes per team, or None. Doubleheaders: the ticker
    embeds the ET start time (e.g. 26JUL072210), so prefer the closest one."""
    home_abbrs = TEAM_ABBRS.get(home_team, [])
    away_abbrs = TEAM_ABBRS.get(away_team, [])
    if not home_abbrs or not away_abbrs:
        return None
    yr, mo, dy = str(game_date).split("-")
    date_prefix = yr[2:] + MONTHS[int(mo)-1] + dy
    game_hhmm = int((game_dt - timedelta(hours=4)).strftime("%H%M"))

    candidates = []
    for ev in events:
        ticker = ev["event_ticker"].upper()
        if date_prefix not in ticker:
            continue
        if not any(h.upper() in ticker for h in home_abbrs):
            continue
        if not any(a.upper() in ticker for a in away_abbrs):
            continue
        try:
            hhmm = int(ticker.split(date_prefix)[1][:4])
        except (IndexError, ValueError):
            hhmm = game_hhmm
        candidates.append((abs(hhmm - game_hhmm), ev))
    if not candidates:
        return None
    ev = min(candidates)[1]

    quotes = {}
    for m in mkt_by_event.get(ev["event_ticker"], []):
        sub = (m.get("yes_sub_title") or "").strip()
        side = None
        if sub and (home_team.startswith(sub) or sub.startswith(home_team)):
            side = "home"
        elif sub and (away_team.startswith(sub) or sub.startswith(away_team)):
            side = "away"
        if side:
            quotes[side] = {
                "bid":      _num(m.get("yes_bid_dollars")),
                "ask":      _num(m.get("yes_ask_dollars")),
                "ask_size": _num(m.get("yes_ask_size_fp")),
            }
    return quotes if "home" in quotes and "away" in quotes else None


# ── Polymarket ───────────────────────────────────────────────────────────────
def poly_events():
    r = requests.get(
        f"{GAMMA_URL}/events?tag_id=100381&active=true&closed=false&limit=500",
        timeout=20,
    )
    r.raise_for_status()
    return [e for e in r.json() if " vs" in (e.get("title") or "")]


def clob_top(token_id):
    """Best bid/ask + ask size from the CLOB book (levels aren't order-guaranteed)."""
    r = requests.get(f"{CLOB_URL}/book", params={"token_id": token_id}, timeout=10)
    r.raise_for_status()
    d = r.json()
    bids = [(float(l["price"]), float(l["size"])) for l in d.get("bids", [])]
    asks = [(float(l["price"]), float(l["size"])) for l in d.get("asks", [])]
    best_bid = max(bids)[0] if bids else None
    best_ask = min(asks) if asks else None
    return {
        "bid":      best_bid,
        "ask":      best_ask[0] if best_ask else None,
        "ask_size": best_ask[1] if best_ask else None,
    }


def poly_quotes(home_team, away_team, game_dt, events):
    """Top-of-book quotes per team, or None. Consecutive-day series: match the
    event whose startTime falls on the same ET date as the game."""
    hname, aname = home_team.lower(), away_team.lower()
    gdate = et_date(game_dt)
    candidates = []
    for ev in events:
        t = (ev.get("title") or "").lower()
        if hname not in t or aname not in t:
            continue
        st = ev.get("startTime") or ev.get("startDate") or ""
        try:
            ev_dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
        except ValueError:
            continue
        if et_date(ev_dt) != gdate:
            continue
        diff = abs((ev_dt - game_dt).total_seconds())
        # Doubleheaders: same-date matching alone assigned game 1's lone Poly
        # event to game 2 as well (2026-07-07 STL/MIL — game 1's resolved market
        # logged against game 2's book as a phantom 45% "arb"). Only trust an
        # event whose startTime is near this game's first pitch.
        if diff > 3 * 3600:
            continue
        candidates.append((diff, ev))
    if not candidates:
        return None
    ev = min(candidates)[1]

    for m in ev.get("markets", []):
        try:
            outcomes = m.get("outcomes")
            outcomes = json.loads(outcomes) if isinstance(outcomes, str) else (outcomes or [])
            tids = m.get("clobTokenIds")
            tids = json.loads(tids) if isinstance(tids, str) else (tids or [])
        except ValueError:
            continue
        if len(outcomes) != 2 or len(tids) != 2 or "Yes" in outcomes:
            continue
        lo = [o.lower() for o in outcomes]
        hi = next((i for i, o in enumerate(lo) if o in hname or hname in o), None)
        ai = next((i for i, o in enumerate(lo) if o in aname or aname in o), None)
        if hi is None or ai is None or hi == ai:
            return None
        try:
            q = {"home": clob_top(tids[hi]), "away": clob_top(tids[ai])}
        except Exception:
            return None
        time.sleep(0.1)
        return q
    return None


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    games = mlb_games()
    print(f"Poll {now_utc.strftime('%Y-%m-%d %H:%M')}Z — {len(games)} non-final games")
    if not games:
        return

    try:
        events, mkt_by_event = kalshi_data()
    except Exception as e:
        print(f"  Kalshi fetch failed: {e}")
        events, mkt_by_event = [], {}
    try:
        p_events = poly_events()
    except Exception as e:
        print(f"  Polymarket fetch failed: {e}")
        p_events = []

    conn = psycopg2.connect(os.getenv("DATABASE_URL"))
    cur = conn.cursor()
    logged = windows = 0

    for g in games:
        kq = kalshi_quotes(g["home"], g["away"], g["game_date"], g["game_dt"],
                           events, mkt_by_event) if events else None
        pq = poly_quotes(g["home"], g["away"], g["game_dt"], p_events) if p_events else None
        if not kq and not pq:
            continue

        kh = kq["home"] if kq else {}
        ka = kq["away"] if kq else {}
        ph = pq["home"] if pq else {}
        pa = pq["away"] if pq else {}

        def margin_cross(k_ask, p_ask):
            if k_ask is None or p_ask is None or k_ask <= 0:
                return None
            return round(1 - k_ask - p_ask - kalshi_fee(k_ask), 4)

        m_kh_pa = margin_cross(kh.get("ask"), pa.get("ask"))
        m_ka_ph = margin_cross(ka.get("ask"), ph.get("ask"))
        m_kk = m_pp = None
        if kh.get("ask") is not None and ka.get("ask") is not None:
            m_kk = round(1 - kh["ask"] - ka["ask"]
                         - kalshi_fee(kh["ask"]) - kalshi_fee(ka["ask"]), 4)
        if ph.get("ask") is not None and pa.get("ask") is not None:
            m_pp = round(1 - ph["ask"] - pa["ask"], 4)
        margins = [m for m in (m_kh_pa, m_ka_ph, m_kk, m_pp) if m is not None]
        best = max(margins) if margins else None

        cur.execute("""
            INSERT INTO arb_quotes
            (game_pk, game_date, home_team, away_team, game_time_utc, started,
             k_home_bid, k_home_ask, k_home_ask_size,
             k_away_bid, k_away_ask, k_away_ask_size,
             p_home_bid, p_home_ask, p_home_ask_size,
             p_away_bid, p_away_ask, p_away_ask_size,
             margin_kh_pa, margin_ka_ph, margin_kalshi, margin_poly, best_margin)
            VALUES (%s,%s,%s,%s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s, %s,%s,%s,%s,%s)
        """, (
            g["game_pk"], g["game_date"], g["home"], g["away"],
            g["game_dt"], now_utc >= g["game_dt"],
            kh.get("bid"), kh.get("ask"), kh.get("ask_size"),
            ka.get("bid"), ka.get("ask"), ka.get("ask_size"),
            ph.get("bid"), ph.get("ask"), ph.get("ask_size"),
            pa.get("bid"), pa.get("ask"), pa.get("ask_size"),
            m_kh_pa, m_ka_ph, m_kk, m_pp, best,
        ))
        logged += 1
        if best is not None and best > 0:
            windows += 1
            print(f"  ARB WINDOW {g['away']} @ {g['home']}  best={best:+.4f}  "
                  f"(kh+pa={m_kh_pa}  ka+ph={m_ka_ph}  kk={m_kk}  pp={m_pp})")
        else:
            print(f"  {g['away']} @ {g['home']}  best={'n/a' if best is None else f'{best:+.4f}'}")

    conn.commit()
    cur.close()
    conn.close()
    print(f"{logged} rows logged, {windows} positive windows")


if __name__ == "__main__":
    main()
