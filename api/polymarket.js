const GAMMA_URL = "https://gamma-api.polymarket.com";
const CLOB_URL  = "https://clob.polymarket.com";

// Polymarket game markets use exact full team names: "Pittsburgh Pirates vs. San Francisco Giants"
// These are the canonical names as they appear on Polymarket (away vs. home)
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

// Fetch mid-price for a CLOB token (0–1 probability)
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

// Search Gamma API for a specific game market using team name as keyword
// Returns matching market or null
async function findGameMarket(homeTeam, awayTeam) {
  const homeName = TEAM_NAMES[homeTeam] || homeTeam;
  const awayName = TEAM_NAMES[awayTeam] || awayTeam;

  // Polymarket format: "[Away] vs. [Home]" — search by the home team name
  // (shorter/more distinctive keyword reduces false positives)
  const query = encodeURIComponent(homeName);
  try {
    const r = await fetch(
      `${GAMMA_URL}/markets?q=${query}&active=true&closed=false&limit=20`
    );
    if (!r.ok) return null;
    const markets = await r.json();
    if (!Array.isArray(markets)) return null;

    // Find exact game market: question must contain both full team names
    // and have binary outcomes (not Yes/No season markets)
    return markets.find(m => {
      const q = (m.question || "").toLowerCase();
      if (!q.includes(homeName.toLowerCase())) return false;
      if (!q.includes(awayName.toLowerCase())) return false;
      // Exclude season-long Yes/No markets — game markets have team names as outcomes
      const outcomes = typeof m.outcomes === "string"
        ? JSON.parse(m.outcomes) : (m.outcomes || []);
      return outcomes.length === 2
        && !outcomes.some(o => o === "Yes" || o === "No");
    }) || null;
  } catch {
    return null;
  }
}

export default async function handler(req, res) {
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

  const results = {};

  // Search for each game individually — parallel fetches, one per game
  await Promise.all(games.map(async (game) => {
    const { gamePk, homeTeam, awayTeam } = game;
    if (!homeTeam || !awayTeam) return;

    const market = await findGameMarket(homeTeam, awayTeam);
    if (!market) return;

    // Parse stringified arrays from Gamma API
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

    // Match outcome index to home/away by exact name
    const homeName = (TEAM_NAMES[homeTeam] || homeTeam).toLowerCase();
    const awayName = (TEAM_NAMES[awayTeam] || awayTeam).toLowerCase();
    let homeIdx = outcomes.findIndex(o => o.toLowerCase().includes(homeName) || homeName.includes(o.toLowerCase()));
    let awayIdx = outcomes.findIndex(o => o.toLowerCase().includes(awayName) || awayName.includes(o.toLowerCase()));
    if (homeIdx === -1) homeIdx = 0;
    if (awayIdx === -1) awayIdx = 1;
    if (homeIdx === awayIdx) awayIdx = homeIdx === 0 ? 1 : 0;

    // Try live CLOB mid-prices; fall back to Gamma's cached outcomePrices
    const [homeMid, awayMid] = await Promise.all([
      getMidPrice(tokenIds[homeIdx]),
      getMidPrice(tokenIds[awayIdx]),
    ]);
    let homeWinProb = homeMid ?? (prices[homeIdx] != null ? parseFloat(prices[homeIdx]) : null);
    let awayWinProb = awayMid ?? (prices[awayIdx] != null ? parseFloat(prices[awayIdx]) : null);
    if (homeWinProb == null) return;
    if (awayWinProb == null) awayWinProb = 1 - homeWinProb;

    // Normalise so they sum to 1 (remove vig)
    const total = homeWinProb + awayWinProb;
    if (total > 0) {
      homeWinProb = +(homeWinProb / total).toFixed(3);
      awayWinProb = +(1 - homeWinProb).toFixed(3);
    }

    results[gamePk] = {
      question:  market.question || market.title,
      homeWinProb,
      awayWinProb,
      volume:    parseFloat(market.volume) || null,
      liquidity: parseFloat(market.liquidity) || null,
    };
  }));

  return res.status(200).json({ odds: results });
}
