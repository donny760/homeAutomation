"""
Powerwall Dashboard — Backend
Polls pypowerwall every 10s, writes to SQLite every 30s, serves JSON via Flask.
Run: py server.py
"""

import os
import sys
import json
import time
import sqlite3
import threading
import urllib.request
from datetime import datetime, date, timedelta, timezone

import asyncio

from flask import Flask, jsonify, send_file, request
import pypowerwall
from rules import seed_default_rules as _seed_rules
from fetch_rates import (
    load_rates, rates_are_stale, fetch_ev_tou2_rates,
    tou_period, load_or_generate_holidays,
)

# ── Config ────────────────────────────────────────────────────────────────────
PW_EMAIL          = 'don@nsdsolutions.com'
PW_CAPACITY_KWH   = 13.5          # Powerwall 2 usable capacity
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
DB_PATH           = os.path.join(BASE_DIR, 'powerwall.db')
POLL_INTERVAL     = 10            # seconds between pypowerwall polls
DB_WRITE_EVERY    = 30            # seconds between DB writes
PURGE_DAYS        = 90            # keep 90 days of readings
POOL_POLL_INTERVAL  = 30           # seconds between pool polls
RACHIO_API_KEY      = 'dc3c7132-00c1-45dc-910c-0d8f06738b92'
RACHIO_BASE         = 'https://api.rach.io/1/public'
RACHIO_TTL          = 300          # 5-minute cache for Rachio schedule

app = Flask(__name__)

# Shared live-data cache
_live: dict = {}
_lock = threading.Lock()

# Pool cache
_pool: dict    = {}
_pool_ts: float = 0.0

# Rachio cache
_rachio_schedule: list = []
_rachio_ts: float      = 0.0


# ── SDG&E TOU rates (standard residential, approximate 2025) ─────────────────
def sdge_rate(dt: datetime) -> float:
    """Return $/kWh for the given datetime."""
    h = dt.hour
    is_summer = 6 <= dt.month <= 10  # June–October
    if 16 <= h < 21:                  # on-peak 4–9 PM
        return 0.60 if is_summer else 0.52
    if 0 <= h < 6:                    # super off-peak midnight–6 AM
        return 0.22
    return 0.38                       # off-peak everything else


# ── Database ──────────────────────────────────────────────────────────────────
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.execute('''
            CREATE TABLE IF NOT EXISTS readings (
                timestamp   INTEGER PRIMARY KEY,
                solar_w     REAL,
                home_w      REAL,
                battery_w   REAL,
                grid_w      REAL,
                battery_pct REAL
            )
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_ts ON readings(timestamp)')
        c.executescript('''
            CREATE TABLE IF NOT EXISTS rules (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                name          TEXT NOT NULL,
                enabled       INTEGER NOT NULL DEFAULT 1,
                days          TEXT NOT NULL,
                months        TEXT NOT NULL,
                hour          INTEGER NOT NULL,
                minute        INTEGER NOT NULL,
                mode          TEXT,
                reserve       INTEGER,
                grid_charging INTEGER,
                grid_export   TEXT
            );
            CREATE TABLE IF NOT EXISTS rule_conditions (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                rule_id   INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
                logic     TEXT NOT NULL DEFAULT 'AND',
                type      TEXT NOT NULL,
                operator  TEXT NOT NULL,
                value     REAL NOT NULL
            );
        ''')
        c.executescript('''
            CREATE TABLE IF NOT EXISTS daily_costs (
                date          TEXT PRIMARY KEY,
                import_kwh    REAL DEFAULT 0,
                export_kwh    REAL DEFAULT 0,
                import_cost   REAL DEFAULT 0,
                export_credit REAL DEFAULT 0
            );
        ''')
        _seed_rules(c)   # idempotent — only inserts if rules table is empty


def write_reading(solar_w, home_w, battery_w, grid_w, battery_pct) -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            'INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?)',
            (int(time.time()), solar_w, home_w, battery_w, grid_w, battery_pct)
        )


def purge_old() -> None:
    cutoff = int(time.time()) - PURGE_DAYS * 86400
    with sqlite3.connect(DB_PATH) as c:
        c.execute('DELETE FROM readings WHERE timestamp < ?', (cutoff,))


def rebuild_daily_costs(year: int = None) -> None:
    """Rebuild daily_costs from readings for a given year (default: current year)."""
    rates = load_rates()
    if not rates:
        print('rebuild_daily_costs: no rates.json, skipping')
        return

    target_year = year or date.today().year
    jan1 = int(datetime(target_year, 1, 1).timestamp())
    dec31_end = int(datetime(target_year + 1, 1, 1).timestamp())

    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            'SELECT timestamp, grid_w FROM readings '
            'WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp',
            (jan1, dec31_end)
        ).fetchall()

        # Aggregate into per-date buckets using trapezoidal intervals
        day_data: dict = {}
        for i in range(1, len(rows)):
            ts0, g0 = rows[i - 1]
            ts1, g1 = rows[i]
            dt_h = (ts1 - ts0) / 3600
            if dt_h > 1:       # gap > 1 h — skip (missing data)
                continue
            dt   = datetime.fromtimestamp(ts1)
            d    = dt.date().isoformat()
            avg_grid = ((g0 or 0) + (g1 or 0)) / 2
            kwh  = avg_grid * dt_h / 1000
            season, period = tou_period(dt)
            rate = rates.get(f'{season}_{period}', 0.0)
            if d not in day_data:
                day_data[d] = {'import_kwh': 0.0, 'export_kwh': 0.0,
                               'import_cost': 0.0, 'export_credit': 0.0}
            if kwh > 0:
                day_data[d]['import_kwh']  += kwh
                day_data[d]['import_cost'] += kwh * rate
            elif kwh < 0:
                day_data[d]['export_kwh']    += abs(kwh)
                day_data[d]['export_credit'] += abs(kwh) * rate

        for d, v in day_data.items():
            c.execute(
                'INSERT OR REPLACE INTO daily_costs '
                '(date, import_kwh, export_kwh, import_cost, export_credit) '
                'VALUES (?,?,?,?,?)',
                (d, round(v['import_kwh'], 4), round(v['export_kwh'], 4),
                 round(v['import_cost'], 4), round(v['export_credit'], 4))
            )

    print(f'rebuild_daily_costs: {len(day_data)} days written for {target_year}')


def _fetch_rows(since_ts: int) -> list:
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            'SELECT timestamp, solar_w, home_w, battery_w, grid_w '
            'FROM readings WHERE timestamp >= ? ORDER BY timestamp',
            (since_ts,)
        ).fetchall()


def today_rows() -> list:
    start = int(datetime.combine(date.today(), datetime.min.time()).timestamp())
    return _fetch_rows(start)


def month_rows() -> list:
    t = date.today()
    start = int(datetime(t.year, t.month, 1).timestamp())
    return _fetch_rows(start)


def calc_stats(rows: list) -> tuple:
    """Return (solar_kwh, savings_$, self_sufficiency_%, grid_import_kwh) from a list of readings."""
    solar_kwh = home_kwh = grid_import_kwh = savings = 0.0
    for i in range(1, len(rows)):
        dt_h = (rows[i][0] - rows[i-1][0]) / 3600
        solar_w = max(0.0, rows[i][1] or 0)
        home_w  = max(0.0, rows[i][2] or 0)
        grid_w  = rows[i][4] or 0
        rate    = sdge_rate(datetime.fromtimestamp(rows[i][0]))

        solar_kwh      += solar_w * dt_h / 1000
        home_kwh       += home_w  * dt_h / 1000
        gi              = max(0.0, grid_w) * dt_h / 1000
        grid_import_kwh += gi
        savings         += max(0.0, home_w * dt_h / 1000 - gi) * rate

    self_suff = 0.0
    if home_kwh > 0:
        self_suff = min(100.0, max(0.0, (home_kwh - grid_import_kwh) / home_kwh * 100))

    return solar_kwh, savings, self_suff, grid_import_kwh


# ── History backfill ──────────────────────────────────────────────────────────
def backfill_history() -> None:
    """On startup, fill gaps in the last 12 hours using Tesla cloud history.

    The API returns ~15-min interval data.  We use INSERT OR IGNORE so existing
    30-second readings are never overwritten.

    Sign convention from Tesla history API:
      solar_power   – positive = producing
      battery_power – positive = charging, negative = discharging
      grid_power    – positive = exporting, negative = importing
    home_w is derived: home = solar - battery - grid  (energy conservation)
    """
    print('Backfill: fetching last 24 h of history from Tesla cloud…')
    try:
        pw = pypowerwall.Powerwall('', cloudmode=True, email=PW_EMAIL, timeout=30, authpath=BASE_DIR)
        sites = pw.client.getsites()
        if not sites:
            print('Backfill: no sites returned.')
            return
        battery = sites[0]

        now_utc   = datetime.now(timezone.utc)
        start_utc = now_utc - timedelta(hours=24)
        end_str   = now_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z')

        data = battery.get_calendar_history_data(
            kind='power',
            period='day',
            end_date=end_str,
            timezone='America/Los_Angeles',
        )

        series = (data or {}).get('time_series', [])
        if not series:
            print('Backfill: no time_series in response.')
            return

        cutoff = int(start_utc.timestamp())
        inserted = 0
        with sqlite3.connect(DB_PATH) as c:
            for row in series:
                raw_ts = row.get('timestamp', '')
                try:
                    dt = datetime.fromisoformat(raw_ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    ts = int(dt.timestamp())
                except ValueError:
                    continue

                if ts < cutoff:
                    continue

                solar_w   = float(row.get('solar_power',   0) or 0)
                batt_w    = float(row.get('battery_power', 0) or 0)
                grid_w    = float(row.get('grid_power',    0) or 0)
                # Tesla history: battery+ = discharging, battery- = charging, grid+ = importing
                home_w      = solar_w + batt_w + grid_w
                batt_stored = -batt_w  # flip to positive=charging, matching live poller

                cur = c.execute(
                    'INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?)',
                    (ts, solar_w, home_w, batt_stored, grid_w, None)
                )
                inserted += cur.rowcount

        print(f'Backfill: inserted {inserted} rows ({len(series)} returned by API).')

    except Exception as exc:
        print(f'Backfill error: {exc}')


# ── Poller thread ─────────────────────────────────────────────────────────────
def poller() -> None:
    pw = None
    last_write = 0
    last_purge = 0
    last_cost_rebuild = 0

    while True:
        try:
            if pw is None:
                print('Connecting to Powerwall (cloud mode)…')
                pw = pypowerwall.Powerwall(
                    '', cloudmode=True, email=PW_EMAIL, timeout=30
                )
                print('Connected.')

            power = pw.power() or {}
            level = pw.level() or 0

            solar_w     = float(power.get('solar',   0) or 0)
            battery_w   = -float(power.get('battery', 0) or 0)  # API: positive=discharging; flip to positive=charging
            grid_w      = float(power.get('site',    0) or 0)  # 'site' = grid
            home_w      = float(power.get('load',    0) or 0)
            battery_pct = float(level)

            # Get operating mode via get_mode() only
            # (pw.mode is the connection type, not the operating mode)
            mode = 'self_consumption'
            try:
                val = pw.get_mode()
                if val:
                    mode = val
            except Exception:
                pass

            now = int(time.time())

            with _lock:
                _live.update({
                    'solar_w': solar_w, 'home_w': home_w,
                    'battery_w': battery_w, 'grid_w': grid_w,
                    'battery_pct': battery_pct, 'mode': mode or 'self_consumption',
                    'ts': now,
                })

            if now - last_write >= DB_WRITE_EVERY:
                write_reading(solar_w, home_w, battery_w, grid_w, battery_pct)
                last_write = now

            if now - last_purge >= 86400:
                purge_old()
                last_purge = now

            if now - last_cost_rebuild >= 86400:
                threading.Thread(target=rebuild_daily_costs, daemon=True).start()
                last_cost_rebuild = now

        except Exception as exc:
            print(f'Poller error: {exc}')
            pw = None  # force reconnect on next iteration

        time.sleep(POLL_INTERVAL)


# ── Weather (Open-Meteo, free, no key) ───────────────────────────────────────
_wx_cache: dict = {}
_wx_ts: float   = 0.0
WX_TTL = 600  # 10 minutes

WMO = {
    0: 'Clear', 1: 'Mainly Clear', 2: 'Partly Cloudy', 3: 'Overcast',
    45: 'Foggy', 48: 'Icy Fog',
    51: 'Light Drizzle', 53: 'Drizzle', 55: 'Heavy Drizzle',
    61: 'Light Rain', 63: 'Rain', 65: 'Heavy Rain',
    71: 'Light Snow', 73: 'Snow', 75: 'Heavy Snow',
    80: 'Rain Showers', 81: 'Showers', 82: 'Heavy Showers',
    95: 'Thunderstorm', 96: 'Thunderstorm', 99: 'Thunderstorm',
}


def fetch_weather() -> dict:
    global _wx_cache, _wx_ts
    if time.time() - _wx_ts < WX_TTL:
        return _wx_cache

    url = (
        'https://api.open-meteo.com/v1/forecast'
        '?latitude=32.7157&longitude=-117.1611'
        '&current_weather=true'
        '&daily=precipitation_sum,cloudcover_mean'
        '&forecast_days=2&timezone=America%2FLos_Angeles'
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        cw    = data.get('current_weather', {})
        daily = data.get('daily', {})
        clouds_tm = (daily.get('cloudcover_mean') or [None, None])[1]
        rain_tm   = (daily.get('precipitation_sum') or [None, None])[1]
        _wx_cache = {
            'temp_f':          round(cw.get('temperature', 0) * 9 / 5 + 32, 1),
            'desc':            WMO.get(cw.get('weathercode', 0), ''),
            'tomorrow_cloud':  clouds_tm,
            'tomorrow_rain':   rain_tm,
            'bad_forecast':    (clouds_tm or 0) > 60 or (rain_tm or 0) > 1,
        }
        _wx_ts = time.time()
    except Exception as exc:
        print(f'Weather error: {exc}')
        if not _wx_cache:
            _wx_cache = {}

    return _wx_cache


# ── Pool (screenlogicpy) ─────────────────────────────────────────────────────
async def _pool_fetch_async() -> dict:
    from screenlogicpy import ScreenLogicGateway
    from screenlogicpy.discovery import async_discover

    gateways = await async_discover()
    if not gateways:
        raise RuntimeError('No ScreenLogic gateway found via UDP discovery')
    gw      = gateways[0]
    gateway = ScreenLogicGateway()
    await gateway.async_connect(ip=gw['ip'], port=gw.get('port', 80))
    try:
        await gateway.async_update()
        data = gateway.get_data()

        def _nested(d, *keys):
            """Safely walk nested dicts, return None if any key missing."""
            for k in keys:
                if not isinstance(d, dict):
                    return None
                d = d.get(k)
            return d

        def _key(d, *candidates):
            """Return first matching value for a list of key candidates (int or str)."""
            for k in candidates:
                if k in d:
                    return d[k]
            return {}

        body    = data.get('body') or data.get(b'body') or {}
        pump    = data.get('pump') or data.get(b'pump') or {}
        circuit = data.get('circuit') or data.get(b'circuit') or {}

        # screenlogicpy may use int or str keys depending on version
        pool_b = _key(body,    0, '0') or {}
        spa_b  = _key(body,    1, '1') or {}
        pump1  = _key(pump,    1, '1') or {}
        pump0  = _key(pump,    0, '0') or {}
        c505   = _key(circuit, 505, '505') or {}
        c500   = _key(circuit, 500, '500') or {}

        temp_f  = _nested(pool_b, 'last_temperature', 'value')
        spa_f   = _nested(spa_b,  'last_temperature', 'value')

        # Heat mode: resolve enum label from index
        hm_idx  = _nested(pool_b, 'heat_mode', 'value')
        hm_opts = _nested(pool_b, 'heat_mode', 'enum_options') or []
        heat_mode = hm_opts[hm_idx] if (hm_idx is not None and isinstance(hm_opts, list) and hm_idx < len(hm_opts)) else None

        # Pump 1 = pool pump, pump 0 = edge/booster pump
        pool_pump_on    = bool(_nested(pump1, 'state', 'value'))
        pool_pump_watts = _nested(pump1, 'watts_now', 'value')
        edge_pump_on    = bool(_nested(pump0, 'state', 'value'))

        # Circuits
        pool_circuit_on = bool(_nested(c505, 'value'))
        spa_circuit_on  = bool(_nested(c500, 'value'))

        return {
            'temp_f':          round(float(temp_f), 1) if temp_f is not None else None,
            'spa_temp_f':      round(float(spa_f), 1)  if spa_f  is not None else None,
            'heat_mode':       heat_mode,
            'pump_on':         pool_pump_on,
            'pump_watts':      int(pool_pump_watts) if pool_pump_watts is not None else None,
            'edge_pump_on':    edge_pump_on,
            'pool_circuit_on': pool_circuit_on,
            'spa_circuit_on':  spa_circuit_on,
        }
    finally:
        await gateway.async_disconnect()


def fetch_pool() -> dict:
    global _pool, _pool_ts
    if time.time() - _pool_ts < POOL_POLL_INTERVAL:
        return _pool
    try:
        _pool    = asyncio.run(_pool_fetch_async())
        _pool_ts = time.time()
    except Exception as exc:
        print(f'Pool error: {exc}')
        if not _pool:
            _pool = {'temp_f': None, 'pump_on': None, 'spa_temp_f': None}
    return _pool


# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_file('dashboard.html')


@app.route('/api/live')
def api_live():
    with _lock:
        d = dict(_live)

    solar_w     = d.get('solar_w', 0)
    home_w      = d.get('home_w', 0)
    battery_w   = d.get('battery_w', 0)
    grid_w      = d.get('grid_w', 0)
    battery_pct = d.get('battery_pct', 0)
    mode        = d.get('mode', 'self_consumption')

    # Battery state
    if battery_w > 50:
        batt_status = 'Charging'
        kwh_to_go   = PW_CAPACITY_KWH * (100 - battery_pct) / 100
        hours_rem   = kwh_to_go / (battery_w / 1000) if battery_w > 0 else None
        time_label  = 'to full'
    elif battery_w < -50:
        batt_status = 'Discharging'
        kwh_left    = PW_CAPACITY_KWH * battery_pct / 100
        hours_rem   = kwh_left / (abs(battery_w) / 1000) if battery_w != 0 else None
        time_label  = 'to empty'
    else:
        batt_status = 'Standby'
        hours_rem   = None
        time_label  = None

    t_rows                          = today_rows()
    solar_kwh, s_today, self_suff, grid_kwh = calc_stats(t_rows)
    _, s_month, _, _                = calc_stats(month_rows())

    return jsonify({
        'solar_w':         round(solar_w),
        'home_w':          round(home_w),
        'battery_w':       round(battery_w),
        'grid_w':          round(grid_w),
        'battery_pct':     round(battery_pct, 1),
        'battery_status':  batt_status,
        'battery_rate_w':  round(abs(battery_w)),
        'hours_remaining': round(hours_rem, 2) if hours_rem else None,
        'time_label':      time_label,
        'solar_kwh_today': round(solar_kwh, 2),
        'grid_kwh_today':  round(grid_kwh, 2),
        'savings_today':   round(s_today, 2),
        'savings_month':   round(s_month, 2),
        'self_sufficiency': round(self_suff, 1),
        'mode':            mode,
        'ts':              d.get('ts', 0),
    })


@app.route('/api/today')
def api_today():
    return jsonify([
        {'ts': r[0], 'solar_w': r[1], 'home_w': r[2]}
        for r in today_rows()
    ])


@app.route('/api/weather')
def api_weather():
    return jsonify(fetch_weather())


@app.route('/api/pool')
def api_pool():
    return jsonify(fetch_pool())


async def _pool_debug_async() -> dict:
    from screenlogicpy import ScreenLogicGateway
    from screenlogicpy.discovery import async_discover
    gateways = await async_discover()
    if not gateways:
        return {'error': 'No ScreenLogic gateway found via UDP discovery'}
    gw = gateways[0]
    gateway = ScreenLogicGateway()
    await gateway.async_connect(ip=gw['ip'], port=gw.get('port', 80))
    try:
        await gateway.async_update()
        return gateway.get_data()
    finally:
        await gateway.async_disconnect()


@app.route('/api/debug/pool')
def api_debug_pool():
    """Dump raw screenlogicpy data — use this to identify correct key paths."""
    try:
        return jsonify(asyncio.run(_pool_debug_async()))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ── Rachio ───────────────────────────────────────────────────────────────────
@app.route('/api/debug/rachio')
def api_debug_rachio():
    """Return embedded scheduleRules from person response — shows actual field names."""
    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')
        result    = {}
        for device in person.get('devices', []):
            rules = device.get('scheduleRules', [])
            result[device['id']] = rules[:3]  # first 3 rules
        return jsonify(result)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


def _rachio_get(path: str) -> dict:
    req = urllib.request.Request(
        RACHIO_BASE + path,
        headers={'Authorization': f'Bearer {RACHIO_API_KEY}', 'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _rachio_next_run(start_h: int, start_m: int, rachio_days: set):
    """Return next local datetime within 8 days matching hour/minute and day set.
    rachio_days: integers extracted from DAY_OF_WEEK_N (0=Sun,1=Mon,…,6=Sat)."""
    from datetime import time as dt_time
    run_t = dt_time(hour=int(start_h), minute=int(start_m))
    now   = datetime.now()
    for delta in range(8):
        cdate      = (now + timedelta(days=delta)).date()
        rachio_dow = (cdate.weekday() + 1) % 7   # Mon(0)→1, Sun(6)→0
        if rachio_dow in rachio_days:
            cdt = datetime.combine(cdate, run_t)
            if cdt > now:
                return cdt
    return None


def _rachio_days_from_job_types(job_types: list) -> set:
    """Parse Rachio scheduleJobTypes into a set of day ints (0=Sun…6=Sat).
    INTERVAL_N entries mean 'every day'."""
    import re
    days = set()
    for jt in job_types:
        m = re.match(r'DAY_OF_WEEK_(\d+)', jt)
        if m:
            days.add(int(m.group(1)))
    if not days and any('INTERVAL' in jt for jt in job_types):
        days = set(range(7))
    return days


def fetch_rachio_schedule() -> list:
    global _rachio_schedule, _rachio_ts
    if time.time() - _rachio_ts < RACHIO_TTL:
        return _rachio_schedule
    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')
        now_utc   = datetime.now(timezone.utc)
        cutoff    = now_utc + timedelta(hours=48)
        events    = []
        for device in person.get('devices', []):
            rules = device.get('scheduleRules', [])
            print(f'Rachio device {device.get("name","?")}: {len(rules)} schedule rules')

            for rule in rules:
                if not rule.get('enabled', True):
                    continue
                try:
                    run_dt = None

                    # Try nextRunDate first (ms int or ISO string)
                    next_run = rule.get('nextRunDate')
                    if next_run:
                        if isinstance(next_run, (int, float)):
                            run_dt = datetime.fromtimestamp(next_run / 1000, tz=timezone.utc).astimezone().replace(tzinfo=None)
                        else:
                            run_dt = datetime.fromisoformat(str(next_run).replace('Z', '+00:00')).astimezone().replace(tzinfo=None)

                    # Compute from startHour/startMinute + scheduleJobTypes
                    if run_dt is None:
                        h    = rule.get('startHour', 0)
                        m    = rule.get('startMinute', 0)
                        days = _rachio_days_from_job_types(rule.get('scheduleJobTypes', []))
                        run_dt = _rachio_next_run(h, m, days)

                    if run_dt is None:
                        continue

                    now_local    = datetime.now()
                    cutoff_local = now_local + timedelta(hours=48)
                    if not (now_local < run_dt <= cutoff_local):
                        continue

                    duration_min = round(rule.get('totalDuration', rule.get('duration', 0)) / 60)
                    events.append({
                        'fire_time':    run_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                        'name':         rule.get('name', rule.get('externalName', 'Irrigation')),
                        'duration_min': duration_min,
                        'source':       'rachio',
                    })
                except Exception as exc:
                    print(f'Rachio rule skip: {exc}')
                    continue

        events.sort(key=lambda e: e['fire_time'])
        _rachio_schedule = events
        _rachio_ts = time.time()
        print(f'Rachio: fetched {len(events)} upcoming events')
    except Exception as exc:
        print(f'Rachio error: {exc}')
    return _rachio_schedule


# ── Rules helpers ────────────────────────────────────────────────────────────
def _rule_row_to_dict(row, conditions):
    rid, name, enabled, days_j, months_j, hour, minute, mode, reserve, gc, ge = row
    return {
        'id':           rid,
        'name':         name,
        'enabled':      bool(enabled),
        'days':         json.loads(days_j),
        'months':       json.loads(months_j),
        'hour':         hour,
        'minute':       minute,
        'mode':         mode,
        'reserve':      reserve,
        'grid_charging': None if gc is None else bool(gc),
        'grid_export':  ge,
        'conditions':   conditions,
    }


def _load_all_rules(c):
    rows = c.execute(
        'SELECT id,name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export FROM rules ORDER BY id'
    ).fetchall()
    cond_rows = c.execute('SELECT rule_id,logic,type,operator,value FROM rule_conditions').fetchall()
    cond_map = {}
    for rule_id, logic, ctype, op, val in cond_rows:
        cond_map.setdefault(rule_id, []).append(
            {'logic': logic, 'type': ctype, 'operator': op, 'value': val}
        )
    return [_rule_row_to_dict(r, cond_map.get(r[0], [])) for r in rows]


def _rule_fires_at(rule, d):
    if d.weekday() not in set(rule['days']):
        return None
    if d.month not in set(rule['months']):
        return None
    return datetime(d.year, d.month, d.day, rule['hour'], rule['minute'])


def _upcoming_firings(rules, hours=48):
    now = datetime.now()
    cutoff = now + timedelta(hours=hours)
    events = []
    for delta_days in (0, 1, 2):
        d = now.date() + timedelta(days=delta_days)
        for rule in rules:
            if not rule['enabled']:
                continue
            fire_dt = _rule_fires_at(rule, d)
            if fire_dt and now < fire_dt <= cutoff:
                events.append({
                    'fire_time':     fire_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                    'source':        'powerwall',
                    'rule_id':       rule['id'],
                    'name':          rule['name'],
                    'mode':          rule['mode'],
                    'reserve':       rule['reserve'],
                    'grid_charging': rule['grid_charging'],
                    'grid_export':   rule['grid_export'],
                    'conditions':    rule['conditions'],
                })
    events.sort(key=lambda e: e['fire_time'])
    return events


# ── Rules API endpoints ───────────────────────────────────────────────────────
@app.route('/api/schedule')
def api_schedule():
    with _lock:
        live = dict(_live)
    with sqlite3.connect(DB_PATH) as c:
        rules = _load_all_rules(c)
    pw_events     = _upcoming_firings(rules)
    rachio_events = fetch_rachio_schedule()
    all_events    = sorted(pw_events + rachio_events, key=lambda e: e['fire_time'])
    current = {
        'mode':        live.get('mode', 'self_consumption'),
        'battery_pct': live.get('battery_pct', 0),
    }
    return jsonify({'current': current, 'schedule': all_events})


@app.route('/api/rules', methods=['GET'])
def api_rules_get():
    with sqlite3.connect(DB_PATH) as c:
        return jsonify(_load_all_rules(c))


@app.route('/api/rules', methods=['POST'])
def api_rules_post():
    body = request.get_json(force=True)
    days_j   = json.dumps(body['days'])
    months_j = json.dumps(body['months'])
    gc = body.get('grid_charging')
    gc_val = None if gc is None else (1 if gc else 0)
    with sqlite3.connect(DB_PATH) as c:
        c.execute('PRAGMA foreign_keys = ON')
        cur = c.execute(
            'INSERT INTO rules (name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export) '
            'VALUES (?,?,?,?,?,?,?,?,?,?)',
            (body['name'], 1 if body.get('enabled', True) else 0,
             days_j, months_j, body['hour'], body['minute'],
             body.get('mode'), body.get('reserve'), gc_val, body.get('grid_export'))
        )
        rid = cur.lastrowid
        for cond in body.get('conditions', []):
            c.execute(
                'INSERT INTO rule_conditions (rule_id,logic,type,operator,value) VALUES (?,?,?,?,?)',
                (rid, cond['logic'], cond['type'], cond['operator'], cond['value'])
            )
        row = c.execute(
            'SELECT id,name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export FROM rules WHERE id=?', (rid,)
        ).fetchone()
        conds = c.execute('SELECT rule_id,logic,type,operator,value FROM rule_conditions WHERE rule_id=?', (rid,)).fetchall()
    cond_list = [{'logic': r[1], 'type': r[2], 'operator': r[3], 'value': r[4]} for r in conds]
    return jsonify(_rule_row_to_dict(row, cond_list)), 201


@app.route('/api/rules/<int:rid>', methods=['PUT'])
def api_rules_put(rid):
    body = request.get_json(force=True)
    days_j   = json.dumps(body['days'])
    months_j = json.dumps(body['months'])
    gc = body.get('grid_charging')
    gc_val = None if gc is None else (1 if gc else 0)
    with sqlite3.connect(DB_PATH) as c:
        c.execute('PRAGMA foreign_keys = ON')
        c.execute(
            'UPDATE rules SET name=?,enabled=?,days=?,months=?,hour=?,minute=?,mode=?,reserve=?,grid_charging=?,grid_export=? WHERE id=?',
            (body['name'], 1 if body.get('enabled', True) else 0,
             days_j, months_j, body['hour'], body['minute'],
             body.get('mode'), body.get('reserve'), gc_val, body.get('grid_export'), rid)
        )
        c.execute('DELETE FROM rule_conditions WHERE rule_id=?', (rid,))
        for cond in body.get('conditions', []):
            c.execute(
                'INSERT INTO rule_conditions (rule_id,logic,type,operator,value) VALUES (?,?,?,?,?)',
                (rid, cond['logic'], cond['type'], cond['operator'], cond['value'])
            )
        row = c.execute(
            'SELECT id,name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export FROM rules WHERE id=?', (rid,)
        ).fetchone()
        conds = c.execute('SELECT rule_id,logic,type,operator,value FROM rule_conditions WHERE rule_id=?', (rid,)).fetchall()
    if not row:
        return jsonify({'error': 'not found'}), 404
    cond_list = [{'logic': r[1], 'type': r[2], 'operator': r[3], 'value': r[4]} for r in conds]
    return jsonify(_rule_row_to_dict(row, cond_list))


@app.route('/api/rules/<int:rid>', methods=['DELETE'])
def api_rules_delete(rid):
    with sqlite3.connect(DB_PATH) as c:
        c.execute('PRAGMA foreign_keys = ON')
        c.execute('DELETE FROM rules WHERE id=?', (rid,))
    return '', 204


@app.route('/api/rules/<int:rid>/toggle', methods=['PUT'])
def api_rules_toggle(rid):
    with sqlite3.connect(DB_PATH) as c:
        c.execute('UPDATE rules SET enabled = 1 - enabled WHERE id=?', (rid,))
        row = c.execute(
            'SELECT id,name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export FROM rules WHERE id=?', (rid,)
        ).fetchone()
    if not row:
        return jsonify({'error': 'not found'}), 404
    return jsonify({'id': rid, 'enabled': bool(row[2])})


# ── Costs + Rates endpoints ──────────────────────────────────────────────────
@app.route('/api/costs/ytd')
def api_costs_ytd():
    year = date.today().year
    jan1 = f'{year}-01-01'
    today = date.today().isoformat()
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            'SELECT SUM(import_kwh), SUM(export_kwh), '
            '       SUM(import_cost), SUM(export_credit) '
            'FROM daily_costs WHERE date >= ? AND date <= ?',
            (jan1, today)
        ).fetchone()
    import_kwh    = round(row[0] or 0, 2)
    export_kwh    = round(row[1] or 0, 2)
    import_cost   = round(row[2] or 0, 2)
    export_credit = round(row[3] or 0, 2)
    return jsonify({
        'import_kwh':    import_kwh,
        'export_kwh':    export_kwh,
        'import_cost':   import_cost,
        'export_credit': export_credit,
        'net_cost':      round(import_cost - export_credit, 2),
        'as_of':         today,
    })


@app.route('/api/rates')
def api_rates():
    return jsonify(load_rates())


@app.route('/api/rates/refresh', methods=['POST'])
def api_rates_refresh():
    try:
        rates = fetch_ev_tou2_rates()
        return jsonify({'ok': True, 'updated': rates.get('updated')})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


# ── Windows Service (optional) ────────────────────────────────────────────────
try:
    import win32event, win32service, win32serviceutil, servicemanager

    class PowerwallDashboardService(win32serviceutil.ServiceFramework):
        _svc_name_         = 'PowerwallDashboard'
        _svc_display_name_ = 'Powerwall Dashboard'
        _svc_description_  = 'Powerwall monitoring dashboard (Flask + pypowerwall)'

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self._stop)

        def SvcDoRun(self):
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                                  servicemanager.PYS_SERVICE_STARTED,
                                  (self._svc_name_, ''))
            _start()

    HAS_WIN32 = True

except ImportError:
    HAS_WIN32 = False


def _start():
    os.chdir(BASE_DIR)
    init_db()
    load_or_generate_holidays()   # regenerates holidays.json if year rolled over
    _rates = load_rates()
    if rates_are_stale(_rates):
        print('rates.json is stale or missing — fetching from SDG&E...')
        try:
            fetch_ev_tou2_rates()
        except Exception as exc:
            print(f'Rate fetch failed: {exc}')
    backfill_history()
    threading.Thread(target=rebuild_daily_costs, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    print('Dashboard → http://localhost:5000')
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    if len(sys.argv) > 1:
        if HAS_WIN32:
            win32serviceutil.HandleCommandLine(PowerwallDashboardService)
        else:
            print('pywin32 not installed.  Run: pip install pywin32')
            sys.exit(1)
    else:
        _start()
