# Powerwall Dashboard — Project Context
# Session 12 handoff — March 30 2026

## Status
- Dashboard running — 5 pages: Dashboard, Event Log, Powerwall Rules, Energy Breakdown, Settings
- File split: dashboard.html (HTML only) + static/dashboard.css + static/dashboard.js
- rules.py v2 complete — reads from SQLite, runs as Windows service
- fetch_rates.py v2 — scrapes SDG&E rates page, finds current EV-TOU-2 PDF, parses rates,
  stores in rate_history table with effective dates
- backfill.py — one-time historical import from Tesla cloud API (run once)
- Energy YTD tile live (pos 1) — daily_costs table in powerwall.db
- Current Rate tile live (pos 2) — big rate, period accent colors, season badge, mini table
- Pool tile (pos 3) — temp, pump/edge/cleaner status, salt PPM + chlorinator %. Security stubbed (pos 4)
- Tile row height: 150px (fixed, tiles overflow:hidden; session 9 two-column layout)
- Rate card on Rules page enhanced — season columns color-coded, active rate cell highlighted,
  period-specific left border (amber/green/blue), NOW badge tracks period accent
- Dark/light theme toggle live — persists via localStorage
- Nav clock removed (Dashboard topbar clock is the only clock)
- Active integrations: pypowerwall (cloud), screenlogicpy (Pentair), Rachio, Abode (websocket)
- Run: `py server.py` (port 5000)
- Flask static file caching disabled (`SEND_FILE_MAX_AGE_DEFAULT = 0`) — prevents
  stale CSS/JS on deployed browser after code changes
- Rachio event logging live — polls `/device/{id}/event?startTime=&endTime=` every 30min
  (configurable `rachio_event_poll_interval`), deduplicates on (ts, title), logs to event_log
- Smart Rain Skip feature — evaluates accumulated rainfall over configurable lookback window,
  extends Rachio rain delay proportionally (mm_per_skip_day), cooperates with Rachio's own
  weather-based skip (never shortens existing delay). Disabled by default (`rain_skip_enabled`).
  Own settings card in UI with enable toggle.
- Active rain delays appear in Upcoming Automations with "Rain Delay" badge + muted styling
- Event Log page live — filter pills (Powerwall, Rachio/Sprinklers, Abode, Pool, Errors), scrollable rows, date dividers, system error logging
- Energy Breakdown page live — TOU period columns (On-Peak, Off-Peak, Super Off-Peak)
  per day with kWh + cost, month headers with per-period totals, YTD summary bar
- Settings page live — connector cards with toggles, editable intervals, unit dropdowns
- Rate history table — stores rates with effective dates, cost calculations use correct
  rate per date period
- Holidays + rates refresh moved out of startup into poller, calendar-driven from
  configurable start date + interval (months)
- All polling/parsing errors logged to event_log table
- Pool state-change events logged (pump, edge pump, cleaner, pool/spa circuits)
  via _log_pool_changes() comparing previous vs current poll state.
  Debounced: state change must persist for 2 consecutive polls before logging —
  filters out single-sample ScreenLogic flickers (e.g. edge pump briefly reporting
  off then back on). Uses _pool_pending dict to track unconfirmed transitions.
- Pool polling is clock-aligned (e.g. 15m → :00/:15/:30/:45, not relative to server start)
- Public port exposure check — every 5 min, checks if port 5000 is reachable from public IP
  (via ifconfig.me + TCP connect). Logs event on state transitions only (open→closed, closed→open).
  System: 'system', event_type: 'port_check', result: 'warning' when open, 'ok' when closed.
- Server binds 0.0.0.0:5000 — accessible externally when router port forward is active
- Salt chlorine generator (SCG) data extracted from screenlogicpy:
  salt_ppm, scg_active, scg_pool_pct, super_chlor — displayed on Pool tile
- Power flow SVG overhauled (session 9):
  - ViewBox widened to 900×360, all nodes shifted +50px for edge clearance
  - All icons centered in their node rings (solar sun, battery, home house, grid bolt)
  - Pill callouts repositioned with shorter leaders, no edge clipping
  - kWh-today readout texts use background rects to stay readable over flow paths
  - Solar kWh text colored #EF9F27 (orange), grid kWh text colored #888780 (gray)
  - Battery-to-grid flow path + animated overlay added (activates when battDis && gridOut)
  - JS: `setFlow('flow-battery-grid', battDis && gridOut, ...)` in dashboard.js
- Chart glitch filtering in /api/today (session 9):
  - Drops all-zero readings (Tesla cloud API glitch — all 4 values exactly 0)
  - Drops single-sample outliers (home_w differs >50% from both neighbors)
  - Real transitions (EV stopping, load changes) pass through — they persist across readings
- Chart theme support (session 9):
  - `syncChartTheme(isLight)` updates grid lines, tick colors, border colors
  - Light theme: grid lines transparent; dark theme: #222226
  - Called on theme toggle and after chart creation on initial load
  - CSS var `--grid-line` set to `transparent` for light, `#222226` for dark
- Dashboard layout refinements (session 9):
  - Tile row height reduced from 185px to 150px — more space for flow diagram + chart
  - Pool tile: two-column layout (`.tile-split`) — temp on left, pump/edge/cleaner/salt on right
  - Security tile: two-column layout — mode on left, open doors/windows on right
  - `.tile-detail` class for right-aligned detail text (0.82rem, line-height 1.4)
  - Nav sparkle icon (AI) enlarged from 0.7em to 0.85em
- AI Insights on Powerwall Rules page (session 8+, accuracy overhaul session 12):
  - Rule-based engine: `_analyze_rules()` — deterministic checks against EV-TOU-2 schedule
    (charging window duration, Sunday gaps, Mar/Apr super off-peak, 4 PM boundary,
    season-aware late export check (summer >=7PM, winter >=6PM), November grouping,
    upcoming holidays, holiday calendar health,
    TOU schedule staleness warning via `tou_periods_last_verified` setting)
  - Gemini AI engine: `_build_ai_context()` gathers monthly summaries, 7d daily costs,
    3-hourly readings, rules, rates, holidays, live snapshot, pre-calculated projection
    tables, data_quality metadata → sends to Gemini with system prompt.
    System prompt has explicit field-name-to-English translation block — Gemini must
    never output JSON keys like on_peak_kwh or solar_w.
  - True-up projection (`_build_trueup_projection()`): baseline + optimized tables.
    - Data-derived TOU period weights via `_compute_period_weights()` (splits daily_costs
      per-period signed kWh by sign — positive=import, negative=export — to get TOU
      distributions; kWh not cost to avoid double-counting rate differences)
    - Optimized projection uses actual avg daily on-peak export from months with active
      export rules. Fallback: prior year same-season avg daily export (`prior_year_seasonal`),
      then `CAPACITY * 0.50` (`capacity_estimate`)
    - Winter projected exports unscaled from prior year (not scaled by home ratio —
      exports driven by solar + rules, not consumption)
    - Actual months use calendar days for base charge (not recorded days count),
      except current incomplete month which uses recorded days
    - Returns metadata: period_weights_source, optimized_export_source, actual/projected
      months, projection_basis (per-month prior year source values + ratio applied)
  - `data_quality` field in Gemini context — tells Gemini what's measured vs estimated.
    System prompt includes "Data quality awareness" section for appropriate hedging.
  - `base_services_charge_per_day` stored in rate_history table (migration on init).
    Projection looks up per-month base charge from rate_history → rates.json → hardcoded.
  - `prior_year_note` derives charging window from actual rules (`_build_prior_year_note()`),
    no longer hardcodes "4–5 AM". Type comparisons fixed: `gc is True`/`gc is False`
    (was `gc == 1`/`gc == 0` against bool values — worked by accident via int coercion).
  - `_rule_charging_hours()` query now `ORDER BY hour, minute` — previously iterated in
    rowid order and overwrote charge_start/end, giving wrong window if rules were out of
    chronological order. Only affects Gemini context metadata.
  - `days_until_trueup` always targets next Jan 1 (January bug fixed)
  - Known: `_aggregate_monthly_power()` still uses SUM × avg_interval instead of trapezoid
    integration — inaccurate with gapped Tesla cloud data. Low priority (Gemini context only).
  - Settings: `gemini_api_key` (text), `gemini_model` (default: gemini-2.0-flash)
  - UI: purple drawer on Rules page, rule-based cards shown immediately,
    AI insights loaded on demand via POST, rendered as markdown

### Pending
- rules.py: write event_log rows on automation fire / skip / fail
- Verify Rachio device event API response fields against RACHIO_EVENT_TYPE_MAP
  (hit /api/debug/rachio/events to inspect raw data)
- Verify device.rainDelayExpirationDate field exists (hit /api/debug/rachio to check)

---

## Data connection — Powerwall
- Library: pypowerwall (v0.14.10, Python 3.14 on Windows)
- Mode: cloud (local mode not yet working)
- Email: don@nsdsolutions.com
- Gateway IP: 10.0.0.41 (PW2, static ARP entry, MAC 28:0F:EB:5D:F4:2B)
- Working init:
    pw = pypowerwall.Powerwall('', cloudmode=True,
                               email='don@nsdsolutions.com',
                               timeout=30, authpath=BASE_DIR)
- pw.power()  → {site, solar, battery, load}  (watts)
- pw.level()  → battery %
- pw.set_mode('self_consumption' | 'autonomous' | 'backup')
- pw.set_reserve(0-100)
- pw.set_grid_charging(True | False)
- pw.set_grid_export('battery_ok' | 'pv_only')
- Auth cached in .pypowerwall.auth and .pypowerwall.site in project root

### Sign conventions (confirmed from Tesla history API)
- battery_power: positive = discharging, negative = charging
- grid_power:    positive = importing, negative = exporting
- home_w = solar_w + batt_w + grid_w  (no sign flipping needed)
- DB stores battery_w = -batt_w so positive = charging (matches live poller)

### Mode label mapping (for display + rules manager)
- self_consumption -> "Self-Powered"       color: #1D9E75 (green)
- autonomous       -> "Time-Based Control" color: #EF9F27 (amber)
- backup           -> "Backup"             color: #e05252 (red)

---

## Data connection — Pentair ScreenLogic
- Library: screenlogicpy (async, UDP autodiscovery)
- Gateway: 10.0.0.177:80 — Pentair: F7-68-17, EasyTouch2 8
- Assign static DHCP reservation for 10.0.0.177 in router
- Access via async bridge in server.py

Confirmed data paths from debug endpoint /api/debug/pool:
  Pool temp:        data["body"]["0"]["last_temperature"]["value"]
  Spa temp:         data["body"]["1"]["last_temperature"]["value"]
  Pool heat mode:   data["body"]["0"]["heat_mode"]["value"]             (0=Off)
  Pool heat state:  data["body"]["0"]["heat_state"]["value"]
  Pool setpoint:    data["body"]["0"]["heat_setpoint"]["value"]         (87)
  Spa setpoint:     data["body"]["1"]["heat_setpoint"]["value"]         (104)
  Pool pump state:  data["pump"]["1"]["state"]["value"]                 (0/1)
  Pool pump watts:  data["pump"]["1"]["watts_now"]["value"]
  Edge pump:        data["circuit"]["506"]["value"]                      (0/1)
  NOTE: pump[0] is unreliable for edge pump — use circuit 506 instead
  Pool circuit:     data["circuit"]["505"]["value"]                     (0/1)
  Spa circuit:      data["circuit"]["500"]["value"]                     (0/1)
  Air temp:         data["controller"]["sensor"]["air_temperature"]["value"]
  Salt ppm:         data["controller"]["sensor"]["salt_ppm"]["value"]   (3050)

SCG (salt chlorine generator) data paths:
  SCG present:      data["scg"]["scg_present"]
  Salt ppm:         data["scg"]["sensor"]["salt_ppm"]["value"]          (value * 50)
  SCG active:       data["scg"]["sensor"]["state"]["value"]             (0/1)
  Pool setpoint %:  data["scg"]["configuration"]["pool_setpoint"]["value"]
  Spa setpoint %:   data["scg"]["configuration"]["spa_setpoint"]["value"]
  Super chlorinate: data["scg"]["super_chlorinate"]["value"]            (0/1)
  Super chlor timer:data["scg"]["configuration"]["super_chlor_timer"]["value"]

Circuit IDs:
  500=Spa, 501=Pool Light, 502=Water Light, 503=Spa Light,
  504=Waterfall, 505=Pool, 506=Edge Pump, 507=Spillway, 508=Cleaner

---

## Data connection — Rachio
- Library: requests (direct REST, v1 API)
- Auth: Bearer token dc3c7132-00c1-45dc-910c-0d8f06738b92
- Person ID: cd9ba73d-bfa7-486b-be4c-df63fe9b74c5
- Rate limit: 3,500 calls/day — poll every 2-5 min max
- Base URL: https://api.rach.io/1/public

Key endpoints:
  GET /person/{id}             -> account + devices + schedules embedded
  GET /device/{id}/event?startTime={ms}&endTime={ms} -> device event history
  PUT /device/{id}/rain_delay  -> body: {"id": device_id, "duration": seconds}
  Note: /device/{id}/event REQUIRES startTime + endTime params (epoch ms), 400 without them

Schedule data lives inside person response:
  person.devices[].scheduleRules[]
  Fields: startHour, startMinute, totalDuration,
          scheduleJobTypes (DAY_OF_WEEK_N pattern, 0=Sun)
  Note: no separate /device/{id}/scheduleRule endpoint

Rain delay fields on device object:
  person.devices[].rainDelayExpirationDate  (epoch ms, 0 or missing = no delay)

Rain delay automation — Smart Rain Skip:
  Algorithm: skip_days = floor(accumulated_rain_mm / mm_per_skip_day), capped at max_days
  - Fetches past N days of precip from Open-Meteo (past_days param on same forecast call)
  - Only EXTENDS existing Rachio delay — checks rainDelayExpirationDate first
  - If Rachio's own weather skip already goes further, does nothing
  - Logs rain_skip_extended events, deduped per day per device
  Configurable settings:
    rain_skip_enabled:        '0' (off by default)
    rain_lookback_days:       '7'
    rain_mm_per_skip_day:     '8' (8mm accumulated rain = 1 skip day)
    rain_skip_max_days:       '7'
    rain_skip_check_interval: '3600' (1 hour)

Debug endpoints:
  GET /api/debug/rachio         -> raw scheduleRules (first 3 per device)
  GET /api/debug/rachio/events  -> raw device events (last 7 days)

---

## Hardware / location
- 3× Tesla Powerwall 2 (40.5 kWh total usable capacity)
- SDG&E EV-TOU-2 plan, San Diego CA (lat 32.7157, lon -117.1611)
- Wall-mounted display — always-on dark mode

---

## Project layout
D:\projects\homeAutomation\
  dashboard.html          <- single-page app (5 pages, HTML only)
  static/dashboard.css    <- extracted styles
  static/dashboard.js     <- extracted JS logic
  server.py               <- Flask backend — serves data API + static files
  rules.py                <- rules engine (reads from SQLite, runs as service)
  fetch_rates.py          <- SDG&E holidays, TOU period, rate page scraper + PDF parser
  backfill.py             <- one-time historical import from Tesla cloud API
  abode_import.py         <- one-time Abode CSV history importer
  powerwall.db            <- SQLite — readings, rules, daily_costs, event_log,
                              rate_history, settings
  rates.json              <- current SDG&E rates (written by fetch_rates.py, backward compat)
  holidays.json           <- SDG&E holiday dates, auto-regenerated
  .pypowerwall.auth       <- pypowerwall auth cache
  .pypowerwall.site       <- pypowerwall site cache
  rules.log               <- rules engine log
  CONTEXT.md          <- this file

---

## Navigation — 5 pages
Dashboard:         live power flow, chart, pool tile, upcoming automations widget
Event Log:         filterable event timeline (Powerwall, Rachio/Sprinklers, Abode, Pool) + system errors
Powerwall Rules:   rules manager (CRUD table + add/edit form) + rate card
Energy Breakdown:  daily cost breakdown by TOU period (On-Peak, Off-Peak, Super Off-Peak)
Settings:          connector toggles, polling intervals, SDG&E rate config, Gemini AI config

---

## Dashboard layout (current v2)

### Top bar
- Left: clock + date
- Right: weather temp + forecast (Open-Meteo, San Diego coords)
- Self-sufficiency % removed

### Power flow quadrant
- 4 nodes (Solar, Battery, Home, Grid) — icons only, no text on nodes
- Pill callouts with dashed leader lines (see pill spec below)
- Solar kWh today: muted text centered in quadrant below solar node
- Grid kWh today: SVG text above grid node
- Mode badge: upper-left corner of quadrant, color-coded by mode

### Upcoming automations widget (on Dashboard)
- Shows next 3-5 rule firings sorted by time
- Next rule highlighted in amber
- Sources labeled "Powerwall" or "Rachio/Sprinklers"
- Active rain delays shown with "Rain Delay" badge, muted/italic styling
- Auto-refreshes every 60 seconds

### Bottom tiles (4) — SESSION 5 ORDER
  Position 1: Energy YTD  (import cost, export credits, net cost)
  Position 2: Current Rate (new — see spec below)
  Position 3: Pool         (temp, pump/edge/cleaner state, salt PPM + chlorinator %)
  Position 4: Security     (stubbed — Abode + garage)

### Chart
- Full-width solar vs home load, today, Chart.js line chart
- Amber = solar, Blue = home load

---

## Power flow diagram — pill callout style
- Nodes: icon only, no text on the node itself
- Each node: short dashed leader line (#2c2c2e, 0.5px, dasharray 3 3)
  with small filled dot at node edge in node's color
- Pill: background #1c1c1e, border 0.5px solid #2c2c2e,
  border-radius 20px, padding 5px 10px, font-size 12px
- Pill content: colored dot + muted label + colored value
- Battery pill: dot . "Battery" . kW . "." . % . status
- Solar kWh today: muted text (#5F5E5A) in quadrant, not a pill
- Grid kWh today: SVG text above grid node
- Positions: Solar -> top-right, Battery -> left, Home -> right, Grid -> bottom

---

## SQLite schema

### readings table (ACTUAL schema — do not change)
CREATE TABLE readings (
    timestamp   INTEGER PRIMARY KEY,  -- unix timestamp (no id column)
    solar_w     REAL,
    home_w      REAL,
    battery_w   REAL,                 -- positive = charging
    grid_w      REAL,
    battery_pct REAL
);
-- Retain 90 days, purge older rows on each write cycle

### rules table
CREATE TABLE IF NOT EXISTS rules (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL,
    enabled       INTEGER NOT NULL DEFAULT 1,
    days          TEXT NOT NULL,   -- JSON array e.g. [0,1,2,3,4]
    months        TEXT NOT NULL,   -- JSON array e.g. [1,2,...,12]
    hour          INTEGER NOT NULL,
    minute        INTEGER NOT NULL,
    mode          TEXT,            -- NULL = no change
    reserve       INTEGER,         -- NULL = no change
    grid_charging INTEGER,         -- NULL=no change, 0=false, 1=true
    grid_export   TEXT             -- NULL = no change
);

### rule_conditions table
CREATE TABLE IF NOT EXISTS rule_conditions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id   INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
    logic     TEXT NOT NULL DEFAULT 'AND',  -- 'AND' | 'OR'
    type      TEXT NOT NULL,                -- 'battery_pct' (extensible)
    operator  TEXT NOT NULL,                -- '>' | '<' | '>=' | '<='
    value     REAL NOT NULL
);

### daily_costs table
CREATE TABLE IF NOT EXISTS daily_costs (
    date               TEXT PRIMARY KEY,  -- 'YYYY-MM-DD'
    import_kwh         REAL DEFAULT 0,
    export_kwh         REAL DEFAULT 0,
    import_cost        REAL DEFAULT 0,
    export_credit      REAL DEFAULT 0,
    on_peak_kwh        REAL DEFAULT 0,    -- net kWh per TOU period
    off_peak_kwh       REAL DEFAULT 0,
    super_off_peak_kwh REAL DEFAULT 0,
    on_peak_cost       REAL DEFAULT 0,    -- net cost per TOU period
    off_peak_cost      REAL DEFAULT 0,
    super_off_peak_cost REAL DEFAULT 0
);

### rate_history table
CREATE TABLE IF NOT EXISTS rate_history (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    effective_date        TEXT NOT NULL,      -- 'YYYY-MM-DD'
    end_date              TEXT,               -- NULL = current
    summer_on_peak        REAL NOT NULL,
    summer_off_peak       REAL NOT NULL,
    summer_super_off_peak REAL NOT NULL,
    winter_on_peak        REAL NOT NULL,
    winter_off_peak       REAL NOT NULL,
    winter_super_off_peak REAL NOT NULL,
    base_services_charge_per_day REAL DEFAULT 0,
    source_url            TEXT,
    fetched_at            TEXT
);
-- rebuild_daily_costs() uses rate_history to apply correct rate per date

### settings table
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
-- Key/value store for all configurable settings (polling intervals, URLs, etc.)

### event_log table
CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    system      TEXT NOT NULL,      -- 'powerwall' | 'rachio' | 'abode' | 'system' | 'holidays' | 'rates' | ...
    event_type  TEXT NOT NULL,
    title       TEXT NOT NULL,
    detail      TEXT,
    result      TEXT,               -- 'ok' | 'failed' | 'skipped' | 'info'
    source      TEXT DEFAULT 'live',
    battery_pct REAL
);

---

## rules.py v2
- Loads rules from SQLite on each eval cycle (no restart needed)
- Evaluates battery_pct conditions against live pw.level()
- AND: fires only if ALL AND conditions pass
- OR:  fires if time matches AND (any OR condition passes OR none exist)
- Seeds DB from v1 RULES list if rules table is empty on first run
- Runs as Windows service (pywin32)

---

## server.py — Flask API endpoints

### Data
  GET  /api/live         -> live power data (solar, home, battery, grid, battery_pct, mode)
  GET  /api/today        -> today's readings for chart
  GET  /api/pool         -> pool/spa temps, pump state, circuit states
  GET  /api/weather      -> Open-Meteo weather data
  GET  /api/debug/pool          -> raw screenlogicpy data (debug)
  GET  /api/debug/rachio        -> raw Rachio scheduleRules (debug)
  GET  /api/debug/rachio/events -> raw Rachio device events, last 7 days (debug)

### Rules
  GET    /api/rules             -> all rules with conditions
  POST   /api/rules             -> create rule
  PUT    /api/rules/<id>        -> update rule
  DELETE /api/rules/<id>        -> delete rule
  PUT    /api/rules/<id>/toggle -> flip enabled flag
  GET    /api/schedule          -> upcoming firings next 48h (for widget)
  GET    /api/rules/insights    -> deterministic rule analysis (rule-based engine)
  POST   /api/rules/ai-insights -> Gemini AI analysis (requires gemini_api_key)
  GET    /api/rules/ai-insights/debug -> full prompt + raw Gemini response + token usage

### Costs & Rates
  GET    /api/costs/ytd         -> YTD totals for dashboard tile
  GET    /api/costs/daily       -> daily costs with per-period TOU breakdown
  POST   /api/costs/rebuild     -> trigger cost rebuild from readings
  GET    /api/rates             -> rates.json contents
  POST   /api/rates/refresh     -> re-fetch from SDG&E

### Security / Abode
  GET    /api/security                  -> alarm status
  GET    /api/debug/abode/devices       -> raw Abode device list
  GET    /api/debug/abode/timeline      -> raw Abode timeline
  GET    /api/debug/abode/status        -> Abode connection status
  POST   /api/debug/abode/backfill      -> backfill Abode events to event_log
  POST   /api/debug/abode/dedup         -> deduplicate Abode events in event_log
  POST   /api/debug/abode/test-event    -> inject synthetic test event

### Events
  GET    /api/events            -> event_log entries (limit, system, days, type params)

### Settings
  GET    /api/settings          -> all settings + connector card definitions
  PUT    /api/settings          -> update settings (partial dict)

### Rule JSON shape
{
  "id": 1,
  "name": "Weekday 6am - Self-Powered reserve 10%",
  "enabled": true,
  "days": [0,1,2,3,4],
  "months": [1,2,3,4,5,6,7,8,9,10,11,12],
  "hour": 6, "minute": 0,
  "mode": "self_consumption",
  "reserve": 10,
  "grid_charging": null,
  "grid_export": null,
  "conditions": [
    {"logic": "AND", "type": "battery_pct", "operator": "<", "value": 50}
  ]
}

---

## SDG&E TOU reference
Plan: EV-TOU-2
Summer: June-October  |  Winter: November-May
On-peak: 4pm-9pm daily (except holidays)
Super off-peak: midnight-6am + weekends/holidays all day
Holidays: New Year's Day, Presidents' Day, Memorial Day, Independence Day,
          Labor Day, Veterans Day, Thanksgiving Day, Christmas Day

### Current rates (effective 1/1/2026)
| Period          | Summer  | Winter  |
|-----------------|---------|---------|
| On-peak         | $0.784  | $0.514  |
| Off-peak        | $0.487  | $0.457  |
| Super off-peak  | $0.258  | $0.251  |

Rates stored in rates.json — always read from file, never hardcode.

---

## fetch_rates.py — auto-update SDG&E rates
Standalone module. Polled periodically from poller thread (configurable interval in months).

Rate discovery (two-step):
1. Scrape configurable rates page (default: https://www.sdge.com/total-electric-rates)
2. Find all <a> links matching configurable schedule name (default: "EV-TOU")
3. Pick the "Current" entry, extract effective date from label
4. Download PDF, parse with pdfplumber — find "SCHEDULE EV-TOU-2" section
5. Store rates + effective date in rate_history table
6. Also write rates.json for backward compat (rate card, current rate tile)

SDG&E changes rates multiple times per year (6 changes in 2024-2025).
Filenames are inconsistent — scraping the listing page is required.

Settings (configurable on Settings page):
  rates_page_url:      https://www.sdge.com/total-electric-rates
  rate_schedule_name:  EV-TOU
  rates_poll_months:   1 (check every N months)
  refresh_start_date:  shared start date for holidays + rates (YYYY-MM-DD)

Also includes: holiday generation (SDG&E holidays), TOU period classification.

```python
import requests, pdfplumber, io, json, re
from datetime import datetime

def fetch_ev_tou2_rates():
    year = datetime.now().year
    url = (f"https://www.sdge.com/sites/default/files/regulatory/"
           f"1-1-{str(year)[2:]}%20Schedule%20EV-TOU%20%26%20EV-TOU-2"
           f"%20Total%20Rates%20Tables.pdf")
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        year -= 1
        url = (f"https://www.sdge.com/sites/default/files/regulatory/"
               f"1-1-{str(year)[2:]}%20Schedule%20EV-TOU%20%26%20EV-TOU-2"
               f"%20Total%20Rates%20Tables.pdf")
        r = requests.get(url, timeout=15)
    r.raise_for_status()
    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        text = pdf.pages[0].extract_text()
    lines = text.split('\n')
    ev_tou2_start = next(i for i, l in enumerate(lines)
                         if 'SCHEDULE EV-TOU-2' in l)
    rates = {}
    season = None
    for line in lines[ev_tou2_start:]:
        if line.strip() in ('Summer', 'Winter'):
            season = line.strip().lower()
            continue
        nums = re.findall(r'\d+\.\d+', line)
        if not nums or not season:
            continue
        last = float(nums[-1])
        if 'On-Peak' in line and 'Super' not in line:
            rates[f'{season}_on_peak'] = last
        elif 'Off-Peak' in line and 'Super' not in line:
            rates[f'{season}_off_peak'] = last
        elif 'Super Off-Peak' in line:
            rates[f'{season}_super_off_peak'] = last
        elif 'Base Services Charge' in line and '$/Day' in line:
            rates['base_services_charge_per_day'] = last
            break
    rates['updated'] = datetime.now().isoformat()
    rates['source_url'] = url
    with open('rates.json', 'w') as f:
        json.dump(rates, f, indent=2)
    return rates

def current_rate(rates):
    now = datetime.now()
    season = 'summer' if now.month in [6,7,8,9,10] else 'winter'
    if 16 <= now.hour < 21:
        return rates[f'{season}_on_peak']
    elif now.hour < 6:
        return rates[f'{season}_super_off_peak']
    else:
        return rates[f'{season}_off_peak']
```

---

## SDG&E Holidays — auto-generated annual list

SDG&E treats holidays like weekends for TOU:
- Super off-peak all day EXCEPT 4pm-9pm which remains on-peak
- holidays.json auto-regenerates each Jan when year changes

Official SDG&E holidays (from sdge.com):
  New Year's Day, Presidents' Day, Memorial Day, Independence Day,
  Labor Day, Veterans Day, Thanksgiving Day, Christmas Day

```python
from datetime import date, timedelta
import json, os

def _nth_weekday(year, month, weekday, n):
    d = date(year, month, 1)
    days_ahead = weekday - d.weekday()
    if days_ahead < 0:
        days_ahead += 7
    d += timedelta(days=days_ahead)
    d += timedelta(weeks=n - 1)
    return d

def _last_monday(year, month):
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    d = next_month - timedelta(days=1)
    while d.weekday() != 0:
        d -= timedelta(days=1)
    return d

def generate_sdge_holidays(year):
    return sorted([
        date(year, 1, 1),               # New Year's Day
        _nth_weekday(year, 2, 0, 3),    # Presidents' Day: 3rd Monday Feb
        _last_monday(year, 5),           # Memorial Day: last Monday May
        date(year, 7, 4),               # Independence Day
        _nth_weekday(year, 9, 0, 1),    # Labor Day: 1st Monday Sep
        date(year, 11, 11),             # Veterans Day
        _nth_weekday(year, 11, 3, 4),   # Thanksgiving: 4th Thursday Nov
        date(year, 12, 25),             # Christmas Day
    ])

def load_or_generate_holidays():
    path = os.path.join(os.path.dirname(__file__), 'holidays.json')
    current_year = date.today().year
    if os.path.exists(path):
        with open(path) as f:
            data = json.load(f)
        if data.get('year') == current_year:
            return {date.fromisoformat(d) for d in data['dates']}
    holidays = generate_sdge_holidays(current_year)
    with open(path, 'w') as f:
        json.dump({'year': current_year,
                   'dates': [d.isoformat() for d in holidays],
                   'generated': date.today().isoformat()}, f, indent=2)
    return set(holidays)

SDGE_HOLIDAYS = load_or_generate_holidays()

def is_sdge_holiday(d: date) -> bool:
    return d in SDGE_HOLIDAYS

def tou_period(dt):
    """Return (season, period) for a local datetime."""
    season = 'summer' if dt.month in [6,7,8,9,10] else 'winter'
    is_weekend = dt.weekday() >= 5
    is_holiday = is_sdge_holiday(dt.date())
    if is_weekend or is_holiday:
        if 16 <= dt.hour < 21:
            return season, 'on_peak'
        return season, 'super_off_peak'
    if 16 <= dt.hour < 21:
        return season, 'on_peak'
    if dt.hour < 6:
        return season, 'super_off_peak'
    return season, 'off_peak'
```

Do NOT use pip install holidays — custom implementation above is more
accurate for SDG&E's specific list and has no external dependencies.

---

## Feature: Historical data backfill (one-time)

### Goal
Populate readings table back to 1/1/2026 from Tesla cloud history API.
Run once manually: `py backfill.py`

### pypowerwall history API
```python
# pw.client.fleet does NOT exist in cloud mode — use this instead:
# sites   = pw.client.getsites()
# battery = sites[0]
# data    = battery.get_calendar_history_data(
#     kind='power', period='month',
#     end_date='2026-02-01T00:00:00.000Z',
#     timezone='America/Los_Angeles'
# )
# Iterate month by month from Jan 2026 to today
```

### backfill.py logic
```python
from datetime import datetime, timezone
import sqlite3, pypowerwall

START = datetime(2026, 1, 1, tzinfo=timezone.utc)

pw = pypowerwall.Powerwall('', cloudmode=True,
                           email='don@nsdsolutions.com',
                           timeout=60, authpath='.')

conn = sqlite3.connect('powerwall.db')

# Fetch history month by month using get_calendar_history_data()
# sites = pw.client.getsites(); battery = sites[0]
# data = battery.get_calendar_history_data(kind='power', period='month',
#     end_date='YYYY-MM-01T00:00:00.000Z', timezone='America/Los_Angeles')
# Iterate from Jan 2026 to current month

inserted = 0
skipped = 0
for row in history:
    ts = int(row['timestamp'])  # unix seconds
    if ts < int(START.timestamp()):
        continue
    try:
        conn.execute("""
            INSERT OR IGNORE INTO readings
            (timestamp, solar_w, home_w, battery_w, grid_w, battery_pct)
            VALUES (?,?,?,?,?,?)
        """, (
            ts,
            row.get('solar_power', 0),
            row.get('load_power', 0),
            -(row.get('battery_power', 0)),  # flip: positive=charging
            row.get('grid_power', 0),
            row.get('percentage', None)
        ))
        inserted += 1
    except Exception as e:
        skipped += 1

conn.commit()
conn.close()
print(f"Backfill complete: {inserted} inserted, {skipped} skipped")
```

Note: Tesla cloud history API may paginate or have a lookback limit.
If get_history() doesn't support date ranges, use get_calendar_history()
with period='day' and iterate month by month from Jan 2026 to today.
Log what actually comes back and adjust accordingly.

---

## Feature: Energy cost YTD tile

### Cost calculation
Run nightly (or on demand) to rebuild daily_costs from readings table:
- Group readings by date
- For each 5-min interval: kWh = watts * (5/60) / 1000
- If grid_w > 0: import, apply import rate for that TOU period
- If grid_w < 0: export, apply export rate (same as import rate for EV-TOU-2)
- Sum per day, write to daily_costs
- YTD totals = SUM from 2026-01-01 to today

### Tile display
```
Energy YTD
─────────────────────
Grid imported    $142.30
Export credits  −$38.20
─────────────────────
Net cost         $128.60
```
Color: net cost in white, credits in green (#1D9E75)

---

## Feature: Rate card on Powerwall Rules page

Compact card, top-right of Rules page, above the rules table.

```
SDG&E EV-TOU-2   updated Jan 1 2026        [now: OFF-PEAK]

           Summer ◀   Winter
On-peak    $0.784      $0.514      4pm – 9pm daily
Off-peak   $0.487      $0.457      6am – 4pm, 9pm – midnight
Super OFP  $0.258      $0.251      midnight – 6am + weekends
```

### Season column treatment (SESSION 5)
- Summer and Winter column headers are color-coded:
    Summer header: amber (#EF9F27), bold
    Winter header: blue (#378ADD), bold
- The ACTIVE season column header gets an underline/border indicator:
    Summer active: amber underline + slightly brighter text
    Winter active: blue underline + slightly brighter text
- The active season column's rate cells are also color-coded
  (amber for summer, blue for winter) to make the active season
  immediately obvious at a glance

### Current period row treatment (existing, keep)
- Active period row: subtle left border highlight
    On-peak:       amber left border
    Off-peak:      green left border
    Super off-peak: blue left border
- Period label text brightens to var(--text) on active row

### Active rate cell treatment (SESSION 5 — NEW)
- The specific cell at intersection of [active season column] x [active period row]
  gets a distinct highlight:
    background: rgba(239,159,39,0.10) for on-peak / amber
    background: rgba(29,158,117,0.10) for off-peak / green
    background: rgba(55,138,221,0.10) for super off-peak / blue
    font-weight: 700, color matches period accent color
  This is the actual $/kWh you are paying RIGHT NOW — make it unmissable.

### "Now" badge (existing, keep)
- Top-right of card: "NOW: OFF-PEAK" badge in amber
- Badge color matches current period accent

### Rate refresh
- Monthly auto-refresh: check rates.json `updated` on server startup
- If >30 days old: run fetch_ev_tou2_rates() automatically
- Small "Refresh rates" link at bottom-right triggers POST /api/rates/refresh

---

## Feature: Current Rate tile (Dashboard tile 2) — SESSION 5 NEW

A condensed rate tile on the Dashboard at position 2 (between Energy YTD and Pool).
Uses the same data from GET /api/rates. Refreshes on the same 10-minute interval
as the rate card on the Rules page.

### Layout
```
CURRENT RATE
$0.251 /kWh
SUPER OFF-PEAK

  On-peak     $0.784 / $0.514
  Off-peak    $0.487 / $0.457
▶ Super OFP   $0.258 / $0.251
```

### Spec
- Tile title: "CURRENT RATE" (standard dim uppercase label)
- Large rate: current $/kWh value in amber, font-size ~1.9rem, font-weight 300
  with "/kWh" suffix in small dim text
- Period name: current period in amber uppercase, small text, below the rate
- Mini rate table: 3 rows (on-peak, off-peak, super off-peak)
  - Each row: period name (dim) | summer rate / winter rate (very-dim)
  - Active period row: period name brightens to var(--dim), rates shown in
    accent color (amber=on-peak, green=off-peak, blue=super off-peak),
    small ▶ indicator or left border highlight
- The large rate displayed is the CURRENT season's rate for the current period
  (i.e. if it's winter off-peak, show $0.457, not $0.487)
- Season indicator: small badge or label showing "SUMMER" or "WINTER"
  in the tile, color-coded (amber/blue), so it's clear which season's
  rates apply right now

### JavaScript
- refreshRates() already populates rate data — extend it to also populate
  the dashboard rate tile using the same computed period/season variables
- Element IDs for the new tile:
    #tile-rate-amount    — the large $/kWh number
    #tile-rate-period    — period name label (e.g. "SUPER OFF-PEAK")
    #tile-rate-season    — season badge (e.g. "WINTER")
  Mini table rows use class rt-active on the current period row

### Period accent colors (consistent across both rate card and rate tile)
  on_peak:       --amber  (#EF9F27)
  off_peak:      --green  (#1D9E75)
  super_off_peak: --blue  (#378ADD)

---

## Design tokens (dark mode)
background:            #111
card:                  #1c1c1e
border:                0.5px solid #2c2c2e
solar/amber:           #EF9F27
battery/green:         #1D9E75
home/blue:             #378ADD
grid/gray:             #888780
text primary:          #e8e6e0
text secondary:        #888780
text muted:            #5F5E5A
accent/next rule:      #EF9F27
mode self_consumption: #1D9E75
mode autonomous:       #EF9F27
mode backup:           #e05252
border radius:         12px cards, 10px tiles
font:                  system-ui / sans-serif

---

## Feature: Unified Event Log — SESSION 6 NEW

### Goal
A single `event_log` table in powerwall.db that captures everything
worth reviewing across all integrated systems — Powerwall automation
actions, Rachio schedule events, Abode security events, and any future
system. Designed so events can be filtered by system, type, or date and
displayed in a unified history view.

This replaces the narrower `action_log` concept. The schema is generic
enough to absorb imported historical data (e.g. Abode's 30-day CSV
export) as well as live events going forward.

---

### SQLite schema — event_log table (add to powerwall.db)

```sql
CREATE TABLE IF NOT EXISTS event_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,       -- unix timestamp of the event
    system      TEXT NOT NULL,          -- 'powerwall' | 'rachio' | 'abode' | 'myq' | ...
    event_type  TEXT NOT NULL,          -- category within system (see below)
    title       TEXT NOT NULL,          -- short human-readable summary
    detail      TEXT,                   -- longer context, JSON ok for structured data
    result      TEXT,                   -- 'ok' | 'failed' | 'skipped' | 'info' | NULL
    source      TEXT DEFAULT 'live',    -- 'live' | 'import' | 'backfill'
    battery_pct REAL                    -- snapshot at event time, NULL if not applicable
);
CREATE INDEX IF NOT EXISTS idx_event_log_ts     ON event_log(ts);
CREATE INDEX IF NOT EXISTS idx_event_log_system ON event_log(system);
```

**system values** (extensible — add new ones as systems are integrated):
- `powerwall`  — Powerwall mode/reserve/grid changes via rules engine
- `rachio`     — irrigation schedule windows, rain delays
- `abode`      — arm/disarm, door/window events, motion, alarms
- `myq`        — garage door open/close (future)
- `kasa`       — smart plug events (future)

**event_type values by system:**
```
powerwall:  automation_fired | mode_changed | reserve_changed |
            grid_charging_changed | grid_export_changed | no_change | error

rachio:     schedule_started | rain_delay_applied | rain_delay_cleared

abode:      arm_away | arm_home | disarm | door_open | door_closed |
            lock_locked | lock_unlocked | motion | alarm | fault | unknown

myq:        door_opened | door_closed

kasa:       plug_on | plug_off
```

**result values:**
- `ok`      — action completed successfully
- `failed`  — action attempted but API call failed
- `skipped` — rule fired but no change needed (state already matched)
- `info`    — informational event, no action taken (e.g. Abode arm status)
- NULL      — not applicable (e.g. imported historical events)

**source values:**
- `live`     — captured in real time by server.py or rules.py
- `import`   — loaded from an external export (e.g. Abode CSV)
- `backfill` — retroactively populated (future use)

---

### Powerwall events — rules.py

One row per individual setting change. If a rule changes both mode and
reserve, that's two rows with the same ts and title.

```python
def log_event(conn, system, event_type, title, detail=None,
              result=None, source='live', battery_pct=None):
    conn.execute(
        'INSERT INTO event_log '
        '(ts, system, event_type, title, detail, result, source, battery_pct) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (int(time.time()), system, event_type, title,
         detail, result, source, battery_pct)
    )
    conn.commit()
```

Example calls in apply_settings():
```python
# Successful mode change
log_event(conn, 'powerwall', 'mode_changed',
          f"Mode → Self-Powered",
          detail=f"rule: {rule_name}, mode: self_consumption",
          result='ok', battery_pct=battery_pct)

# Successful reserve change
log_event(conn, 'powerwall', 'reserve_changed',
          f"Reserve → 10%",
          detail=f"rule: {rule_name}",
          result='ok', battery_pct=battery_pct)

# No change needed
log_event(conn, 'powerwall', 'no_change',
          f"Rule fired — no change",
          detail=f"rule: {rule_name}, already at target state",
          result='skipped', battery_pct=battery_pct)

# API failure
log_event(conn, 'powerwall', 'error',
          f"set_mode failed",
          detail=f"rule: {rule_name}, set_mode returned None",
          result='failed', battery_pct=battery_pct)
```

---

### Rachio events — server.py

```python
# Schedule window opened
log_event(conn, 'rachio', 'schedule_started',
          f"{schedule_name}",
          detail=f"duration: {duration_min} min",
          result='info')

# Rain delay applied
log_event(conn, 'rachio', 'rain_delay_applied',
          f"Rain delay: {days} days",
          detail=f"{inches:.1f}in forecast, zones paused",
          result='ok')
```

---

### Abode events — server.py (live, via websocket listener)

abodepy pushes `com.goabode.gateway.timeline` websocket events in real
time. Each event has: event_utc, event_type, event_name, device_name,
device_type, severity, is_alarm.

Map incoming Abode timeline events to event_log rows:

```python
ABODE_TYPE_MAP = {
    'Closed':       'door_closed',
    'Open':         'door_open',
    'LockClosed':   'lock_locked',
    'LockOpen':     'lock_unlocked',
    'Motion':       'motion',
    'Alarm':        'alarm',
    'Disarmed':     'disarm',
    'Armed Away':   'arm_away',
    'Armed Home':   'arm_home',
    'Home':         'arm_home',
    'Away':         'arm_away',
    'Standby':      'disarm',
}

def on_abode_timeline_event(event_json):
    event_type = ABODE_TYPE_MAP.get(event_json.get('event_type'), 'unknown')
    title = event_json.get('event_name', event_json.get('device_name', '?'))
    detail = (f"device: {event_json.get('device_name')}  "
              f"type: {event_json.get('device_type')}  "
              f"severity: {event_json.get('severity')}")
    ts = int(event_json.get('event_utc', time.time()))
    conn.execute(
        'INSERT INTO event_log (ts, system, event_type, title, detail, result, source) '
        'VALUES (?,?,?,?,?,?,?)',
        (ts, 'abode', event_type, title, detail, 'info', 'live')
    )
    conn.commit()
```

Start the abodepy websocket listener as a daemon thread in server.py
on startup, alongside the existing Powerwall poller thread.

---

### Abode historical import — abode_import.py (new script)

One-time script to load Abode's 30-day CSV export into event_log.
Run manually: `py abode_import.py abode_activity.csv`

Abode's website activity log can be exported as CSV. The script needs
to be written defensively since Abode's export format is undocumented
— run it against the actual CSV first and log what columns come back.

```python
import csv, sqlite3, time, sys
from datetime import datetime

DB_PATH = 'powerwall.db'

ABODE_TYPE_MAP = {
    'Closed':     'door_closed',
    'Open':       'door_open',
    'LockClosed': 'lock_locked',
    'LockOpen':   'lock_unlocked',
    'Motion':     'motion',
    'Alarm':      'alarm',
    # add more as seen in actual export
}

def import_abode_csv(path):
    inserted = skipped = 0
    with open(path, newline='', encoding='utf-8-sig') as f:
        # Print first row to confirm column names before committing anything
        reader = csv.DictReader(f)
        print("Columns found:", reader.fieldnames)

        with sqlite3.connect(DB_PATH) as conn:
            for row in reader:
                try:
                    # Adjust field names to match actual Abode CSV export
                    # Common field names: 'Date', 'Time', 'Event', 'Device', 'User'
                    # Run once with print(row) to confirm before writing
                    date_str  = row.get('Date') or row.get('date', '')
                    time_str  = row.get('Time') or row.get('time', '')
                    event_str = row.get('Event') or row.get('event', '')
                    device    = row.get('Device') or row.get('device', '')

                    dt  = datetime.strptime(f"{date_str} {time_str}",
                                            "%m/%d/%Y %I:%M %p")
                    ts  = int(dt.timestamp())
                    evt = ABODE_TYPE_MAP.get(event_str, 'unknown')
                    title  = f"{device}: {event_str}" if device else event_str
                    detail = f"imported from Abode activity export"

                    # INSERT OR IGNORE prevents duplicates on re-run
                    conn.execute(
                        'INSERT OR IGNORE INTO event_log '
                        '(ts, system, event_type, title, detail, result, source) '
                        'VALUES (?,?,?,?,?,?,?)',
                        (ts, 'abode', evt, title, detail, None, 'import')
                    )
                    inserted += 1
                except Exception as e:
                    print(f"Skip row: {e} — {row}")
                    skipped += 1
            conn.commit()

    print(f"Import complete: {inserted} inserted, {skipped} skipped")

if __name__ == '__main__':
    import_abode_csv(sys.argv[1])
```

**Important:** run with `print(row)` on first few rows before writing
anything — Abode's CSV column names are not publicly documented and
may differ from what's shown above. Adjust field names to match actual
export before committing.

---

### New API endpoint

```
GET /api/events?limit=50&system=all&days=7
```

Returns recent event_log entries, newest first.

```json
[
  {
    "id": 201,
    "ts": 1743120000,
    "ts_display": "Mar 28  6:00 AM",
    "system": "powerwall",
    "event_type": "mode_changed",
    "title": "Mode → Self-Powered",
    "detail": "rule: Weekday 6am – Self-Powered reserve 10%",
    "result": "ok",
    "source": "live",
    "battery_pct": 87.3
  },
  {
    "id": 198,
    "ts": 1743033600,
    "ts_display": "Mar 27  6:15 AM",
    "system": "abode",
    "event_type": "disarm",
    "title": "System Disarmed",
    "detail": "device: Keypad  type: Keypad  severity: 6",
    "result": "info",
    "source": "live",
    "battery_pct": null
  }
]
```

Query params:
- `limit`  — default 50, max 500
- `system` — 'all' | 'powerwall' | 'rachio' | 'abode' | ... (default 'all')
- `days`   — how many days back (default 7, max 90)
- `type`   — optional event_type filter

---

### Retention
Keep 90 days. Purge on server startup and nightly alongside readings
purge. For imported historical data (source='import'), retain
indefinitely — these can never be recaptured.

```python
def purge_event_log(conn):
    cutoff = int(time.time()) - 90 * 86400
    conn.execute(
        "DELETE FROM event_log WHERE ts < ? AND source != 'import'",
        (cutoff,)
    )
    conn.commit()
```

---

### Future: Event Log page / panel on dashboard
A dedicated third page tab or expandable panel showing a filterable
event timeline across all systems:

```
Mar 28  6:00 AM  ⚡ Powerwall  Mode → Self-Powered          ok    87%
Mar 28  6:00 AM  ⚡ Powerwall  Reserve → 10%                ok    87%
Mar 28  5:00 AM  ⚡ Powerwall  Mode → Time-Based Control    ok    62%
Mar 27  7:15 PM  ⚡ Powerwall  Reserve → 1%, export solar+batt  ok  34%
Mar 27  6:12 AM  🔒 Abode      Front Door: Open             info  —
Mar 27  6:11 AM  🔒 Abode      System Disarmed              info  —
Mar 27  5:55 AM  🌿 Rachio     Front Lawn                   info  —
```

Filter buttons: All · Powerwall · Rachio · Abode
Color coding per system: amber=powerwall, rachio=teal, abode=purple

---

## Feature: Event Log page (dashboard.html) — SESSION 6 NEW

### Navigation
Add a third nav tab: "Event Log" (data-page="events"), between "Powerwall Rules" and the theme toggle.

`showPage('events')` should call `refreshEvents()` on load.

---

### Page layout — #page-events

Full-height page, same padding/gap pattern as #page-rules (16px, gap 12px, overflow hidden).

```
┌─────────────────────────────────────────────────────────────────┐
│  EVENT LOG                    [filter bar]          updated HH:MM│
├─────────────────────────────────────────────────────────────────┤
│  scrollable event rows (flex: 1, overflow-y: auto)              │
└─────────────────────────────────────────────────────────────────┘
```

### Toolbar (flex row, space-between, flex-shrink: 0)
- Left: page title "EVENT LOG" (standard .page-title style)
- Center: filter pill buttons — All · Powerwall · Rachio · Abode
  - Pill style: small rounded buttons, inactive = badge-bg/dim,
    active = system accent color with tinted background
  - Clicking a filter re-renders the visible rows client-side
    (no new fetch — filter the already-loaded _events array)
- Right: "updated HH:MM" timestamp + auto-refresh indicator

### System accent colors
```css
--abode: #9B6DFF;   /* purple */
--pool:  #00BCD4;   /* cyan/teal */
/* existing: --amber, --green (used for powerwall), --rachio, --blue */
```

Filter pill active states:
- All:       var(--badge-bg) background, var(--text) color
- Powerwall: rgba(239,159,39,0.15) background, var(--amber) color
- Rachio:    rgba(41,181,212,0.15) background, var(--rachio) color
- Abode:     rgba(155,109,255,0.15) background, var(--abode) color
- Pool:      rgba(0,188,212,0.15) background, var(--pool) color

### Event rows

Each row is a single flex line, full-width, with a subtle left border
colored by system. Rows are separated by a 0.5px border-bottom
(var(--border)), no card wrapper — the list itself sits in a card.

```
│ ▏ Mar 28  6:00 AM   ⚡ Powerwall   Mode → Self-Powered              ✓  87%  │
│ ▏ Mar 28  6:00 AM   ⚡ Powerwall   Reserve → 10%                    ✓  87%  │
│ ▏ Mar 27  7:15 PM   ⚡ Powerwall   Reserve → 1%, export solar+batt  ✓  34%  │
│ ▏ Mar 27  6:12 AM   🔒 Abode       Front Door: Open                 —   —   │
│ ▏ Mar 27  6:11 AM   🔒 Abode       System Disarmed                  —   —   │
│ ▏ Mar 27  5:55 AM   🌿 Rachio      Front Lawn                       —   —   │
```

Row layout (grid or flex with fixed column widths):
- Left accent bar: 3px, system color, border-radius on left
- Timestamp: ~110px, var(--very-dim), font-size 0.82rem, monospace-ish
- System icon + label: ~130px — icon emoji + system name in system color
- Title: flex: 1, var(--text), font-size 0.88rem, truncate with ellipsis
- Result badge: ~50px right-aligned — ✓ green, ✗ red, — dim
- Battery %: ~40px right-aligned, var(--very-dim), font-size 0.8rem
  (show "—" if null)

Row hover: subtle background highlight (var(--bg) → slightly lighter)

### System icon + label mapping
```javascript
const SYSTEM_META = {
  powerwall: { icon: '⚡', label: 'Powerwall', color: 'var(--amber)' },
  rachio:    { icon: '🌿', label: 'Rachio',    color: 'var(--rachio)' },
  abode:     { icon: '🔒', label: 'Abode',     color: 'var(--abode)' },
  myq:       { icon: '🚗', label: 'MyQ',       color: 'var(--gray)' },
};
```

### Result badge rendering
```javascript
function resultBadge(result) {
  if (result === 'ok')     return '<span style="color:var(--green)">✓</span>';
  if (result === 'failed') return '<span style="color:#e05252">✗</span>';
  if (result === 'skipped') return '<span style="color:var(--very-dim)">–</span>';
  return '<span style="color:var(--very-dim)">—</span>';
}
```

### Date group dividers
When the date changes between consecutive rows, insert a sticky date
divider (not a full row — just a thin labeled separator):

```
──────────── Mar 27 ─────────────
```

Style: 0.5px lines either side of the date label, var(--very-dim) text,
font-size 0.72rem, uppercase, letter-spacing 0.1em.
position: sticky; top: 0; background: var(--card-bg); z-index: 1.

### JavaScript — refreshEvents()

```javascript
let _events = [];
let _eventsFilter = 'all';

async function refreshEvents() {
  try {
    const data = await fetch('/api/events?limit=200&days=7').then(r => r.json());
    _events = data;
    renderEvents();
    const upd = document.getElementById('events-updated');
    if (upd) upd.textContent = new Date().toLocaleTimeString(
      [], {hour:'numeric', minute:'2-digit'});
  } catch(e) { console.warn('Events:', e); }
}

function setEventsFilter(system) {
  _eventsFilter = system;
  document.querySelectorAll('.evf-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.system === system);
  });
  renderEvents();
}

function renderEvents() {
  const list = document.getElementById('events-list');
  if (!list) return;

  const filtered = _eventsFilter === 'all'
    ? _events
    : _events.filter(e => e.system === _eventsFilter);

  if (!filtered.length) {
    list.innerHTML = '<div class="events-empty">No events found.</div>';
    return;
  }

  let lastDate = null;
  let html = '';
  for (const e of filtered) {
    const d = new Date(e.ts * 1000);
    const dateKey = d.toLocaleDateString('en-US',
      {month:'short', day:'numeric'});
    if (dateKey !== lastDate) {
      html += `<div class="event-date-divider"><span>${dateKey}</span></div>`;
      lastDate = dateKey;
    }
    const meta = SYSTEM_META[e.system] || {icon:'?', label:e.system, color:'var(--dim)'};
    const timeStr = d.toLocaleTimeString([],
      {hour:'numeric', minute:'2-digit'});
    const batt = e.battery_pct != null
      ? Math.round(e.battery_pct) + '%' : '—';
    html += `
      <div class="event-row" data-system="${e.system}">
        <div class="event-accent" style="background:${meta.color}"></div>
        <div class="event-ts">${timeStr}</div>
        <div class="event-system" style="color:${meta.color}">
          ${meta.icon} ${meta.label}
        </div>
        <div class="event-title">${e.title}</div>
        <div class="event-result">${resultBadge(e.result)}</div>
        <div class="event-batt">${batt}</div>
      </div>`;
  }
  list.innerHTML = html;
}
```

Add to init and intervals:
- Call `refreshEvents()` only when the events page is active
- `setInterval` every 60 seconds, but skip render if page not active
  (check `_activePage === 'events'`)

### CSS additions needed

```css
/* ── Page 3: Event Log ───────────────────────────────────────────*/
#page-events {
  display: none;
  flex-direction: column;
  padding: 16px;
  gap: 12px;
  overflow: hidden;
}
#page-events.active { display: flex; }

.events-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-shrink: 0;
  gap: 12px;
}
.events-toolbar-center { display: flex; gap: 6px; }
.evf-btn {
  background: var(--badge-bg);
  border: none;
  color: var(--dim);
  border-radius: 20px;
  padding: 4px 14px;
  font-size: 0.82rem;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.evf-btn:hover { color: var(--text); }
.evf-btn.active[data-system="all"]       { background: var(--badge-bg); color: var(--text); }
.evf-btn.active[data-system="powerwall"] { background: rgba(239,159,39,0.15); color: var(--amber); }
.evf-btn.active[data-system="rachio"]    { background: rgba(41,181,212,0.15); color: var(--rachio); }
.evf-btn.active[data-system="abode"]     { background: rgba(155,109,255,0.15); color: var(--abode); }
.events-updated { font-size: 0.72rem; color: var(--very-dim); }

.events-card {
  flex: 1;
  overflow-y: auto;
  background: var(--card-bg);
  border: 0.5px solid var(--border);
  border-radius: 12px;
}
.event-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 14px 8px 0;
  border-bottom: 0.5px solid var(--border);
  cursor: default;
  transition: background 0.1s;
}
.event-row:last-child { border-bottom: none; }
.event-row:hover { background: var(--bg); }
.event-accent {
  width: 3px;
  min-width: 3px;
  align-self: stretch;
  border-radius: 0 2px 2px 0;
}
.event-ts     { width: 90px; flex-shrink: 0; font-size: 0.80rem; color: var(--very-dim); }
.event-system { width: 110px; flex-shrink: 0; font-size: 0.80rem; font-weight: 500; }
.event-title  { flex: 1; font-size: 0.87rem; white-space: nowrap;
                overflow: hidden; text-overflow: ellipsis; }
.event-result { width: 30px; flex-shrink: 0; text-align: center; font-size: 0.88rem; }
.event-batt   { width: 38px; flex-shrink: 0; text-align: right;
                font-size: 0.78rem; color: var(--very-dim); }

.event-date-divider {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 14px;
  position: sticky;
  top: 0;
  background: var(--card-bg);
  z-index: 1;
}
.event-date-divider::before,
.event-date-divider::after {
  content: '';
  flex: 1;
  height: 0.5px;
  background: var(--border);
}
.event-date-divider span {
  font-size: 0.72rem;
  color: var(--very-dim);
  text-transform: uppercase;
  letter-spacing: 0.1em;
  white-space: nowrap;
}
.events-empty {
  padding: 40px;
  text-align: center;
  color: var(--very-dim);
  font-size: 0.88rem;
}
```

### HTML to add

Nav button (after the "Powerwall Rules" button):
```html
<button class="nav-link" data-page="events" onclick="showPage('events')">Event Log</button>
```

Page div (after #page-rules, before the modal):
```html
<!-- ── Page 3: Event Log ──────────────────────────────────────────────────────-->
<div id="page-events" class="page">
  <div class="events-toolbar">
    <div class="page-title">Event Log</div>
    <div class="events-toolbar-center">
      <button class="evf-btn active" data-system="all"       onclick="setEventsFilter('all')">All</button>
      <button class="evf-btn"        data-system="powerwall" onclick="setEventsFilter('powerwall')">⚡ Powerwall</button>
      <button class="evf-btn"        data-system="rachio"    onclick="setEventsFilter('rachio')">🌿 Rachio</button>
      <button class="evf-btn"        data-system="abode"     onclick="setEventsFilter('abode')">🔒 Abode</button>
    </div>
    <div id="events-updated" class="events-updated"></div>
  </div>
  <div class="events-card" id="events-list">
    <div class="events-empty">Loading…</div>
  </div>
</div>
```