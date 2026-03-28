# Powerwall Dashboard — Project Context
# Session 6 handoff — March 28 2026

## Status
- Dashboard v2 running — all 4 bottom tiles live
- rules.py v2 complete — reads from SQLite, runs as Windows service
- fetch_rates.py live — SDG&E EV-TOU-2 rates + holiday generation
- backfill.py added — one-time historical import from Tesla cloud API (run once)
- Bottom tiles: Energy YTD | Current Rate | Pool | Security (stubbed)
- Rate card live on Powerwall Rules page (season color-coding + active cell highlight)
- Dark/light theme toggle in nav bar (persists via localStorage)
- Active integrations: pypowerwall (cloud), screenlogicpy (Pentair), Rachio
- Run: `py server.py` (port 5000)

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
  Edge pump state:  data["pump"]["0"]["state"]["value"]                 (1=on)
  Edge pump watts:  data["pump"]["0"]["watts_now"]["value"]             (140)
  Pool circuit:     data["circuit"]["505"]["value"]                     (0/1)
  Spa circuit:      data["circuit"]["500"]["value"]                     (0/1)
  Air temp:         data["controller"]["sensor"]["air_temperature"]["value"]
  Salt ppm:         data["controller"]["sensor"]["salt_ppm"]["value"]   (3050)

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
  PUT /device/{id}/rain_delay  -> body: {"duration": seconds}

Schedule data lives inside person response:
  person.devices[].scheduleRules[]
  Fields: startHour, startMinute, totalDuration,
          scheduleJobTypes (DAY_OF_WEEK_N pattern, 0=Sun)
  Note: no separate /device/{id}/scheduleRule endpoint

Rain delay automation (replaces manual Netzero skip):
  Fetch Open-Meteo forecast -> calculate days from rainfall amount
  -> PUT rain_delay with duration in seconds (days x 86400)
  Thresholds: >=2" -> 7 days, >=1" -> 5 days, >=0.5" -> 3 days, >=0.25" -> 1 day

---

## Hardware / location
- Tesla Powerwall 2
- SDG&E EV-TOU-2 plan, San Diego CA (lat 32.7157, lon -117.1611)
- Wall-mounted display — always-on dark mode

---

## Project layout
D:\projects\homeAutomation\
  dashboard.html      <- single-page app (2 pages: Dashboard + Powerwall Rules)
  server.py           <- Flask backend — serves data API + static files
  rules.py            <- rules engine (reads from SQLite, runs as service)
  fetch_rates.py      <- SDG&E holidays, TOU period, rate fetch + parse
  backfill.py         <- one-time historical import from Tesla cloud API
  powerwall.db        <- SQLite — readings + rules
  rates.json          <- current SDG&E rates (written by fetch_rates.py)
  holidays.json       <- SDG&E holiday dates, auto-regenerated each Jan
  .pypowerwall.auth   <- pypowerwall auth cache
  .pypowerwall.site   <- pypowerwall site cache
  rules.log           <- rules engine log
  CONTEXT.md          <- this file

---

## Navigation — 2 pages
Dashboard:       live power flow, chart, pool tile, upcoming automations widget
Powerwall Rules: rules manager (CRUD table + add/edit form)

Schedule page removed — upcoming automations is now a widget on the Dashboard.

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
- Auto-refreshes every 60 seconds

### Nav bar
- Left: page links (Dashboard | Powerwall Rules)
- Right: Light/Dark theme toggle button (persists via localStorage)
- Nav clock removed — time only shown in the large Dashboard clock

### Bottom tiles (4) — current order
  Position 1: Energy YTD   (import cost, export credits, net cost)
  Position 2: Current Rate  (big $/kWh, period name, season badge, mini rate table)
  Position 3: Pool          (temp, pump state, heat mode)
  Position 4: Security      (stubbed — Abode + garage)
- height: 160px (increased from 130px to fit Current Rate tile content)

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
    date          TEXT PRIMARY KEY,  -- 'YYYY-MM-DD'
    import_kwh    REAL DEFAULT 0,
    export_kwh    REAL DEFAULT 0,
    import_cost   REAL DEFAULT 0,    -- dollars paid to SDG&E
    export_credit REAL DEFAULT 0     -- dollars credited by SDG&E
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

### Existing
  GET  /api/power       -> {solar, home, battery, grid, battery_pct,
                             solar_today_kwh, grid_today_kwh,
                             battery_status, mode, time_to_empty_min}
  GET  /api/history     -> last 24h readings for chart
  GET  /api/pool        -> pool/spa temps, pump state, circuit states
  GET  /api/debug/pool  -> raw screenlogicpy data (debug)

### Rules endpoints
  GET    /api/rules             -> all rules with conditions
  POST   /api/rules             -> create rule
  PUT    /api/rules/<id>        -> update rule
  DELETE /api/rules/<id>        -> delete rule
  PUT    /api/rules/<id>/toggle -> flip enabled flag
  GET    /api/schedule          -> upcoming firings next 48h (for widget)
  GET    /api/costs/ytd         -> {import_cost, export_credit, net_cost,
                                    import_kwh, export_kwh, as_of}
  GET    /api/rates             -> rates.json contents
  POST   /api/rates/refresh     -> re-fetch from SDG&E PDF

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
Standalone module. Run on startup + schedule annually (Jan 2).

URL pattern:
  https://www.sdge.com/sites/default/files/regulatory/
  1-1-{YY}%20Schedule%20EV-TOU%20%26%20EV-TOU-2%20Total%20Rates%20Tables.pdf

Try current year, fall back to previous year on 404.
Parse with pdfplumber — find "SCHEDULE EV-TOU-2" section.
Tested and verified against actual 2026 PDF.

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

### Season column treatment (LIVE)
- Summer header: amber (#EF9F27), bold
- Winter header: blue (#378ADD), bold
- Active season header: underline in season color
- Active season column cells: colored amber (summer) or blue (winter)

### Current period row treatment (LIVE)
- Active row left border color is period-specific:
    On-peak:        amber (#EF9F27)
    Off-peak:       green (#1D9E75)
    Super off-peak: blue  (#378ADD)
- Period label text brightens to var(--text) on active row

### Active rate cell (LIVE)
- Intersection of [active season column] × [active period row]:
    background tint + font-weight 700 + accent color
    on_peak: rgba(239,159,39,0.12) amber
    off_peak: rgba(29,158,117,0.12) green
    super_off_peak: rgba(55,138,221,0.12) blue
- This is the $/kWh you're paying RIGHT NOW

### "Now" badge (LIVE)
- Top-right of card: "NOW: SUPER OFF-PEAK" etc.
- Badge color matches current period accent (not always amber)

### Rate refresh
- Monthly auto-refresh: check rates.json `updated` on server startup
- If >30 days old: run fetch_ev_tou2_rates() automatically
- Small "Refresh rates" link at bottom-right triggers POST /api/rates/refresh

---

## Current Rate tile (Dashboard tile 2) — LIVE

Dashboard tile 2. Populated by refreshRates() every 10 minutes.

### Layout
```
CURRENT RATE
$0.251 /kWh          [WINTER]
SUPER OFF-PEAK

  On-peak      $0.784 / $0.514
  Off-peak     $0.487 / $0.457
▶ Super OFP    $0.258 / $0.251
```

### Spec
- Large rate: current season's $/kWh in period accent color, 1.9rem weight 300
- Period name below in accent color, uppercase
- Season badge top-right: "SUMMER" (amber) or "WINTER" (blue)
- Mini table: summer/winter rates per row, ▶ on active row
- Large rate color + period color both track the period accent:
    on_peak → amber, off_peak → green, super_off_peak → blue

### Element IDs
  #tile-rate-amount    — large $/kWh
  #tile-rate-period    — period label
  #tile-rate-season    — season badge (class "summer" or "winter")
  #rt-row-on-peak, #rt-row-off-peak, #rt-row-super — mini table rows (rt-active class)
  #rt-on-rates, #rt-off-rates, #rt-sup-rates — mini table rate cells

### Period accent colors (consistent across rate card + rate tile)
  on_peak:        --amber  (#EF9F27)
  off_peak:       --green  (#1D9E75)
  super_off_peak: --blue   (#378ADD)

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