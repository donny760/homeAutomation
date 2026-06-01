# homeAutomation — Powerwall Dashboard
> Last indexed: 2026-05-31

## What This Is
Home automation dashboard for a San Diego home with 3x Powerwall 2 (40.5 kWh), solar, pool, and smart home devices. Tracks energy, runs TOU-aware automation rules, and integrates Abode security, Nest thermostats, Rachio irrigation, Kasa/Tuya plugs, and network device discovery.

## Stack
- **Backend:** Python/Flask, `server.py`, port 5001
- **Frontend:** Next.js 14 + React 18 + TypeScript, static export served by Flask from `static/frontend/`
- **Database:** SQLite at `powerwall.db` (primary — all state), `readings.db` (legacy, unused)
- **External APIs:** pypowerwall (Powerwall local gateway), Rachio REST, Abode (abode.pickle session), Nest SDM (Pub/Sub + OAuth), Kasa LAN (python-kasa), Tuya LAN (tinytuya), Gemini AI (google-genai SDK), Azure OpenAI (fallback), Open-Meteo (weather), National Weather Service (AQI)
- **Deployment:** Windows service on `\\server-04`; deploy via `deploy.bat`

## Architecture
Flask (`server.py`) is the backend entry point: it imports from `lib/` modules and registers all API routes, but the core logic lives in the `lib/` package (20 modules). The main poller (`lib/powerwall.py`) runs every 10s/30s to fetch Powerwall + device state and write to SQLite. The Next.js frontend is built to a static export (`static/frontend/`), which Flask serves directly — no separate Node server in production. Dev mode proxies `/api/*` to `localhost:5001`. The rules engine (`rules.py`) runs as a separate process/service, importing from `lib/`, evaluating Powerwall mode rules every 60s and writing to `event_log`. AI insights use Gemini (primary) or Azure OpenAI (fallback), both configured through the `settings` DB table.

## Key Files
- `server.py` — Flask backend entry point: registers all API routes, imports from `lib.*`; network device CRUD routes (`GET/PUT/DELETE /api/network/devices`, `/api/network/rediscover`, `/api/network/ap_filters`, per-MAC filter/pin/unpin); `HTTPException` passes through with its own status code; `PUT /api/rules/<id>/toggle` and `POST /api/rules/reorder` endpoints; 404 guard runs before UPDATE in `api_rules_put` / `api_rules_toggle`; debug insights endpoint uses `_call_gemini` helper
- `rules.py` — Powerwall automation rules engine, Windows-service-capable; imports from `lib.*`; `rules.log` via RotatingFileHandler (10 MB, 3 backups); `_rule_fires_at` treats holidays as weekends; `apply_settings` has no `first_run` param
- `lib/powerwall.py` — main `poller` loop: fetches Powerwall + all device data every 10s/30s, logs readings and events, triggers periodic cost rebuilds and rate/holiday refreshes
- `lib/db.py` — SQLite schema init, migrations, default rule/settings seeding, raw readings read/write; `tou_periods` seed uses `INSERT OR IGNORE` to preserve user-edited TOU settings across restarts
- `lib/state.py` — global `_live` dict + `_lock` for thread-safe real-time Powerwall data; defines `BASE_DIR` and `DB_PATH`
- `lib/costs.py` — calculates/stores daily import/export costs by TOU tier; rebuilds historical data from readings; `rebuild_daily_costs` accepts `from_date`
- `lib/rule_helpers.py` — loads rules from DB, computes upcoming fire times, validates rule bodies, provides schedule insights
- `lib/settings.py` — loads all settings from SQLite `settings` table; typed getters (str/int/bool); default value definitions
- `lib/events.py` — utility functions for writing to `event_log`; tracks device failure counts for all integrations
- `lib/fetch_rates.py` — SDG&E EV-TOU-2 rate fetching from PDF, TOU period classification, writes `rates.json` + `rate_history` table
- `lib/abode.py` — Abode security integration: event logging, backfill, event listener lifecycle, mode get/set
- `lib/ai_insights.py` — builds JSON context (true-up projections, rule analysis, daily data) and calls Gemini (via google-genai SDK `Client.models.generate_content`) or Azure OpenAI (fallback) for energy advice; simplified transient-error detection; condition label map includes `net_cost_ytd` and `tomorrow_solar_kwh`
- `lib/kasa.py` — Kasa smart device discovery, polling, and control via persistent asyncio loop (`_kasa_loop`); use `_kasa_submit()`, never `asyncio.run()`
- `lib/nest.py` — Nest SDM integration: OAuth token management, device state, Pub/Sub event polling, thermostat control
- `lib/pool.py` — ScreenLogic pool integration: status polling, circuit control, daily gallon accumulation; `_accumulate_pool_gallons`, `_recalc_pool_target`
- `lib/rachio.py` — Rachio irrigation API: schedules, recent events, rain-based smart skip logic
- `lib/solar_forecast.py` — Open-Meteo solar forecast for today/tomorrow; scales radiation to local peak output; stores total kWh to DB
- `lib/weather.py` — Open-Meteo current conditions + short-term forecast (rain history/forecast) using Rachio device coordinates
- `lib/network.py` — DD-WRT/router polling for network device discovery, state merging, JSON persistence, MAC filtering; race condition fix: quarantine dict snapshot-read before iterating APs; writes to `_network_ap_quarantine` inside `_network_state_lock`
- `lib/network_devices.py` — Linksys LRT224 + DD-WRT scraping, ARP table scans, hostname/MAC vendor enrichment
- `lib/switches.py` — unified interface for Kasa/Tuya/Pool/Abode/Nest switches: reads metadata from DB, dispatches commands
- `lib/tuya.py` — Tuya LAN device discovery, state polling, on/off control; manages connection failures and logging
- `lib/backfill.py` — one-off utility: backfill Powerwall readings from Tesla cloud, scrape historical SDG&E rates from PDF, rebuild daily costs
- `powerwall.db` — primary SQLite: readings, rules, rule_conditions, daily_costs, event_log, switches_meta, settings, rate_history
- `rates.json` — cached SDG&E EV-TOU-2 rates (auto-refreshed monthly; excluded from git)
- `holidays.json` — SDG&E holiday calendar (auto-refreshed monthly; excluded from git)
- `devices.json` — Kasa/Tuya device definitions (not committed — runtime on server)
- `network_devices.json` — discovered network clients with friendly names (not committed — runtime on server)
- `abode.pickle` — Abode session token (not committed; excluded from git)
- `.env` — credentials: ABODE_EMAIL, ABODE_PASSWORD, NEST_CLIENT_ID, NEST_CLIENT_SECRET, NEST_PROJECT_ID, DB_PATH, LOG_PATH
- `frontend/src/app/page.tsx` — SPA root, hash-based page routing
- `frontend/src/components/Dashboard.tsx` — main dashboard (powerflow, tiles)
- `frontend/src/components/PowerflowSVG.tsx` — animated SVG power flow diagram; energy split computed from watt values; includes `flow-grid-battery` path
- `frontend/src/components/AutomationsPanel.tsx` — upcoming automations list; pause/resume toggle per rule; "PAUSED" badge
- `frontend/src/components/BottomTiles.tsx` — pool tile showing RPM + GPM from Pentair gateway
- `frontend/src/components/DayChart.tsx` — historical power chart (Solar/Home/Battery/Grid); uses `usePolling` hook with AbortController
- `frontend/src/components/SwitchesDrawer.tsx` — nav drawer for Kasa/Pool/Nest thermostat tiles
- `frontend/src/components/Rules.tsx` — rules management + today's firing timeline; drag-and-drop reordering via `@dnd-kit`; `notes` field; `enabled` toggle; `nextFireForRule` mirrors server-side holiday-as-weekend logic
- `frontend/src/components/EnergyCosts.tsx` — YTD and daily cost breakdown
- `frontend/src/components/NetworkDevices.tsx` — network client table with PIN-protected name editing
- `frontend/src/lib/tou.ts` — TOU period classification (client-side, mirrors server logic)
- `frontend/src/lib/format.ts` — shared formatting utilities (fmtTime12 etc.)
- `frontend/src/lib/markdown.ts` — renders AI markdown: caps headings at ###, styles #### as h5 with `--purple`
- `deploy.bat` — builds frontend, backs up server DBs, mirrors files + `lib/` to `\\server-04`
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
- Kasa devices: persistent asyncio event loop (`_kasa_loop`) in `lib/kasa.py` — never call `asyncio.run()` for Kasa, use `_kasa_submit()`
- Network devices: ignore DHCP hostnames matching `new-host\d+` (LRT224 auto-assigns these)
- `PURGE_DAYS = 0` — never auto-delete readings or event_log
- No venv at project root — Python dependencies installed to system/user Python on server-04
- Pool GPM baselines and daily gallon targets (weekday/weekend) derived from 30 days of event_log and persisted to `settings`
- rules.py: `battery_pct` is read from the `readings` table (latest row by timestamp), NOT from pypowerwall directly; `get_live_state` signature is `get_live_state(conn)` — no `pw` argument
- rules.py: pypowerwall connection is only reset on `apply_settings` failure — DB/logic errors in rule evaluation are caught separately and do not trigger a reconnect
- rules.py: `cond_cache` stores condition results keyed by `(rule_id, fire_dt_iso)`; passed into `current_target_state`; pruned when older than 3 days
- rules.py: `load_rules_from_db` uses a JOIN to fetch conditions only for enabled rules — disabled rules have no conditions loaded
- rules.py/server.py: `_rule_fires_at` treats SDG&E holidays as weekends — only rules with Sat(5) or Sun(6) in their days set fire on holidays; this logic is mirrored client-side in `Rules.tsx` `nextFireForRule`
- rules.py: STATE transitions and next-fire-time updates are logged only when the value changes (not every eval loop)
- rules.py: `apply_settings` has no `first_run` parameter — startup settings changes are always written to event_log
- Rules drag reorder: `PUT /api/rules/reorder` accepts `{ids: [...]}` ordered list; `sort_order` column updated; frontend uses `@dnd-kit/sortable`
- Rules pause/resume: `PUT /api/rules/<id>/toggle` flips `enabled`; AutomationsPanel shows "PAUSED" badge + ⏸/▶ button for upcoming rule entries

## Deployment
1. `deploy.bat` — builds frontend, backs up server runtime DBs, copies Python + `lib/` + static bundle to `\\server-04\Applications\projects\homeAutomation`
2. `.env` is intentionally NOT overwritten by deploy — update manually if new keys added
3. Restart Windows service on server-04 manually after deploy
4. Live at `http://server-04:5001`

## Recent Focus (as of 2026-05-31)
- `frontend/src/components/Rules.tsx`: `nextFireForRule` now mirrors server-side holiday-as-weekend logic — holiday dates treated as Sunday for rule day-matching in the UI
- `lib/db.py`: `tou_periods` seed changed to `INSERT OR IGNORE` — prevents overwriting user-edited TOU settings on every server restart
- `server.py`: 404 guard moved before UPDATE in `api_rules_put` / `api_rules_toggle`; debug insights endpoint refactored to use `_call_gemini` helper
- `lib/ai_insights.py`: condition label map extended with `net_cost_ytd` and `tomorrow_solar_kwh`
- Earlier (2026-05-30): `lib/ai_insights.py`: Gemini call migrated from raw HTTP requests to google-genai SDK (`Client.models.generate_content`); simplified transient-error detection
- Earlier (2026-05-30): `lib/network.py`: race condition fix — quarantine dict snapshot-read before iterating APs; writes to `_network_ap_quarantine` inside `_network_state_lock`
- Earlier (2026-05-30): `server.py`: 9 new network device API routes (`GET/PUT/DELETE /api/network/devices`, `/api/network/rediscover`, `/api/network/ap_filters`, per-MAC filter/pin/unpin); `HTTPException` passes through with its own status code; 3 unused imports removed
- Earlier (2026-05-30): `requirements.txt`: added `google-genai`, `pysnmp`, `mac-vendor-lookup`
- Earlier (2026-05-27): **Major refactor:** `server.py` split into 20 modules under `lib/`; `fetch_rates.py`, `network_devices.py`, `backfill.py` moved to `lib/` and deleted from root; `deploy.bat` updated to mirror `lib/` to server-04
- Earlier: `DayChart.tsx` refactored to use `usePolling` hook with AbortController; `.gitignore` updated (abode.pickle, holidays.json)
- Earlier: rules page drag-and-drop reorder (`@dnd-kit/sortable`), `notes` field, pause/resume toggle
- Earlier: PowerflowSVG energy split rework, TOU `super_off_peak_winter_mar_apr` removal, holiday logic fix
- Earlier: rules.py hardening (RotatingFileHandler, cond_cache, load_rules JOIN, battery_pct from readings table)
- Earlier: pool tile GPM/RPM telemetry, daily gallon tracking, `net_cost` condition type

## Agents Available
- @code-expert — general code quality, security, performance
- @python-expert — Flask backend, SQLite, pypowerwall integration
- @frontend-expert — Next.js/React/TypeScript dashboard components
- @security-expert — credentials, Abode/Nest OAuth, API key storage
- @git-commit — pre-commit checks and commit messages
- @project-indexer — update this file when major structural changes land
