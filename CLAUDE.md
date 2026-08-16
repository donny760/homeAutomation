# homeAutomation — Powerwall Dashboard
> Last indexed: 2026-08-15

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
- `server.py` — Flask backend entry point: registers all API routes, imports from `lib.*`; network device CRUD routes (`GET/PUT/DELETE /api/network/devices`, `/api/network/rediscover`, `/api/network/ap_filters`, per-MAC filter/pin/unpin); `HTTPException` passes through with its own status code; `PUT /api/rules/<id>/toggle` and `POST /api/rules/reorder` endpoints; 404 guard runs before UPDATE in `api_rules_put` / `api_rules_toggle`; debug insights endpoint uses `_call_gemini` helper; `_record_tou_change` helper stamps a new `rate_history` row when TOU periods saved via Settings; all DB calls use `connect()`; suppresses `optimized_table` when identical to projection; AI insights failure logged to `event_log`; `@app.before_request _gate_debug_routes()` 404s any `/api/debug/*` path plus `/api/rules/ai-insights/debug` unless the `debug_enabled` setting is on; `POST /api/powerwall/backfill?days=N` (default 3, capped 30) triggers `trigger_backfill`, 202/409; `/api/events` clamps `limit` to `max(1, min(limit, 500))` (a negative limit previously reached SQLite as unbounded `LIMIT -1`); Nest debug routes read `nest_mod._nest_devices` / `._nest_devices_raw` / `._nest_devices_ts` as module attributes, not by-value imports; `_start()` runs backfill via `trigger_backfill()` and the startup cost rebuild via `_spawn_rebuild_daily_costs()` off-thread instead of blocking the socket bind
- `rules.py` — Powerwall automation rules engine, Windows-service-capable; imports from `lib.*`; `rules.log` via RotatingFileHandler (10 MB, 3 backups); `_rule_fires_at` treats holidays as weekends; `apply_settings` has no `first_run` param; duplicate schema/seed code removed; `pw_retry_after` backoff on Tesla 429 rate limits; `get_live_state` skips NULL and zero `battery_pct` rows (Fleet API backfill has no SoC; outage rows have zero — both would cause spurious battery condition hits); own Fleet OAuth token dir `FLEET_AUTH_DIR = BASE_DIR/.fleet_rules`, separate from `server.py`'s
- `lib/powerwall.py` — main `poller` loop: fetches Powerwall + all device data every 10s/30s (Fleet API mode), logs readings and events, triggers periodic cost rebuilds and rate/holiday refreshes; detects all-zero power streak over 2 minutes, logs Tesla cloud outage event + recovery, sets `pw=None` to force reconnect, and spawns `backfill_history` in a background thread on recovery; `_backfill_running` threading Event prevents overlapping backfill threads; `home_w` derived as solar + battery + grid; all logging via `_log_system_error` / `_log_success` (no bare `print()`); `trigger_backfill(lookback_days=3)` threads `backfill_history` behind `_backfill_running` for manual + auto-recovery callers; `backfill_history(lookback_days=3)` iterates **local** calendar days via `zoneinfo.ZoneInfo('America/Los_Angeles')` with tz-aware start/end and caps `end` at now (a future-ending window returned no data from Tesla — the bug behind same-day outage gaps staying empty); forced reconnects during an outage throttle to once per 120s (`_last_reconnect_ts`); poller catch-all error logging throttles to 1/5min and truncates tracebacks to 500 chars (`_last_poller_error_log` / `POLLER_ERROR_LOG_INTERVAL` / `POLLER_ERROR_DETAIL_MAX`)
- `lib/db.py` — SQLite schema init, migrations, default rule/settings seeding, raw readings read/write; `tou_periods` seed uses `INSERT OR IGNORE` to preserve user-edited TOU settings across restarts; `connect()` helper returns a connection with WAL mode, busy_timeout, and FK enforcement; migration v2 adds `tou_periods_json` column to `rate_history` and backfills pre/post 2026-03-01 TOU windows; migration v3 adds nullable `*_export` columns; migration v4 adds `switches_meta.source_name`, drops the dead `feat1` pool row, and re-keys the pool GPM cache to `pool_cached_gpm_samples`; all `sqlite3.connect(DB_PATH)` calls across `lib/` migrated to `connect()`
- `lib/state.py` — global `_live` dict + `_lock` for thread-safe real-time Powerwall data; defines `BASE_DIR` and `DB_PATH`; calls `load_dotenv(BASE_DIR/.env, override=False)` at import so standalone tools (backfill scripts, ad-hoc `py -c`) resolve the same `DB_PATH` as the running service
- `lib/costs.py` — calculates/stores daily import/export costs by TOU tier; rebuilds historical data from readings; `rebuild_daily_costs` accepts `from_date` and spans from_date's year through the current year (`_rebuild_year` does one year); `_rebuild_today` and `calc_stats` use per-date `tou_periods_json` from `rate_history` instead of the global setting; `_period_rates()` returns `(import_rate, export_rate)` with import-rate fallback for NULL `*_export` columns; `mark_costs_stale()` + `cost_rebuild_pending_from` setting retry a rate-change rebuild if the lock is held; `month_savings_prior_days()` caches month-to-date savings for all days before today (`_month_cache`, keyed on `(today, first_reading_ts_of_today)`) so `/api/live` stops rescanning ~89k rows every 10s by month-end; export-direction kWh now credits `{period}_cost` at the export rate via `_period_rates()` (previously the import rate both directions); `_rebuild_today_locked()`'s day window is next-local-midnight via `datetime.combine`, not `midnight+86400` (DST-correct)
- `lib/rule_helpers.py` — loads rules from DB, computes upcoming fire times, validates rule bodies, provides schedule insights; `_rule_fires_at` catches bad hour/minute rows (`ValueError`/`TypeError`) instead of 500ing `/api/schedule`; `_validate_rule_body` range-checks `hour` (0–23) and `minute` (0–59) and rejects bools/non-ints; `_upcoming_firings` scans paused rules out to 8 days (ignoring the 48h cutoff that still applies to enabled rules) so a paused rule always yields one `pinned: true` row
- `lib/settings.py` — loads all settings from SQLite `settings` table; typed getters (str/int/bool); default value definitions; `set_setting(key, value)` writes (INSERT OR REPLACE) and invalidates the read cache; internal-state defaults `debug_enabled` ('0'), `cost_rebuild_pending_from`, `rates_last_success`, `holidays_last_success` (deliberately not exposed on the Settings page)
- `lib/events.py` — utility functions for writing to `event_log`; tracks device failure counts for all integrations; `_log_system_error` / `_log_success` no longer swallow write failures silently — they fall back to a module `logging.getLogger(__name__)` warning
- `lib/fetch_rates.py` — SDG&E EV-TOU-2 rate fetching from PDF, TOU period classification, writes `rates.json` + `rate_history` table; stamps `tou_periods_json` when saving new rates; warns in `event_log` if TOU windows differ from stored settings
- `lib/abode.py` — Abode security integration: event logging, backfill, event listener lifecycle, mode get/set; auto-deletes stale `abode.pickle` after 3 consecutive auth failures
- `lib/ai_insights.py` — builds JSON context (true-up projections, rule analysis, daily data) and calls Gemini (via google-genai SDK `Client.models.generate_content`) or Azure OpenAI (fallback) for energy advice; projection uses `daily_costs` import ratio (not home kWh), partial-month blending, base_charge double-count removed; system prompt uses DEFICIT/UNDERSHOOT/WITHIN TARGET/OVERSHOOT buckets, bans snake_case output; condition label map includes `net_cost_ytd` and `tomorrow_solar_kwh`
- `lib/kasa.py` — Kasa smart device discovery, polling, and control via persistent asyncio loop (`_kasa_loop`); use `_kasa_submit()`, never `asyncio.run()`; `_log_system_error` added at error sites
- `lib/nest.py` — Nest SDM integration: OAuth token management, device state, Pub/Sub event polling, thermostat control; `_log_system_error` added at error sites; module-level `_nest_devices` / `_nest_devices_raw` / `_nest_devices_ts` are rebound via `global` — callers (e.g. `server.py`'s debug routes) must read them as module attributes, not by-value imports
- `lib/pool.py` — ScreenLogic pool integration: status polling, circuit control, daily gallon accumulation; `_accumulate_pool_gallons`, `_recalc_pool_target`; tracks 12 circuits (500-508 + 510 Edge Prime / 511 Pool 2150 / 512 Pool 2700 — pump-speed presets); circuits are addressed by **id**, never by panel name (names are user-mutable at the keypad); `_pool_fetch_async` returns `(state, meta)` where meta carries panel names + pump preset RPMs; `_pool_sync_panel_meta` pushes keypad renames into `switches_meta` unless overridden locally; GPM is learned per preset speed into `pool_cached_gpm_samples` and `_recalc_pool_target` prices pump runtime by effective RPM (`max` of active preset circuits)
- `lib/rachio.py` — Rachio irrigation API: schedules, recent events, rain-based smart skip logic; `_log_system_error` added at error sites
- `lib/solar_forecast.py` — Open-Meteo solar forecast for today/tomorrow; scales radiation to local peak output; stores total kWh to DB; `_log_system_error` added at error sites
- `lib/weather.py` — Open-Meteo current conditions + short-term forecast (rain history/forecast) using Rachio device coordinates; `_log_system_error` added at error sites
- `lib/network.py` — DD-WRT/router polling for network device discovery, state merging, JSON persistence, MAC filtering; race condition fix: quarantine dict snapshot-read before iterating APs; writes to `_network_ap_quarantine` inside `_network_state_lock`; `_log_system_error` calls throttled (max once per 5 min) to avoid flooding `event_log`
- `lib/network_devices.py` — Linksys LRT224 + DD-WRT scraping, ARP table scans, hostname/MAC vendor enrichment
- `lib/switches.py` — unified interface for Kasa/Tuya/Pool/Abode/Nest switches: reads metadata from DB, dispatches commands; `_get_all_switches()` returns the new `source_name` field; pool circuits with no `POOL_EXT_TO_FIELD` mapping or a `None` reading report `reachable=False` instead of inventing a state
- `lib/tuya.py` — Tuya LAN device discovery, state polling, on/off control; manages connection failures and logging; wsdcg sensor category support: skips LAN DPS probe, uses cloud polling via `_tuya_cloud_poll_sensors` every 5 min; `_log_system_error` added at error sites
- `lib/backfill.py` — one-off utility: backfill Powerwall readings from Tesla Fleet API (day-by-day `get_calendar_history` for power + SOE/battery-pct); scrapes historical SDG&E rate PDFs into `rate_history`; rebuilds `daily_costs`; uses `db.connect()`; power rows use `INSERT OR IGNORE` (skip existing); SOE rows update `battery_pct` only where NULL; weekly commit to limit re-run data loss
- `powerwall.db` — primary SQLite: readings, rules, rule_conditions, daily_costs, event_log, switches_meta, settings, rate_history
- `rates.json` — cached SDG&E EV-TOU-2 rates (auto-refreshed monthly; excluded from git)
- `holidays.json` — SDG&E holiday calendar (auto-refreshed monthly; excluded from git)
- `devices.json` — Kasa/Tuya device definitions (not committed — runtime on server)
- `network_devices.json` — discovered network clients with friendly names (not committed — runtime on server)
- `abode.pickle` — Abode session token (not committed; excluded from git)
- `.env` — credentials: ABODE_EMAIL, ABODE_PASSWORD, NEST_CLIENT_ID, NEST_CLIENT_SECRET, NEST_PROJECT_ID, DB_PATH, LOG_PATH
- `frontend/src/app/page.tsx` — SPA root, hash-based page routing; validates the initial hash and `popstate` state via `isPageName()` (from `Nav.tsx`), falling back to `dashboard` for a hand-edited hash
- `frontend/src/components/Nav.tsx` — nav bar + page registry; `PAGES` (`as const satisfies`) is the single source of truth for the page set — `PageName`, `PAGE_KEYS`, and the `isPageName(v): v is PageName` type guard all derive from it
- `frontend/src/components/Dashboard.tsx` — main dashboard (powerflow, tiles)
- `frontend/src/components/PowerflowSVG.tsx` — animated SVG power flow diagram; energy split computed from watt values; includes `flow-grid-battery` path
- `frontend/src/components/EventLog.tsx` — event log page/table; AbortController on the polling fetch, `AbortError` swallowed
- `frontend/src/components/AutomationsPanel.tsx` — upcoming automations list; pause/resume toggle per rule; "PAUSED" badge; `ScheduleEntry.pinned` (from `/api/schedule`) keeps paused rules visible past the top-5 cut, appended after the upcoming entries
- `frontend/src/components/BottomTiles.tsx` — 5 bottom tiles: pool (RPM + GPM from Pentair), plus new TemperatureTile (outside weather + Nest thermostat + Tuya wsdcg sensor)
- `frontend/src/components/DayChart.tsx` — historical power chart (Solar/Home/Battery/Grid); uses `usePolling` hook with AbortController; `breakGaps()` inserts a `null` point across gaps >5min (`GAP_MS`) so outage gaps render as chart breaks, not interpolated ramps
- `frontend/src/components/SwitchesDrawer.tsx` — nav drawer for Kasa/Pool/Nest thermostat tiles; SensorTile for Tuya wsdcg sensors; polling migrated to `usePolling` hook; `Switch.source_name` drives a "Use panel name: …" button in the pool tile's edit form when it differs from the edited name
- `frontend/src/components/Rules.tsx` — rules management + today's firing timeline; drag-and-drop reordering via `@dnd-kit`; `notes` field; `enabled` toggle; `nextFireForRule` mirrors server-side holiday-as-weekend logic; `refreshRules`/`loadRates` accept an optional `AbortSignal`
- `frontend/src/components/EnergyCosts.tsx` — YTD and daily cost breakdown; month header rows show kWh totals per TOU tier; summary strip driven by `/api/costs/daily`'s range-scoped `totals` (not the loaded page); AbortController on the fetch, post-rebuild `setTimeout`s cleared on unmount via `rebuildTimersRef`
- `frontend/src/components/NetworkDevices.tsx` — network client table with PIN-protected name editing; `has_bans` field enables pin button for wired devices with prior AP bans; AbortController on the polling fetch
- `frontend/src/lib/tou.ts` — TOU period classification (client-side, mirrors server logic)
- `frontend/src/lib/format.ts` — shared formatting utilities: `fmtTime12`, `relativeTime` (moved here from `NetworkDevices.tsx`)
- `frontend/src/lib/markdown.ts` — renders AI markdown: caps headings at ###, styles #### as h5 with `--purple`; `escapeHtml()` runs on the whole input before parsing — output is `dangerouslySetInnerHTML`'d and includes user-controlled rule names/notes
- `deploy.bat` — builds frontend, backs up server DBs, mirrors files + `lib/` to `\\server-04`; comment documents that Fleet API tokens (`.pypowerwall.fleetapi`, `.pypowerwall.private.pem`, `.fleet_rules/`) are server-side state and not copied — `rules.py` needs its own one-time Tesla OAuth on the server
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
- `daily_costs` — per-day kWh + $ by TOU tier (import/export/on_peak/off_peak/super_off_peak); today's row rebuilt hourly by `_rebuild_today` (lock-guarded); a daily catch-up `rebuild_daily_costs` re-runs the last `cost_rebuild_days` days (default 7); full-year rebuilds only via `POST /api/costs/rebuild`
- `event_log` — all system events: rules, abode, nest, pool, rachio, home_control; never purge
- `switches_meta` — display config for Kasa/Tuya/Nest/Pool tiles in SwitchesDrawer; `source_name` (migration v4) holds the last name the source device reported — `name == source_name` means the tile still follows device renames, `name != source_name` means the user overrode it here and syncs must not clobber it
- `settings` — all runtime config (API keys, poll intervals, TOU periods, feature flags, pool GPM baselines, weekday/weekend gallon targets); also holds internal state not shown on the Settings page — `debug_enabled`, `cost_rebuild_pending_from`, `rates_last_success`, `holidays_last_success`
- `rate_history` — historical SDG&E rate records per effective date; `tou_periods_json` column (added migration v2) stores the TOU windows active at that effective date; used by `lib/costs.py` for historically-correct tier classification; six nullable `*_export` columns (migration v3) hold export credit rates — NULL means "credit exports at the retail import rate" (NEM 2.0), which is the current state for every row

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
- **Two money buckets, never summed:** fixed charges (Base Services Charge) and non-bypassable charges are cash, billed monthly, not offsettable by generation; energy import/export nets to a balance banked to annual true-up. Any "total cost" that adds a negative energy balance to a fixed charge is wrong
- **Known limitation:** `net_cost` = `import_cost - export_credit` at full retail TOU both ways, so it implicitly offsets the non-bypassable-charge portion (~2–3¢/kWh on imports) that NEM 2.0 does not actually let you offset — `net_cost` is therefore modestly optimistic. Fixing it needs an NBC $/kWh figure that `_parse_ev_tou2_pdf` does not extract and `rate_history` does not store
- Abode timeline API: always use `size=5`, larger page sizes drop recent events
- Kasa devices: persistent asyncio event loop (`_kasa_loop`) in `lib/kasa.py` — never call `asyncio.run()` for Kasa, use `_kasa_submit()`
- Network devices: ignore DHCP hostnames matching `new-host\d+` (LRT224 auto-assigns these)
- `PURGE_DAYS = 0` — never auto-delete readings or event_log
- No venv at project root — Python dependencies installed to system/user Python on server-04
- Pool GPM baselines and daily gallon targets (weekday/weekend) derived from 30 days of event_log and persisted to `settings`
- SQLite connections: always use `db.connect()` helper (not bare `sqlite3.connect(DB_PATH)`) — it sets WAL mode, busy_timeout, and FK enforcement
- `rate_history` writes: **never `INSERT OR REPLACE`** — REPLACE deletes the row first, nulling columns the statement doesn't name (`tou_periods_json`, `base_services_charge_per_day`, `end_date`, `*_export`). Migration v2 will not re-run to restore them. Use `ON CONFLICT(effective_date) DO UPDATE SET` listing only the columns that writer owns
- Rate changes retroactively correct costs: `mark_costs_stale(effective_date)` (in `lib/costs.py`) sets the `cost_rebuild_pending_from` setting and spawns a rebuild; if the non-blocking `_cost_rebuild_lock` is held the marker survives and the poller retries every 5 min. Callers: poller rate refresh, `POST /api/rates/refresh`, TOU change via Settings
- `rebuild_daily_costs(from_date=...)` spans from_date's year through the current year (`_rebuild_year` does one year); years without readings write nothing and leave existing rows intact
- Export credit: `lib/costs.py:_period_rates()` returns `(import_rate, export_rate)`, falling back to the import rate when the `*_export` column is NULL. Don is on NEM 2.0 (exports credited at full retail), so all rows are NULL and behaviour is unchanged. To move to NBT: populate the `*_export` columns, then `POST /api/costs/rebuild?from=<effective_date>`
- `lib/ai_insights.py` hands `_rate_for_date` its own hand-rolled **9-column** tuple — anything indexed past 8 in `_rate_for_date` must stay behind a `len(row) >` guard or AI insights raises IndexError
- `lib/fetch_rates.py` must not import `lib/events.py` (`fetch_rates → events → db → fetch_rates` cycle) — it returns `_changes` and lets callers log
- Rate refresh writes `rate_history` **before** `rates.json`; the JSON write is best-effort (a failure is logged, not raised) since the DB is authoritative for cost calculations
- Rate change detection diffs against `rate_history`, not `rates.json` — `deploy.bat` copies the dev machine's `rates.json` over the server's, which previously caused the same change to be re-reported on every poll
- `_is_refresh_due(start, months, last_run)` only enforces the interval when `last_run` is passed (`rates_last_success` / `holidays_last_success` settings); without it, it returns True forever once the first boundary passes
- Error surfacing: use `_log_system_error` (from `lib/events.py`) at all `print()`-only error sites so failures appear in `event_log`; `lib/network.py` throttles these calls to max once per 5 min
- rules.py: `battery_pct` is read from the `readings` table (latest row by timestamp), NOT from pypowerwall directly; `get_live_state` signature is `get_live_state(conn)` — no `pw` argument
- rules.py: pypowerwall connection is only reset on `apply_settings` failure — DB/logic errors in rule evaluation are caught separately and do not trigger a reconnect
- rules.py: `cond_cache` stores condition results keyed by `(rule_id, fire_dt_iso)`; passed into `current_target_state`; pruned when older than 3 days
- rules.py: `load_rules_from_db` uses a JOIN to fetch conditions only for enabled rules — disabled rules have no conditions loaded
- rules.py/server.py: `_rule_fires_at` treats SDG&E holidays as weekends — only rules with Sat(5) or Sun(6) in their days set fire on holidays; this logic is mirrored client-side in `Rules.tsx` `nextFireForRule`
- rules.py: STATE transitions and next-fire-time updates are logged only when the value changes (not every eval loop)
- rules.py: `apply_settings` has no `first_run` parameter — startup settings changes are always written to event_log
- Rules drag reorder: `PUT /api/rules/reorder` accepts `{ids: [...]}` ordered list; `sort_order` column updated; frontend uses `@dnd-kit/sortable`
- Rules pause/resume: `PUT /api/rules/<id>/toggle` flips `enabled`; AutomationsPanel shows "PAUSED" badge + ⏸/▶ button for upcoming rule entries
- `/api/debug/*` (and `/api/rules/ai-insights/debug`) are gated behind the `debug_enabled` setting and return 404 when it's off — don't add new debug routes outside that prefix without also adding them to `_DEBUG_EXTRA_PATHS`
- Nest module state must be read as module attributes (`nest_mod._nest_devices`), never by-value imports — `lib/nest.py` rebinds them under `global`
- `rules.py` and `server.py` use **separate** Fleet API auth dirs (`.fleet_rules/` vs project root) — Tesla rotates the refresh token, so a shared dir causes a race
- Frontend: recurring/polling reads use AbortController and swallow `AbortError`; mutations (PUT/DELETE/reorder/save) are deliberately not abortable
- `lib/markdown.ts` output is `dangerouslySetInnerHTML`'d and includes user-controlled rule names/notes — input must stay HTML-escaped
- `static/frontend/` is no longer tracked in git (build output); `build:local` clears it before each build

## Deployment
1. `deploy.bat` — builds frontend, backs up server runtime DBs, copies Python + `lib/` + static bundle to `\\server-04\Applications\projects\homeAutomation`
2. `.env` is intentionally NOT overwritten by deploy — update manually if new keys added
3. Restart Windows service on server-04 manually after deploy
4. Live at `http://server-04:5001`

## Recent Focus (as of 2026-08-15)
- **Pool circuits 510/511/512 + panel-name sync + gallon-model fixes.** The Home Control "Feature 1" tile broke because `lib/pool.py` found that circuit by scanning for the literal name `Feature 1`; it was circuit **510**, renamed to `Edge Prime` on the keypad 2026-08-15 15:52 (75 `feature1_changed` event rows end at that timestamp), after which the tile read `--` and 409'd on tap. Circuits 510/511/512 (`Edge Prime`, `Pool 2150`, `Pool 2700` — pump-speed presets, formerly Feature 1/2/3) are now tracked by circuit id. Circuits 513-517 (`Feature 4`-`8`) and 519 (`AuxEx`) are deliberately skipped — no pump preset assigned, so they would toggle nothing.
  - `switches_meta.source_name` (migration v4) makes tile names follow keypad renames while preserving local overrides; `switch_update_meta` needed no change (writing a different `name` *is* the override signal). "Use panel name" button in the drawer's edit form rejoins the synced set.
  - Gallon-model fixes: `pool_cached_normal_gpm` was corrupt (38.0 — a `Pool 2150` run overwrote the 1800-rpm baseline because the old cache bucketed by "cleaner on/off"), inflating `pool_gallons_target_weekday` to ~53k. GPM is now learned **per preset RPM** per pump into `pool_cached_gpm_samples`, and `_recalc_pool_target` slices pump runtime at override boundaries and prices each slice at `max(active preset rpm)` — replacing the cleaner-∩-pump intersection and the wrong `_CLEANER_PRESET_RPM = 2950` constant (real cleaner preset is 3000). Weekday target drops ~53k → ~45k.
  - `edge_pump_on` stays keyed to circuit 506, **not** `pump[0].state` — that field was observed reading 0 while 506 was on and the pump drew 174 W at 1380 rpm. `Edge Prime` runtime is captured via circuit 510's own spans instead.
  - `_log_pool_changes` now ignores `None` (circuit absent from a payload) instead of logging it as "turned off" — that bug produced the phantom `Feature 1 turned off`.
- **Rate-change handling audit + hardening.** Verified first: all 227 days of 2026 recomputed from raw `readings` matched stored `daily_costs`, so the 2026-06-01 and 2026-08-01 SDG&E rate changes were already being applied to the correct date ranges — no cost data was wrong. The work below hardens the fetch/persist path around that (already correct) calculation.
- `lib/db.py`: migration v3 adds six nullable `*_export` columns to `rate_history` (no DEFAULT — a `DEFAULT 0` would have zeroed every historical export credit)
- `lib/costs.py`: `_period_rates()` helper (export rate with import-rate fallback); `rebuild_daily_costs` split into a year loop + `_rebuild_year` so a prior-year `from_date` is no longer clamped to Jan 1 of the current year; `mark_costs_stale()` + `cost_rebuild_pending_from` marker so a rate change retroactively rebuilds and a lock conflict can't drop the correction; `_is_refresh_due` gained an optional `last_run` arg that actually enforces the interval
- `lib/fetch_rates.py`: `rate_history` written **before** `rates.json` (a 2026-06-14 `WinError 5` on `rates.json.tmp` had discarded a successfully parsed rate); JSON write now best-effort; `INSERT OR REPLACE` → `ON CONFLICT` so `end_date`/`*_export` survive and `tou_periods_json` is not restamped with the current global setting; returns `_changes` computed against `rate_history`
- `lib/backfill.py`: `INSERT OR REPLACE` → `ON CONFLICT` — re-running `backfill_rate_history()` would have permanently nulled `tou_periods_json` on every row (migration v2 will not re-run)
- `lib/powerwall.py`: rate/holiday refresh use `rates_last_success`/`holidays_last_success` so a restart no longer refetches (5 duplicate `rates_updated` events on 2026-06-01); change detection reads `_changes`; new 5-min pending-rebuild retry loop
- `server.py`: `/api/rates/refresh` now passes `db_path` + settings (it previously updated `rates.json` only, so the manual refresh button never reached the cost engine); `/api/costs/daily` returns range-scoped `totals` (incl. `base_charge`, `base_charge_known`, `total_cost`) and `rates_note`/`rate_periods` replacing the misleading `rates_as_of`; `_record_tou_change` returns bool and triggers `mark_costs_stale`
- `frontend/src/components/EnergyCosts.tsx`: summary strip is 5 columns (On-Peak / Off-Peak / Super Off-Peak / **Energy (Net)** / **Base Charge**) driven by server `totals` — previously it summed only the loaded 60-row page over a 227-day range. Base Services Charge renders "—" for ranges whose rate rows carry no BSC (all pre-2026 rows)
- **No combined total on the Energy Breakdown page — this is deliberate.** Energy net and Base Services Charge are not fungible: under NEM 2.0 the BSC is cash billed monthly and cannot be offset by export credits, while energy net is a balance banked to annual true-up. Summing them (`net_cost + base_charge`) implies the credit paid the charge, which cannot happen, and overstates the position whenever energy nets to a credit — i.e. most of the time here. A sum is only meaningful when `net_cost > 0`, and sign-dependent semantics do not belong in one tile
- **Security/robustness pass.** Closes off debug/admin routes, hardens error handling so failures actually surface, fixes a same-day backfill gap, and adds defensive input handling across the schedule/rules API and the frontend.
  - `server.py`: `@app.before_request _gate_debug_routes()` 404s any `/api/debug/*` path (plus `/api/rules/ai-insights/debug`) unless `debug_enabled` is on — these routes bulk-delete against the never-purged `event_log`, inject synthetic events, run full LAN sweeps, bypass the AP quarantine, and echo router credentials. New `POST /api/powerwall/backfill?days=N` (default 3, capped 30) manually triggers `trigger_backfill`. `/api/events` clamps `limit` to `max(1, min(limit, 500))` — a negative limit previously reached SQLite as unbounded `LIMIT -1`. Nest debug routes now read `nest_mod._nest_devices*` as module attributes rather than by-value imports, which had frozen them at the empty initial values. Startup no longer blocks the socket bind on `backfill_history()` or an unbounded `rebuild_daily_costs` — both now spawn off-thread (`trigger_backfill()`, `_spawn_rebuild_daily_costs(from_date=today - cost_rebuild_days)`).
  - `lib/powerwall.py`: `backfill_history(lookback_days=3)` iterates **local** calendar days (`zoneinfo.ZoneInfo('America/Los_Angeles')`) with tz-aware start/end and caps `end` at now — a future-ending window returned no data from Tesla, which is why same-day outage gaps stayed permanently empty. New `trigger_backfill()` wraps it behind `_backfill_running` for manual and auto-recovery callers alike. Forced reconnects during an outage throttle to once per 120s instead of once per outage, and the `connect` success log is suppressed on outage-retry reconnects. The poller's catch-all error log throttles to once per 5 min and truncates tracebacks to 500 chars — unthrottled, a dead Fleet token wrote ~8,640 rows and 10+ MB of tracebacks a day into `event_log`.
  - `lib/state.py`: `load_dotenv(BASE_DIR/.env, override=False)` at import so standalone tools resolve the same `DB_PATH` as the running service instead of falling back to an empty local DB.
  - `lib/events.py`: `_log_system_error`/`_log_success` log a `logging.getLogger(__name__)` warning on write failure instead of swallowing it — silently eating it had hidden the error reporter's own failures.
  - `lib/rule_helpers.py`: `_rule_fires_at` catches bad hour/minute rows instead of 500ing all of `/api/schedule`; `_validate_rule_body` range-checks `hour`/`minute` to block new bad rows; `_upcoming_firings` scans paused rules out to 8 days with a new `pinned: bool` field so a paused rule always yields one row even outside the 48h horizon. `AutomationsPanel.tsx` appends pinned entries after the top-5 upcoming ones instead of dropping them.
  - `rules.py`: separate Fleet OAuth token dir `FLEET_AUTH_DIR = BASE_DIR/.fleet_rules` — sharing `server.py`'s token dir let the two processes race on Tesla's rotating refresh token (whichever refreshed second presented a token the first had already invalidated, and pypowerwall swallowed the failure).
  - `lib/switches.py`: `_get_all_switches()` returns the new `source_name` field; pool circuits with no `POOL_EXT_TO_FIELD` mapping or a `None` reading report `reachable=False` instead of inventing a state.
  - `lib/costs.py`: `month_savings_prior_days()` caches month-to-date savings for all days before today — `/api/live` was rescanning ~89k rows every 10s by month-end. Export-direction kWh now credits `{period}_cost` at the export rate (previously the import rate both directions). `_rebuild_today_locked()`'s day window is next-local-midnight, not `midnight+86400` (DST-correct).
  - Frontend: `lib/markdown.ts` HTML-escapes the whole input before parsing — output is `dangerouslySetInnerHTML`'d and includes user-controlled rule names/notes. `Nav.tsx`'s `PAGES` is now the single source of truth (`as const satisfies`) with `PageName`/`PAGE_KEYS`/`isPageName()` derived from it; `page.tsx` validates hashes through `isPageName`, falling back to `dashboard`. `DayChart.tsx` inserts `null` points across gaps >5min so outage gaps render as chart breaks, not interpolated ramps. `EventLog.tsx`, `NetworkDevices.tsx`, `Rules.tsx`, `EnergyCosts.tsx` add AbortController to recurring/active reads (swallowing `AbortError`); mutations stay non-abortable; `EnergyCosts` also clears its post-rebuild timers on unmount.
  - `.gitignore` adds Fleet API credential files (`.pypowerwall.fleetapi`, `.pypowerwall.private.pem`, `.fleet_rules/`), SQLite WAL/SHM sidecars, `static/frontend/` (build output, no longer tracked), and `.claude/agent-memory/` (local agent scratch state). `deploy.bat` comment documents Fleet tokens are server-side state and not copied. `frontend/package.json`'s `build:local` now clears `static/frontend/` before copying so stale Next.js build hashes don't linger. Deleted `check_tou_page.py`, `commit.bat`, `frontend/rules.log`.

## Earlier Focus (as of 2026-06-14)
- `lib/powerwall.py` + `lib/backfill.py` + `rules.py`: Fleet API migration fixes — `backfill_history` guarded by `_backfill_running` threading Event (overlap guard); `get_live_state` skips NULL and zero `battery_pct` rows so Fleet API history rows (no SoC) and cloud-outage zero-rows never trigger spurious battery conditions; `lib/backfill.py` migrated to Fleet API `get_calendar_history` with SOE data and `db.connect()`
- Earlier (2026-06-14): `lib/powerwall.py`: `backfill_history` reworked — lookback extended from 24h to 72h; switched to day-by-day iteration (one API call per calendar day); upsert changed to `INSERT ... ON CONFLICT DO UPDATE WHERE` so cloud-outage zero-rows get overwritten with real data but genuine readings are never touched; on outage detection sets `pw=None` to force reconnect on next iteration; on cloud recovery spawns `backfill_history` in a background thread automatically; all `print()` calls replaced with `_log_system_error` / `_log_success`; `home_w` derivation comment fixed (solar + battery + grid)
- Earlier (2026-06-11): `lib/tuya.py`: wsdcg sensor category support — skips LAN DPS probe, polls via `_tuya_cloud_poll_sensors` every 5 min
- Earlier (2026-06-11): `lib/db.py`: `connect()` helper (WAL + busy_timeout + FK); all `sqlite3.connect(DB_PATH)` calls across `lib/` migrated to it; migration v2 adds `tou_periods_json` to `rate_history` and backfills pre/post 2026-03-01 windows
- Earlier (2026-06-11): `lib/costs.py`: `rebuild_daily_costs`, `_rebuild_today`, `calc_stats` now use per-date `tou_periods_json` from `rate_history` for historically-correct TOU tier classification
- Earlier (2026-06-11): `lib/fetch_rates.py`: stamps `tou_periods_json` when saving new rates; warns in `event_log` if TOU windows differ from stored settings
- Earlier (2026-06-11): `lib/abode.py`: auto-deletes stale `abode.pickle` after 3 consecutive auth failures
- Earlier (2026-06-11): `lib/powerwall.py`: detects all-zero power streak over 2 minutes, logs Tesla cloud outage event + recovery to `event_log`
- Earlier (2026-06-11): `lib/network.py`: `_log_system_error` calls throttled to max once per 5 min to avoid flooding `event_log`
- Earlier (2026-06-11): `lib/kasa.py`, `lib/nest.py`, `lib/rachio.py`, `lib/solar_forecast.py`, `lib/tuya.py`, `lib/weather.py`: `_log_system_error` added at all `print()`-only error sites
- Earlier (2026-06-11): `lib/ai_insights.py`: projection uses `daily_costs` import ratio (not home kWh), partial-month blending, base_charge double-count removed; system prompt uses DEFICIT/UNDERSHOOT/WITHIN TARGET/OVERSHOOT buckets, bans snake_case output
- Earlier (2026-06-11): `server.py`: `_record_tou_change` stamps new `rate_history` row when TOU periods saved via Settings; all DB calls use `connect()`; suppresses `optimized_table` when identical to projection; AI insights failure logged to `event_log`
- Earlier (2026-06-11): `rules.py`: duplicate schema/seed code removed; `pw_retry_after` backoff on Tesla 429 rate limits
- Earlier (2026-06-11): `frontend/src/components/BottomTiles.tsx`: new TemperatureTile (outside weather + Nest + Tuya wsdcg sensor) as 5th bottom tile
- Earlier (2026-06-11): `frontend/src/components/SwitchesDrawer.tsx`: SensorTile for Tuya wsdcg; polling migrated to `usePolling` hook
- Earlier (2026-06-11): `frontend/src/components/NetworkDevices.tsx`: `has_bans` field enables pin button for wired devices with prior AP bans
- Earlier (2026-06-11): `frontend/src/components/EnergyCosts.tsx` + `globals.css`: month header rows show kWh totals per TOU tier
- Earlier (2026-06-11): `frontend/src/lib/format.ts`: `relativeTime` moved here from `NetworkDevices.tsx`
- Earlier (2026-05-31): `Rules.tsx` `nextFireForRule` mirrors server-side holiday-as-weekend logic; `lib/db.py` `tou_periods` seed changed to `INSERT OR IGNORE`; `server.py` 404 guard before UPDATE; `lib/ai_insights.py` condition label map extended
- Earlier (2026-05-30): Gemini migrated to google-genai SDK; `lib/network.py` race condition fix; 9 new network device API routes in `server.py`
- Earlier (2026-05-27): Major refactor — `server.py` split into 20 modules under `lib/`
- Earlier: rules page drag-and-drop reorder, `notes` field, pause/resume toggle; PowerflowSVG energy split rework; rules.py hardening; pool tile GPM/RPM telemetry

## Agents Available
- @code-expert — general code quality, security, performance
- @python-expert — Flask backend, SQLite, pypowerwall integration
- @frontend-expert — Next.js/React/TypeScript dashboard components
- @security-expert — credentials, Abode/Nest OAuth, API key storage
- @git-commit — pre-commit checks and commit messages
- @project-indexer — update this file when major structural changes land
