const GAMMA_URL = "https://gamma-api.polymarket.com";
const CLOB_URL  = "https://clob.polymarket.com";

// Polymarket game market questions: "[Away Team] vs. [Home Team]" using full names
const TEAM_NAMES = {
  "Arizona Diamondbacks":  "Arizona Diamondbacks",
  "Atlanta Braves":        "Atlanta Braves",
  "Baltimore Orioles":     "Baltimore Orioles",
  "Boston Red Sox":        "Boston Red Sox",
  "Chicago Cubs":          "Chicago Cubs",
  "Chicago White Sox":     "Chicago White Sox",
  "Cincinnati Reds":       "Cincinnati Reds",
  "Cleveland Guardians":   "Cleveland Guardians",
  "Colorado Rockies":      "Colorado Rockies",
  "Detroit Tigers":        "Detroit Tigers",
  "Houston Astros":        "Houston Astros",
  "Kansas City Royals":    "Kansas City Royals",
  "Los Angeles Angels":    "Los Angeles Angels",
  "Los Angeles Dodgers":   "Los Angeles Dodgers",
  "Miami Marlins":         "Miami Marlins",
  "Milwaukee Brewers":     "Milwaukee Brewers",
  "Minnesota Twins":       "Minnesota Twins",
  "New York Mets":         "New York Mets",
  "New York Yankees":      "New York Yankees",
  "Athletics":             "Athletics",
  "Philadelphia Phillies": "Philadelphia Phillies",
  "Pittsburgh Pirates":    "Pittsburgh Pirates",
  "San Diego Padres":      "San Diego Padres",
  "San Francisco Giants":  "San Francisco Giants",
  "Seattle Mariners":      "Seattle Mariners",
  "St. Louis Cardinals":   "St. Louis Cardinals",
  "Tampa Bay Rays":        "Tampa Bay Rays",
  "Texas Rangers":         "Texas Rangers",
  "Toronto Blue Jays":     "Toronto Blue Jays",
  "Washington Nationals":  "Washington Nationals",
};

async function getMidPrice(tokenId) {
  try {
    const r = await fetch(`${CLOB_URL}/midpoint?token_id=${tokenId}`);
    if (!r.ok) return null;
    const d = await r.json();
    return d.mid != null ? parseFloat(d.mid) : null;
  } catch {
    return null;
  }
}

module.exports = async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") return res.status(204).end();
  if (req.method !== "POST")   return res.status(405).json({ error: "Method not allowed" });

  let games;
  try {
    games = req.body?.games;
    if (!Array.isArray(games) || !games.length) throw new Error("Missing games array");
  } catch (e) {
    return res.status(400).json({ error: "Bad request: " + e.message });
  }

  // Fetch active MLB game events — /events?tag=mlb returns game win/loss markets
  // Each event has a nested `markets` array with individual outcome markets
  let allMarkets = [];
  try {
    const r = await fetch(
      `${GAMMA_URL}/events?tag=mlb&active=true&closed=false&limit=100`
    );
    if (!r.ok) throw new Error(`Gamma events API ${r.status}`);
    const events = await r.json();
    if (Array.isArray(events)) {
      for (const ev of events) {
        // Each event: { title, markets: [...] }
        // Game markets have binary team-name outcomes, not Yes/No
        const nested = Array.isArray(ev.markets) ? ev.markets : [];
        for (const m of nested) {
          // Attach event title so we can match by team name
          m._eventTitle = ev.title || ev.question || "";
          allMarkets.push(m);
        }
      }
    }
  } catch (e) {
    return res.status(502).json({ error: "Polymarket fetch failed: " + e.message });
  }

  // Also include top-level markets that look like game markets (question contains "vs.")
  // Some events expose markets directly with a question field
  allMarkets = allMarkets.filter(m => {
    const q = (m.question || m._eventTitle || "").toLowerCase();
    return q.includes(" vs") || q.includes(" vs.");
  });

  const results = {};

  await Promise.all(games.map(async (game) => {
    const { gamePk, homeTeam, awayTeam } = game;
    if (!homeTeam || !awayTeam) return;

    const homeName = (TEAM_NAMES[homeTeam] || homeTeam).toLowerCase();
    const awayName = (TEAM_NAMES[awayTeam] || awayTeam).toLowerCase();

    // Find market whose question/title contains both team names
    const market = allMarkets.find(m => {
      const q = (m.question || m._eventTitle || "").toLowerCase();
      return q.includes(homeName) && q.includes(awayName);
    });
    if (!market) return;

    // Parse stringified arrays
    let outcomes, prices, tokenIds;
    try {
      outcomes = typeof market.outcomes === "string"
        ? JSON.parse(market.outcomes) : (market.outcomes || []);
      prices = typeof market.outcomePrices === "string"
        ? JSON.parse(market.outcomePrices) : (market.outcomePrices || []);
      tokenIds = typeof market.clobTokenIds === "string"
        ? JSON.parse(market.clobTokenIds) : (market.clobTokenIds || []);
    } catch {
      return;
    }

    if (outcomes.length < 2 || tokenIds.length < 2) return;

    // Match outcome index to home/away by name
    let homeIdx = outcomes.findIndex(o => o.toLowerCase().includes(homeName) || homeName.includes(o.toLowerCase()));
    let awayIdx = outcomes.findIndex(o => o.toLowerCase().includes(awayName) || awayName.includes(o.toLowerCase()));
    if (homeIdx === -1) homeIdx = 0;
    if (awayIdx === -1) awayIdx = 1;
    if (homeIdx === awayIdx) awayIdx = homeIdx === 0 ? 1 : 0;

    // Try live CLOB mid-prices; fall back to Gamma cached prices
    const [homeMid, awayMid] = await Promise.all([
      getMidPrice(tokenIds[homeIdx]),
      getMidPrice(tokenIds[awayIdx]),
    ]);
    let homeWinProb = homeMid ?? (prices[homeIdx] != null ? parseFloat(prices[homeIdx]) : null);
    let awayWinProb = awayMid ?? (prices[awayIdx] != null ? parseFloat(prices[awayIdx]) : null);
    if (homeWinProb == null) return;
    if (awayWinProb == null) awayWinProb = 1 - homeWinProb;

    // Normalise to sum to 1
    const total = homeWinProb + awayWinProb;
    if (total > 0) {
      homeWinProb = +(homeWinProb / total).toFixed(3);
      awayWinProb = +(1 - homeWinProb).toFixed(3);
    }

    results[gamePk] = {
      question:  market.question || market._eventTitle,
      homeWinProb,
      awayWinProb,
      volume:    parseFloat(market.volume) || null,
      liquidity: parseFloat(market.liquidity) || null,
    };
  }));

  return res.status(200).json({ odds: results });
};
