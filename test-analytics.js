import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const analyticsSource = readFileSync(new URL('./analytics.js', import.meta.url), 'utf8');
const sandbox = {
  console,
  fetch,
  module: { exports: {} },
};
sandbox.exports = sandbox.module.exports;
vm.createContext(sandbox);
vm.runInContext(analyticsSource, sandbox, { filename: 'analytics.js' });

const { analyzePlayerCycles, _predictGame } = sandbox.module.exports;

const fakeGames = Array.from({ length: 15 }, (_, i) => ({ avg: 0.250 + Math.sin(i) * 0.05 }));
const cycles = analyzePlayerCycles(fakeGames);
if (!cycles || !Array.isArray(cycles.dominantCycles)) {
  throw new Error('analyzePlayerCycles did not return dominantCycles');
}

const pred = _predictGame(
  Array(9).fill(0.320),
  Array(9).fill(55),
  4.20,
  0.500,
  0.310,
  0.500,
  { parkFactor: 1.0 }
);

if (pred.predictedRuns !== 3.86 || pred.winProb !== 0.486) {
  throw new Error(`Unexpected _predictGame output: ${JSON.stringify(pred)}`);
}

console.log('analytics smoke tests passed');
