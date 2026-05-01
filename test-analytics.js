const { analyzePlayerCycles, computePredictionScore } = require('./analytics.js');

const fakeGames = Array.from({ length: 15 }, (_, i) => ({ avg: 0.250 + Math.sin(i) * 0.05 }));
console.log(analyzePlayerCycles(fakeGames));
