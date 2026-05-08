const GAMMA_URL = "https://gamma-api.polymarket.com";
const CLOB_URL  = "https://clob.polymarket.com";

// Canonical team names as they appear in Polymarket event titles
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

  // Fetch all active MLB events — tag_id=100381, limit=500 to get all game events
  // Game events have titles like "Milwaukee Brewers vs. St. Louis Cardinals"
  let gameEvents = [];
  try {
    const r = await fetch(
      `${GAMMA_URL}/events?tag_id=100381&active=true&closed=false&limit=500`
    );
    if (!r.ok) throw new Error(`Gamma events API ${r.status}`);
    const events = await r.json();
    if (Array.isArray(events)) {
      // Keep only game events (title contains "vs.")
      gameEvents = events.filter(ev => (ev.title || "").includes(" vs. ") || (ev.title || "").includes(" vs "));
    }
  } catch (e) {
    return res.status(502).json({ error: "Polymarket fetch failed: " + e.message });
  }

  const results = {};

  await Promise.all(games.map(async (game) => {
    const { gamePk, homeTeam, awayTeam } = game;
    if (!homeTeam || !awayTeam) return;

    const homeName = (TEAM_NAMES[homeTeam] || homeTeam).toLowerCase();
    const awayName = (TEAM_NAMES[awayTeam] || awayTeam).toLowerCase();

    // Find the event whose title contains both team names
    const event = gameEvents.find(ev => {
      const t = (ev.title || "").toLowerCase();
      return t.includes(homeName) && t.includes(awayName);
    });
    if (!event) return;

    // Find the moneyline market: outcomes are team names (not Yes/No)
    const markets = Array.isArray(event.markets) ? event.markets : [];
    const moneyline = markets.find(m => {
      let outcomes;
      try { outcomes = typeof m.outcomes === "string" ? JSON.parse(m.outcomes) : (m.outcomes || []); }
      catch { return false; }
      return outcomes.length === 2 && !outcomes.some(o => o === "Yes" || o === "No");
    });
    if (!moneyline) return;

    // Parse stringified arrays
    let outcomes, prices, tokenIds;
    try {
      outcomes = typeof moneyline.outcomes === "string" ? JSON.parse(moneyline.outcomes) : (moneyline.outcomes || []);
      prices   = typeof moneyline.outcomePrices === "string" ? JSON.parse(moneyline.outcomePrices) : (moneyline.outcomePrices || []);
      tokenIds = typeof moneyline.clobTokenIds === "string" ? JSON.parse(moneyline.clobTokenIds) : (moneyline.clobTokenIds || []);
    } catch { return; }

    if (outcomes.length < 2 || tokenIds.length < 2) return;

    // Match outcome index to home/away by name
    let homeIdx = outcomes.findIndex(o => o.toLowerCase().includes(homeName) || homeName.includes(o.toLowerCase()));
    let awayIdx = outcomes.findIndex(o => o.toLowerCase().includes(awayName) || awayName.includes(o.toLowerCase()));
    if (homeIdx === -1) homeIdx = 0;
    if (awayIdx === -1) awayIdx = 1;
    if (homeIdx === awayIdx) awayIdx = homeIdx === 0 ? 1 : 0;

    // Try live CLOB mid-prices; fall back to Gamma cached outcomePrices
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
      question:  event.title,
      homeWinProb,
      awayWinProb,
      volume:    parseFloat(event.volume) || null,
      liquidity: parseFloat(event.liquidity) || null,
    };
  }));

  return res.status(200).json({ odds: results });
}
