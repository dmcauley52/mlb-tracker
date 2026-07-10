"""
scan_f5_cross.py
Cross-venue first-5-innings totals scan: Kalshi KXMLBF5TOTAL rung N
("Over N-0.5 runs") vs Polymarket "1st 5 Innings O/U N-0.5" — the only
same-instrument overlap between the venues besides the moneyline.

For each matched rung on today's slate, prints both venues' top-of-book, the
mid divergence, and the executable arb margin in both directions (net of the
Kalshi fee). First run (2026-07-10, 75 rungs / 15 games): zero positive arbs,
median divergence +0.5¢ — both venues appear to share a market maker. Large
divergences are empty Poly books (0.01/0.99 placeholder quotes), not signal.

Run:  python scan_f5_cross.py   (one live snapshot; no DB writes)
"""
import sys, json, math, time, re, statistics, requests
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
import fetch_arb_quotes as f   # reuses TEAM_ABBRS, MONTHS, mlb_games, poly_events, clob_top

KBASE = "https://api.elections.kalshi.com/trade-api/v2"


def kfee(p):
    return math.ceil(0.07 * p * (1 - p) * 100) / 100


def main():
    games = f.mlb_games()
    p_events = f.poly_events()
    km = requests.get(f"{KBASE}/markets",
                      params={"limit": 200, "series_ticker": "KXMLBF5TOTAL",
                              "status": "open"}, timeout=20).json()["markets"]
    kalshi_by_event = {}
    for m in km:
        kalshi_by_event.setdefault(m["ticker"].rsplit("-", 1)[0], []).append(m)

    divs, best_arbs = [], []
    for g in games:
        habbr = f.TEAM_ABBRS.get(g["home"], [])
        aabbr = f.TEAM_ABBRS.get(g["away"], [])
        yr, mo, dy = str(g["game_date"]).split("-")
        dp = yr[2:] + f.MONTHS[int(mo) - 1] + dy
        kev = next((e for e in kalshi_by_event
                    if dp in e and any(h in e for h in habbr) and any(a in e for a in aabbr)), None)
        pev = None
        for ev in p_events:
            t = (ev.get("title") or "").lower()
            if g["home"].lower() in t and g["away"].lower() in t:
                st = ev.get("startTime") or ""
                try:
                    ev_dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if abs((ev_dt - g["game_dt"]).total_seconds()) <= 3 * 3600:
                    pev = ev
                    break
        if not kev or not pev:
            continue

        krungs = {}
        for m in kalshi_by_event[kev]:
            mm = re.search(r"Over (\d+\.5)", m.get("yes_sub_title") or "")
            if mm and m.get("yes_bid_dollars") and m.get("yes_ask_dollars"):
                krungs[float(mm.group(1))] = (float(m["yes_bid_dollars"]),
                                              float(m["yes_ask_dollars"]))

        for m in pev.get("markets", []):
            mm = re.search(r"1st 5 Innings O/U (\d+\.5)", m.get("question") or "")
            if not mm or float(mm.group(1)) not in krungs:
                continue
            t = float(mm.group(1))
            outs = json.loads(m["outcomes"]) if isinstance(m.get("outcomes"), str) else m["outcomes"]
            tids = json.loads(m["clobTokenIds"]) if isinstance(m.get("clobTokenIds"), str) else m["clobTokenIds"]
            if outs != ["Over", "Under"]:
                continue
            try:
                ob = f.clob_top(tids[0])   # Over book
                ub = f.clob_top(tids[1])   # Under book
            except Exception:
                continue
            time.sleep(0.05)
            kb, ka = krungs[t]
            if None in (ob["bid"], ob["ask"], ub["ask"]):
                continue
            div = (kb + ka) / 2 - (ob["bid"] + ob["ask"]) / 2
            divs.append(div)
            arb_kp = 1 - ka - ub["ask"] - kfee(ka)        # buy K Over + buy P Under
            arb_pk = kb - ob["ask"] - kfee(1 - kb)        # buy P Over + buy K No
            best_arbs.append(max(arb_kp, arb_pk))
            flag = " <-- ARB" if best_arbs[-1] > 0 else ""
            print(f"{g['away'][:12]:>12} @ {g['home'][:12]:<12} O{t}: "
                  f"K={kb:.2f}/{ka:.2f}  P={ob['bid']:.2f}/{ob['ask']:.2f}  "
                  f"div={div:+.3f}  arbKP={arb_kp:+.3f} arbPK={arb_pk:+.3f}{flag}")

    if divs:
        print(f"\n{len(divs)} matched rungs | mid divergence: "
              f"mean={statistics.mean(divs):+.4f} median={statistics.median(divs):+.4f} "
              f"max|.|={max(abs(d) for d in divs):.3f}")
        print(f"positive-arb rungs: {sum(1 for a in best_arbs if a > 0)}  "
              f"best margin: {max(best_arbs):+.3f}")


if __name__ == "__main__":
    main()
