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

## Python Scripts
| Script | Purpose |
|--------|---------|
| `fetch_stats.py` | Nightly hitter gamelogs (yesterday) |
| `backfill_stats.py` | Historical hitter backfill |
| `fetch_pitcher_stats.py` | Nightly pitcher gamelogs (yesterday) |
| `backfill_pitcher_stats.py` | Historical pitcher backfill |

- All use `statsapi` + `psycopg2` + `python-dotenv`
- Pitcher script: fetches top 500 by IP, upserts via `ON CONFLICT (player_id, game_date) DO NOTHING`
- Game score formula: `50 + 3*outs + 2*IP + K - 2*H - 4*ER - 2*BB - HR`
- `MIN_INNINGS = 1.0` filter applied per game

## GitHub Actions (`.github/workflows/fetch.yml`)
Modes: `nightly`, `backfill`, `nightly-pitchers`, `backfill-pitchers`

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
- **Tab bar**: 🏏 Hitters / ⚾ Pitchers (top-level, `mainTab` state)
- **Hitters**: sidebar player list → Timeline / Cycle / Phases / Streak / Predict views + AI panel
- **Pitchers**: sidebar pitcher list → ERA/WHIP / K9 / Forecast / Game Log views + AI panel
- Hitters accent: `#3b82f6` (blue). Pitchers accent: `#7c3aed` (purple)
- Score colors: green ≥75, amber ≥50, red <50

### Supabase pagination
Always paginate in 1000-row chunks until `page.length < PAGE` to avoid silent truncation.

### AI scouting
POST to `/api/scout` with `{ prompt }`. Hitters prompt includes DFT cycle data + prediction score. Pitchers prompt includes ERA/WHIP/K9 trend + cycle length + prediction score.

## Common Fixes
- **ON CONFLICT error on pitcher insert**: missing unique constraint → `ALTER TABLE pitcher_gamelogs ADD CONSTRAINT pitcher_gamelogs_player_id_game_date_key UNIQUE (player_id, game_date);`
- **Env vars not injecting**: check `vercel.json` for `%SB_URL%` replacement config; values starting with `%` are treated as unset and fall back to localStorage
- **No players returned**: check Supabase RLS policies — anon key needs SELECT on both gamelogs tables