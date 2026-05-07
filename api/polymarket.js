const GAMMA_URL = "https://gamma-api.polymarket.com";
const CLOB_URL  = "https://clob.polymarket.com";

// MLB team name fragments used to match Polymarket market questions
// Polymarket uses full city+nickname, e.g. "Will the New York Yankees win..."
const TEAM_KEYWORDS = {
  "Arizona Diamondbacks":  ["diamondbacks", "arizona"],
  "Atlanta Braves":        ["braves", "atlanta"],
  "Baltimore Orioles":     ["orioles", "baltimore"],
  "Boston Red Sox":        ["red sox", "boston"],
  "Chicago Cubs":          ["cubs", "chicago cubs"],
  "Chicago White Sox":     ["white sox", "chicago white"],
  "Cincinnati Reds":       ["reds", "cincinnati"],
  "Cleveland Guardians":   ["guardians", "cleveland"],
  "Colorado Rockies":      ["rockies", "colorado"],
  "Detroit Tigers":        ["tigers", "detroit"],
  "Houston Astros":        ["astros", "houston"],
  "Kansas City Royals":    ["royals", "kansas city"],
  "Los Angeles Angels":    ["angels", "los angeles angels", "l.a. angels"],
  "Los Angeles Dodgers":   ["dodgers", "los angeles dodgers", "l.a. dodgers"],
  "Miami Marlins":         ["marlins", "miami"],
  "Milwaukee Brewers":     ["brewers", "milwaukee"],
  "Minnesota Twins":       ["twins", "minnesota"],
  "New York Mets":         ["mets", "new york mets", "ny mets"],
  "New York Yankees":      ["yankees", "new york yankees", "ny yankees"],
  "Athletics":             ["athletics", "oakland"],
  "Philadelphia Phillies": ["phillies", "philadelphia"],
  "Pittsburgh Pirates":    ["pirates", "pittsburgh"],
  "San Diego Padres":      ["padres", "san diego"],
  "San Francisco Giants":  ["giants", "san francisco"],
  "Seattle Mariners":      ["mariners", "seattle"],
  "St. Louis Cardinals":   ["cardinals", "st. louis", "saint louis"],
  "Tampa Bay Rays":        ["rays", "tampa bay"],
  "Texas Rangers":         ["rangers", "texas"],
  "Toronto Blue Jays":     ["blue jays", "toronto"],
  "Washington Nationals":  ["nationals", "washington"],
};

function teamKeywords(teamName) {
  return TEAM_KEYWORDS[teamName] || [teamName.toLowerCase()];
}

// Returns true if the question text mentions both teams
function questionMatchesGame(question, homeTeam, awayTeam) {
  const q = question.toLowerCase();
  const homeKws = teamKeywords(homeTeam);
  const awayKws = teamKeywords(awayTeam);
  return homeKws.some(k => q.includes(k)) && awayKws.some(k => q.includes(k));
}

// Fetch mid-price for a CLOB token (0–1 probability)
async function getMidPrice(tokenId) {
  const r = await fetch(`${CLOB_URL}/midpoint?token_id=${tokenId}`);
  if (!r.ok) return null;
  const d = await r.json();
  return d.mid != null ? parseFloat(d.mid) : null;
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

  // Fetch open MLB markets from Gamma API — tag_id 100381 = Baseball
  // Fetch enough to cover a full day's slate (30 teams = up to 15 games)
  let markets = [];
  try {
    const r = await fetch(
      `${GAMMA_URL}/markets?tag_id=100381&active=true&closed=false&limit=50&order=volume`
    );
    if (!r.ok) throw new Error(`Gamma API ${r.status}`);
    markets = await r.json();
    if (!Array.isArray(markets)) markets = [];
  } catch (e) {
    return res.status(502).json({ error: "Polymarket fetch failed: " + e.message });
  }

  const results = {};

  for (const game of games) {
    const { gamePk, homeTeam, awayTeam } = game;
    if (!homeTeam || !awayTeam) continue;

    // Find market whose question mentions both teams
    const market = markets.find(m => {
      const q = m.question || m.title || "";
      return questionMatchesGame(q, homeTeam, awayTeam);
    });
    if (!market) continue;

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
      continue;
    }

    if (outcomes.length < 2 || tokenIds.length < 2) continue;

    // Match outcome index to home/away team
    const homeKws = teamKeywords(homeTeam);
    const awayKws = teamKeywords(awayTeam);
    let homeIdx = outcomes.findIndex(o => homeKws.some(k => o.toLowerCase().includes(k)));
    let awayIdx = outcomes.findIndex(o => awayKws.some(k => o.toLowerCase().includes(k)));

    // Fallback: assume [0]=home, [1]=away if matching fails
    if (homeIdx === -1) homeIdx = 0;
    if (awayIdx === -1) awayIdx = 1;
    if (homeIdx === awayIdx) awayIdx = homeIdx === 0 ? 1 : 0;

    // Try to get live mid-prices from CLOB; fall back to outcomePrices from Gamma
    const homeTokenId = tokenIds[homeIdx];
    const awayTokenId = tokenIds[awayIdx];
    let homeWinProb = homeTokenId ? await getMidPrice(homeTokenId) : null;
    let awayWinProb = awayTokenId ? await getMidPrice(awayTokenId) : null;

    // Fall back to Gamma's cached outcomePrices if CLOB mid unavailable
    if (homeWinProb == null && prices[homeIdx] != null) {
      homeWinProb = parseFloat(prices[homeIdx]);
    }
    if (awayWinProb == null && prices[awayIdx] != null) {
      awayWinProb = parseFloat(prices[awayIdx]);
    }
    if (homeWinProb == null) continue;

    // Normalise so they sum to 1 (remove vig)
    const total = (homeWinProb ?? 0) + (awayWinProb ?? (1 - homeWinProb));
    if (total > 0) {
      homeWinProb = +(homeWinProb / total).toFixed(3);
      awayWinProb = +(1 - homeWinProb).toFixed(3);
    }

    results[gamePk] = {
      question: market.question || market.title,
      homeWinProb,
      awayWinProb,
      volume:   parseFloat(market.volume) || null,
      liquidity: parseFloat(market.liquidity) || null,
    };
  }

  return res.status(200).json({ odds: results });
}
