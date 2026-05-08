const crypto = require("crypto");

const BASE_URL = "https://api.elections.kalshi.com/trade-api/v2";

// MLB team full name → abbreviations used in Kalshi event tickers
// Derived from observed ticker format: KXMLBGAME-26MAY042145SDSF → SD vs SF
const TEAM_ABBRS = {
  "Arizona Diamondbacks":   ["AZ",  "ARI"],
  "Atlanta Braves":         ["ATL"],
  "Baltimore Orioles":      ["BAL"],
  "Boston Red Sox":         ["BOS"],
  "Chicago Cubs":           ["CHC"],
  "Chicago White Sox":      ["CWS"],
  "Cincinnati Reds":        ["CIN"],
  "Cleveland Guardians":    ["CLE"],
  "Colorado Rockies":       ["COL"],
  "Detroit Tigers":         ["DET"],
  "Houston Astros":         ["HOU"],
  "Kansas City Royals":     ["KC",  "KCR"],
  "Los Angeles Angels":     ["LAA"],
  "Los Angeles Dodgers":    ["LAD"],
  "Miami Marlins":          ["MIA", "MIA"],
  "Milwaukee Brewers":      ["MIL"],
  "Minnesota Twins":        ["MIN"],
  "New York Mets":          ["NYM"],
  "New York Yankees":       ["NYY"],
  "Athletics":              ["ATH", "OAK"],
  "Philadelphia Phillies":  ["PHI"],
  "Pittsburgh Pirates":     ["PIT"],
  "San Diego Padres":       ["SD",  "SDP"],
  "San Francisco Giants":   ["SF",  "SFG"],
  "Seattle Mariners":       ["SEA"],
  "St. Louis Cardinals":    ["STL"],
  "Tampa Bay Rays":         ["TB",  "TBR"],
  "Texas Rangers":          ["TEX"],
  "Toronto Blue Jays":      ["TOR"],
  "Washington Nationals":   ["WSH", "WAS"],
};

const { readFileSync, existsSync } = require("fs");

function loadPrivateKey() {
  let keyStr = process.env.KALSHI_PRIVATE_KEY || "";
  // Fall back to local pem file when running via vercel dev
  if (keyStr.length < 100 && existsSync("kalshi_private.pem")) {
    keyStr = readFileSync("kalshi_private.pem", "utf8");
  }
  return crypto.createPrivateKey({ key: keyStr.replace(/\\n/g, "\n"), format: "pem" });
}

function signedHeaders(method, path) {
  const ts  = Date.now().toString();
  const msg = `${ts}${method.toUpperCase()}/trade-api/v2${path}`;
  const privateKey = loadPrivateKey();
  const sign = crypto.createSign("SHA256");
  sign.update(msg);
  sign.end();
  const sig = sign.sign(
    { key: privateKey, padding: crypto.constants.RSA_PKCS1_PSS_PADDING, saltLength: 32 },
    "base64"
  );
  return {
    "Content-Type":            "application/json",
    "KALSHI-ACCESS-KEY":       process.env.KALSHI_API_KEY_ID || "",
    "KALSHI-ACCESS-TIMESTAMP": ts,
    "KALSHI-ACCESS-SIGNATURE": sig,
  };
}

async function kalshiFetch(path) {
  const r = await fetch(`${BASE_URL}${path}`, { headers: signedHeaders("GET", path) });
  if (!r.ok) throw new Error(`Kalshi ${r.status}: ${await r.text()}`);
  return r.json();
}

// Check whether a Kalshi event ticker contains both team abbreviations
function tickerMatchesGame(ticker, homeAbbrs, awayAbbrs) {
  // Ticker format: KXMLBGAME-26MAY042145SDSF — last segment has both abbrs concatenated
  const suffix = ticker.split("-").slice(-1)[0]; // e.g. "SDSF" or "CWSLAA"
  const suffixUp = suffix.toUpperCase();
  return homeAbbrs.some(h => awayAbbrs.some(a =>
    suffixUp.includes(h.toUpperCase()) && suffixUp.includes(a.toUpperCase())
  ));
}

module.exports = async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "POST")   return res.status(405).json({ error: "Method not allowed" });

  if (!process.env.KALSHI_API_KEY_ID || !process.env.KALSHI_PRIVATE_KEY) {
    return res.status(500).json({ error: "Kalshi credentials not configured" });
  }

  // Expect: { games: [{gamePk, homeTeam, awayTeam}] }
  let games;
  try {
    games = req.body?.games;
    if (!Array.isArray(games) || !games.length) throw new Error("Missing games array");
  } catch (e) {
    return res.status(400).json({ error: "Bad request: " + e.message });
  }

  // Fetch all open KXMLBGAME events in one call, then match locally
  let events = [], markets = [];
  try {
    const [evData, mkData] = await Promise.all([
      kalshiFetch("/events?limit=100&series_ticker=KXMLBGAME&status=open"),
      kalshiFetch("/markets?limit=200&series_ticker=KXMLBGAME&status=open"),
    ]);
    events  = evData.events  || [];
    markets = mkData.markets || [];
  } catch (e) {
    return res.status(502).json({ error: "Kalshi fetch failed: " + e.message });
  }

  // Index markets by event_ticker for fast lookup
  const marketsByEvent = {};
  for (const m of markets) {
    if (!marketsByEvent[m.event_ticker]) marketsByEvent[m.event_ticker] = [];
    marketsByEvent[m.event_ticker].push(m);
  }

  const results = {};

  // Month abbreviations for Kalshi ticker date format (26MAY02 = 2026-05-02)
  const MONTHS = ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"];

  for (const game of games) {
    const { gamePk, homeTeam, awayTeam, dateStr } = game;
    const homeAbbrs = TEAM_ABBRS[homeTeam] || [];
    const awayAbbrs = TEAM_ABBRS[awayTeam] || [];
    if (!homeAbbrs.length || !awayAbbrs.length) continue;

    // Build Kalshi date prefix from gameDate e.g. "2026-05-02" → "26MAY02"
    let kalshiDatePrefix = null;
    if (dateStr) {
      const [yr, mo, dy] = dateStr.split("-");
      kalshiDatePrefix = yr.slice(2) + MONTHS[parseInt(mo,10)-1] + dy;
    }

    // Find matching event — filter by date prefix if available to avoid multi-series matches
    const event = events.find(e => {
      if (!tickerMatchesGame(e.event_ticker, homeAbbrs, awayAbbrs)) return false;
      if (kalshiDatePrefix) return e.event_ticker.includes(kalshiDatePrefix);
      return true;
    });
    if (!event) continue;

    // Ticker format: KXMLBGAME-26MAY041840PHIMIA → suffix "PHIMIA" = away+home concatenated
    // Away abbr comes first, home abbr second. Find split point using our known abbrs.
    const suffix = event.event_ticker.split("-").slice(-1)[0].toUpperCase();
    // Try each away abbr as a prefix — what remains should match a home abbr
    let tickerHomeAbbr = null;
    outer: for (const a of awayAbbrs) {
      if (suffix.startsWith(a.toUpperCase())) {
        const rest = suffix.slice(a.length);
        for (const h of homeAbbrs) {
          if (rest === h.toUpperCase() || rest.startsWith(h.toUpperCase())) {
            tickerHomeAbbr = h.toUpperCase();
            break outer;
          }
        }
      }
    }

    // Get its markets — 2 per event, one per team. yes_sub_title names the winning team.
    // Find the market where yes = home team wins, using yes_sub_title word match.
    const mks = marketsByEvent[event.event_ticker] || [];
    const homeWords = homeTeam.toLowerCase().split(" ").filter(w => w.length > 2);
    const homeMkt = mks.find(m => {
      const yes = (m.yes_sub_title || "").toLowerCase();
      return homeWords.some(w => yes.includes(w));
    }) || mks.find(m => {
      // Fallback: use ticker position — the market whose custom_strike baseball_team
      // corresponds to the home team. Since we can't resolve UUIDs, use last_price as a
      // tiebreaker: the home team market typically has last_price closer to 0.5–0.6
      return true; // take first if no name match
    });

    if (!homeMkt) continue;

    // Verify we got the right market — if yes_sub_title contains away team words, flip
    const awayWords = awayTeam.toLowerCase().split(" ").filter(w => w.length > 2);
    const yesIsAway = awayWords.some(w => (homeMkt.yes_sub_title || "").toLowerCase().includes(w))
                   && !homeWords.some(w => (homeMkt.yes_sub_title || "").toLowerCase().includes(w));

    const yesAsk = parseFloat(homeMkt.yes_ask_dollars);
    const yesBid = parseFloat(homeMkt.yes_bid_dollars);
    const midProb = (isNaN(yesAsk) || isNaN(yesBid))
      ? (isNaN(yesAsk) ? null : yesAsk)
      : +((yesAsk + yesBid) / 2).toFixed(3);

    // If yes = away team wins, flip so homeWinProb is always from home team's perspective
    const homeWinProb = midProb != null
      ? (yesIsAway ? +(1 - midProb).toFixed(3) : midProb)
      : null;

    results[gamePk] = {
      eventTicker:  event.event_ticker,
      title:        event.title,
      homeWinProb,                          // 0–1 implied probability home team wins
      awayWinProb:  homeWinProb != null ? +(1 - homeWinProb).toFixed(3) : null,
      lastPrice:    parseFloat(homeMkt.last_price_dollars) || null,
      yesAsk,
      yesBid,
    };
  }

  return res.status(200).json({ odds: results });
}
