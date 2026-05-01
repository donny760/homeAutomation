# homeAutomation — Powerwall Dashboard
> Last indexed: 2026-04-30

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
- `server.py` — entire Flask backend (7300+ lines): polls, all API routes, device integrations, AI insights
- `rules.py` — Powerwall automation rules engine, Windows-service-capable, logs to `rules.log`
- `fetch_rates.py` — SDG&E EV-TOU-2 rate fetching from PDF, TOU period classification, holiday calendar
- `network_devices.py` — network client discovery via Linksys LRT224 + DD-WRT web-UI scraping
- `backfill.py` — one-off data repair/backfill utility for `event_log` and costs
- `powerwall.db` — primary SQLite: readings, rules, rule_conditions, daily_costs, event_log, switches_meta, settings, rate_history
- `rates.json` — cached SDG&E EV-TOU-2 rates (auto-refreshed monthly)
- `holidays.json` — SDG&E holiday calendar (auto-refreshed monthly)
- `devices.json` — Kasa/Tuya device definitions (not committed — runtime on server)
- `network_devices.json` — discovered network clients with friendly names (not committed — runtime on server)
- `abode.pickle` — Abode session token (not committed)
- `.env` — credentials: ABODE_EMAIL, ABODE_PASSWORD, NEST_CLIENT_ID, NEST_CLIENT_SECRET, NEST_PROJECT_ID, DB_PATH, LOG_PATH
- `frontend/src/app/page.tsx` — SPA root, hash-based page routing
- `frontend/src/components/Dashboard.tsx` — main dashboard (powerflow, tiles)
- `frontend/src/components/PowerflowSVG.tsx` — animated SVG power flow diagram
- `frontend/src/components/DayChart.tsx` — historical power chart with prev/next nav
- `frontend/src/components/SwitchesDrawer.tsx` — nav drawer for Kasa/Pool/Nest thermostat tiles
- `frontend/src/components/Rules.tsx` — rules management + today's firing timeline
- `frontend/src/components/EnergyCosts.tsx` — YTD and daily cost breakdown
- `frontend/src/components/NetworkDevices.tsx` — network client table with PIN-protected name editing
- `frontend/src/lib/tou.ts` — TOU period classification (client-side, mirrors server logic)
- `frontend/src/lib/format.ts` — shared formatting utilities (fmtTime12 etc.)
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

# Deploy to server-04
deploy.bat
```

## SQLite Schema (powerwall.db)
- `readings` — 30s power snapshots: solar_w, home_w, battery_w, grid_w, battery_pct
- `rules` + `rule_conditions` — Powerwall mode automation rules
- `daily_costs` — per-day kWh + $ by TOU tier (import/export/on_peak/off_peak/super_off_peak)
- `event_log` — all system events: rules, abode, nest, pool, rachio, home_control; never purge
- `switches_meta` — display config for Kasa/Tuya/Nest/Pool tiles in SwitchesDrawer
- `settings` — all runtime config (API keys, poll intervals, TOU periods, feature flags)
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
- All AI UI elements use CSS var `--purple` (#A87CFF), not amber
- SDG&E EV-TOU-2 rate: on-peak 4–9 PM, super-off-peak midnight–6 AM (and 10 AM–2 PM weekdays Mar/Apr winter)
- Target SDG&E credit $100–$500/yr; excess credits not paid out — don't over-export
- Abode timeline API: always use `size=5`, larger page sizes drop recent events
- Kasa devices: persistent asyncio event loop (`_kasa_loop`) — never call `asyncio.run()` for Kasa, use `_kasa_submit()`
- Network devices: ignore DHCP hostnames matching `new-host\d+` (LRT224 auto-assigns these)
- `PURGE_DAYS = 0` — never auto-delete readings or event_log
- No venv at project root — Python dependencies installed to system/user Python on server-04

## Deployment
1. `deploy.bat` — builds frontend, backs up server runtime DBs, copies Python + static bundle to `\\server-04\Applications\projects\homeAutomation`
2. `.env` is intentionally NOT overwritten by deploy — update manually if new keys added
3. Restart Windows service on server-04 manually after deploy
4. Live at `http://server-04:5001`

## Recent Focus (as of 2026-04-30)
- Frontend bundle rebuild + install_dep.bat added
- Powerflow animation: single dot using `pathLength=1`
- Abode credentials moved to .env; Nest OAuth helpers refactored; `fmtTime12` shared util
- Weather: uses Rachio device GPS coordinates instead of hardcoded location
- Network Devices page: LRT224 + DD-WRT client discovery, PIN-protected name editing
- Kasa/Tuya persistent connections + quarantine to avoid WiFi disruption

## Agents Available
- @code-reviewer — general code review
- @python-reviewer — Flask backend, SQLite, pypowerwall integration
- @frontend-reviewer — Next.js/React/TypeScript dashboard components
- @security-auditor — credentials, Abode/Nest OAuth, API key storage
- @git-commit — pre-commit checks and commit messages
- @project-indexer — update this file when major structural changes land
