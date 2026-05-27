# homeAutomation — Powerwall Dashboard
> Last indexed: 2026-05-26

## What This Is
Home automation dashboard for a San Diego home with 3x Powerwall 2 (40.5 kWh), solar, pool, and smart home devices. Tracks energy, runs TOU-aware automation rules, and integrates Abode security, Nest thermostats, Rachio irrigation, Kasa/Tuya plugs, and network device discovery.

## Stack
- **Backend:** Python/Flask, `server.py`, port 5001
- **Frontend:** Next.js 14 + React 18 + TypeScript, static export served by Flask from `static/frontend/`
- **Database:** SQLite at `powerwall.db` (primary — all state), `readings.db` (legacy, unused)
- **External APIs:** pypowerwall (Powerwall local gateway), Rachio REST, Abode (abode.pickle session), Nest SDM (Pub/Sub + OAuth), Kasa LAN (python-kasa), Tuya LAN (tinytuya), Gemini AI (google), Azure OpenAI (fallback), Open-Meteo (weather), National Weather Service (AQI)
- **Deployment:** Windows service on `\\server-04`; deploy via `deploy.bat`

## Architecture
Flask (`server.py`) is the single backend: it polls pypowerwall every 10s, writes to SQLite every 30s, and caches all device state in memory. The Next.js frontend is built to a static export (`static/frontend/`), which Flask serves directly — no separate Node server in production. Dev mode proxies `/api/*` to `localhost:5001`. The rules engine (`rules.py`) runs as a separate process/service, evaluating Powerwall mode rules every 60s and writing to `event_log`. AI insights use Gemini (primary) or Azure OpenAI (fallback), both configured through the `settings` DB table.

## Key Files
- `server.py` — entire Flask backend (7300+ lines): polls, all API routes, device integrations, AI insights; includes pool GPM/gallon accumulation (`_accumulate_pool_gallons`, `_recalc_pool_target`, `_rebuild_today`, `_load_pool_gpm_cache`); `_rule_fires_at` suppresses weekday-only rules on SDG&E holidays; `PUT /api/rules/<id>/toggle` and `POST /api/rules/reorder` endpoints; `rules` table has `sort_order` + `notes` columns (auto-migrated); `rebuild_daily_costs` accepts `from_date` to avoid backfilling historical periods with wrong TOU config
- `rules.py` — Powerwall automation rules engine, Windows-service-capable; `rules.log` via RotatingFileHandler (10 MB, 3 backups); supports `net_cost` condition type; `load_rules_from_db` JOIN-loads conditions for enabled rules only; `cond_cache` passed to `current_target_state` (keyed by rule_id + fire_dt, pruned entries older than 3 days); `get_live_state(conn)` reads `battery_pct` from `readings` table — does NOT take `pw` arg; STATE and next-fire-time logged only on change; `apply_settings` has no `first_run` param — startup changes always log to event_log; split try/except so DB/logic errors don't reset pypowerwall connection; `_rule_fires_at` treats holidays as weekends (only weekend rules fire)
- `fetch_rates.py` — SDG&E EV-TOU-2 rate fetching from PDF, TOU period classification, holiday calendar
- `network_devices.py` — network client discovery via Linksys LRT224 + DD-WRT web-UI scraping
- `backfill.py` — one-off data repair/backfill utility for `event_log` and costs
- `powerwall.db` — primary SQLite: readings, rules, rule_conditions, daily_costs, event_log, switches_meta, settings, rate_history
- `rates.json` — cached SDG&E EV-TOU-2 rates (auto-refreshed monthly; excluded from git)
- `holidays.json` — SDG&E holiday calendar (auto-refreshed monthly)
- `devices.json` — Kasa/Tuya device definitions (not committed — runtime on server)
- `network_devices.json` — discovered network clients with friendly names (not committed — runtime on server)
- `abode.pickle` — Abode session token (not committed)
- `.env` — credentials: ABODE_EMAIL, ABODE_PASSWORD, NEST_CLIENT_ID, NEST_CLIENT_SECRET, NEST_PROJECT_ID, DB_PATH, LOG_PATH
- `frontend/src/app/page.tsx` — SPA root, hash-based page routing
- `frontend/src/components/Dashboard.tsx` — main dashboard (powerflow, tiles)
- `frontend/src/components/PowerflowSVG.tsx` — animated SVG power flow diagram; energy split computed from watt values (grid→home, solar→home/battery/grid, battery→home/grid, grid→battery); includes `flow-grid-battery` path
- `frontend/src/components/AutomationsPanel.tsx` — upcoming automations list; pause/resume toggle button per rule; "PAUSED" badge; exposes `rule_id` + `enabled` from `/api/schedule`
- `frontend/src/components/BottomTiles.tsx` — pool tile showing RPM + GPM from Pentair gateway
- `frontend/src/components/DayChart.tsx` — historical power chart (Solar/Home/Battery/Grid, 4 datasets), titled "Power"
- `frontend/src/components/SwitchesDrawer.tsx` — nav drawer for Kasa/Pool/Nest thermostat tiles
- `frontend/src/components/Rules.tsx` — rules management + today's firing timeline; drag-and-drop reordering via `@dnd-kit`; `notes` field per rule; `enabled` toggle; `SortableRow` component
- `frontend/src/components/EnergyCosts.tsx` — YTD and daily cost breakdown
- `frontend/src/components/NetworkDevices.tsx` — network client table with PIN-protected name editing
- `frontend/src/lib/tou.ts` — TOU period classification (client-side, mirrors server logic); `super_off_peak_winter_mar_apr` removed — weekday 10–14 is now year-round in `super_off_peak`
- `frontend/src/lib/format.ts` — shared formatting utilities (fmtTime12 etc.)
- `frontend/src/lib/markdown.ts` — renders AI markdown: caps headings at ###, styles #### as h5 with `--purple`
- `deploy.bat` — builds frontend, backs up server DBs, mirrors files to `\\server-04`
- `install_dep.bat` — pip install from requirements.txt

## Entry Points
```
# Backend (dev)
py server.py                        # runs on port 5001

# Rules engine (separate process)
py rules.py

# Frontend (dev — proxies /api/* to :5001)
cd frontend && npm run dev          # port 3000

# Both together
cd frontend && npm run dev:all

# Build for local testing (no deploy)
cd frontend && npm run build:local

# Deploy to server-04
deploy.bat
```

## SQLite Schema (powerwall.db)
- `readings` — 30s power snapshots: solar_w, home_w, battery_w, grid_w, battery_pct
- `rules` + `rule_conditions` — Powerwall mode automation rules; `rules` has `sort_order` (drag reorder) + `notes` (free text) columns; `rule_conditions` supports `net_cost` type
- `daily_costs` — per-day kWh + $ by TOU tier (import/export/on_peak/off_peak/super_off_peak); rebuilt hourly by `_rebuild_today` (default lookback: 7 days)
- `event_log` — all system events: rules, abode, nest, pool, rachio, home_control; never purge
- `switches_meta` — display config for Kasa/Tuya/Nest/Pool tiles in SwitchesDrawer
- `settings` — all runtime config (API keys, poll intervals, TOU periods, feature flags, pool GPM baselines, weekday/weekend gallon targets)
- `rate_history` — historical SDG&E rate records per effective date

## Environment Variables (.env)
| Key | Purpose |
|-----|---------|
| ABODE_EMAIL / ABODE_PASSWORD | Abode security login |
| NEST_CLIENT_ID / NEST_CLIENT_SECRET / NEST_PROJECT_ID | Google SDM OAuth |
| DB_PATH | SQLite path (default: `./powerwall.db`) |
| LOG_PATH | Rules log path (default: `./rules.log`) |

Gemini API key and Azure OpenAI credentials are stored in the `settings` DB table (configured via Settings page), not in `.env`.

## Development Conventions
- All calculations server-side in Python; LLM (AI Insights) receives data, never does math
- All AI UI elements use CSS var `--purple` (#A87CFF), not amber; AI markdown rendered via `markdown.ts`
- SDG&E EV-TOU-2 rate: on-peak 4–9 PM, super-off-peak midnight–6 AM and 10 AM–2 PM weekdays year-round (effective 2026-05-01); `super_off_peak_winter_mar_apr` key is gone — do not reference it
- Target SDG&E credit $100–$500/yr; excess credits not paid out — don't over-export
- Abode timeline API: always use `size=5`, larger page sizes drop recent events
- Kasa devices: persistent asyncio event loop (`_kasa_loop`) — never call `asyncio.run()` for Kasa, use `_kasa_submit()`
- Network devices: ignore DHCP hostnames matching `new-host\d+` (LRT224 auto-assigns these)
- `PURGE_DAYS = 0` — never auto-delete readings or event_log
- No venv at project root — Python dependencies installed to system/user Python on server-04
- Pool GPM baselines and daily gallon targets (weekday/weekend) derived from 30 days of event_log and persisted to `settings`
- rules.py: `battery_pct` is read from the `readings` table (latest row by timestamp), NOT from pypowerwall directly; `get_live_state` signature is `get_live_state(conn)` — no `pw` argument
- rules.py: pypowerwall connection is only reset on `apply_settings` failure — DB/logic errors in rule evaluation are caught separately and do not trigger a reconnect
- rules.py: `cond_cache` stores condition results keyed by `(rule_id, fire_dt_iso)`; passed into `current_target_state`; pruned when older than 3 days
- rules.py: `load_rules_from_db` uses a JOIN to fetch conditions only for enabled rules — disabled rules have no conditions loaded
- rules.py/server.py: `_rule_fires_at` treats SDG&E holidays as weekends — only rules with Sat(5) or Sun(6) in their days set fire on holidays
- rules.py: STATE transitions and next-fire-time updates are logged only when the value changes (not every eval loop)
- rules.py: `apply_settings` has no `first_run` parameter — startup settings changes are always written to event_log
- Rules drag reorder: `PUT /api/rules/reorder` accepts `{ids: [...]}` ordered list; `sort_order` column updated; frontend uses `@dnd-kit/sortable`
- Rules pause/resume: `PUT /api/rules/<id>/toggle` flips `enabled`; AutomationsPanel shows "PAUSED" badge + ⏸/▶ button for upcoming rule entries

## Deployment
1. `deploy.bat` — builds frontend, backs up server runtime DBs, copies Python + static bundle to `\\server-04\Applications\projects\homeAutomation`
2. `.env` is intentionally NOT overwritten by deploy — update manually if new keys added
3. Restart Windows service on server-04 manually after deploy
4. Live at `http://server-04:5001`

## Recent Focus (as of 2026-05-26)
- rules page: drag-and-drop reorder via `@dnd-kit/sortable`; `sort_order` column in `rules` table (auto-migrated)
- rules page: `notes` field per rule (shown below rule name in table; auto-migrated column)
- rules page / AutomationsPanel: pause/resume toggle — `PUT /api/rules/<id>/toggle`; AutomationsPanel shows "PAUSED" badge and ⏸/▶ button
- PowerflowSVG: energy split fully computed from watt values (grid→home, solar→home/battery/grid, battery→home/grid, grid→battery); new `flow-grid-battery` SVG path added
- tou.ts / server.py: `super_off_peak_winter_mar_apr` removed; weekday 10–14 is now always in `super_off_peak`; DB migration force-updates `tou_periods` setting on startup; `rebuild_daily_costs` accepts `from_date` to avoid retroactive TOU changes
- server.py: `_rule_fires_at` holiday logic corrected — holidays are treated as weekends (only weekend rules fire), matching rules.py behavior
- .gitignore: `powerwall.db`, `rates.json`, `rules.log`, `.pypowerwall.auth`, `__pycache__/`, `frontend/tsconfig.tsbuildinfo` now excluded
- Earlier: rules.py hardening (RotatingFileHandler, cond_cache, load_rules JOIN, holiday fix, battery_pct from readings table)
- Earlier: pool tile GPM/RPM telemetry, daily gallon tracking, `net_cost` condition type

## Agents Available
- @code-reviewer — general code review
- @python-reviewer — Flask backend, SQLite, pypowerwall integration
- @frontend-reviewer — Next.js/React/TypeScript dashboard components
- @security-auditor — credentials, Abode/Nest OAuth, API key storage
- @git-commit — pre-commit checks and commit messages
- @project-indexer — update this file when major structural changes land
