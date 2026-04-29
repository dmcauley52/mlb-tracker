// analytics.js — pure analytics functions, no React/DOM dependencies
// Usable in the browser (globals) and in Node.js (CommonJS exports at bottom)

// ── Constants ──────────────────────────────────────────────────────────────
var CYCLE_LENGTH    = 28;
var FORECAST_DAYS   = 14;
var MAX_COMPONENTS  = 5;
var MIN_PERIOD      = 4;
var MIN_AMPLITUDE   = 0.005;

var PHASE_CONFIG = [
  { name:"Peak",      start:0,  end:6,  color:"#22c55e", desc:"High energy, strong output" },
  { name:"Ovulation", start:7,  end:13, color:"#3b82f6", desc:"Sustained performance window" },
  { name:"Luteal",    start:14, end:20, color:"#f59e0b", desc:"Slight decline, still capable" },
  { name:"Recovery",  start:21, end:27, color:"#ef4444", desc:"Rest & reset phase" },
];

function getPhase(d) {
  return PHASE_CONFIG.find(function(p) { return d >= p.start && d <= p.end; });
}

var UPCOMING_SCHEDULE = {
  "New York Yankees":       { opponents:["TEX","TEX","TEX"], matchupFactor:1.15 },
  "Los Angeles Dodgers":    { opponents:["MIA","MIA","MIA"], matchupFactor:1.20 },
  "Houston Astros":         { opponents:["BAL","BAL"],       matchupFactor:1.05 },
  "Seattle Mariners":       { opponents:["MIN","MIN","MIN"], matchupFactor:1.08 },
  "Boston Red Sox":         { opponents:["TOR","TOR","TOR"], matchupFactor:1.05 },
  "San Diego Padres":       { opponents:["CHC","CHC","CHC"], matchupFactor:1.10 },
  "Chicago Cubs":           { opponents:["SD","SD","SD"],    matchupFactor:0.95 },
  "Atlanta Braves":         { opponents:["DET","DET","DET"], matchupFactor:1.18 },
  "Philadelphia Phillies":  { opponents:["SF","SF","SF"],    matchupFactor:1.02 },
  "Cleveland Guardians":    { opponents:["TB","TB","TB"],    matchupFactor:1.05 },
  "Tampa Bay Rays":         { opponents:["CLE","CLE","CLE"], matchupFactor:0.98 },
  "Minnesota Twins":        { opponents:["SEA","SEA","SEA"], matchupFactor:0.97 },
  "St. Louis Cardinals":    { opponents:["PIT","PIT","PIT"], matchupFactor:1.18 },
  "Kansas City Royals":     { opponents:["ATH","ATH"],       matchupFactor:1.12 },
  "Cincinnati Reds":        { opponents:["COL","COL","COL"], matchupFactor:1.22 },
  "Pittsburgh Pirates":     { opponents:["STL","STL","STL"], matchupFactor:0.92 },
  "Baltimore Orioles":      { opponents:["HOU","HOU"],       matchupFactor:0.95 },
  "Toronto Blue Jays":      { opponents:["BOS","BOS","BOS"], matchupFactor:1.00 },
  "Texas Rangers":          { opponents:["NYY","NYY","NYY"], matchupFactor:0.90 },
  "Chicago White Sox":      { opponents:["LAA","LAA","LAA"], matchupFactor:1.10 },
  "default":                { opponents:[],                  matchupFactor:1.00 },
};

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
  var raw = gameData.map(function(g) { return g.avg; });
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
      actual:   +g.avg.toFixed(3),
      fitted:   +Math.max(0.050, reconstructed[i]).toFixed(3),
      forecast: null,
    };
  });
  var lastFitted = historical.length ? historical[historical.length - 1].fitted : 0;
  var forecastPoints = forecastValues.map(function(v, i) {
    return {
      date:       '+' + (i + 1) + 'd',
      actual:     null,
      fitted:     null,
      forecast:   +Math.max(0.050, v).toFixed(3),
      isForecast: true,
    };
  });
  if (forecastPoints.length && historical.length) {
    forecastPoints.unshift({
      date:       historical[historical.length - 1].date,
      actual:     null,
      fitted:     lastFitted,
      forecast:   +Math.max(0.050, forecastValues[0]).toFixed(3),
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

// ── Prediction scoring (hitters, 0–99) ────────────────────────────────────
function computePredictionScore(playerName, teamName, gameData, seasonStats) {
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
    phaseScore = np.name === "Peak" ? 30 : np.name === "Ovulation" ? 22 : np.name === "Luteal" ? 12 : 4;
    phaseLabel = np.name + " phase";
  }

  var last5    = gameData.slice(-5);
  var last5Avg = last5.reduce(function(s,g){return s+g.avg;},0) / last5.length;
  var trendScore = Math.min(25, Math.round(last5Avg * 75));

  var ops      = seasonStats ? parseFloat(seasonStats.ops) : 0;
  var opsScore = ops >= 0.900 ? 20 : ops >= 0.800 ? 16 : ops >= 0.700 ? 11 : ops >= 0.600 ? 6 : 3;

  var last10     = gameData.slice(-10);
  var firstHalf  = last10.slice(0,5).reduce(function(s,g){return s+g.avg;},0) / 5;
  var secondHalf = last10.slice(5).reduce(function(s,g){return s+g.avg;},0) / Math.max(last10.slice(5).length,1);
  var momentumScore = secondHalf > firstHalf ? Math.min(15, Math.round((secondHalf - firstHalf) * 100)) : 0;

  var sched       = UPCOMING_SCHEDULE[teamName] || UPCOMING_SCHEDULE["default"];
  var matchupScore = Math.max(0, Math.min(10, Math.round((sched.matchupFactor - 0.85) / 0.45 * 10)));

  var score = Math.min(99, Math.max(1, phaseScore + trendScore + opsScore + momentumScore + matchupScore));
  var tier  = score >= 75 ? "hot" : score >= 55 ? "warm" : score >= 35 ? "neutral" : "cold";
  var label = score >= 75 ? 'Fire — ' + phaseLabel + ', strong outlook'
            : score >= 55 ? 'Trending up — ' + phaseLabel
            : score >= 35 ? 'Steady — ' + phaseLabel
            : 'Cooling off — ' + phaseLabel;

  return {
    score: score, tier: tier, label: label, phaseLabel: phaseLabel,
    opponents: sched.opponents,
    breakdown: { phaseScore: phaseScore, trendScore: trendScore, opsScore: opsScore, momentumScore: momentumScore, matchupScore: matchupScore },
  };
}

// ── Utilities ──────────────────────────────────────────────────────────────
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
    var cyc = i % CYCLE_LENGTH, ph = getPhase(cyc), avg = parseFloat(row.batting_avg) || 0;
    return {
      date:     new Date(row.game_date + "T12:00:00").toLocaleDateString("en-US", { month:"short", day:"numeric" }),
      cycleDay: cyc, phase: ph.name, phaseColor: ph.color,
      avg:  +avg.toFixed(3),
      ops:  +(parseFloat(row.ops) || avg * 1.7).toFixed(3),
      hits: row.hits      || 0,
      hr:   row.home_runs || 0,
      ab:   row.at_bats   || 0,
      rbi:  row.rbi       || 0,
    };
  });
}

function transformMLBSplits(splits) {
  return splits.filter(function(s){ return (s.stat && s.stat.atBats || 0) > 0; }).map(function(s, i) {
    var cyc = i % CYCLE_LENGTH, ph = getPhase(cyc);
    var avg = parseFloat(s.stat && s.stat.avg) || 0;
    var obp = parseFloat(s.stat && s.stat.obp) || 0;
    var slg = parseFloat(s.stat && s.stat.slg) || 0;
    return {
      date:     s.date ? new Date(s.date + "T12:00:00").toLocaleDateString("en-US", { month:"short", day:"numeric" }) : 'G' + (i + 1),
      cycleDay: cyc, phase: ph.name, phaseColor: ph.color,
      avg:  +avg.toFixed(3),
      ops:  +(obp + slg).toFixed(3),
      hits: (s.stat && s.stat.hits)      || 0,
      hr:   (s.stat && s.stat.homeRuns)  || 0,
      ab:   (s.stat && s.stat.atBats)    || 0,
      rbi:  (s.stat && s.stat.rbi)       || 0,
    };
  });
}

// ── Node.js exports ────────────────────────────────────────────────────────
if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    CYCLE_LENGTH, FORECAST_DAYS, MAX_COMPONENTS, MIN_PERIOD, MIN_AMPLITUDE,
    PHASE_CONFIG, UPCOMING_SCHEDULE,
    getPhase,
    dft, reconstructAt, analyzePlayerCycles, buildCycleChartData,
    fftPitcher, buildPitcherForecast,
    computePredictionScore,
    scoreColor, phaseAvg,
    transformRows, transformMLBSplits,
  };
}
