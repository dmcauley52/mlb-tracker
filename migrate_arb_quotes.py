"""
migrate_arb_quotes.py
Creates the arb_quotes table — append-only time series of executable
top-of-book quotes on Kalshi + Polymarket MLB winner markets, logged by
fetch_arb_quotes.py every ~20 min during game hours. Used to measure how
often true cross-venue arbitrage windows (price sum < $1 after fees) open,
how wide they get, and how long they persist — before building any
execution machinery.
Run once: python migrate_arb_quotes.py
"""
import psycopg2, os
from dotenv import load_dotenv
load_dotenv()

conn = psycopg2.connect(os.getenv("DATABASE_URL"))
cur  = conn.cursor()

cur.execute("""
CREATE TABLE IF NOT EXISTS arb_quotes (
    id              BIGSERIAL PRIMARY KEY,
    ts              TIMESTAMPTZ DEFAULT NOW(),
    game_pk         INTEGER,
    game_date       DATE,
    home_team       TEXT,
    away_team       TEXT,
    game_time_utc   TIMESTAMPTZ,
    started         BOOLEAN,
    -- Kalshi top of book (YES contract per team), dollars
    k_home_bid      NUMERIC(6,4),
    k_home_ask      NUMERIC(6,4),
    k_home_ask_size NUMERIC(12,2),
    k_away_bid      NUMERIC(6,4),
    k_away_ask      NUMERIC(6,4),
    k_away_ask_size NUMERIC(12,2),
    -- Polymarket top of book (outcome token per team), dollars
    p_home_bid      NUMERIC(6,4),
    p_home_ask      NUMERIC(6,4),
    p_home_ask_size NUMERIC(12,2),
    p_away_bid      NUMERIC(6,4),
    p_away_ask      NUMERIC(6,4),
    p_away_ask_size NUMERIC(12,2),
    -- Net arb margins per $1 pair (negative = no window). Kalshi fee included;
    -- Polymarket charges no trading fee.
    margin_kh_pa    NUMERIC(7,4),   -- buy Kalshi home YES + Poly away
    margin_ka_ph    NUMERIC(7,4),   -- buy Kalshi away YES + Poly home
    margin_kalshi   NUMERIC(7,4),   -- buy both sides on Kalshi alone
    margin_poly     NUMERIC(7,4),   -- buy both sides on Polymarket alone
    best_margin     NUMERIC(7,4)
);
""")
cur.execute("CREATE INDEX IF NOT EXISTS arb_quotes_game_date_idx ON arb_quotes (game_date);")
cur.execute("CREATE INDEX IF NOT EXISTS arb_quotes_best_margin_idx ON arb_quotes (best_margin);")

conn.commit()
cur.close()
conn.close()
print("arb_quotes table ready")
