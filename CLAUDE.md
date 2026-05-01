# MLB Cycle Analyzer — Claude.md

## Project
- **Repo**: `mlb-tracker` (GitHub), deployed on Vercel
- **Stack**: `index.html` (React 18 + Recharts 2.10 + Babel CDN — single file, no build step)
- **Serverless**: `api/scout.js` — Anthropic proxy, accepts `{ prompt }`, returns `{ text }`
- **DB**: Supabase PostgreSQL (psycopg2 for Python scripts, REST API for frontend)

## Env Vars
| Var | Where | Purpose |
|-----|-------|---------|
| `SB_URL` | Vercel + `.env` | Supabase project URL |
| `SB_KEY` | Vercel + `.env` | Supabase anon key |
| `ANTHROPIC_API_KEY` | Vercel | Claude API |
| `DEFAULT_TEAM` | Vercel | e.g. `Seattle Mariners` — pre-filters sidebar |
| `DATABASE_URL` | `.env` | Direct postgres connection for Python scripts |

Vercel injects `%SB_URL%`, `%SB_KEY%`, `%DEFAULT_TEAM%` into `index.html` as `<meta>` tags at build time. Frontend reads them via `readMeta()`, falls back to `localStorage`.

## Database Tables

### `player_gamelogs` (hitters)
Key columns: `player_name`, `team`, `game_date`, `batting_avg`, `ops`, `hits`, `home_runs`, `at_bats`, `rbi`
Unique constraint: implied by fetch pattern (check if explicit constraint exists)

### `pitcher_gamelogs` (pitchers)
Key columns: `player_id`, `player_name`, `game_date`, `season`, `team`, `opponent`, `innings_pitched`, `hits_allowed`, `runs_allowed`, `earned_runs`, `walks`, `strikeouts`, `home_runs_allowed`, `era`, `whip`, `pitches`, `strikes`, `game_score`, `is_starter`
**Unique constraint**: `UNIQUE (player_id, game_date)` — required for ON CONFLICT upsert

### `game_results` (cache — completed games)
PK: `game_pk`. Key columns: `game_date`, `season`, `home_team`, `away_team`, `home_team_id`, `away_team_id`, `home_score`, `away_score`, `home_team_woba`, `away_team_woba`, `sp_home_id`, `sp_away_id`, `sp_home_name`, `sp_away_name`, `home_lineup` (JSONB), `away_lineup` (JSONB)

### `team_stats_cache` (cache — refreshed nightly)
PK: `team_id`. Key columns: `team_name`, `abbreviation`, `league_id`, `season`, `season_era`, `season_whip`, `win_pct`, `l10_win_pct`, `wins`, `losses`, `fetched_date`

### `pitcher_profiles` (cache — refreshed nightly)
PK: `player_id`. Key columns: `player_name`, `team`, `team_id`, `league_id`, `hand`, `season`, `season_era`, `season_whip`, `k_per_9`, `bb_per_9`, `innings_pitched`, `games_started`, `median_ip`, `era_rank_al`, `era_rank_nl`, `fetched_date`

### `backtest_results` (cache — refreshed nightly)
PK: `(team, run_date, days_window)`. Key columns: `season`, `games_eval`, `win_accuracy`, `avg_run_mae`, `avg_woba_mae`, `game_rows` (JSONB with full per-game detail)

## Python Scripts
| Script | Purpose |
|--------|---------|
| `fetch_stats.py` | Nightly hitter gamelogs (yesterday) |
| `backfill_stats.py` | Historical hitter backfill |
| `fetch_pitcher_stats.py` | Nightly pitcher gamelogs (yesterday) |
| `backfill_pitcher_stats.py` | Historical pitcher backfill |
| `fetch_game_results.py` | Nightly/backfill completed game scores, lineups, team wOBA → `game_results` |
| `fetch_team_stats.py` | Nightly all-30-teams ERA + standings → `team_stats_cache` |
| `fetch_pitcher_profiles.py` | Nightly pitcher season summaries → `pitcher_profiles` |
| `fetch_backtest_cache.py` | Nightly backtest for all 30 teams (zero API calls) → `backtest_results` |
| `migrate_cache_tables.py` | One-time: creates the 4 cache tables |

- All use `psycopg2` + `python-dotenv`. Cache scripts don't use `statsapi`.
- On Windows set env vars with `set MODE=backfill` before running, not inline.
- Nightly pipeline order: fetch_stats → fetch_pitcher_stats → fetch_game_results → fetch_team_stats → fetch_pitcher_profiles → fetch_backtest_cache
- Game score formula: `50 + 3*outs + 2*IP + K - 2*H - 4*ER - 2*BB - HR`
- `MIN_INNINGS = 1.0` filter applied per game

## GitHub Actions (`.github/workflows/nightly.yml`)
Modes: `nightly`, `backfill`, `nightly-pitchers`, `backfill-pitchers`, `migrate-cache`, `backfill-games`, `nightly-cache`

## Frontend Architecture (`index.html`)
Single `<script type="text/babel">` — all React in one file.

### Key constants
```js
SEASON = 2026
MIN_AT_BATS = 20
CYCLE_LENGTH = 28          // fixed fallback cycle
FORECAST_DAYS = 14
MAX_COMPONENTS = 5         // DFT frequency components kept
MIN_PERIOD = 4             // games
MIN_AMPLITUDE = 0.005      // batting avg units
```

### Data sources (toggle in UI)
- `demo` — generated sinusoidal data, no DB needed
- `mlb` — live MLB Stats API (`statsapi.mlb.com`)
- `live` — Supabase (`player_gamelogs` / `pitcher_gamelogs`)

### Cycle analysis engine
- Full DFT (O(n²)) on batting avg signal, mean-subtracted
- Keeps top N components by amplitude above threshold
- Reconstructs fitted curve + extrapolates 14-game forecast
- Returns: `dominantCycles`, `reconstructed`, `forecastValues`, `r2`, `mean`

### Pitcher FFT
- Same DFT approach on `game_score` (preferred) or `era` (fallback)
- Returns: `dominantCycleDays`, `predictionScore` (0–99), `forecast[]`

### Prediction score (hitters, 0–99)
| Component | Max | Source |
|-----------|-----|--------|
| Cycle phase (FFT slope) | 30 | DFT forecast direction |
| Last-5 avg trend | 25 | recent game logs |
| Season OPS | 20 | season stats |
| 10-game momentum | 15 | first vs second half of last 10 |
| Matchup quality | 10 | `UPCOMING_SCHEDULE` hardcoded map |

### UI structure
- **Tab bar**: 🏏 Hitters / ⚾ Pitchers / 📋 Game Predictions / 🎯 Prediction Accuracy (`mainTab` state)
- **Hitters**: sidebar player list → Timeline / Cycle / Phases / Streak / Predict views + AI panel
- **Pitchers**: sidebar pitcher list → ERA/WHIP / K9 / Forecast / Game Log views + AI panel
- **Game Predictions** (`GamePlanTab`): team selector → upcoming schedule → Historical lineup + Opponent lineup + predicted runs/win% → Manager's Pregame Brief (AI) → Backtest panel (last 3 weeks, expandable per-game rows)
- **Prediction Accuracy** (`PredictionAccuracyTab`): loads from `backtest_results` cache instantly; falls back to live MLB API computation; ranks all 30 teams by W/L accuracy
- Hitters accent: `#3b82f6` (blue). Pitchers accent: `#7c3aed` (purple). Game Predictions: mixed
- Score colors: green ≥75, amber ≥50, red <50

### Supabase pagination
Always paginate in 1000-row chunks until `page.length < PAGE` to avoid silent truncation.

### AI scouting
POST to `/api/scout` with `{ prompt }`. Hitters prompt includes DFT cycle data + prediction score. Pitchers prompt includes ERA/WHIP/K9 trend + cycle length + prediction score.

## Game Predictions — Backtest Model
Defined in `analytics.js` — `BACKTEST_WEIGHTS` (keep in sync with `fetch_backtest_cache.py`):
- `wobaRunScale: 18.5` — predicted runs = avg lineup wOBA × 18.5
- `scoreBoost: 0.08` — player prediction scores nudge runs ±8% max
- `oppEraScale: 0.55` — blend weight for opponent ERA factor
- Win model: `oppRunsEst = 4.65 + (oppWinPct - 0.500) × 13` → logistic sigmoid on run diff
- Two predictions per game: **actual** (box score lineup) and **suggested** (optimizer top-9)
- `BtGameRow` component in `index.html` (before `GamePlanTab`) renders each backtest row — needs own component so `useState` is legal inside `.map()`
- Key bug history: `g.opponent` field name collision (team abbr string vs prediction object) — prediction object is now `g.oppPred`

## Key analytics.js functions (all globals via plain `<script>` load)
- `backtestGamePlan(teamName, roster, predCache, sbUrl, sbKey, days)` — main backtest entry
- `_fetchGameBoxScore(gameId, teamId, rosterMap)` — returns `{woba, lineup[]}` from box score
- `_predictGame(lineupWobas, lineupScores, oppEra, oppWinPct)` — run/win model
- `buildOptimalLineup(players, predCache)` — optimizer, fills spots by priority order
- `buildOpponentLineup(players, predCache)` — sorts by `typicalSpot` (historical batting order)
- `fetchUpcomingSchedule(teamName)` — fetches 14-day schedule + SP details + standings

## Common Fixes
- **ON CONFLICT error on pitcher insert**: missing unique constraint → `ALTER TABLE pitcher_gamelogs ADD CONSTRAINT pitcher_gamelogs_player_id_game_date_key UNIQUE (player_id, game_date);`
- **Env vars not injecting**: check `vercel.json` for `%SB_URL%` replacement config; values starting with `%` are treated as unset and fall back to localStorage
- **No players returned**: check Supabase RLS policies — anon key needs SELECT on all tables
- **Windows env vars**: use `set MODE=backfill` on its own line before running the script, not inline
- **React "object not valid as child"**: check for field name collisions — `g.opponent` is the team abbreviation string, prediction object is `g.oppPred`
- **useState in .map()**: extract to a named component (see `BtGameRow`) — hooks cannot be called inside array callbacks