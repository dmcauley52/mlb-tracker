// analytics.js — pure analytics functions, no React/DOM dependencies
// Usable in the browser (globals) and in Node.js (CommonJS exports at bottom)

// ── Constants ──────────────────────────────────────────────────────────────
var CYCLE_LENGTH    = 28;
var FORECAST_DAYS   = 14;
var MAX_COMPONENTS  = 5;
var MIN_PERIOD      = 4;
var MIN_AMPLITUDE   = 0.005;

// 2024-2026 FanGraphs wOBA weights
var WOBA_WEIGHTS = { bb:0.690, hbp:0.722, single:0.888, double:1.271, triple:1.616, hr:2.101 };

function computeWOBA(stat) {
  var bb  = stat.baseOnBalls       || 0;
  var ibb = stat.intentionalWalks  || 0;
  var hbp = stat.hitByPitch        || 0;
  var h   = stat.hits              || 0;
  var dbl = stat.doubles           || 0;
  var trp = stat.triples           || 0;
  var hr  = stat.homeRuns          || 0;
  var ab  = stat.atBats            || 0;
  var sf  = stat.sacFlies          || stat.sacrificeFlies || 0;
  var ubb = bb - ibb;
  var s1b = h - dbl - trp - hr;
  var num = WOBA_WEIGHTS.bb * ubb + WOBA_WEIGHTS.hbp * hbp +
            WOBA_WEIGHTS.single * s1b + WOBA_WEIGHTS.double * dbl +
            WOBA_WEIGHTS.triple * trp + WOBA_WEIGHTS.hr * hr;
  var den = ab + ubb + hbp + sf;
  return den > 0 ? num / den : 0;
}

var PHASE_CONFIG = [
  { name:"Surge",   start:0,  end:6,  color:"#22c55e", desc:"Hot streak, strong output" },
  { name:"Cruise",  start:7,  end:13, color:"#3b82f6", desc:"Sustained performance window" },
  { name:"Slide",   start:14, end:20, color:"#f59e0b", desc:"Slight decline, still capable" },
  { name:"Rebuild", start:21, end:27, color:"#ef4444", desc:"Reset and building back up" },
];

function getPhase(d) {
  return PHASE_CONFIG.find(function(p) { return d >= p.start && d <= p.end; });
}

var UPCOMING_SCHEDULE = {
  "default": { opponents: [], matchupFactor: 1.00 },
};

// Park factors: wOBA-based, centered on 1.00. >1 = hitter-friendly, <1 = pitcher-friendly.
// Source: FanGraphs multi-year park factors (2022-2025 blend), normalized to 1.00 average.
// Applied to predicted run environment when a player's next game is at that venue.
var PARK_FACTORS = {
  'COL': 1.18, // Coors Field — extreme hitter park
  'CIN': 1.10, // Great American Ball Park
  'TEX': 1.08, // Globe Life Field
  'BOS': 1.07, // Fenway Park
  'PHI': 1.06, // Citizens Bank Park
  'MIL': 1.05, // American Family Field
  'BAL': 1.04, // Camden Yards
  'HOU': 1.03, // Minute Maid Park
  'NYY': 1.03, // Yankee Stadium
  'MIN': 1.02, // Target Field
  'TOR': 1.02, // Rogers Centre
  'CHC': 1.01, // Wrigley Field
  'ATL': 1.01, // Truist Park
  'LAA': 1.00, // Angel Stadium
  'DET': 1.00, // Comerica Park
  'ARI': 1.00, // Chase Field
  'SEA': 0.99, // T-Mobile Park
  'STL': 0.99, // Busch Stadium
  'WSH': 0.98, // Nationals Park
  'KCR': 0.98, // Kauffman Stadium
  'CLE': 0.97, // Progressive Field
  'NYM': 0.97, // Citi Field
  'PIT': 0.97, // PNC Park
  'LAD': 0.96, // Dodger Stadium
  'SFG': 0.96, // Oracle Park
  'TBR': 0.96, // Tropicana Field
  'CHW': 0.95, // Guaranteed Rate Field
  'OAK': 0.95, // Oakland Coliseum / Sacramento
  'MIA': 0.94, // loanDepot park
  'SDP': 0.93, // Petco Park — extreme pitcher park
};

// ── Real schedule fetch ────────────────────────────────────────────────────
var LEAGUE_AVG_ERA = 4.20;
var _teamsListCache = null;
var _scheduleCache = {};
var _eraLeaderboardCache = null;
var _standingsCache = null;

async function _fetchStandings() {
  if (_standingsCache) return _standingsCache;
  try {
    var r = await fetch('https://statsapi.mlb.com/api/v1/standings?leagueId=103,104&season=2026&standingsTypes=regularSeason');
    var d = await r.json();
    var map = {};
    (d.records || []).forEach(function(rec) {
      (rec.teamRecords || []).forEach(function(tr) {
        var splits = (tr.records && tr.records.splitRecords) || [];
        var findSplit = function(type) {
          var s = splits.find(function(x) { return x.type === type; });
          return s && (s.wins + s.losses) > 0 ? s.wins / (s.wins + s.losses) : null;
        };
        var last10Pct = findSplit('lastTen');
        var seasonPct = tr.wins + tr.losses > 0
          ? tr.wins / (tr.wins + tr.losses)
          : 0.500;
        // Blend 50/50: last-10 is noisy (10 games), season is more stable
        var winPct = last10Pct != null ? (last10Pct * 0.5 + seasonPct * 0.5) : seasonPct;
        map[tr.team.id] = {
          winPct:         +winPct.toFixed(3),
          homeWinPct:     findSplit('home'),
          awayWinPct:     findSplit('away'),
          vsWinnersWinPct: findSplit('winners'),
        };
      });
    });
    _standingsCache = map;
  } catch(e) {
    _standingsCache = {};
  }
  return _standingsCache;
}

async function _fetchTeamsList() {
  if (_teamsListCache) return _teamsListCache;
  var r = await fetch('https://statsapi.mlb.com/api/v1/teams?sportId=1&season=2026');
  var d = await r.json();
  _teamsListCache = d.teams || [];
  return _teamsListCache;
}

// Fetch a hitter's home/away split stats from the MLB Stats API.
// Returns { homeAvg, homeOps, homeWoba, awayAvg, awayOps, awayWoba } or null on failure.
var _hitterSplitsCache = {};
async function fetchHitterSplits(mlbPlayerId) {
  if (!mlbPlayerId) return null;
  if (_hitterSplitsCache[mlbPlayerId] !== undefined) return _hitterSplitsCache[mlbPlayerId];
  try {
    var r = await fetch(
      'https://statsapi.mlb.com/api/v1/people/' + mlbPlayerId +
      '/stats?stats=statSplits&group=hitting&season=2026&sitCodes=h,a&gameType=R'
    );
    var d = await r.json();
    var splits = (d.stats && d.stats[0] && d.stats[0].splits) || [];
    var result = {};
    splits.forEach(function(s) {
      var code = s.split && s.split.code; // 'h' = home, 'a' = away
      var st   = s.stat || {};
      if (code === 'h') {
        result.homeAvg  = parseFloat(st.avg)  || null;
        result.homeOps  = parseFloat(st.ops)  || null;
        result.homeWoba = parseFloat(st.obp)  || null; // MLB API doesn't serve wOBA in splits; use OBP as proxy
        result.homeAb   = st.atBats || 0;
      } else if (code === 'a') {
        result.awayAvg  = parseFloat(st.avg)  || null;
        result.awayOps  = parseFloat(st.ops)  || null;
        result.awayWoba = parseFloat(st.obp)  || null;
        result.awayAb   = st.atBats || 0;
      }
    });
    var out = (result.homeAvg || result.awayAvg) ? result : null;
    _hitterSplitsCache[mlbPlayerId] = out;
    return out;
  } catch(e) {
    _hitterSplitsCache[mlbPlayerId] = null;
    return null;
  }
}

async function _fetchEraLeaderboard() {
  if (_eraLeaderboardCache) return _eraLeaderboardCache;
  try {
    var r = await fetch(
      'https://statsapi.mlb.com/api/v1/stats?stats=season&group=pitching&season=2026' +
      '&sportId=1&sortStat=era&order=asc&limit=300&gameType=R'
    );
    var d = await r.json();
    var splits = (d.stats && d.stats[0] && d.stats[0].splits) || [];
    var al = [], nl = [];
    splits.forEach(function(s) {
      var leagueId = s.team && s.team.league && s.team.league.id;
      var pid = s.player && s.player.id;
      if (!pid) return;
      if (leagueId === 103) al.push(pid);
      else if (leagueId === 104) nl.push(pid);
    });
    _eraLeaderboardCache = { al: al, nl: nl };
  } catch(e) {
    _eraLeaderboardCache = { al: [], nl: [] };
  }
  return _eraLeaderboardCache;
}

function _ipToDecimal(ip) {
  var parts = String(ip || '0').split('.');
  return (parseInt(parts[0]) || 0) + (parseInt(parts[1] || 0)) / 3;
}

function _decimalToIP(d) {
  var full = Math.floor(d);
  var thirds = Math.round((d - full) * 3);
  return thirds === 0 ? full + '.0' : full + '.' + thirds;
}

async function _fetchPitcherDetails(pitcherId) {
  try {
    var responses = await Promise.all([
      fetch('https://statsapi.mlb.com/api/v1/people/' + pitcherId + '?hydrate=currentTeam'),
      fetch('https://statsapi.mlb.com/api/v1/people/' + pitcherId + '/stats?stats=season&group=pitching&season=2026&gameType=R'),
      fetch('https://statsapi.mlb.com/api/v1/people/' + pitcherId + '/stats?stats=gameLog&group=pitching&season=2026&gameType=R'),
    ]);
    var data = await Promise.all(responses.map(function(r) { return r.json(); }));
    var personData = data[0], statsData = data[1], logsData = data[2];

    var person = personData.people && personData.people[0];
    var seasonSplit = statsData.stats && statsData.stats[0] && statsData.stats[0].splits && statsData.stats[0].splits[0];
    var gameLogs = (logsData.stats && logsData.stats[0] && logsData.stats[0].splits) || [];

    var leagueId = person && person.currentTeam && person.currentTeam.league && person.currentTeam.league.id;
    var stat = (seasonSplit && seasonSplit.stat) || {};
    var startIPs = gameLogs
      .filter(function(g) { return g.stat && (g.stat.gamesStarted > 0 || g.stat.gamesStarted === '1'); })
      .map(function(g) { return _ipToDecimal(g.stat.inningsPitched); })
      .sort(function(a, b) { return a - b; });
    var medianIP = null;
    if (startIPs.length > 0) {
      var mid = Math.floor(startIPs.length / 2);
      var med = startIPs.length % 2 !== 0 ? startIPs[mid] : (startIPs[mid - 1] + startIPs[mid]) / 2;
      medianIP = _decimalToIP(med);
    }

    return {
      id:       pitcherId,
      name:     (person && person.fullName) || 'Unknown',
      hand:     (person && person.pitchHand && person.pitchHand.code) || '?',
      leagueId: leagueId || null,
      league:   leagueId === 103 ? 'AL' : leagueId === 104 ? 'NL' : '?',
      era:      parseFloat(stat.era)                || null,
      whip:     parseFloat(stat.whip)               || null,
      k9:       parseFloat(stat.strikeoutsPer9Inn)  || null,
      medianIP: medianIP,
    };
  } catch(e) {
    return { id: pitcherId, name: 'Unknown', hand: '?', leagueId: null, league: '?', era: null, whip: null, k9: null, medianIP: null };
  }
}

async function fetchUpcomingSchedule(teamName) {
  if (_scheduleCache[teamName]) return _scheduleCache[teamName];
  var fallback = { opponents: [], matchupFactor: 1.0 };
  try {
    var teams = await _fetchTeamsList();
    var team = teams.find(function(t) { return t.name === teamName; });
    if (!team) return (_scheduleCache[teamName] = fallback);

    var abbrById = {}, nameById = {};
    teams.forEach(function(t) { abbrById[t.id] = t.abbreviation; nameById[t.id] = t.name; });

    var today = new Date().toISOString().slice(0, 10);
    var endDate = new Date(Date.now() + 14 * 86400000).toISOString().slice(0, 10);
    var schedResp = await fetch(
      'https://statsapi.mlb.com/api/v1/schedule?sportId=1&teamId=' + team.id +
      '&startDate=' + today + '&endDate=' + endDate + '&hydrate=probablePitcher'
    );
    var schedData = await schedResp.json();

    var dateMap = {};
    if (schedData.dates) {
      schedData.dates.forEach(function(d) {
        d.games.forEach(function(g) {
          var isHome = g.teams.home.team.id === team.id;
          var opp = isHome ? g.teams.away.team : g.teams.home.team;
          var oppSide = isHome ? g.teams.away : g.teams.home;
          var probablePitcherId = oppSide.probablePitcher && oppSide.probablePitcher.id || null;
          // venueAbbr = home team's abbreviation (determines park factor regardless of who's playing)
          var venueAbbr = abbrById[g.teams.home.team.id] || null;
          dateMap[d.date] = { abbr: abbrById[opp.id] || opp.name, id: opp.id, pitcherId: probablePitcherId, isHome: isHome, venueAbbr: venueAbbr };
        });
      });
    }

    var opponents = [];
    var gameDayIds = [];
    var gameDayPitcherIds = [];
    var gameDayIsHome = [];
    var gameDayVenueAbbr = [];
    var seenTeams = {}, seenPitchers = {};
    var uniqueTeamIds = [], uniquePitcherIds = [];
    for (var i = 0; i < 14; i++) {
      var dayStr = new Date(Date.now() + i * 86400000).toISOString().slice(0, 10);
      var entry = dateMap[dayStr];
      if (entry) {
        opponents.push(entry.abbr);
        gameDayIds.push(entry.id);
        gameDayPitcherIds.push(entry.pitcherId);
        gameDayIsHome.push(entry.isHome);
        gameDayVenueAbbr.push(entry.venueAbbr);
        if (!seenTeams[entry.id]) { seenTeams[entry.id] = true; uniqueTeamIds.push(entry.id); }
        if (entry.pitcherId && !seenPitchers[entry.pitcherId]) {
          seenPitchers[entry.pitcherId] = true; uniquePitcherIds.push(entry.pitcherId);
        }
      } else {
        opponents.push('---');
        gameDayIds.push(null);
        gameDayPitcherIds.push(null);
        gameDayIsHome.push(null);
        gameDayVenueAbbr.push(null);
      }
    }

    var matchupFactor = 1.0;
    var opponentFactors = gameDayIds.map(function() { return null; });
    var matchupDetails = gameDayIds.map(function() { return null; });

    if (uniqueTeamIds.length > 0) {
      // Include our own team ID so we can return myTeamEra for opponent prediction
      var allEraIds = uniqueTeamIds.indexOf(team.id) >= 0 ? uniqueTeamIds : uniqueTeamIds.concat([team.id]);
      var teamEraPromises = allEraIds.map(function(id) {
        return fetch(
          'https://statsapi.mlb.com/api/v1/teams/' + id +
          '/stats?stats=season&group=pitching&season=2026&sportId=1'
        ).then(function(r) { return r.json(); })
         .then(function(d) {
           var s = d.stats && d.stats[0] && d.stats[0].splits && d.stats[0].splits[0];
           return { id: id, era: parseFloat(s && s.stat && s.stat.era) || LEAGUE_AVG_ERA };
         })
         .catch(function() { return { id: id, era: LEAGUE_AVG_ERA }; });
      });

      var allResults = await Promise.all([
        Promise.all(teamEraPromises),  // allEraIds order
        Promise.all(uniquePitcherIds.map(_fetchPitcherDetails)),
        _fetchEraLeaderboard(),
        _fetchStandings(),
      ]);
      var teamEraResults = allResults[0];
      var pitcherDetails = allResults[1];
      var leaderboard    = allResults[2];
      var standings      = allResults[3];

      var teamIdToEra = {};
      teamEraResults.forEach(function(r) { teamIdToEra[r.id] = r.era; });

      var pitcherById = {};
      pitcherDetails.forEach(function(pd) {
        var leagueList = pd.leagueId === 103 ? leaderboard.al : leaderboard.nl;
        var rank = leagueList.indexOf(pd.id);
        pd.eraRank = rank >= 0 ? rank + 1 : null;
        pd.eraRankTotal = leagueList.length;
        pitcherById[pd.id] = pd;
      });

      opponentFactors = gameDayIds.map(function(teamId, i) {
        if (!teamId) return null;
        var pd = gameDayPitcherIds[i] ? pitcherById[gameDayPitcherIds[i]] : null;
        var era = (pd && pd.era != null) ? pd.era : teamIdToEra[teamId];
        var eraFactor = Math.max(0.85, Math.min(1.25, era / LEAGUE_AVG_ERA));
        var winPct = standings[teamId] != null ? standings[teamId].winPct : 0.500;
        var wlFactor  = Math.max(0.85, Math.min(1.15, 1 + (0.500 - winPct) * 1.5));
        return Math.max(0.80, Math.min(1.30, +(eraFactor * 0.6 + wlFactor * 0.4).toFixed(3)));
      });

      matchupDetails = gameDayIds.map(function(teamId, i) {
        if (!teamId) return null;
        var pd = gameDayPitcherIds[i] ? pitcherById[gameDayPitcherIds[i]] : null;
        var venueAbbr = gameDayVenueAbbr[i];
        return {
          team:         opponents[i],
          teamFullName: nameById[teamId] || null,
          teamId:       teamId,
          teamEra:      teamIdToEra[teamId],
          teamWinPct:   standings[teamId] != null ? standings[teamId].winPct : null,
          isHome:       gameDayIsHome[i],
          venueAbbr:    venueAbbr,
          parkFactor:   venueAbbr ? (PARK_FACTORS[venueAbbr] || 1.00) : 1.00,
          pitcher: pd ? {
            name:         pd.name,
            hand:         pd.hand,
            league:       pd.league,
            era:          pd.era,
            eraRank:      pd.eraRank,
            eraRankTotal: pd.eraRankTotal,
            whip:         pd.whip,
            k9:           pd.k9,
            medianIP:     pd.medianIP,
          } : null,
        };
      });

      var gameDayFactors = opponentFactors.filter(function(f) { return f !== null; });
      matchupFactor = gameDayFactors.reduce(function(a, b) { return a + b; }, 0) / gameDayFactors.length;
    }

    var myWinPct  = standings[team.id] != null ? standings[team.id].winPct : 0.500;
    var myTeamEra = teamIdToEra[team.id] || LEAGUE_AVG_ERA;
    var result = {
      opponents: opponents,
      opponentFactors: opponentFactors,
      matchupDetails: matchupDetails,
      matchupFactor: +matchupFactor.toFixed(3),
      gameDayIsHome: gameDayIsHome,
      gameDayVenueAbbr: gameDayVenueAbbr,
      myWinPct: myWinPct,
      myTeamEra: myTeamEra,
    };
    _scheduleCache[teamName] = result;
    return result;
  } catch (e) {
    return (_scheduleCache[teamName] = fallback);
  }
}

// ── DFT / Hitter cycle engine ──────────────────────────────────────────────
function dft(signal) {
  var N = signal.length;
  var re = new Float64Array(N), im = new Float64Array(N);
  for (var k = 0; k < N; k++) {
    for (var n = 0; n < N; n++) {
      var angle = (2 * Math.PI * k * n) / N;
      re[k] += signal[n] * Math.cos(angle);
      im[k] -= signal[n] * Math.sin(angle);
    }
    re[k] /= N; im[k] /= N;
  }
  return { re: re, im: im };
}

function reconstructAt(components, N, positions) {
  return positions.map(function(x) {
    var v = 0;
    for (var i = 0; i < components.length; i++) {
      var c = components[i];
      var angle = (2 * Math.PI * c.k * x) / N;
      v += c.re * Math.cos(angle) - c.im * Math.sin(angle);
    }
    return v;
  });
}

function analyzePlayerCycles(gameData) {
  var N = gameData.length;
  if (N < 10) return null;
  var raw = gameData.map(function(g) { return g.signal != null ? g.signal : g.avg; });
  var mean = raw.reduce(function(a, b) { return a + b; }, 0) / N;
  var signal = raw.map(function(v) { return v - mean; });
  var result = dft(signal);
  var re = result.re, im = result.im;
  var spectrum = [];
  for (var k = 1; k <= Math.floor(N / 2); k++) {
    var amp = 2 * Math.sqrt(re[k]*re[k] + im[k]*im[k]);
    var periodDays = N / k;
    if (periodDays < MIN_PERIOD) continue;
    spectrum.push({ k: k, amp: amp, periodDays: periodDays, re: re[k], im: im[k] });
  }
  spectrum.sort(function(a, b) { return b.amp - a.amp; });
  var totalPower = spectrum.reduce(function(s, c) { return s + c.amp*c.amp; }, 0) || 1;
  var kept = spectrum.filter(function(c) { return c.amp >= MIN_AMPLITUDE; }).slice(0, MAX_COMPONENTS);
  if (kept.length === 0) return null;
  var components = [{ k:0, re: mean, im: 0 }].concat(kept);
  var positions = Array.from({ length: N }, function(_, i) { return i; });
  var reconstructed = reconstructAt(components, N, positions);
  var ssTot = raw.reduce(function(s, v) { return s + (v - mean) * (v - mean); }, 0);
  var ssRes = raw.reduce(function(s, v, i) { return s + (v - reconstructed[i]) * (v - reconstructed[i]); }, 0);
  var r2 = ssTot > 0 ? Math.max(0, 1 - ssRes / ssTot) : 0;
  var forecastPositions = Array.from({ length: FORECAST_DAYS }, function(_, i) { return N + i; });
  var forecastValues = reconstructAt(components, N, forecastPositions);
  var dominantCycles = kept.map(function(c) {
    return {
      periodDays:  +c.periodDays.toFixed(1),
      amplitude:   +c.amp.toFixed(4),
      pctVariance: +(100 * c.amp*c.amp / totalPower).toFixed(1),
    };
  });
  return { dominantCycles: dominantCycles, reconstructed: reconstructed, forecastValues: forecastValues, components: components, N: N, mean: mean, r2: r2 };
}

function buildCycleChartData(gameData, analysis) {
  if (!analysis) return [];
  var reconstructed = analysis.reconstructed, forecastValues = analysis.forecastValues;
  var historical = gameData.map(function(g, i) {
    return {
      date:     g.date,
      actual:   +(g.signal != null ? g.signal : g.avg).toFixed(3),
      fitted:   +Math.max(0, reconstructed[i]).toFixed(3),
      forecast: null,
    };
  });
  var lastFitted = historical.length ? historical[historical.length - 1].fitted : 0;
  var forecastPoints = forecastValues.map(function(v, i) {
    return {
      date:       '+' + (i + 1) + 'd',
      actual:     null,
      fitted:     null,
      forecast:   +Math.max(0, v).toFixed(3),
      isForecast: true,
    };
  });
  if (forecastPoints.length && historical.length) {
    forecastPoints.unshift({
      date:       historical[historical.length - 1].date,
      actual:     null,
      fitted:     lastFitted,
      forecast:   +Math.max(0, forecastValues[0]).toFixed(3),
      isForecast: false,
    });
  }
  return historical.concat(forecastPoints);
}

// ── Pitcher FFT ────────────────────────────────────────────────────────────
function fftPitcher(values) {
  var N = values.length;
  if (N < 4) return { dominantCycleDays: null, amplitude: 0, predictionScore: 0 };
  var mean = values.reduce(function(a, b) { return a + b; }, 0) / N;
  var signal = values.map(function(v) { return v - mean; });
  var re = new Float64Array(N), im = new Float64Array(N);
  for (var k = 0; k < N; k++) {
    for (var n = 0; n < N; n++) {
      var angle = (2 * Math.PI * k * n) / N;
      re[k] += signal[n] * Math.cos(angle);
      im[k] -= signal[n] * Math.sin(angle);
    }
    re[k] /= N; im[k] /= N;
  }
  var bestIdx = 1, bestMag = 0;
  for (var i = 1; i <= Math.floor(N / 2); i++) {
    var mag = 2 * Math.sqrt(re[i]*re[i] + im[i]*im[i]);
    if (mag > bestMag) { bestMag = mag; bestIdx = i; }
  }
  var cycleDays = Math.round(N / bestIdx);
  var amplitude = bestMag;
  var variance = signal.reduce(function(a, b) { return a + b * b; }, 0) / N;
  var score = variance > 0 ? Math.min(99, Math.round((amplitude / Math.sqrt(variance)) * 33)) : 0;
  return { dominantCycleDays: cycleDays, amplitude: amplitude, predictionScore: score };
}

function buildPitcherForecast(gamelogs, metric) {
  var vals = gamelogs.map(function(g) { return parseFloat(g[metric]) || 0; });
  var fft = fftPitcher(vals);
  var dominantCycleDays = fft.dominantCycleDays, predictionScore = fft.predictionScore;
  if (!dominantCycleDays) return { forecast: [], predictionScore: 0, dominantCycleDays: null };
  var last = vals.slice(-dominantCycleDays);
  var lastDate = new Date(gamelogs[gamelogs.length - 1].game_date + "T12:00:00");
  var forecast = Array.from({ length: 14 }, function(_, i) {
    var d = new Date(lastDate);
    d.setDate(d.getDate() + i + 1);
    var idx = i % dominantCycleDays;
    return {
      date:  d.toISOString().slice(0, 10),
      value: +(last[idx % last.length] || 0).toFixed(3),
    };
  });
  return { forecast: forecast, predictionScore: predictionScore, dominantCycleDays: dominantCycleDays };
}

// ── Forecast backtest ─────────────────────────────────────────────────────
function backtestForecast(gameData) {
  var N = gameData.length;
  var MIN_HIST = 15, NUM_WINDOWS = 5;
  if (N < MIN_HIST + FORECAST_DAYS) return null;

  var maxEnd = N - FORECAST_DAYS;
  var step   = Math.max(1, Math.floor((maxEnd - MIN_HIST) / (NUM_WINDOWS - 1)));
  var endPts = [];
  for (var e = MIN_HIST; e <= maxEnd && endPts.length < NUM_WINDOWS; e += step) endPts.push(e);

  var windows = endPts.map(function(end) {
    var history  = gameData.slice(0, end);
    var actual   = gameData.slice(end, end + FORECAST_DAYS).map(function(g) { return g.signal != null ? g.signal : g.avg; });
    var analysis = analyzePlayerCycles(history);
    if (!analysis) return null;

    var forecast  = analysis.forecastValues.slice(0, actual.length);
    var lastFit   = analysis.reconstructed[analysis.reconstructed.length - 1];
    var mae = 0, dirCorrect = 0;
    forecast.forEach(function(fv, i) {
      mae += Math.abs(fv - actual[i]);
      var fDir = fv  - (i === 0 ? lastFit      : forecast[i - 1]);
      var lastSig = history[history.length - 1]; lastSig = lastSig.signal != null ? lastSig.signal : lastSig.avg;
      var aDir = actual[i] - (i === 0 ? lastSig : actual[i - 1]);
      if ((fDir >= 0) === (aDir >= 0)) dirCorrect++;
    });
    return {
      end:                  end,
      mae:                  mae / forecast.length,
      directionalAccuracy:  dirCorrect / forecast.length,
      forecast:             forecast,
      actual:               actual,
    };
  }).filter(Boolean);

  if (!windows.length) return null;

  var avgMAE = windows.reduce(function(s, w) { return s + w.mae; }, 0) / windows.length;
  var avgDir = windows.reduce(function(s, w) { return s + w.directionalAccuracy; }, 0) / windows.length;
  var last   = windows[windows.length - 1];

  return {
    windowCount:            windows.length,
    avgMAE:                 +avgMAE.toFixed(4),
    avgDirectionalAccuracy: +avgDir.toFixed(3),
    recentForecast:         last.forecast,
    recentActual:           last.actual,
    recentStartGame:        last.end + 1,
    windows:                windows,
  };
}

// ── Prediction scoring (hitters, 0–99) ────────────────────────────────────
// gameData rows must include a `rawDate` string (YYYY-MM-DD) for fatigue calculation.
// hitterSplits: { vsLHP, vsRHP, homeAvg, homeOps, awayAvg, awayOps, homeAb, awayAb }
function computePredictionScore(playerName, teamName, gameData, seasonStats, scheduleData, hitterSplits) {
  if (!gameData || gameData.length < 3) {
    return { score:50, label:"Insufficient data", tier:"neutral", breakdown:{phaseScore:0,trendScore:0,opsScore:0,momentumScore:0,matchupScore:0,fatigueScore:0,homeAwayScore:0,parkScore:0}, opponents:[] };
  }

  var phaseScore = 15, phaseLabel = "current cycle";
  var analysis = analyzePlayerCycles(gameData);
  if (analysis && analysis.forecastValues.length > 0) {
    var f = analysis.forecastValues;
    var trend3  = f.slice(0,3).reduce(function(a,b){return a+b;},0)/3;
    var trend3b = f.slice(1,4).reduce(function(a,b){return a+b;},0)/3;
    var slope   = trend3b - trend3;
    var level   = f[0] - analysis.mean;
    phaseScore = slope > 0.010 ? 30 : slope > 0.004 ? 26
               : slope > 0     ? 20 : slope > -0.004 ? 14
               : slope > -0.010 ? 8 : 4;
    if (level > 0.015) phaseScore = Math.min(30, phaseScore + 4);
    var topPeriod = analysis.dominantCycles[0] && analysis.dominantCycles[0].periodDays;
    phaseLabel = topPeriod
      ? (slope > 0 ? 'rising ' + topPeriod + 'd cycle' : 'falling ' + topPeriod + 'd cycle')
      : (slope > 0 ? "rising cycle" : "falling cycle");
  } else {
    var lastCycleDay = (gameData[gameData.length - 1] && gameData[gameData.length - 1].cycleDay) || 0;
    var nextCycleDay = (lastCycleDay + 1) % CYCLE_LENGTH;
    var np = getPhase(nextCycleDay);
    phaseScore = np.name === "Surge" ? 30 : np.name === "Cruise" ? 22 : np.name === "Slide" ? 12 : 4;
    phaseLabel = np.name + " phase · 28d fixed cycle";
  }
  var usingFixedCycle  = !(analysis && analysis.forecastValues.length > 0);
  var insufficientData = usingFixedCycle && gameData.length < 10;

  function sig(g) { return g.signal != null ? g.signal : g.avg; }

  // Consistency: std dev of per-game signal (wOBA preferred as true per-game measure)
  var allSig   = gameData.map(function(g) { return g.woba != null ? g.woba : sig(g); });
  var sigMean2 = allSig.reduce(function(a,b){return a+b;},0) / allSig.length;
  var stdDev   = Math.sqrt(allSig.reduce(function(a,b){return a+(b-sigMean2)*(b-sigMean2);},0) / allSig.length);
  // stdDev 0 = perfectly flat, 0.350+ = very streaky; scale to 0-100
  var consistency = Math.max(0, Math.min(100, Math.round(100 - (stdDev / 0.350) * 100)));

  var last5      = gameData.slice(-5);
  var last5Val   = last5.reduce(function(s,g){return s+sig(g);},0) / last5.length;
  // Normalise: wOBA ~.315 avg → ~17/25; OPS ~.740 avg → ~17/25; AVG ~.255 avg → ~17/25
  var sigMid     = gameData[0] && gameData[0].woba != null ? 0.315 : gameData[0] && gameData[0].signal > 0.5 ? 0.740 : 0.255;
  var trendScore = Math.min(25, Math.max(0, Math.round((last5Val / (sigMid * 1.4)) * 25)));

  var ops      = seasonStats ? parseFloat(seasonStats.ops) : 0;
  var opsScore = ops >= 0.900 ? 20 : ops >= 0.800 ? 16 : ops >= 0.700 ? 11 : ops >= 0.600 ? 6 : 3;

  var last10     = gameData.slice(-10);
  var firstHalf  = last10.slice(0,5).reduce(function(s,g){return s+sig(g);},0) / 5;
  var secondHalf = last10.slice(5).reduce(function(s,g){return s+sig(g);},0) / Math.max(last10.slice(5).length,1);
  var momentumScore = secondHalf > firstHalf ? Math.min(15, Math.round((secondHalf - firstHalf) * 200)) : 0;

  // ── Matchup quality (0–10): opponent ERA + win% + pitcher handedness vs batter ──
  var sched        = scheduleData || UPCOMING_SCHEDULE["default"];
  var matchupFactor = sched.matchupFactor || 1.0;
  if (hitterSplits && sched.matchupDetails) {
    var adjFactors = (sched.opponentFactors || []).map(function(f, i) {
      if (f == null) return null;
      var hand = sched.matchupDetails[i] && sched.matchupDetails[i].pitcher && sched.matchupDetails[i].pitcher.hand;
      var ba   = hand === 'L' ? parseFloat(hitterSplits.vsLHP)
               : hand === 'R' ? parseFloat(hitterSplits.vsRHP) : NaN;
      if (isNaN(ba) || !ba) return f;
      var sf = Math.max(0.85, Math.min(1.15, ba / 0.255));
      return Math.max(0.80, Math.min(1.30, +(f * 0.65 + sf * 0.35).toFixed(3)));
    });
    var validAdj = adjFactors.filter(function(f) { return f !== null; });
    if (validAdj.length) matchupFactor = validAdj.reduce(function(a,b){return a+b;},0) / validAdj.length;
  }
  var matchupScore = Math.max(0, Math.min(10, Math.round((matchupFactor - 0.85) / 0.45 * 10)));

  // ── Fatigue (−5 to 0): consecutive days played + PA load in last 5 days ───
  // Derived from rawDate fields in gameData. No rawDate → no penalty.
  var fatigueScore = 0;
  var rawDates = gameData.map(function(g) { return g.rawDate || null; }).filter(Boolean);
  if (rawDates.length >= 3) {
    var today = new Date();
    today.setHours(0, 0, 0, 0);
    var msPerDay = 86400000;
    // Count games in the last 5 calendar days
    var last5Days = rawDates.filter(function(d) {
      var ms = today - new Date(d + 'T12:00:00');
      return ms >= 0 && ms < 5 * msPerDay;
    }).length;
    // Count consecutive days ending yesterday (no day off recently)
    var consec = 0;
    for (var ci = 1; ci <= 7; ci++) {
      var check = new Date(today - ci * msPerDay).toISOString().slice(0, 10);
      if (rawDates.indexOf(check) >= 0) consec++; else break;
    }
    // PA load: sum plate appearances from last 5 gamelog rows
    var last5Rows = gameData.slice(-5);
    var recentPa = last5Rows.reduce(function(s, g) { return s + (g.plate_appearances || g.ab || 0); }, 0);

    var penalty = 0;
    if (last5Days >= 5 && consec >= 4) penalty += 3; // 5 games in 5 days, no day off
    else if (last5Days >= 4) penalty += 2;
    if (recentPa >= 22) penalty += 2;               // >4.4 PA/game average = heavy usage
    else if (recentPa >= 18) penalty += 1;
    fatigueScore = -Math.min(5, penalty);
  }

  var score = Math.min(99, Math.max(1,
    phaseScore + trendScore + opsScore + momentumScore + matchupScore + fatigueScore
  ));
  var tier  = score >= 75 ? "hot" : score >= 55 ? "warm" : score >= 35 ? "neutral" : "cold";
  var label = score >= 75 ? 'Peak — ' + phaseLabel + ', strong outlook'
            : score >= 55 ? 'Rising — ' + phaseLabel
            : score >= 35 ? 'Holding — ' + phaseLabel
            : 'Fading — ' + phaseLabel;

  // Expose next-game context for display (park + home/away) without affecting the score
  var nextGame = sched.matchupDetails && sched.matchupDetails.find(function(d) { return d != null; });

  return {
    score: score, tier: tier, label: label, phaseLabel: phaseLabel,
    usingFixedCycle: usingFixedCycle, insufficientData: insufficientData,
    consistency: consistency, stdDev: +stdDev.toFixed(3),
    opponents: sched.opponents,
    opponentFactors: sched.opponentFactors || [],
    matchupDetails: sched.matchupDetails || [],
    nextGame: nextGame || null,
    breakdown: {
      phaseScore: phaseScore, trendScore: trendScore, opsScore: opsScore,
      momentumScore: momentumScore, matchupScore: matchupScore,
      fatigueScore: fatigueScore,
    },
  };
}

// ── Utilities ──────────────────────────────────────────────────────────────
function applyPhaseColoring(gameData, analysis) {
  if (!analysis || !analysis.reconstructed || analysis.reconstructed.length !== gameData.length) {
    return gameData;
  }
  var rec  = analysis.reconstructed;
  var mean = analysis.mean;
  var N    = rec.length;
  return gameData.map(function(g, i) {
    var prev  = i > 0     ? rec[i - 1] : rec[0];
    var next  = i < N - 1 ? rec[i + 1] : rec[N - 1];
    var slope = (next - prev) / 2;
    var level = rec[i] - mean;
    var ph;
    if      (level >  0 && slope >  0) ph = PHASE_CONFIG[0]; // Surge:   above mean, rising
    else if (level >  0 && slope <= 0) ph = PHASE_CONFIG[1]; // Cruise:  above mean, falling
    else if (level <= 0 && slope <= 0) ph = PHASE_CONFIG[2]; // Slide:   below mean, falling
    else                               ph = PHASE_CONFIG[3]; // Rebuild: below mean, rising
    return Object.assign({}, g, { phase: ph.name, phaseColor: ph.color });
  });
}

function scoreColor(score) {
  if (score >= 75) return '#22c55e';
  if (score >= 50) return '#f59e0b';
  return '#ef4444';
}

function phaseAvg(games, ph, stat) {
  var f = games.filter(function(g) { return g.phase === ph; });
  return f.length ? (f.reduce(function(s,g){return s+g[stat];},0)/f.length).toFixed(3) : '—';
}

// ── Data transforms ────────────────────────────────────────────────────────
function transformRows(rows) {
  return rows.map(function(row, i) {
    var cyc  = i % CYCLE_LENGTH, ph = getPhase(cyc), avg = parseFloat(row.batting_avg) || 0;
    var obp  = parseFloat(row.obp) || null;
    var slg  = parseFloat(row.slg) || null;
    var ops  = (obp && slg) ? +(obp + slg).toFixed(3) : +(parseFloat(row.ops) || avg * 1.7).toFixed(3);
    var woba = row.woba != null ? +parseFloat(row.woba).toFixed(4) : null;
    return {
      rawDate:  row.game_date || null,
      date:     new Date(row.game_date + "T12:00:00").toLocaleDateString("en-US", { month:"short", day:"numeric" }),
      cycleDay: cyc, phase: ph.name, phaseColor: ph.color,
      avg:    +avg.toFixed(3),
      obp:    obp,
      slg:    slg,
      ops:    ops,
      woba:   woba,
      signal: woba != null ? woba : ops,
      hits:   row.hits       || 0,
      hr:     row.home_runs  || 0,
      ab:     row.at_bats    || 0,
      rbi:    row.rbi        || 0,
      doubles:          row.doubles           || 0,
      triples:          row.triples           || 0,
      walks:            row.walks             || 0,
      strikeouts:       row.strikeouts        || 0,
      hit_by_pitch:     row.hit_by_pitch      || 0,
      sac_flies:        row.sac_flies         || 0,
      stolen_bases:     row.stolen_bases      || 0,
      plate_appearances:row.plate_appearances || row.at_bats || 0,
      total_bases:      row.total_bases       || 0,
      runs:             row.runs              || 0,
      babip:            row.babip != null ? +parseFloat(row.babip).toFixed(3) : null,
    };
  });
}

function transformMLBSplits(splits) {
  return splits.filter(function(s){ return (s.stat && s.stat.atBats || 0) > 0; }).map(function(s, i) {
    var cyc  = i % CYCLE_LENGTH, ph = getPhase(cyc);
    var avg  = parseFloat(s.stat && s.stat.avg) || 0;
    var obp  = parseFloat(s.stat && s.stat.obp) || 0;
    var slg  = parseFloat(s.stat && s.stat.slg) || 0;
    var woba = s.stat ? computeWOBA(s.stat) : 0;
    return {
      date:     s.date ? new Date(s.date + "T12:00:00").toLocaleDateString("en-US", { month:"short", day:"numeric" }) : 'G' + (i + 1),
      cycleDay: cyc, phase: ph.name, phaseColor: ph.color,
      avg:    +avg.toFixed(3),
      ops:    +(obp + slg).toFixed(3),
      woba:   +woba.toFixed(4),
      signal: +woba.toFixed(4),
      hits: (s.stat && s.stat.hits)      || 0,
      hr:   (s.stat && s.stat.homeRuns)  || 0,
      ab:   (s.stat && s.stat.atBats)    || 0,
      rbi:  (s.stat && s.stat.rbi)       || 0,
    };
  });
}

// ── Lineup optimizer ───────────────────────────────────────────────────────
// Weights per batting spot: [obp, woba, power, speed]
// Philosophy: 1/2/9 = OBP+speed, 3/4/5 = wOBA+power, 6/7/8 = balanced
var LINEUP_SPOT_WEIGHTS = [
  null,
  [0.40, 0.20, 0.08, 0.32], // 1 leadoff  — OBP + speed
  [0.32, 0.28, 0.12, 0.28], // 2          — contact + speed
  [0.20, 0.40, 0.28, 0.12], // 3 best     — overall excellence
  [0.12, 0.28, 0.48, 0.12], // 4 cleanup  — pure power
  [0.14, 0.28, 0.46, 0.12], // 5          — power
  [0.22, 0.34, 0.30, 0.14], // 6          — balanced
  [0.24, 0.34, 0.26, 0.16], // 7          — balanced
  [0.28, 0.34, 0.20, 0.18], // 8          — balanced + speed
  [0.38, 0.22, 0.08, 0.32], // 9 modern   — OBP + speed (turns over to top)
];

// Fill spots in this order so best positions get best players
var LINEUP_PRIORITY = [3, 4, 1, 2, 5, 6, 7, 9, 8];

function scorePlayerAtSpot(player, spot, pred) {
  var woba = player.avgWoba || 0.310;
  var obp  = player.avgObp  || (woba * 1.05); // estimated if missing
  var gp   = player.gamesPlayed || 1;
  var hrPG = (player.sumHR||0) / gp;
  var sbPG = (player.sumSB||0) / gp;
  var form = pred ? pred.score / 99 : 0.50;

  var wobaN = Math.max(0, Math.min(1, (woba - 0.265) / 0.185));
  var obpN  = Math.max(0, Math.min(1, (obp  - 0.295) / 0.165));
  var powN  = Math.max(0, Math.min(1,  hrPG / 0.085));  // ~14/162 = avg HR rate
  var spdN  = Math.max(0, Math.min(1,  sbPG / 0.080));  // ~13/162 = avg SB rate

  var w = LINEUP_SPOT_WEIGHTS[spot];
  // Blend analytics weights 80% + current form 20%
  return (w[0]*obpN + w[1]*wobaN + w[2]*powN + w[3]*spdN) * 0.80 + form * 0.20;
}

function buildOptimalLineup(players, predCache) {
  var available = players.slice();
  var lineup = {};
  LINEUP_PRIORITY.forEach(function(spot) {
    if (!available.length) return;
    var best = null, bestScore = -Infinity;
    available.forEach(function(p) {
      var sc = scorePlayerAtSpot(p, spot, predCache && predCache[p.player_name]);
      if (sc > bestScore) { bestScore = sc; best = p; }
    });
    if (best) {
      lineup[spot] = { player: best, spotScore: +bestScore.toFixed(3) };
      available = available.filter(function(p) { return p !== best; });
    }
  });
  return Array.from({ length: 9 }, function(_, i) {
    return lineup[i + 1] || null;
  });
}

function buildOpponentLineup(players, predCache) {
  // Sort by most common historical batting spot, fill gaps by pred score
  var withSpot    = players.filter(function(p) { return p.typicalSpot; })
                           .sort(function(a, b) { return a.typicalSpot - b.typicalSpot; });
  var withoutSpot = players.filter(function(p) { return !p.typicalSpot; })
                           .sort(function(a, b) {
                             return ((predCache&&predCache[b.player_name]&&predCache[b.player_name].score)||0) -
                                    ((predCache&&predCache[a.player_name]&&predCache[a.player_name].score)||0);
                           });
  var ordered = withSpot.concat(withoutSpot).slice(0, 9);
  return ordered.map(function(p, i) {
    return { spot: i + 1, player: p, typicalSpot: p.typicalSpot };
  });
}

// ── Game Plan Backtesting ──────────────────────────────────────────────────
// Weights for the run-prediction model (tunable)
var BACKTEST_WEIGHTS = {
  wobaRunScale:    17.0,  // predicted runs = avg_lineup_woba * wobaRunScale (lowered from 18.5)
  maxPredictedRuns: 7.5,  // cap prevents 9-10R predictions that always flip to Win
  scoreBoost:      0.08,  // fractional bonus from avg prediction score (0–99)
  oppEraScale:     0.75,  // era factor weight — raised from 0.55 so elite/bad starters matter more
  winMarginThresh: 0.35,  // run-diff threshold below which we call it a toss-up
  // Batting order PA weights (spots 1-9): top of order sees more PAs per game
  spotWeights:     [1.15, 1.12, 1.10, 1.05, 1.02, 0.95, 0.90, 0.88, 0.83],
};

// Fetch past N days of a team's completed games from the MLB Stats API
async function _fetchPastGames(teamName, days) {
  var teams = await _fetchTeamsList();
  var team = teams.find(function(t) { return t.name === teamName; });
  if (!team) return [];

  var abbrById = {}, nameById = {};
  teams.forEach(function(t) { abbrById[t.id] = t.abbreviation; nameById[t.id] = t.name; });

  var endDate   = new Date(Date.now() - 86400000); // yesterday
  var startDate = new Date(Date.now() - days * 86400000);
  var fmt = function(d) { return d.toISOString().slice(0, 10); };

  var r = await fetch(
    'https://statsapi.mlb.com/api/v1/schedule?sportId=1&teamId=' + team.id +
    '&startDate=' + fmt(startDate) + '&endDate=' + fmt(endDate) +
    '&hydrate=linescore,probablePitcher,boxscore'
  );
  var data = await r.json();

  var games = [];
  (data.dates || []).forEach(function(d) {
    d.games.forEach(function(g) {
      if (g.status && g.status.abstractGameState !== 'Final') return;
      var isHome   = g.teams.home.team.id === team.id;
      var mySide   = isHome ? g.teams.home : g.teams.away;
      var oppSide  = isHome ? g.teams.away : g.teams.home;
      var myRuns   = mySide.score;
      var oppRuns  = oppSide.score;
      if (myRuns == null || oppRuns == null) return;

      var spId  = oppSide.probablePitcher && oppSide.probablePitcher.id;
      games.push({
        date:        d.date,
        gameId:      g.gamePk,
        opponent:    abbrById[oppSide.team.id] || oppSide.team.name,
        opponentId:  oppSide.team.id,
        opponentFullName: nameById[oppSide.team.id] || oppSide.team.name,
        isHome:      isHome,
        myRuns:      myRuns,
        oppRuns:     oppRuns,
        win:         myRuns > oppRuns,
        spId:        spId || null,
      });
    });
  });

  return games.sort(function(a, b) { return a.date < b.date ? -1 : 1; });
}

// Fetch team box score data: actual lineup (name, spot, season wOBA from roster) + team wOBA for the game
async function _fetchGameBoxScore(gameId, teamId, rosterMap) {
  try {
    var r = await fetch('https://statsapi.mlb.com/api/v1/game/' + gameId + '/boxscore');
    var d = await r.json();
    var side = null;
    if (d.teams) {
      var home = d.teams.home, away = d.teams.away;
      if (home && home.team && home.team.id === teamId) side = home;
      else if (away && away.team && away.team.id === teamId) side = away;
    }
    if (!side || !side.battingOrder) return { woba: null, lineup: [] };

    var agg = { atBats:0, hits:0, doubles:0, triples:0, homeRuns:0,
                baseOnBalls:0, intentionalWalks:0, hitByPitch:0, sacFlies:0 };
    var lineup = [];

    side.battingOrder.forEach(function(pid, idx) {
      var ps  = side.players && side.players['ID' + pid];
      var st  = ps && ps.stats && ps.stats.batting;
      var info = ps && ps.person;
      if (!st) return;

      agg.atBats           += st.atBats          || 0;
      agg.hits             += st.hits            || 0;
      agg.doubles          += st.doubles         || 0;
      agg.triples          += st.triples         || 0;
      agg.homeRuns         += st.homeRuns        || 0;
      agg.baseOnBalls      += st.baseOnBalls     || 0;
      agg.intentionalWalks += st.intentionalWalks || 0;
      agg.hitByPitch       += st.hitByPitch      || 0;
      agg.sacFlies         += st.sacFlies        || 0;

      var fullName = info && info.fullName;
      // Look up season avgWoba from roster (matched by name)
      var rp = fullName && rosterMap && rosterMap[fullName];
      lineup.push({
        spot:     idx + 1,
        name:     fullName || ('Player ' + pid),
        avgWoba:  rp ? (rp.avgWoba || 0.310) : 0.310,
        position: ps && ps.position && ps.position.abbreviation,
      });
    });

    var teamWoba = computeWOBA(agg);
    return {
      woba:   teamWoba > 0 ? +teamWoba.toFixed(4) : null,
      lineup: lineup,
    };
  } catch(e) { return { woba: null, lineup: [] }; }
}

// Compute predicted runs/win using pre-game lineup wOBA + pred scores + opp ERA factor
// oppLineupWoba: opponent's season avg wOBA (from box score lookup) — improves opp run estimate
// myWinPct: our team's blended win% — applies quality discount for weak teams
// ctx (optional): { isHome, homeWinPct, awayWinPct, vsWinnersWinPct, oppIsAbove500 }
function _predictGame(lineupWobas, lineupScores, oppEra, oppWinPct, oppLineupWoba, myWinPct, ctx) {
  var w = BACKTEST_WEIGHTS;
  var n = lineupWobas.length;

  // #3: Spot-weighted wOBA — top of order sees more PAs; normalized weighted average
  var rawWeights = w.spotWeights.slice(0, n);
  var weightSum  = rawWeights.reduce(function(a,b){return a+b;}, 0);
  var avgWoba = n && weightSum > 0
    ? lineupWobas.reduce(function(acc, woba, i) {
        return acc + woba * (rawWeights[i] || 0.83);
      }, 0) / weightSum
    : 0.310;

  // #4: Score confidence — dampen score boost when most players default to 50
  var realScores = lineupScores.filter(function(s) { return s !== 50; }).length;
  var scoreConfidence = n > 0 ? realScores / n : 0;
  var avgScore = lineupScores.length ? lineupScores.reduce(function(a,b){return a+b;},0) / lineupScores.length : 50;

  // ERA factor: high opp ERA (easy pitcher) → more runs for us
  var eraFactor = oppEra != null ? Math.max(0.80, Math.min(1.30, oppEra / LEAGUE_AVG_ERA)) : 1.0;
  // Score factor dampened by how many players have real (non-default) scores
  var scoreFactor = 1 + (avgScore - 50) / 99 * w.scoreBoost * scoreConfidence;
  // Blend ERA factor with neutral (1.0) using oppEraScale weight
  var adjustedEraFactor = eraFactor * w.oppEraScale + 1.0 * (1 - w.oppEraScale);
  // Team quality discount — bad teams convert wOBA to runs less efficiently
  var teamQuality = myWinPct != null ? Math.max(0.88, Math.min(1.12, 1.0 + (myWinPct - 0.500) * 0.5)) : 1.0;

  // ── Home/away split factor ──────────────────────────────────────────────────
  var homeAwayFactor = 1.0;
  if (ctx) {
    var splitPct = ctx.isHome ? ctx.homeWinPct : ctx.awayWinPct;
    if (splitPct != null && myWinPct > 0) {
      var splitDelta = splitPct - myWinPct;
      homeAwayFactor = Math.max(0.94, Math.min(1.06, 1.0 + splitDelta * 0.5));
    }
  }

  // ── vs-inferior-opponent factor ─────────────────────────────────────────────
  var inferiorOppFactor = 1.0;
  if (ctx && ctx.vsWinnersWinPct != null && !ctx.oppIsAbove500) {
    var vsGoodDelta = myWinPct - ctx.vsWinnersWinPct;
    inferiorOppFactor = Math.max(0.96, Math.min(1.04, 1.0 + vsGoodDelta * 0.3));
  }

  // ── Park factor ─────────────────────────────────────────────────────────────
  // Applied to both teams' run estimates — the park affects both offenses equally.
  var parkFactor = (ctx && ctx.parkFactor != null) ? ctx.parkFactor : 1.0;

  var predictedRuns = Math.min(
    avgWoba * w.wobaRunScale * scoreFactor * adjustedEraFactor * teamQuality * homeAwayFactor * inferiorOppFactor * parkFactor,
    w.maxPredictedRuns
  );

  // Opponent run estimate — also scaled by park factor
  var oppRunsWinPct = 4.65 + (oppWinPct - 0.500) * 13.0;
  var oppRunsEst = (oppLineupWoba != null && oppLineupWoba > 0)
    ? (oppLineupWoba * w.wobaRunScale * 0.6 + oppRunsWinPct * 0.4) * parkFactor
    : oppRunsWinPct * parkFactor;

  // Softened sigmoid (0.40) — less confident on close run differentials
  var runDiff = predictedRuns - oppRunsEst;
  var winProb = 1 / (1 + Math.exp(-runDiff * 0.40));

  return {
    predictedRuns:     +predictedRuns.toFixed(2),
    predictedWoba:     +avgWoba.toFixed(4),
    predictedWin:      winProb >= 0.50,
    winProb:           +winProb.toFixed(3),
    oppRunsEst:        +oppRunsEst.toFixed(2),
    avgScore:          Math.round(avgScore),
    eraFactor:         +eraFactor.toFixed(3),
    homeAwayFactor:    +homeAwayFactor.toFixed(3),
    inferiorOppFactor: +inferiorOppFactor.toFixed(3),
    parkFactor:        +parkFactor.toFixed(3),
  };
}

// Main backtest entry point — call with roster data (array of player objects with avgWoba + predScore)
async function backtestGamePlan(teamName, roster, predCache, sbUrl, sbKey, days) {
  days = days || 21;
  var teams = await _fetchTeamsList();
  var team  = teams.find(function(t) { return t.name === teamName; });
  if (!team) return { error: 'Team not found: ' + teamName, games: [] };

  var abbrById = {};
  teams.forEach(function(t) { abbrById[t.id] = t.abbreviation; });
  var myAbbr = abbrById[team.id] || null;

  // 1. Fetch past games
  var pastGames = await _fetchPastGames(teamName, days);
  if (!pastGames.length) return { error: 'No completed games found in last ' + days + ' days', games: [] };

  // Build name → player lookup for box score matching
  var rosterMap = {};
  roster.forEach(function(p) { rosterMap[p.player_name] = p; });

  // Suggested lineup: top-9 from our pool sorted by prediction score (constant across all past games)
  var myPool = roster.filter(function(p) { return p.team === teamName && (p.gamesPlayed || 0) >= 5; })
                     .sort(function(a, b) { return (predCache[b.player_name]&&predCache[b.player_name].score||0) - (predCache[a.player_name]&&predCache[a.player_name].score||0) });
  var suggestedTop9 = myPool.slice(0, 9);
  var suggestedLineup = suggestedTop9.map(function(p, i) {
    return {
      spot:     i + 1,
      name:     p.player_name,
      avgWoba:  p.avgWoba || 0.310,
      position: p.position ? p.position.split(',')[0] : '?',
    };
  });

  // Gather unique opp team IDs and pitcher IDs
  var uniqueTeamIds    = [...new Set(pastGames.map(function(g) { return g.opponentId; }))];
  var uniquePitcherIds = [...new Set(pastGames.map(function(g) { return g.spId; }).filter(Boolean))];

  var [teamEraMap, pitcherMap, standingsMap] = await Promise.all([
    Promise.all(uniqueTeamIds.map(function(id) {
      return fetch('https://statsapi.mlb.com/api/v1/teams/' + id + '/stats?stats=season&group=pitching&season=2026&sportId=1')
        .then(function(r) { return r.json(); })
        .then(function(d) {
          var s = d.stats && d.stats[0] && d.stats[0].splits && d.stats[0].splits[0];
          return { id: id, era: parseFloat(s && s.stat && s.stat.era) || LEAGUE_AVG_ERA };
        }).catch(function() { return { id: id, era: LEAGUE_AVG_ERA }; });
    })).then(function(arr) {
      var m = {}; arr.forEach(function(x) { m[x.id] = x.era; }); return m;
    }),
    Promise.all(uniquePitcherIds.map(_fetchPitcherDetails)).then(function(arr) {
      var m = {}; arr.forEach(function(pd) { m[pd.id] = pd; }); return m;
    }),
    _fetchStandings(),
  ]);

  // 3. Fetch box scores (actual lineup + team wOBA) and build result rows
  var gameRows = await Promise.all(pastGames.map(async function(g) {
    var oppEra = g.spId && pitcherMap[g.spId] && pitcherMap[g.spId].era != null
      ? pitcherMap[g.spId].era
      : (teamEraMap[g.opponentId] || LEAGUE_AVG_ERA);
    var oppEntry   = standingsMap[g.opponentId];
    var oppWinPct  = oppEntry != null ? oppEntry.winPct : 0.500;
    var myEntry    = standingsMap[team.id];
    var myWinPct   = myEntry  != null ? myEntry.winPct  : 0.500;

    // Split records for the new predictors
    // Venue is our park when home, opp park when away
    var venueAbbr    = g.isHome ? myAbbr : (abbrById[g.opponentId] || null);
    var gameParkFactor = venueAbbr ? (PARK_FACTORS[venueAbbr] || 1.0) : 1.0;

    var myCtx = {
      isHome:          g.isHome,
      homeWinPct:      myEntry  ? myEntry.homeWinPct      : null,
      awayWinPct:      myEntry  ? myEntry.awayWinPct      : null,
      vsWinnersWinPct: myEntry  ? myEntry.vsWinnersWinPct : null,
      oppIsAbove500:   oppWinPct >= 0.500,
      parkFactor:      gameParkFactor,
    };
    var oppCtx = {
      isHome:          !g.isHome,
      homeWinPct:      oppEntry ? oppEntry.homeWinPct      : null,
      awayWinPct:      oppEntry ? oppEntry.awayWinPct      : null,
      vsWinnersWinPct: oppEntry ? oppEntry.vsWinnersWinPct : null,
      oppIsAbove500:   myWinPct >= 0.500,
      parkFactor:      gameParkFactor,
    };

    // Fetch both box scores simultaneously: our team + opponent
    var [boxScore, oppBoxScore] = await Promise.all([
      _fetchGameBoxScore(g.gameId, team.id, rosterMap),
      _fetchGameBoxScore(g.gameId, g.opponentId, {}),
    ]);
    var actualLineup = boxScore.lineup; // [{spot, name, avgWoba, position}]

    var myTeamEra = teamEraMap[team.id] || LEAGUE_AVG_ERA;

    // Prediction A: actual lineup that played (season avgWoba per player)
    var actualWobas  = actualLineup.map(function(p) { return p.avgWoba; });
    var actualScores = actualLineup.map(function(p) {
      var rp = rosterMap[p.name];
      return (rp && predCache[rp.player_name] && predCache[rp.player_name].score) || 50;
    });
    var oppLineupWoba = oppBoxScore.woba || null;
    var predActual = actualWobas.length
      ? _predictGame(actualWobas, actualScores, oppEra, oppWinPct, oppLineupWoba, myWinPct, myCtx)
      : null;

    // Prediction B: suggested (optimizer) lineup — kept for summary stats
    var sugWobas  = suggestedTop9.map(function(p) { return p.avgWoba || 0.310; });
    var sugScores = suggestedTop9.map(function(p) { return (predCache[p.player_name] && predCache[p.player_name].score) || 50; });
    var predSuggested = _predictGame(sugWobas, sugScores, oppEra, oppWinPct, oppLineupWoba, myWinPct, myCtx);

    // Independent opponent prediction — call _predictGame for opp side so win probs can be normalised
    var oppBatWobas = oppBoxScore.lineup && oppBoxScore.lineup.length
      ? oppBoxScore.lineup.map(function(p) { return p.avgWoba || 0.310; })
      : (oppLineupWoba ? Array(9).fill(oppLineupWoba) : Array(9).fill(0.310));
    var predOppSide = _predictGame(oppBatWobas, Array(oppBatWobas.length).fill(50), myTeamEra, myWinPct, oppLineupWoba, oppWinPct, oppCtx);

    // Normalise so our win prob + opp win prob = 1 (same scale as Today's Games)
    function normProbs(myRaw, oppRaw) {
      var s = myRaw + oppRaw;
      return s > 0 ? { my: +(myRaw / s).toFixed(3), opp: +(oppRaw / s).toFixed(3) } : { my: 0.500, opp: 0.500 };
    }
    var normActual    = predActual ? normProbs(predActual.winProb, predOppSide.winProb) : null;
    var normSuggested = normProbs(predSuggested.winProb, predOppSide.winProb);

    var sp = g.spId ? pitcherMap[g.spId] : null;

    // Primary metrics use the actual-lineup prediction for backtesting accuracy
    var primary     = predActual || predSuggested;
    var normPrimary = normActual || normSuggested;
    return {
      date:            g.date,
      opponent:        g.opponent,
      opponentFull:    g.opponentFullName,
      isHome:          g.isHome,
      actualWin:       g.win,
      actualRuns:      g.myRuns,
      oppRuns:         g.oppRuns,
      actualWoba:      boxScore.woba,
      oppActualWoba:   oppBoxScore.woba,
      actualLineup:    actualLineup,
      suggestedLineup: suggestedLineup,
      // Opponent — independent prediction, normalised
      oppPred: {
        predictedRuns: predOppSide.predictedRuns,
        actualRuns:    g.oppRuns,
        actualWoba:    oppBoxScore.woba,
        runError:      +(Math.abs(predOppSide.predictedRuns - g.oppRuns)).toFixed(2),
        winProb:       normSuggested.opp,
      },
      // Actual-lineup prediction (normalised win prob)
      actual: predActual ? {
        predictedRuns:  predActual.predictedRuns,
        predictedWoba:  predActual.predictedWoba,
        predictedWin:   normActual.my >= 0.50,
        winProb:        normActual.my,
        avgScore:       predActual.avgScore,
        runError:       +(Math.abs(predActual.predictedRuns - g.myRuns)).toFixed(2),
        wobaError:      boxScore.woba != null ? +(Math.abs(predActual.predictedWoba - boxScore.woba)).toFixed(4) : null,
      } : null,
      // Suggested-lineup prediction (normalised win prob)
      suggested: {
        predictedRuns:  predSuggested.predictedRuns,
        predictedWoba:  predSuggested.predictedWoba,
        predictedWin:   normSuggested.my >= 0.50,
        winProb:        normSuggested.my,
        avgScore:       predSuggested.avgScore,
        runError:       +(Math.abs(predSuggested.predictedRuns - g.myRuns)).toFixed(2),
        wobaError:      boxScore.woba != null ? +(Math.abs(predSuggested.predictedWoba - boxScore.woba)).toFixed(4) : null,
      },
      // Legacy flat fields
      predictedRuns:   primary.predictedRuns,
      predictedWoba:   primary.predictedWoba,
      predictedWin:    normPrimary.my >= 0.50,
      winProb:         normPrimary.my,
      oppRunsEst:      predOppSide.predictedRuns,
      avgScore:        primary.avgScore,
      oppEra:          +oppEra.toFixed(2),
      oppWinPct:       oppWinPct,
      sp:              sp ? { name: sp.name, hand: sp.hand, era: sp.era } : null,
      runError:        +(Math.abs(primary.predictedRuns - g.myRuns)).toFixed(2),
      wobaError:       boxScore.woba != null ? +(Math.abs(primary.predictedWoba - boxScore.woba)).toFixed(4) : null,
    };
  }));

  // 4. Compute summary metrics for each prediction type
  function _summarize(rows, key) {
    var valid = rows.filter(function(r) { return r[key]; });
    if (!valid.length) return null;
    var avgRunMAE  = valid.reduce(function(s,r){return s+r[key].runError;},0) / valid.length;
    var withWoba   = valid.filter(function(r) { return r[key].wobaError != null; });
    var avgWobaMAE = withWoba.length ? withWoba.reduce(function(s,r){return s+r[key].wobaError;},0) / withWoba.length : null;
    var correct    = valid.filter(function(r) { return r[key].predictedWin === r.actualWin; }).length;
    return {
      winAccuracy:  +(correct / valid.length).toFixed(3),
      avgRunMAE:    +avgRunMAE.toFixed(2),
      avgWobaMAE:   avgWobaMAE != null ? +avgWobaMAE.toFixed(4) : null,
      gamesEval:    valid.length,
    };
  }

  var actualSummary    = _summarize(gameRows, 'actual');
  var suggestedSummary = _summarize(gameRows, 'suggested');

  return {
    team:             teamName,
    days:             days,
    gamesEval:        gameRows.length,
    suggestedLineup:  suggestedLineup,
    actualSummary:    actualSummary,
    suggestedSummary: suggestedSummary,
    // Legacy top-level fields (actual-lineup based)
    winAccuracy:  actualSummary ? actualSummary.winAccuracy  : (suggestedSummary ? suggestedSummary.winAccuracy  : 0),
    avgRunMAE:    actualSummary ? actualSummary.avgRunMAE    : (suggestedSummary ? suggestedSummary.avgRunMAE    : 0),
    avgWobaMAE:   actualSummary ? actualSummary.avgWobaMAE  : (suggestedSummary ? suggestedSummary.avgWobaMAE   : null),
    games:        gameRows,
    weights:      Object.assign({}, BACKTEST_WEIGHTS),
  };
}

// ── Today's Matchups: fetch all games for today+tomorrow with predictions ──
// Returns array of game objects sorted by probability mismatch (largest first).
// For each game, calls _predictGame for both home and away sides.
// Lineups: if boxscore is available (game started or within ~1h of first pitch), use it.
//           Otherwise fall back to season wOBA averages from the roster.
async function fetchTodayGames(roster, predCache) {
  // Use local date strings so "today" matches the user's timezone, not UTC
  var fmt = function(d) {
    var y = d.getFullYear();
    var m = String(d.getMonth() + 1).padStart(2, '0');
    var day = String(d.getDate()).padStart(2, '0');
    return y + '-' + m + '-' + day;
  };
  var today = new Date();
  var tomorrow = new Date(today.getFullYear(), today.getMonth(), today.getDate() + 1);
  var todayStr    = fmt(today);
  var tomorrowStr = fmt(tomorrow);

  var teams      = await _fetchTeamsList();
  var standings  = await _fetchStandings();

  var abbrById = {}, nameById = {}, leagueById = {};
  teams.forEach(function(t) {
    abbrById[t.id]  = t.abbreviation;
    nameById[t.id]  = t.name;
    leagueById[t.id] = t.league && t.league.id;
  });

  // Roster lookup: teamId → players with avgWoba + predScore
  var rosterByTeam = {};
  (roster || []).forEach(function(p) {
    // match by team name
    var teamObj = teams.find(function(t) { return t.name === p.team; });
    var tid = teamObj && teamObj.id;
    if (!tid) return;
    if (!rosterByTeam[tid]) rosterByTeam[tid] = [];
    rosterByTeam[tid].push(p);
  });

  var schedResp = await fetch(
    'https://statsapi.mlb.com/api/v1/schedule?sportId=1' +
    '&startDate=' + todayStr + '&endDate=' + tomorrowStr +
    '&hydrate=probablePitcher,linescore,teams'
  );
  var schedData = await schedResp.json();

  var rawGames = [];
  (schedData.dates || []).forEach(function(d) {
    d.games.forEach(function(g) {
      var state = g.status && g.status.abstractGameState;
      // Skip final games (show in-progress and upcoming)
      if (state === 'Final') return;
      rawGames.push({ dateStr: d.date, game: g });
    });
  });

  if (!rawGames.length) return [];

  // Collect unique pitcher IDs to batch-fetch details
  var pitcherIds = {};
  rawGames.forEach(function(entry) {
    var g = entry.game;
    var homeSp = g.teams.home.probablePitcher && g.teams.home.probablePitcher.id;
    var awaySp = g.teams.away.probablePitcher && g.teams.away.probablePitcher.id;
    if (homeSp) pitcherIds[homeSp] = true;
    if (awaySp) pitcherIds[awaySp] = true;
  });

  // Collect unique team IDs for ERA fetching
  var teamIds = {};
  rawGames.forEach(function(entry) {
    teamIds[entry.game.teams.home.team.id] = true;
    teamIds[entry.game.teams.away.team.id] = true;
  });

  var uniquePitcherIds = Object.keys(pitcherIds).map(Number);
  var uniqueTeamIds    = Object.keys(teamIds).map(Number);

  var pitcherDetailsList = await Promise.all(uniquePitcherIds.map(_fetchPitcherDetails));
  var pitcherMap = {};
  pitcherDetailsList.forEach(function(pd) { pitcherMap[pd.id] = pd; });

  var teamEraResults = await Promise.all(uniqueTeamIds.map(function(id) {
    return fetch(
      'https://statsapi.mlb.com/api/v1/teams/' + id +
      '/stats?stats=season&group=pitching&season=2026&sportId=1'
    ).then(function(r) { return r.json(); })
     .then(function(d) {
       var s = d.stats && d.stats[0] && d.stats[0].splits && d.stats[0].splits[0];
       return { id: id, era: parseFloat(s && s.stat && s.stat.era) || LEAGUE_AVG_ERA };
     })
     .catch(function() { return { id: id, era: LEAGUE_AVG_ERA }; });
  }));
  var teamEraMap = {};
  teamEraResults.forEach(function(r) { teamEraMap[r.id] = r.era; });

  // Helper: get season wOBA avg for a team from roster (fallback when no lineup)
  function teamSeasonWoba(teamId) {
    var players = rosterByTeam[teamId];
    if (!players || !players.length) return 0.310;
    var vals = players.map(function(p) { return p.avgWoba || 0.310; });
    return vals.reduce(function(a,b){return a+b;},0) / vals.length;
  }

  // Helper: build spot-indexed woba/score arrays from a lineup (9 entries, nulls ok)
  function lineupArrays(lineup, teamId) {
    var wobas = [], scores = [];
    var players = rosterByTeam[teamId] || [];
    var rosterMap = {};
    players.forEach(function(p) { rosterMap[p.player_name] = p; });

    for (var i = 0; i < 9; i++) {
      var entry = lineup && lineup[i];
      var name  = entry && (entry.name || (entry.player && entry.player.player_name));
      var rp    = name && rosterMap[name];
      wobas.push(rp ? (rp.avgWoba || 0.310) : 0.310);
      var score = rp && predCache && predCache[rp.player_name] && predCache[rp.player_name].score;
      scores.push(typeof score === 'number' ? score : 50);
    }
    return { wobas: wobas, scores: scores };
  }

  // For each game, attempt to fetch boxscore (used when game is live or lineup is posted)
  var gameResults = await Promise.all(rawGames.map(async function(entry) {
    var g       = entry.game;
    var dateStr = entry.dateStr;
    var homeId  = g.teams.home.team.id;
    var awayId  = g.teams.away.team.id;
    var homeAbbr = abbrById[homeId] || g.teams.home.team.name;
    var awayAbbr = abbrById[awayId] || g.teams.away.team.name;
    var homeName = nameById[homeId] || g.teams.home.team.name;
    var awayName = nameById[awayId] || g.teams.away.team.name;
    var state    = g.status && g.status.abstractGameState; // 'Preview' or 'Live'

    var homeSpId = g.teams.home.probablePitcher && g.teams.home.probablePitcher.id;
    var awaySpId = g.teams.away.probablePitcher && g.teams.away.probablePitcher.id;
    var homeSp   = homeSpId ? pitcherMap[homeSpId] : null;
    var awaySp   = awaySpId ? pitcherMap[awaySpId] : null;

    // ERA used for prediction: SP era if available, else team era
    var homeSpEra = (homeSp && homeSp.era != null) ? homeSp.era : teamEraMap[homeId] || LEAGUE_AVG_ERA;
    var awaySpEra = (awaySp && awaySp.era != null) ? awaySp.era : teamEraMap[awayId] || LEAGUE_AVG_ERA;
    var homeEntry  = standings[homeId];
    var awayEntry  = standings[awayId];
    var homeWinPct = homeEntry != null ? homeEntry.winPct : 0.500;
    var awayWinPct = awayEntry != null ? awayEntry.winPct : 0.500;
    var gameParkFactor = PARK_FACTORS[homeAbbr] || 1.0;
    var homeCtx = {
      isHome:          true,
      homeWinPct:      homeEntry ? homeEntry.homeWinPct      : null,
      awayWinPct:      homeEntry ? homeEntry.awayWinPct      : null,
      vsWinnersWinPct: homeEntry ? homeEntry.vsWinnersWinPct : null,
      oppIsAbove500:   awayWinPct >= 0.500,
      parkFactor:      gameParkFactor,
    };
    var awayCtx = {
      isHome:          false,
      homeWinPct:      awayEntry ? awayEntry.homeWinPct      : null,
      awayWinPct:      awayEntry ? awayEntry.awayWinPct      : null,
      vsWinnersWinPct: awayEntry ? awayEntry.vsWinnersWinPct : null,
      oppIsAbove500:   homeWinPct >= 0.500,
      parkFactor:      gameParkFactor,
    };

    // Try to get actual lineup from boxscore (game started or lineup released)
    var homeLineup = null, awayLineup = null;
    var lineupSource = 'estimated'; // 'actual' or 'estimated'
    try {
      var bsResp = await fetch('https://statsapi.mlb.com/api/v1/game/' + g.gamePk + '/boxscore');
      var bs     = await bsResp.json();
      if (bs.teams) {
        var homeSide = bs.teams.home, awaySide = bs.teams.away;
        if (homeSide && homeSide.battingOrder && homeSide.battingOrder.length >= 9) {
          homeLineup = homeSide.battingOrder.map(function(pid, idx) {
            var ps   = homeSide.players && homeSide.players['ID' + pid];
            var info = ps && ps.person;
            return { spot: idx + 1, name: info && info.fullName };
          });
          lineupSource = 'actual';
        }
        if (awaySide && awaySide.battingOrder && awaySide.battingOrder.length >= 9) {
          awayLineup = awaySide.battingOrder.map(function(pid, idx) {
            var ps   = awaySide.players && awaySide.players['ID' + pid];
            var info = ps && ps.person;
            return { spot: idx + 1, name: info && info.fullName };
          });
          lineupSource = 'actual';
        }
      }
    } catch(e) { /* boxscore unavailable — use estimated */ }

    // Build wOBA/score arrays (from actual lineup if available, else team season avg)
    var homeArr = lineupArrays(homeLineup, homeId);
    var awayArr = lineupArrays(awayLineup, awayId);
    var homeSeasonWoba = teamSeasonWoba(homeId);
    var awaySeasonWoba = teamSeasonWoba(awayId);

    // Build lineup entry arrays for CycleEdge (need full player objects with predCache)
    // For confirmed lineups: match by name. For estimated: use top-9 roster by predCache score.
    function lineupEntries(lineup, teamId, teamName) {
      var players = rosterByTeam[teamId] || [];
      // Fallback: match by team name string if ID lookup returned nothing
      if (!players.length && teamName) {
        players = (roster || []).filter(function(p) { return p.team === teamName; });
      }
      if (!players.length) return [];

      var rosterMap = {};
      players.forEach(function(p) { rosterMap[p.player_name] = p; });

      // Try name-matching from confirmed lineup entries first
      var named = [];
      for (var i = 0; i < 9; i++) {
        var entry = lineup && lineup[i];
        var name  = entry && (entry.name || (entry.player && entry.player.player_name));
        var rp    = name && rosterMap[name];
        named.push(rp ? { player: rp } : null);
      }
      var namedCount = named.filter(Boolean).length;

      // If fewer than 5 matched by name, fall back to top-9 by predCache score
      if (namedCount < 5) {
        var sorted = players
          .filter(function(p) { return predCache[p.player_name]; })
          .sort(function(a, b) {
            return (predCache[b.player_name].score || 0) - (predCache[a.player_name].score || 0);
          })
          .slice(0, 9);
        return sorted.map(function(p) { return { player: p }; });
      }
      return named;
    }
    var homeEntries = lineupEntries(homeLineup, homeId, homeName);
    var awayEntries = lineupEntries(awayLineup, awayId, awayName);
    var homeCycleEdge = computeCycleEdge(homeEntries, predCache);
    var awayCycleEdge = computeCycleEdge(awayEntries, predCache);

    // CycleEdge win probability: logistic on (home - away) cycle edge score difference
    var cycleEdgeHomeProb = null, cycleEdgeAwayProb = null, cycleEdgeFavorite = null;
    if (homeCycleEdge && awayCycleEdge) {
      var ceDiff = (homeCycleEdge.score - awayCycleEdge.score) / 100; // -1 to +1
      var ceHomeRaw = 1 / (1 + Math.exp(-ceDiff * 3.0)); // sigmoid, k=3
      var ceAwayRaw = 1 - ceHomeRaw;
      var ceSum = ceHomeRaw + ceAwayRaw;
      cycleEdgeHomeProb = +(ceHomeRaw / ceSum).toFixed(3);
      cycleEdgeAwayProb = +(ceAwayRaw / ceSum).toFixed(3);
      cycleEdgeFavorite = cycleEdgeHomeProb >= 0.50 ? 'home' : 'away';
    }

    // Home team prediction: faces away SP, away wOBA is opponent lineup
    var homePred = _predictGame(homeArr.wobas, homeArr.scores, awaySpEra, awayWinPct, awaySeasonWoba, homeWinPct, homeCtx);
    // Away team prediction: faces home SP, home wOBA is opponent lineup
    var awayPred = _predictGame(awayArr.wobas, awayArr.scores, homeSpEra, homeWinPct, homeSeasonWoba, awayWinPct, awayCtx);

    // Normalise to sum to 1 (sigmoid outputs are independent; blend for display)
    var rawHome = homePred.winProb;
    var rawAway = awayPred.winProb;
    var sum     = rawHome + rawAway;
    var normHomeWin = sum > 0 ? rawHome / sum : 0.500;
    var normAwayWin = sum > 0 ? rawAway / sum : 0.500;

    // Mismatch = how far from 50/50 (0 = coin flip, 1 = certain)
    var mismatch = Math.abs(normHomeWin - 0.5) * 2;

    // Parse game time (ISO string from API)
    var gameTime = g.gameDate || null; // UTC ISO string

    return {
      gamePk:        g.gamePk,
      dateStr:       dateStr,
      gameTime:      gameTime,
      state:         state,
      homeId:        homeId,
      awayId:        awayId,
      homeAbbr:      homeAbbr,
      awayAbbr:      awayAbbr,
      homeName:      homeName,
      awayName:      awayName,
      homeSp:        homeSp,
      awaySp:        awaySp,
      homeSpEra:     +homeSpEra.toFixed(2),
      awaySpEra:     +awaySpEra.toFixed(2),
      homeWinPct:    homeWinPct,
      awayWinPct:    awayWinPct,
      lineupSource:  lineupSource,
      home: {
        predictedRuns: homePred.predictedRuns,
        predictedWoba: homePred.predictedWoba,
        winProb:       +normHomeWin.toFixed(3),
        avgScore:      homePred.avgScore,
      },
      away: {
        predictedRuns: awayPred.predictedRuns,
        predictedWoba: awayPred.predictedWoba,
        winProb:       +normAwayWin.toFixed(3),
        avgScore:      awayPred.avgScore,
      },
      mismatch: +mismatch.toFixed(3),
      favorite: normHomeWin >= normAwayWin ? 'home' : 'away',
      cycleEdge: {
        home:     homeCycleEdge,
        away:     awayCycleEdge,
        homeProb: cycleEdgeHomeProb,
        awayProb: cycleEdgeAwayProb,
        favorite: cycleEdgeFavorite,
      },
    };
  }));

  // Sort by mismatch descending (strongest prediction first)
  return gameResults.sort(function(a, b) { return b.mismatch - a.mismatch; });
}

// ── CycleEdge Score ────────────────────────────────────────────────────────
// A cycle-and-lineup-weighted alternative to the wOBA run model.
// Weights cycle phase (40%) + recent trend (30%) + lineup heat (20%) + matchup (10%).
// Returns 0-100 where 50 = neutral, >50 favours this team.
function computeCycleEdge(lineupPlayers, predCache) {
  if (!lineupPlayers || !lineupPlayers.length) return null;

  var valid = lineupPlayers.filter(function(e) {
    return e && e.player && predCache[e.player.player_name];
  });
  if (!valid.length) return null;

  var n = valid.length;

  // Phase score: predCache breakdown.phaseScore (0-30) → normalise to 0-100
  var avgPhase = valid.reduce(function(s, e) {
    return s + (predCache[e.player.player_name].breakdown.phaseScore || 15);
  }, 0) / n;
  var phaseNorm = (avgPhase / 30) * 100; // 0-100

  // Trend score: breakdown.trendScore (0-25) → normalise to 0-100
  var avgTrend = valid.reduce(function(s, e) {
    return s + (predCache[e.player.player_name].breakdown.trendScore || 12);
  }, 0) / n;
  var trendNorm = (avgTrend / 25) * 100;

  // Lineup heat: fraction of hot/warm players → 0-100
  var hotCount = valid.filter(function(e) {
    var tier = predCache[e.player.player_name].tier;
    return tier === 'hot' || tier === 'warm';
  }).length;
  var heatNorm = (hotCount / n) * 100;

  // Matchup score: breakdown.matchupScore (0-10) → normalise to 0-100
  var avgMatchup = valid.reduce(function(s, e) {
    return s + (predCache[e.player.player_name].breakdown.matchupScore || 5);
  }, 0) / n;
  var matchupNorm = (avgMatchup / 10) * 100;

  // Weighted blend
  var raw = phaseNorm * 0.40 + trendNorm * 0.30 + heatNorm * 0.20 + matchupNorm * 0.10;

  return {
    score:       Math.round(raw),       // 0-100
    phaseNorm:   Math.round(phaseNorm),
    trendNorm:   Math.round(trendNorm),
    heatNorm:    Math.round(heatNorm),
    hotCount:    hotCount,
    totalCount:  n,
  };
}

// ── Node.js exports ────────────────────────────────────────────────────────
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    CYCLE_LENGTH, FORECAST_DAYS, MAX_COMPONENTS, MIN_PERIOD, MIN_AMPLITUDE,
    WOBA_WEIGHTS, computeWOBA,
    PHASE_CONFIG, UPCOMING_SCHEDULE,
    getPhase,
    dft, reconstructAt, analyzePlayerCycles, buildCycleChartData,
    fftPitcher, buildPitcherForecast,
    fetchUpcomingSchedule, computePredictionScore, fetchHitterSplits,
    backtestForecast, applyPhaseColoring, scoreColor, phaseAvg,
    transformRows, transformMLBSplits,
    scorePlayerAtSpot, buildOptimalLineup, buildOpponentLineup,
    backtestGamePlan, BACKTEST_WEIGHTS,
    fetchTodayGames, computeCycleEdge,
  };
}
