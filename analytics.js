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
        var last10 = (tr.records && tr.records.splitRecords || []).find(function(s) { return s.type === 'lastTen'; });
        var winPct = last10 && (last10.wins + last10.losses) > 0
          ? last10.wins / (last10.wins + last10.losses)
          : (tr.wins + tr.losses > 0 ? tr.wins / (tr.wins + tr.losses) : 0.500);
        map[tr.team.id] = +winPct.toFixed(3);
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
          dateMap[d.date] = { abbr: abbrById[opp.id] || opp.name, id: opp.id, pitcherId: probablePitcherId };
        });
      });
    }

    var opponents = [];
    var gameDayIds = [];
    var gameDayPitcherIds = [];
    var seenTeams = {}, seenPitchers = {};
    var uniqueTeamIds = [], uniquePitcherIds = [];
    for (var i = 0; i < 14; i++) {
      var dayStr = new Date(Date.now() + i * 86400000).toISOString().slice(0, 10);
      var entry = dateMap[dayStr];
      if (entry) {
        opponents.push(entry.abbr);
        gameDayIds.push(entry.id);
        gameDayPitcherIds.push(entry.pitcherId);
        if (!seenTeams[entry.id]) { seenTeams[entry.id] = true; uniqueTeamIds.push(entry.id); }
        if (entry.pitcherId && !seenPitchers[entry.pitcherId]) {
          seenPitchers[entry.pitcherId] = true; uniquePitcherIds.push(entry.pitcherId);
        }
      } else {
        opponents.push('---');
        gameDayIds.push(null);
        gameDayPitcherIds.push(null);
      }
    }

    var matchupFactor = 1.0;
    var opponentFactors = gameDayIds.map(function() { return null; });
    var matchupDetails = gameDayIds.map(function() { return null; });

    if (uniqueTeamIds.length > 0) {
      var teamEraPromises = uniqueTeamIds.map(function(id) {
        return fetch(
          'https://statsapi.mlb.com/api/v1/teams/' + id +
          '/stats?stats=season&group=pitching&season=2026&sportId=1'
        ).then(function(r) { return r.json(); })
         .then(function(d) {
           var s = d.stats && d.stats[0] && d.stats[0].splits && d.stats[0].splits[0];
           return parseFloat(s && s.stat && s.stat.era) || LEAGUE_AVG_ERA;
         })
         .catch(function() { return LEAGUE_AVG_ERA; });
      });

      var allResults = await Promise.all([
        Promise.all(teamEraPromises),
        Promise.all(uniquePitcherIds.map(_fetchPitcherDetails)),
        _fetchEraLeaderboard(),
        _fetchStandings(),
      ]);
      var teamEras     = allResults[0];
      var pitcherDetails = allResults[1];
      var leaderboard  = allResults[2];
      var standings    = allResults[3];

      var teamIdToEra = {};
      uniqueTeamIds.forEach(function(id, idx) { teamIdToEra[id] = teamEras[idx]; });

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
        var winPct = standings[teamId] != null ? standings[teamId] : 0.500;
        var wlFactor  = Math.max(0.85, Math.min(1.15, 1 + (0.500 - winPct) * 1.5));
        return Math.max(0.80, Math.min(1.30, +(eraFactor * 0.6 + wlFactor * 0.4).toFixed(3)));
      });

      matchupDetails = gameDayIds.map(function(teamId, i) {
        if (!teamId) return null;
        var pd = gameDayPitcherIds[i] ? pitcherById[gameDayPitcherIds[i]] : null;
        return {
          team:         opponents[i],
          teamFullName: nameById[teamId] || null,
          teamId:       teamId,
          teamEra:      teamIdToEra[teamId],
          teamWinPct:   standings[teamId] != null ? standings[teamId] : null,
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

    var result = { opponents: opponents, opponentFactors: opponentFactors, matchupDetails: matchupDetails, matchupFactor: +matchupFactor.toFixed(3) };
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
function computePredictionScore(playerName, teamName, gameData, seasonStats, scheduleData, hitterSplits) {
  if (!gameData || gameData.length < 3) {
    return { score:50, label:"Insufficient data", tier:"neutral", breakdown:{phaseScore:0,trendScore:0,opsScore:0,momentumScore:0,matchupScore:0}, opponents:[] };
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

  var score = Math.min(99, Math.max(1, phaseScore + trendScore + opsScore + momentumScore + matchupScore));
  var tier  = score >= 75 ? "hot" : score >= 55 ? "warm" : score >= 35 ? "neutral" : "cold";
  var label = score >= 75 ? 'Peak — ' + phaseLabel + ', strong outlook'
            : score >= 55 ? 'Rising — ' + phaseLabel
            : score >= 35 ? 'Holding — ' + phaseLabel
            : 'Fading — ' + phaseLabel;

  return {
    score: score, tier: tier, label: label, phaseLabel: phaseLabel,
    usingFixedCycle: usingFixedCycle, insufficientData: insufficientData,
    consistency: consistency, stdDev: +stdDev.toFixed(3),
    opponents: sched.opponents,
    opponentFactors: sched.opponentFactors || [],
    matchupDetails: sched.matchupDetails || [],
    breakdown: { phaseScore: phaseScore, trendScore: trendScore, opsScore: opsScore, momentumScore: momentumScore, matchupScore: matchupScore },
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

// ── Node.js exports ────────────────────────────────────────────────────────
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    CYCLE_LENGTH, FORECAST_DAYS, MAX_COMPONENTS, MIN_PERIOD, MIN_AMPLITUDE,
    WOBA_WEIGHTS, computeWOBA,
    PHASE_CONFIG, UPCOMING_SCHEDULE,
    getPhase,
    dft, reconstructAt, analyzePlayerCycles, buildCycleChartData,
    fftPitcher, buildPitcherForecast,
    fetchUpcomingSchedule, computePredictionScore,
    backtestForecast, applyPhaseColoring, scoreColor, phaseAvg,
    transformRows, transformMLBSplits,
    scorePlayerAtSpot, buildOptimalLineup, buildOpponentLineup,
  };
}
