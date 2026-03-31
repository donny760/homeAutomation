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
import requests as _requests
from datetime import datetime, date, timedelta, timezone

import asyncio
import socket

from flask import Flask, jsonify, send_file, send_from_directory, request, redirect
import pypowerwall
from rules import seed_default_rules as _seed_rules
from fetch_rates import (
    load_rates, rates_are_stale, fetch_ev_tou2_rates,
    tou_period, load_or_generate_holidays, SDGE_HOLIDAYS,
)

# ── Config ────────────────────────────────────────────────────────────────────
PW_EMAIL          = 'don@nsdsolutions.com'
PW_CAPACITY_KWH   = 40.5          # 3× Powerwall 2 usable capacity (3 × 13.5 kWh)
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
DB_PATH           = os.path.join(BASE_DIR, 'powerwall.db')
POLL_INTERVAL     = 10            # seconds between pypowerwall polls
DB_WRITE_EVERY    = 30            # seconds between DB writes
PURGE_DAYS        = 0             # disabled — keep all readings forever
POOL_POLL_INTERVAL  = 30           # seconds between pool polls
RACHIO_API_KEY      = 'dc3c7132-00c1-45dc-910c-0d8f06738b92'
RACHIO_BASE         = 'https://api.rach.io/1/public'
RACHIO_TTL          = 300          # 5-minute cache for Rachio schedule
ABODE_EMAIL         = 'don@nsdsolutions.com'
ABODE_PASSWORD      = 'RKf3^KH^'

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # no browser caching of static files

# Shared live-data cache
_live: dict = {}
_lock = threading.Lock()

# Pool cache
_pool: dict    = {}
_pool_ts: float = 0.0
_pool_prev: dict = {}       # previous state for change detection
_pool_pending: dict = {}    # pending state changes (debounce — must persist 2 consecutive polls)

# Security cache
_security: dict    = {}
_security_ts: float = 0.0

# Rachio cache
_rachio_schedule: list = []
_rachio_ts: float      = 0.0



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
                date               TEXT PRIMARY KEY,
                import_kwh         REAL DEFAULT 0,
                export_kwh         REAL DEFAULT 0,
                import_cost        REAL DEFAULT 0,
                export_credit      REAL DEFAULT 0,
                on_peak_kwh        REAL DEFAULT 0,
                off_peak_kwh       REAL DEFAULT 0,
                super_off_peak_kwh REAL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS event_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          INTEGER NOT NULL,
                system      TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                title       TEXT NOT NULL,
                detail      TEXT,
                result      TEXT,
                source      TEXT DEFAULT 'live',
                battery_pct REAL
            );
            CREATE INDEX IF NOT EXISTS idx_event_log_ts     ON event_log(ts);
            CREATE INDEX IF NOT EXISTS idx_event_log_system ON event_log(system);
        ''')
        # Migration: remove overly aggressive unique index (caused backfill failures)
        try:
            c.execute('DROP INDEX IF EXISTS idx_event_log_unique')
        except Exception:
            pass
        # Migration: add per-period kWh + cost columns if missing
        for col in ('on_peak_kwh', 'off_peak_kwh', 'super_off_peak_kwh',
                     'on_peak_cost', 'off_peak_cost', 'super_off_peak_cost'):
            try:
                c.execute(f'ALTER TABLE daily_costs ADD COLUMN {col} REAL DEFAULT 0')
            except Exception:
                pass
        # Migration: add base_services_charge_per_day to rate_history
        try:
            c.execute('ALTER TABLE rate_history ADD COLUMN base_services_charge_per_day REAL DEFAULT 0')
        except Exception:
            pass
        # Settings table
        c.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        # Rate history table
        c.executescript('''
            CREATE TABLE IF NOT EXISTS rate_history (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                effective_date        TEXT NOT NULL,
                end_date              TEXT,
                summer_on_peak        REAL NOT NULL,
                summer_off_peak       REAL NOT NULL,
                summer_super_off_peak REAL NOT NULL,
                winter_on_peak        REAL NOT NULL,
                winter_off_peak       REAL NOT NULL,
                winter_super_off_peak REAL NOT NULL,
                source_url            TEXT,
                fetched_at            TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rate_history_eff
                ON rate_history(effective_date);
        ''')
        _seed_rules(c)   # idempotent — only inserts if rules table is empty
        _seed_settings(c)  # idempotent — only inserts missing keys
        _seed_rate_history(c)  # seed from rates.json if rate_history is empty


# ── Settings helpers ──────────────────────────────────────────────────────────
_SETTINGS_DEFAULTS = {
    # Backend connectors
    'powerwall_enabled':           '1',
    'powerwall_poll_interval':     str(POLL_INTERVAL),
    'powerwall_db_write_interval': str(DB_WRITE_EVERY),
    'pool_enabled':                '1',
    'pool_poll_interval':          str(POOL_POLL_INTERVAL),
    'rachio_enabled':              '1',
    'rachio_poll_interval':        str(RACHIO_TTL),
    'rachio_event_poll_interval':  '1800',    # 30 min — poll for completed watering events
    'rain_skip_enabled':           '0',       # off by default — smart rain skip
    'rain_lookback_days':          '7',       # days of precipitation history to check
    'rain_mm_per_skip_day':        '8',       # mm of accumulated rain per skip day
    'rain_skip_max_days':          '7',       # max skip days to apply
    'rain_skip_check_interval':    '3600',    # 1 hour — how often to evaluate
    'abode_enabled':               '1',
    # Backend maintenance
    'cost_rebuild_days':           '1',       # rebuild daily costs every N days
    'holidays_poll_months':        '1',       # check every N months
    'rates_poll_months':           '1',       # check every N months
    'refresh_start_date':          '',        # YYYY-MM-DD, shared start for holidays + rates
    # SDG&E rate source (configurable)
    'rates_page_url':              'https://www.sdge.com/total-electric-rates',
    'rate_schedule_name':          'EV-TOU',
    # TOU period definitions (JSON) — per official SDG&E EV-TOU-2 tariff Sheet 3
    'tou_periods':                 json.dumps({
        'weekday': {
            'on_peak':        [[16, 21]],
            'super_off_peak': [[0, 6]],
            'super_off_peak_winter_mar_apr': [[10, 14]],
        },
        'weekend_holiday': {
            'on_peak':        [[16, 21]],
            'super_off_peak': [[0, 14]],
        },
    }),
    # TOU schedule verification
    'tou_periods_last_verified':   '',        # YYYY-MM-DD — when TOU time windows were last confirmed
    # Frontend refresh intervals (milliseconds)
    'fe_poll_interval':            '10000',   # live power poll
    'fe_chart_interval':           '60000',   # chart refresh
    'fe_weather_interval':         '600000',  # weather refresh
    'fe_automations_interval':     '60000',   # upcoming automations
    'fe_pool_interval':            '60000',   # pool tile
    'fe_costs_interval':           '300000',  # YTD costs tile
    'fe_rates_interval':           '600000',  # rate card + tile
    'fe_events_interval':          '60000',   # event log
    'fe_security_interval':        '60000',   # security tile
    'fe_forecast_interval':        '3600000', # solar forecast refresh (1 hour)
    'security_poll_interval':      '30',      # backend cache TTL
    # Gemini AI
    'gemini_api_key':              '',
    'gemini_model':                'gemini-2.0-flash',
    # Nest / Google SDM
    'nest_enabled':                '0',
    'nest_poll_interval':          '60',
    'nest_client_id':              'REDACTED_NEST_CLIENT_ID',
    'nest_client_secret':          'REDACTED_NEST_CLIENT_SECRET',
    'nest_project_id':             'REDACTED_NEST_PROJECT_ID',
    'nest_pubsub_subscription':    '',
    'nest_refresh_token':          '',
    'nest_access_token':           '',
    'nest_token_expiry':           '0',
}

def _seed_settings(conn):
    for key, default in _SETTINGS_DEFAULTS.items():
        conn.execute(
            'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
            (key, default)
        )
    conn.commit()

def load_settings() -> dict:
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute('SELECT key, value FROM settings').fetchall()
    return {k: v for k, v in rows}

def get_setting(key: str, default=None):
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
    return row[0] if row else default

def get_setting_int(key: str, default: int = 0) -> int:
    val = get_setting(key)
    try:
        return int(val)
    except (TypeError, ValueError):
        return default

def get_setting_bool(key: str, default: bool = True) -> bool:
    val = get_setting(key)
    if val is None:
        return default
    return val == '1'


def _load_tou_periods() -> dict:
    """Load TOU period definitions from DB setting, falling back to default."""
    raw = get_setting('tou_periods')
    if raw:
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            pass
    return None  # tou_period() will use its built-in default


def _seed_rate_history(conn):
    """If rate_history is empty, seed from rates.json so existing data isn't lost."""
    count = conn.execute('SELECT COUNT(*) FROM rate_history').fetchone()[0]
    if count > 0:
        return
    rates = load_rates()
    if not rates or 'summer_on_peak' not in rates:
        return
    # Try to parse effective date from source_url (e.g. "1-1-26%20Schedule...")
    eff_date = '2026-01-01'  # fallback
    url = rates.get('source_url', '')
    import re as _re
    m = _re.search(r'(\d{1,2})-(\d{1,2})-(\d{2,4})', url)
    if m:
        mo, day, yr = m.groups()
        yr = int(yr) if len(yr) == 4 else 2000 + int(yr)
        eff_date = f'{yr}-{int(mo):02d}-{int(day):02d}'
    conn.execute(
        'INSERT OR IGNORE INTO rate_history '
        '(effective_date, summer_on_peak, summer_off_peak, summer_super_off_peak, '
        ' winter_on_peak, winter_off_peak, winter_super_off_peak, '
        ' base_services_charge_per_day, source_url, fetched_at) '
        'VALUES (?,?,?,?,?,?,?,?,?,?)',
        (eff_date,
         rates.get('summer_on_peak', 0), rates.get('summer_off_peak', 0),
         rates.get('summer_super_off_peak', 0),
         rates.get('winter_on_peak', 0), rates.get('winter_off_peak', 0),
         rates.get('winter_super_off_peak', 0),
         rates.get('base_services_charge_per_day', 0),
         url, rates.get('updated'))
    )
    conn.commit()
    print(f'rate_history: seeded from rates.json (effective {eff_date})')


# ── Rate history helpers ──────────────────────────────────────────────────────
def _load_rate_history() -> list:
    """Load all rate periods sorted by effective_date."""
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            'SELECT effective_date, end_date, '
            '       summer_on_peak, summer_off_peak, summer_super_off_peak, '
            '       winter_on_peak, winter_off_peak, winter_super_off_peak, '
            '       COALESCE(base_services_charge_per_day, 0) '
            'FROM rate_history ORDER BY effective_date'
        ).fetchall()


def _rate_for_date(rate_periods, d_iso: str) -> dict | None:
    """Find the rate dict applicable to a given date string 'YYYY-MM-DD'."""
    for row in reversed(rate_periods):
        eff = row[0]
        if d_iso >= eff:
            return {
                'summer_on_peak': row[2], 'summer_off_peak': row[3],
                'summer_super_off_peak': row[4],
                'winter_on_peak': row[5], 'winter_off_peak': row[6],
                'winter_super_off_peak': row[7],
                'base_services_charge_per_day': row[8] if len(row) > 8 else 0,
            }
    return None


def _is_refresh_due(start_date_str: str, interval_months: int) -> bool:
    """Check if a recurring task anchored to start_date is due today.

    Schedule: start_date, start_date + N months, start_date + 2N months, ...
    Returns True if today >= the most recent scheduled date.
    If no start date, always due (immediate).
    """
    if not start_date_str:
        return True
    try:
        start = date.fromisoformat(start_date_str)
    except ValueError:
        return True
    today = date.today()
    if today < start:
        return False
    if interval_months <= 0:
        return True
    # How many full intervals have elapsed since start?
    months_elapsed = (today.year - start.year) * 12 + (today.month - start.month)
    intervals_passed = months_elapsed // interval_months
    # Compute the most recent due date
    total_months = (start.month - 1) + intervals_passed * interval_months
    due_year = start.year + total_months // 12
    due_month = total_months % 12 + 1
    due_day = min(start.day, 28)  # safe for all months
    last_due = date(due_year, due_month, due_day)
    return today >= last_due


def _log_system_error(system: str, title: str, detail: str = None) -> None:
    """Log a system error to the event_log table."""
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                'INSERT INTO event_log '
                '(ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), system, 'error', title, detail, 'failed', 'live')
            )
    except Exception:
        pass  # don't let logging errors crash the caller


def write_reading(solar_w, home_w, battery_w, grid_w, battery_pct) -> None:
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            'INSERT OR REPLACE INTO readings VALUES (?,?,?,?,?,?)',
            (int(time.time()), solar_w, home_w, battery_w, grid_w, battery_pct)
        )


def purge_old() -> None:
    """Disabled — keep all readings forever."""
    pass


def rebuild_daily_costs(year: int = None) -> None:
    """Rebuild daily_costs from readings for a given year (default: current year)."""
    # Load rate history — fall back to rates.json if empty
    rate_periods = _load_rate_history()
    fallback_rates = load_rates() if not rate_periods else None
    if not rate_periods and not fallback_rates:
        print('rebuild_daily_costs: no rate data available, skipping')
        return

    target_year = year or date.today().year
    jan1 = int(datetime(target_year, 1, 1).timestamp())
    dec31_end = int(datetime(target_year + 1, 1, 1).timestamp())

    # Load TOU period definitions from DB setting
    tou_cfg = _load_tou_periods()

    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            'SELECT timestamp, grid_w FROM readings '
            'WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp',
            (jan1, dec31_end)
        ).fetchall()

        # Aggregate into per-date buckets using trapezoidal intervals
        day_data: dict = {}
        _rate_cache: dict = {}  # cache rate lookup per day
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
            season, period = tou_period(dt, tou_cfg)
            # Look up rate for this day (cached per day)
            if d not in _rate_cache:
                if rate_periods:
                    _rate_cache[d] = _rate_for_date(rate_periods, d) or fallback_rates or {}
                else:
                    _rate_cache[d] = fallback_rates or {}
            rate = _rate_cache[d].get(f'{season}_{period}', 0.0)
            if d not in day_data:
                day_data[d] = {
                    'import_kwh': 0.0, 'export_kwh': 0.0,
                    'import_cost': 0.0, 'export_credit': 0.0,
                    'on_peak_kwh': 0.0, 'off_peak_kwh': 0.0, 'super_off_peak_kwh': 0.0,
                    'on_peak_cost': 0.0, 'off_peak_cost': 0.0, 'super_off_peak_cost': 0.0,
                }
            if kwh > 0:
                day_data[d]['import_kwh']  += kwh
                day_data[d]['import_cost'] += kwh * rate
            elif kwh < 0:
                day_data[d]['export_kwh']    += abs(kwh)
                day_data[d]['export_credit'] += abs(kwh) * rate
            # Per-period net (signed: positive=import cost, negative=export credit)
            day_data[d][f'{period}_kwh']  += kwh
            day_data[d][f'{period}_cost'] += kwh * rate

        for d, v in day_data.items():
            c.execute(
                'INSERT OR REPLACE INTO daily_costs '
                '(date, import_kwh, export_kwh, import_cost, export_credit, '
                ' on_peak_kwh, off_peak_kwh, super_off_peak_kwh, '
                ' on_peak_cost, off_peak_cost, super_off_peak_cost) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (d, round(v['import_kwh'], 4), round(v['export_kwh'], 4),
                 round(v['import_cost'], 4), round(v['export_credit'], 4),
                 round(v['on_peak_kwh'], 4), round(v['off_peak_kwh'], 4),
                 round(v['super_off_peak_kwh'], 4),
                 round(v['on_peak_cost'], 4), round(v['off_peak_cost'], 4),
                 round(v['super_off_peak_cost'], 4))
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
    rate_periods = _load_rate_history()
    fallback_rates = load_rates() if not rate_periods else None
    tou_cfg = _load_tou_periods()
    _rc: dict = {}  # per-day rate cache

    solar_kwh = home_kwh = grid_import_kwh = savings = 0.0
    for i in range(1, len(rows)):
        dt_h = (rows[i][0] - rows[i-1][0]) / 3600
        solar_w = max(0.0, rows[i][1] or 0)
        home_w  = max(0.0, rows[i][2] or 0)
        grid_w  = rows[i][4] or 0
        dt      = datetime.fromtimestamp(rows[i][0])
        d       = dt.date().isoformat()
        season, period = tou_period(dt, tou_cfg)
        if d not in _rc:
            _rc[d] = (_rate_for_date(rate_periods, d) or fallback_rates or {}) if rate_periods else (fallback_rates or {})
        rate = _rc[d].get(f'{season}_{period}', 0.0)

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
      battery_power – positive = discharging, negative = charging
      grid_power    – positive = importing, negative = exporting
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


# ── Public port check ────────────────────────────────────────────────────────
_port_open: bool = False    # assume closed on startup (no log needed)
_port_open_since: float = 0

def _get_public_ip() -> str | None:
    try:
        req = urllib.request.Request('https://ifconfig.me', headers={'User-Agent': 'curl/7'})
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.read().decode().strip()
    except Exception:
        return None

def check_public_port(port: int = 5000) -> None:
    """Check if our public IP has the given port open; only log when OPEN."""
    global _port_open, _port_open_since
    pub_ip = _get_public_ip()
    if not pub_ip:
        return
    try:
        s = socket.create_connection((pub_ip, port), timeout=5)
        s.close()
        is_open = True
    except (OSError, socket.timeout):
        is_open = False

    if is_open == _port_open:
        if is_open and _port_open_since:
            # Still open — update event with duration
            mins = int((time.time() - _port_open_since) / 60)
            if mins >= 5:
                dur = f'{mins} min' if mins < 60 else f'{mins // 60}h {mins % 60}m'
                title = f'Port {port} is OPEN publicly ({dur})'
                detail = f'Public IP: {pub_ip}'
                print(f'Port check: {title}')
                with sqlite3.connect(DB_PATH) as c:
                    # Update the existing open event instead of creating new ones
                    c.execute(
                        'UPDATE event_log SET title=?, ts=? '
                        'WHERE system="system" AND event_type="port_check" AND result="warning" '
                        'ORDER BY ts DESC LIMIT 1',
                        (title, int(time.time()))
                    )
        return

    _port_open = is_open

    if is_open:
        _port_open_since = time.time()
        title = f'Port {port} is OPEN publicly'
        detail = f'Public IP: {pub_ip}'
        print(f'Port check: {title} ({detail})')
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                'INSERT INTO event_log '
                '(ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), 'system', 'port_check', title, detail, 'warning', 'live')
            )
    else:
        # Port closed — silently update state, no log entry
        _port_open_since = 0


# ── Poller thread ─────────────────────────────────────────────────────────────
def poller() -> None:
    pw = None
    last_write = 0
    last_purge = 0
    last_cost_rebuild = 0
    last_holidays_check = 0
    last_rates_check = 0
    last_rachio_event_poll = 0
    last_rain_skip_check = 0
    last_nest_event_poll = 0
    last_port_check = 0
    last_pool_poll = 0

    while True:
        poll_interval = get_setting_int('powerwall_poll_interval', POLL_INTERVAL)
        db_write_interval = get_setting_int('powerwall_db_write_interval', DB_WRITE_EVERY)

        if not get_setting_bool('powerwall_enabled', True):
            time.sleep(poll_interval)
            continue

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

            if now - last_write >= db_write_interval:
                write_reading(solar_w, home_w, battery_w, grid_w, battery_pct)
                last_write = now

            if now - last_purge >= 86400:
                purge_old()
                last_purge = now

            cost_interval = get_setting_int('cost_rebuild_days', 1) * 86400
            if now - last_cost_rebuild >= cost_interval:
                threading.Thread(target=rebuild_daily_costs, daemon=True).start()
                last_cost_rebuild = now

            # Holidays + Rates refresh (calendar-driven from shared start date)
            refresh_start = get_setting('refresh_start_date', '')

            # Holidays
            holidays_months = get_setting_int('holidays_poll_months', 1)
            if _is_refresh_due(refresh_start, holidays_months) and now - last_holidays_check >= 86400:
                try:
                    load_or_generate_holidays()
                    print('Holidays refreshed')
                except Exception as exc:
                    print(f'Holidays refresh error: {exc}')
                    _log_system_error('holidays', 'Holiday refresh failed', str(exc))
                last_holidays_check = now

            # Energy rates
            rates_months = get_setting_int('rates_poll_months', 1)
            if _is_refresh_due(refresh_start, rates_months) and now - last_rates_check >= 86400:
                try:
                    page_url = get_setting('rates_page_url',
                                           'https://www.sdge.com/total-electric-rates')
                    schedule = get_setting('rate_schedule_name', 'EV-TOU')
                    fetch_ev_tou2_rates(page_url=page_url,
                                        schedule_name=schedule,
                                        db_path=DB_PATH)
                except Exception as exc:
                    print(f'Rate fetch error: {exc}')
                    _log_system_error('rates', 'Energy rate refresh failed', str(exc))
                last_rates_check = now

            # Rachio event logging
            rachio_event_interval = get_setting_int('rachio_event_poll_interval', 1800)
            if now - last_rachio_event_poll >= rachio_event_interval:
                if get_setting_bool('rachio_enabled', True):
                    try:
                        fetch_rachio_events()
                    except Exception as exc:
                        print(f'Rachio event poll error: {exc}')
                        _log_system_error('rachio', 'Event poll error', str(exc))
                last_rachio_event_poll = now

            # Rain-based smart skip
            rain_skip_interval = get_setting_int('rain_skip_check_interval', 3600)
            if now - last_rain_skip_check >= rain_skip_interval:
                try:
                    evaluate_rain_skip()
                except Exception as exc:
                    print(f'Rain skip check error: {exc}')
                    _log_system_error('rachio', 'Rain skip check error', str(exc))
                last_rain_skip_check = now

            # Nest camera/doorbell events (Pub/Sub pull)
            nest_event_interval = get_setting_int('nest_poll_interval', 60)
            if now - last_nest_event_poll >= nest_event_interval:
                if get_setting_bool('nest_enabled', False):
                    try:
                        fetch_nest_events()
                    except Exception as exc:
                        print(f'Nest event poll error: {exc}')
                        _log_system_error('nest', 'Event poll error', str(exc))
                last_nest_event_poll = now

            # Public port exposure check (every 5 min)
            if now - last_port_check >= 300:
                try:
                    check_public_port()
                except Exception as exc:
                    print(f'Port check error: {exc}')
                last_port_check = now

            # Pool equipment state polling
            pool_event_interval = get_setting_int('pool_poll_interval', POOL_POLL_INTERVAL)
            if now - last_pool_poll >= pool_event_interval:
                if get_setting_bool('pool_enabled', True):
                    try:
                        fetch_pool()
                    except Exception as exc:
                        print(f'Pool poll error: {exc}')
                        _log_system_error('pool', 'Pool poll error', str(exc))
                last_pool_poll = now

        except Exception as exc:
            print(f'Poller error: {exc}')
            _log_system_error('powerwall', 'Poller error', str(exc))
            pw = None  # force reconnect on next iteration

        time.sleep(poll_interval)


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

    lookback = get_setting_int('rain_lookback_days', 7)
    url = (
        'https://api.open-meteo.com/v1/forecast'
        '?latitude=32.7157&longitude=-117.1611'
        '&current_weather=true'
        '&daily=precipitation_sum,cloudcover_mean'
        f'&past_days={lookback}'
        '&forecast_days=2&timezone=America%2FLos_Angeles'
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        cw    = data.get('current_weather', {})
        daily = data.get('daily', {})
        dates    = daily.get('time', [])
        precip   = daily.get('precipitation_sum', [])
        clouds   = daily.get('cloudcover_mean', [])

        # Tomorrow is the entry after today — last entry if forecast_days=2
        clouds_tm = clouds[-1] if len(clouds) >= 2 else None
        rain_tm   = precip[-1] if len(precip) >= 2 else None

        # Rain history: past N days (exclude today and tomorrow forecast)
        rain_history = []
        for i, (d, mm) in enumerate(zip(dates, precip)):
            if i < len(dates) - 2:           # skip today + tomorrow
                rain_history.append({'date': d, 'mm': mm or 0})

        _wx_cache = {
            'temp_f':          round(cw.get('temperature', 0) * 9 / 5 + 32, 1),
            'desc':            WMO.get(cw.get('weathercode', 0), ''),
            'tomorrow_cloud':  clouds_tm,
            'tomorrow_rain':   rain_tm,
            'bad_forecast':    (clouds_tm or 0) > 60 or (rain_tm or 0) > 1,
            'rain_history':    rain_history,
        }
        _wx_ts = time.time()
    except Exception as exc:
        print(f'Weather error: {exc}')
        if not _wx_cache:
            _wx_cache = {}

    return _wx_cache


# ── Solar Forecast ────────────────────────────────────────────────────────────
_sf_cache: dict = {}
_sf_ts: float   = 0.0
SF_TTL = 3600  # 1 hour
PEAK_RAD_WM2 = 950.0  # clear-sky noon shortwave radiation for San Diego


def _peak_solar_w() -> float:
    cutoff = int((datetime.combine(date.today(), datetime.min.time())
                  - timedelta(days=14)).timestamp())
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute('SELECT MAX(solar_w) FROM readings WHERE timestamp >= ?',
                        (cutoff,)).fetchone()
    return float(row[0]) if row and row[0] else 8100.0


def fetch_solar_forecast() -> dict:
    global _sf_cache, _sf_ts
    now = datetime.now()
    today_str = now.date().isoformat()
    current_hour = now.hour

    if _sf_cache.get('date') == today_str and time.time() - _sf_ts < SF_TTL:
        return _sf_cache

    url = (
        'https://api.open-meteo.com/v1/forecast'
        '?latitude=32.7157&longitude=-117.1611'
        '&hourly=shortwave_radiation'
        '&forecast_days=1&timezone=America%2FLos_Angeles'
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())

        hourly = data.get('hourly', {})
        times = hourly.get('time', [])
        rads  = hourly.get('shortwave_radiation', [])

        peak_solar = _peak_solar_w()
        scale = peak_solar / PEAK_RAD_WM2

        new_hours = {}
        for t_str, rad in zip(times, rads):
            h = int(t_str.split('T')[1].split(':')[0])
            new_hours[h] = round(max(0, rad * scale))

        # Preserve past hours from previous fetch, only update future
        if _sf_cache.get('date') == today_str and 'hours' in _sf_cache:
            merged = dict(_sf_cache['hours'])
            for h, w in new_hours.items():
                if h >= current_hour:
                    merged[h] = w
        else:
            merged = new_hours

        _sf_cache = {'date': today_str, 'hours': merged}
        _sf_ts = time.time()

    except Exception as exc:
        print(f'Solar forecast error: {exc}')
        if not _sf_cache or _sf_cache.get('date') != today_str:
            _sf_cache = {'date': today_str, 'hours': {}}

    return _sf_cache


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
        c500   = _key(circuit, 500, '500') or {}
        c501   = _key(circuit, 501, '501') or {}
        c502   = _key(circuit, 502, '502') or {}
        c503   = _key(circuit, 503, '503') or {}
        c504   = _key(circuit, 504, '504') or {}
        c505   = _key(circuit, 505, '505') or {}
        c506   = _key(circuit, 506, '506') or {}
        c507   = _key(circuit, 507, '507') or {}
        c508   = _key(circuit, 508, '508') or {}

        temp_f  = _nested(pool_b, 'last_temperature', 'value')
        spa_f   = _nested(spa_b,  'last_temperature', 'value')

        # Heat mode: resolve enum label from index
        hm_idx  = _nested(pool_b, 'heat_mode', 'value')
        hm_opts = _nested(pool_b, 'heat_mode', 'enum_options') or []
        heat_mode = hm_opts[hm_idx] if (hm_idx is not None and isinstance(hm_opts, list) and hm_idx < len(hm_opts)) else None

        # Pump 1 = pool pump; edge pump via circuit 506 (pump 0 is unreliable)
        pool_pump_on    = bool(_nested(pump1, 'state', 'value'))
        pool_pump_watts = _nested(pump1, 'watts_now', 'value')
        edge_pump_on    = bool(_nested(c506, 'value'))

        # Circuits
        pool_circuit_on = bool(_nested(c505, 'value'))
        spa_circuit_on  = bool(_nested(c500, 'value'))
        cleaner_on      = bool(_nested(c508, 'value'))
        pool_light_on   = bool(_nested(c501, 'value'))
        water_light_on  = bool(_nested(c502, 'value'))
        spa_light_on    = bool(_nested(c503, 'value'))
        waterfall_on    = bool(_nested(c504, 'value'))
        spillway_on     = bool(_nested(c507, 'value'))

        # Feature 1 — circuit ID unknown, find by name
        feature1_on = None
        for cid, cdata in circuit.items():
            if isinstance(cdata, dict):
                cname = _nested(cdata, 'name') or _nested(cdata, 'name', 'value') or ''
                if isinstance(cname, str) and cname.strip() == 'Feature 1':
                    feature1_on = bool(_nested(cdata, 'value'))
                    break

        # Salt chlorine generator (SCG)
        scg = data.get('scg') or data.get(b'scg') or {}
        scg_sensor = scg.get('sensor') or scg.get(b'sensor') or {}
        scg_config = scg.get('configuration') or scg.get(b'configuration') or {}
        salt_ppm     = _nested(scg_sensor, 'salt_ppm', 'value')
        scg_state    = _nested(scg_sensor, 'state', 'value')  # 0=off, 1=on
        scg_pool_pct = _nested(scg_config, 'pool_setpoint', 'value')
        super_chlor  = _nested(scg, 'super_chlorinate', 'value')  # 0=off, 1=on

        return {
            'temp_f':          round(float(temp_f), 1) if temp_f is not None else None,
            'pump_on':         pool_pump_on,
            'pump_watts':      int(pool_pump_watts) if pool_pump_watts is not None else None,
            'edge_pump_on':    edge_pump_on,
            'cleaner_on':      cleaner_on,
            'pool_circuit_on': pool_circuit_on,
            'spa_circuit_on':  spa_circuit_on,
            'pool_light_on':   pool_light_on,
            'water_light_on':  water_light_on,
            'spa_light_on':    spa_light_on,
            'waterfall_on':    waterfall_on,
            'spillway_on':     spillway_on,
            'feature1_on':     feature1_on,
            'salt_ppm':        int(salt_ppm) if salt_ppm is not None else None,
            'scg_active':      bool(scg_state) if scg_state is not None else None,
            'scg_pool_pct':    int(scg_pool_pct) if scg_pool_pct is not None else None,
            'super_chlor':     bool(super_chlor) if super_chlor is not None else None,
        }
    finally:
        await gateway.async_disconnect()


_POOL_EVENT_FIELDS = {
    'pump_on':         ('pump_changed',         'Pool pump'),
    'edge_pump_on':    ('edge_pump_changed',    'Edge pump'),
    'cleaner_on':      ('cleaner_changed',      'Cleaner'),
    'pool_circuit_on': ('pool_circuit_changed',  'Pool circuit'),
    'spa_circuit_on':  ('spa_circuit_changed',   'Spa circuit'),
    'pool_light_on':   ('pool_light_changed',    'Pool light'),
    'water_light_on':  ('water_light_changed',   'Water light'),
    'spa_light_on':    ('spa_light_changed',     'Spa light'),
    'waterfall_on':    ('waterfall_changed',     'Waterfall'),
    'spillway_on':     ('spillway_changed',      'Spillway'),
    'feature1_on':     ('feature1_changed',      'Feature 1'),
}


def _log_pool_changes(new: dict) -> None:
    """Compare new pool state against previous and log confirmed changes.

    Debounce: a state change must persist for 2 consecutive polls before
    logging.  This filters out single-sample flickers from ScreenLogic
    (e.g. edge pump briefly reporting None/0 then back to 1).
    """
    global _pool_prev, _pool_pending
    if not _pool_prev:
        # First fetch — seed state, don't log
        _pool_prev = {k: new.get(k) for k in _POOL_EVENT_FIELDS}
        _pool_pending = {}
        return
    now = int(time.time())
    try:
        with sqlite3.connect(DB_PATH) as c:
            for field, (event_type, label) in _POOL_EVENT_FIELDS.items():
                confirmed_val = _pool_prev.get(field)
                new_val = new.get(field)
                if confirmed_val == new_val:
                    # Stable — clear any pending change for this field
                    _pool_pending.pop(field, None)
                    continue
                # Value differs from confirmed state
                if _pool_pending.get(field) == new_val:
                    # Same new value two polls in a row — confirmed real change
                    state = 'on' if new_val else 'off'
                    title = f'{label} turned {state}'
                    detail = None
                    if field == 'pump_on' and new_val and new.get('pump_watts'):
                        detail = f'{new["pump_watts"]} W'
                    c.execute(
                        'INSERT INTO event_log '
                        '(ts, system, event_type, title, detail, result, source) '
                        'VALUES (?,?,?,?,?,?,?)',
                        (now, 'pool', event_type, title, detail, 'ok', 'live')
                    )
                    _pool_prev[field] = new_val
                    _pool_pending.pop(field, None)
                else:
                    # First time seeing this new value — mark pending, wait for confirmation
                    _pool_pending[field] = new_val
    except Exception as exc:
        print(f'Pool event log error: {exc}')


def fetch_pool() -> dict:
    global _pool, _pool_ts
    if not get_setting_bool('pool_enabled', True):
        return _pool or {'temp_f': None, 'pump_on': None, 'spa_temp_f': None}
    pool_ttl = get_setting_int('pool_poll_interval', POOL_POLL_INTERVAL)
    # Clock-aligned polling: fetch when we enter a new interval window
    # e.g. 900s → :00, :15, :30, :45 regardless of server start time
    now = time.time()
    if _pool_ts and int(now) // pool_ttl == int(_pool_ts) // pool_ttl:
        return _pool
    try:
        _pool    = asyncio.run(_pool_fetch_async())
        _pool_ts = time.time()
        _log_pool_changes(_pool)
    except Exception as exc:
        print(f'Pool error: {exc}')
        _log_system_error('pool', 'Pool fetch error', str(exc))
        if not _pool:
            _pool = {'temp_f': None, 'pump_on': None, 'spa_temp_f': None}
    return _pool


# ── Security (Abode device state) ────────────────────────────────────────────
_MODE_DISPLAY = {'standby': 'Disarmed', 'home': 'Armed Home', 'away': 'Armed Away'}


def fetch_security() -> dict:
    global _security, _security_ts
    if _abode_instance is None:
        return {'mode': None, 'mode_display': None, 'issues': [], 'connected': False}
    ttl = get_setting_int('security_poll_interval', 30)
    if time.time() - _security_ts < ttl:
        return _security
    try:
        alarm = _abode_instance.get_alarm()
        mode = alarm.mode if alarm else 'standby'
        devices = _abode_instance.get_devices()
        issues = []
        for d in devices:
            dtype = getattr(d, 'type', '') or ''
            status = getattr(d, 'status', '') or ''
            name = getattr(d, 'name', '') or ''
            if 'Contact' in dtype and status == 'Open':
                issues.append({'name': name, 'type': 'open'})
            elif 'Lock' in dtype and status == 'LockOpen':
                issues.append({'name': name, 'type': 'unlocked'})
        _security = {
            'mode': mode,
            'mode_display': _MODE_DISPLAY.get(mode, mode),
            'issues': issues,
            'connected': True,
        }
        _security_ts = time.time()
    except Exception as exc:
        print(f'Security fetch error: {exc}')
        _log_system_error('abode', 'Security fetch error', str(exc))
        if not _security:
            _security = {'mode': None, 'mode_display': None, 'issues': [], 'connected': False}
    return _security


# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_file(os.path.join('static', 'frontend', 'index.html'))


@app.route('/_next/<path:filename>')
def next_static(filename):
    return send_from_directory(os.path.join('static', 'frontend', '_next'), filename)


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
    raw = today_rows()
    out = []
    for i, r in enumerate(raw):
        # Drop all-zero glitch readings
        if r[1] == 0 and r[2] == 0 and r[3] == 0 and r[4] == 0:
            continue
        # Drop single-sample outliers: home_w differs >50% from both neighbors
        if 0 < i < len(raw) - 1:
            prev_h, cur_h, next_h = raw[i-1][2], r[2], raw[i+1][2]
            if prev_h > 0 and next_h > 0 and cur_h > 0:
                if abs(cur_h - prev_h) / prev_h > 0.5 and abs(cur_h - next_h) / next_h > 0.5:
                    continue
        out.append({'ts': r[0], 'solar_w': r[1], 'home_w': r[2]})
    return jsonify(out)


@app.route('/api/weather')
def api_weather():
    return jsonify(fetch_weather())


@app.route('/api/solar-forecast')
def api_solar_forecast():
    fc = fetch_solar_forecast()
    today_str = fc.get('date', date.today().isoformat())
    base_ts = int(datetime.strptime(today_str, '%Y-%m-%d').timestamp())
    points = []
    for h in sorted(fc.get('hours', {}).keys(), key=int):
        w = fc['hours'][h]
        if w > 0:
            points.append({'ts': base_ts + int(h) * 3600, 'solar_w': w})
    return jsonify(points)


@app.route('/api/pool')
def api_pool():
    return jsonify(fetch_pool())


@app.route('/api/security')
def api_security():
    return jsonify(fetch_security())


@app.route('/api/debug/abode/devices')
def api_debug_abode_devices():
    if _abode_instance is None:
        return jsonify({'error': 'Abode not connected'}), 503
    try:
        devices = _abode_instance.get_devices()
        return jsonify([
            {'name': getattr(d, 'name', ''), 'type': getattr(d, 'type', ''),
             'status': getattr(d, 'status', ''), 'battery_low': getattr(d, 'battery_low', None)}
            for d in devices
        ])
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


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


@app.route('/api/debug/rachio/events')
def api_debug_rachio_events():
    """Return raw device events from Rachio — shows actual field names."""
    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')
        end_ms    = int(time.time() * 1000)
        start_ms  = end_ms - 7 * 86400 * 1000  # last 7 days
        result    = {}
        for device in person.get('devices', []):
            did = device['id']
            events = _rachio_get(f'/device/{did}/event?startTime={start_ms}&endTime={end_ms}')
            result[did] = events
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


def _rachio_put(path: str, body: dict) -> dict | None:
    """PUT request to Rachio API (used for rain_delay etc.)."""
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        RACHIO_BASE + path, data=data, method='PUT',
        headers={'Authorization': f'Bearer {RACHIO_API_KEY}', 'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


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


# ── Rachio event logging ─────────────────────────────────────────────────────
RACHIO_EVENT_TYPE_MAP = {
    'ZONE_STARTED':         'zone_started',
    'ZONE_COMPLETED':       'zone_completed',
    'ZONE_STOPPED':         'zone_stopped',
    'SCHEDULE_STARTED':     'schedule_started',
    'SCHEDULE_COMPLETED':   'schedule_completed',
    'SCHEDULE_STOPPED':     'schedule_stopped',
    'RAIN_DELAY_ON':        'rain_delay',
    'RAIN_DELAY_OFF':       'rain_delay_off',
    'RAIN_SENSOR_TRIPPED':  'rain_sensor',
    'WEATHER_INTELLIGENCE': 'weather_skip',
    'SKIP':                 'skip',
    'DEVICE_OFFLINE':       'device_offline',
    'DEVICE_ONLINE':        'device_online',
}

_rachio_event_ts: float = 0.0


def fetch_rachio_events() -> int:
    """Poll Rachio device events and log new ones to event_log. Returns insert count."""
    global _rachio_event_ts
    if not get_setting_bool('rachio_enabled', True):
        return 0
    inserted = 0
    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')

        # Collect all events from all devices (last 48h)
        end_ms   = int(time.time() * 1000)
        start_ms = end_ms - 48 * 3600 * 1000
        rows = []
        for device in person.get('devices', []):
            did   = device['id']
            dname = device.get('name', '?')
            raw_events = _rachio_get(f'/device/{did}/event?startTime={start_ms}&endTime={end_ms}')
            if not isinstance(raw_events, list):
                raw_events = raw_events.get('events', []) if isinstance(raw_events, dict) else []
            for ev in raw_events:
                try:
                    raw_type   = ev.get('type') or ev.get('subType') or 'UNKNOWN'
                    event_type = RACHIO_EVENT_TYPE_MAP.get(raw_type, raw_type.lower())
                    title      = ev.get('summary') or ev.get('eventType', raw_type)
                    # eventDate is epoch ms
                    ts_raw = ev.get('eventDate') or ev.get('createDate')
                    ts = int(ts_raw / 1000) if ts_raw else int(time.time())
                    zone   = ev.get('zoneName', '')
                    sched  = ev.get('scheduleName', '')
                    dur    = ev.get('durationInMinutes', '')
                    detail = f'device: {dname}  zone: {zone}  schedule: {sched}  duration: {dur}min'.strip()
                    rows.append((ts, 'rachio', event_type, title, detail, 'info', 'live'))
                except Exception:
                    continue

        # Batch deduplicate (same pattern as abode_backfill)
        if rows:
            with sqlite3.connect(DB_PATH, timeout=30) as c:
                existing = set(
                    c.execute(
                        'SELECT ts, title FROM event_log WHERE system = ?', ('rachio',)
                    ).fetchall()
                )
                for row in rows:
                    ts, sys_, evt, title, detail, result, source = row
                    if (ts, title) not in existing:
                        c.execute(
                            'INSERT INTO event_log '
                            '(ts, system, event_type, title, detail, result, source) '
                            'VALUES (?,?,?,?,?,?,?)', row)
                        existing.add((ts, title))
                        inserted += 1
            if inserted:
                print(f'Rachio events: logged {inserted} new events')

        _rachio_event_ts = time.time()
    except Exception as exc:
        print(f'Rachio event poll error: {exc}')
        _log_system_error('rachio', 'Event poll error', str(exc))
    return inserted


# ── Rain-based smart skip ────────────────────────────────────────────────────
_rain_skip_ts: float = 0.0


def evaluate_rain_skip() -> None:
    """Check accumulated rainfall and extend Rachio rain delay if warranted.

    Cooperates with Rachio's own weather-based skip: only applies a delay
    if our calculated end time is later than any existing delay.  Never
    shortens an active Rachio delay.
    """
    global _rain_skip_ts
    if not get_setting_bool('rain_skip_enabled', False):
        return
    if not get_setting_bool('rachio_enabled', True):
        return

    import math
    mm_per_day = get_setting_int('rain_mm_per_skip_day', 8)
    max_days   = get_setting_int('rain_skip_max_days', 7)

    wx = fetch_weather()
    rain_history = wx.get('rain_history', [])
    if not rain_history:
        return

    accumulated = sum(entry['mm'] for entry in rain_history)
    skip_days   = min(int(math.floor(accumulated / mm_per_day)), max_days) if mm_per_day > 0 else 0

    if skip_days <= 0:
        _rain_skip_ts = time.time()
        return

    now_ts        = time.time()
    our_end_ts    = now_ts + skip_days * 86400

    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')

        for device in person.get('devices', []):
            did   = device['id']
            dname = device.get('name', '?')

            # Check if Rachio already has an active rain delay
            existing_end_ts = 0
            rd_exp = device.get('rainDelayExpirationDate')
            if rd_exp and isinstance(rd_exp, (int, float)) and rd_exp > 0:
                existing_end_ts = rd_exp / 1000  # epoch ms → seconds

            if existing_end_ts >= our_end_ts:
                # Rachio's own delay already extends further — don't shorten it
                existing_dt = datetime.fromtimestamp(existing_end_ts).strftime('%Y-%m-%d %H:%M')
                print(f'Rain skip: {dname} — existing delay until {existing_dt} is longer, skipping')
                continue

            # Apply our extended delay (duration from now)
            duration_secs = int(our_end_ts - now_ts)
            _rachio_put(f'/device/{did}/rain_delay', {'id': did, 'duration': duration_secs})

            existing_info = ''
            if existing_end_ts > now_ts:
                existing_dt = datetime.fromtimestamp(existing_end_ts).strftime('%Y-%m-%d %H:%M')
                existing_info = f'  existing_delay_until: {existing_dt}'
            our_end_dt = datetime.fromtimestamp(our_end_ts).strftime('%Y-%m-%d %H:%M')
            detail = (f'device: {dname}  accumulated: {accumulated:.1f}mm  '
                      f'lookback: {len(rain_history)} days  skip: {skip_days} days  '
                      f'delay_until: {our_end_dt}{existing_info}')

            # Log (deduplicate on today's date so we don't re-log hourly)
            today_ts = int(datetime.now().replace(hour=0, minute=0, second=0).timestamp())
            title    = f'Rain skip: {skip_days} days ({dname})'
            with sqlite3.connect(DB_PATH, timeout=10) as c:
                exists = c.execute(
                    'SELECT 1 FROM event_log WHERE system=? AND ts=? AND title=?',
                    ('rachio', today_ts, title)
                ).fetchone()
                if not exists:
                    c.execute(
                        'INSERT INTO event_log '
                        '(ts, system, event_type, title, detail, result, source) '
                        'VALUES (?,?,?,?,?,?,?)',
                        (today_ts, 'rachio', 'rain_skip_extended', title, detail, 'ok', 'live'))
            print(f'Rain skip applied: {dname} — {skip_days} days ({accumulated:.1f}mm accumulated)')

        _rain_skip_ts = time.time()
    except Exception as exc:
        print(f'Rain skip error: {exc}')
        _log_system_error('rachio', 'Rain skip evaluation error', str(exc))


def fetch_rachio_schedule() -> list:
    global _rachio_schedule, _rachio_ts
    if not get_setting_bool('rachio_enabled', True):
        return _rachio_schedule or []
    rachio_ttl = get_setting_int('rachio_poll_interval', RACHIO_TTL)
    if time.time() - _rachio_ts < rachio_ttl:
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

            # Check for active rain delay on this device
            rd_exp = device.get('rainDelayExpirationDate')
            if rd_exp and isinstance(rd_exp, (int, float)) and rd_exp > 0:
                rd_dt = datetime.fromtimestamp(rd_exp / 1000, tz=timezone.utc).astimezone().replace(tzinfo=None)
                if rd_dt > datetime.now():
                    rd_label = f'{rd_dt.month}/{rd_dt.day} {rd_dt.strftime("%I:%M%p").lstrip("0")}'
                    events.append({
                        'fire_time':    rd_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                        'name':         f'Rain Delay until {rd_label}',
                        'duration_min': 0,
                        'source':       'rachio',
                        'skip':         True,
                    })

        events.sort(key=lambda e: e['fire_time'])
        _rachio_schedule = events
        _rachio_ts = time.time()
        print(f'Rachio: fetched {len(events)} upcoming events')
    except Exception as exc:
        print(f'Rachio error: {exc}')
    return _rachio_schedule


# ── Abode websocket listener ─────────────────────────────────────────────────
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


def _abode_event_val(event, key):
    """Get a value from an abodepy event whether it's a dict or object."""
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


_abode_instance = None  # shared session reused by backfill
_abode_status = {
    'state': 'idle',            # idle | disabled | connecting | connected | error
    'last_error': None,
    'last_error_time': None,
    'last_event_time': None,
    'events_received': 0,
    'reconnect_count': 0,
    'last_backfill_time': None,
    'last_backfill_inserted': None,
    'last_backfill_error': None,
}


def _abode_write_event(event):
    """Parse an abodepy event (live or history dict) and write to event_log."""
    try:
        event_type_raw = (
            _abode_event_val(event, 'event_type') or
            _abode_event_val(event, 'type') or
            _abode_event_val(event, 'event_label') or ''
        )
        event_type = ABODE_TYPE_MAP.get(event_type_raw, 'unknown')
        title = (
            _abode_event_val(event, 'event_name') or
            _abode_event_val(event, 'device_name') or
            event_type_raw or '?'
        )
        device_name = _abode_event_val(event, 'device_name') or ''
        device_type = _abode_event_val(event, 'device_type') or ''
        severity    = _abode_event_val(event, 'severity') or ''
        detail = f'device: {device_name}  type: {device_type}  severity: {severity}'

        raw_ts = _abode_event_val(event, 'event_utc')
        ts = int(raw_ts) if raw_ts else int(time.time())

        with sqlite3.connect(DB_PATH, timeout=10) as c:
            c.execute(
                'INSERT OR IGNORE INTO event_log '
                '(ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (ts, 'abode', event_type, title, detail, 'info', 'live')
            )
        _abode_status['events_received'] += 1
        _abode_status['last_event_time'] = int(time.time())
    except Exception as exc:
        print(f'Abode event write error: {exc}')
        _log_system_error('abode', 'Event write error', str(exc))


def abode_backfill(abode, days=30):
    """Fetch historical Abode timeline events and insert any missing ones."""
    try:
        cutoff = int(time.time()) - days * 86400
        inserted = 0
        skipped = 0
        page = 1
        rows_to_insert = []
        page1_raw = None
        while True:
            url = f'https://my.goabode.com/api/v1/timeline?size=10&page={page}'
            resp = abode.send_request('get', url)
            data = resp.json()
            if not isinstance(data, list) or not data:
                break
            if page == 1:
                page1_raw = [{'event_utc': e.get('event_utc'), 'event_name': e.get('event_name'),
                              'device_name': e.get('device_name')} for e in data]
            oldest_ts = None
            for item in data:
                raw_ts = item.get('event_utc')
                ts = int(raw_ts) if raw_ts else None
                if ts is None:
                    skipped += 1
                    continue
                if oldest_ts is None or ts < oldest_ts:
                    oldest_ts = ts
                if ts < cutoff:
                    continue
                event_type_raw = (
                    item.get('event_type') or item.get('type') or
                    item.get('event_label') or ''
                )
                event_type = ABODE_TYPE_MAP.get(event_type_raw, 'unknown')
                title = (
                    item.get('event_name') or item.get('device_name') or
                    event_type_raw or '?'
                )
                device_name = item.get('device_name') or ''
                device_type = item.get('device_type') or ''
                severity    = item.get('severity') or ''
                detail = f'device: {device_name}  type: {device_type}  severity: {severity}'
                rows_to_insert.append(
                    (ts, 'abode', event_type, title, detail, 'info', 'import'))
            # Stop paging once we've gone past the cutoff
            if oldest_ts is not None and oldest_ts < cutoff:
                break
            page += 1
        # Batch insert — skip rows where (ts, system, title) already exists
        with sqlite3.connect(DB_PATH, timeout=30) as c:
            existing = set(
                (r[0], r[1]) for r in c.execute(
                    'SELECT ts, title FROM event_log WHERE system = ?', ('abode',)
                ).fetchall()
            )
            for row in rows_to_insert:
                ts, sys, evt, title, detail, result, source = row
                if (ts, title) not in existing:
                    c.execute(
                        'INSERT INTO event_log '
                        '(ts, system, event_type, title, detail, result, source) '
                        'VALUES (?,?,?,?,?,?,?)', row)
                    existing.add((ts, title))
                    inserted += 1
        # Debug: count collected rows by date
        from collections import Counter
        date_counts = Counter()
        for row in rows_to_insert:
            from datetime import datetime as _dt
            date_counts[_dt.fromtimestamp(row[0]).strftime('%Y-%m-%d')] += 1
        _abode_status['last_backfill_time'] = int(time.time())
        _abode_status['last_backfill_inserted'] = inserted
        _abode_status['last_backfill_error'] = None
        _abode_status['last_backfill_collected'] = len(rows_to_insert)
        _abode_status['last_backfill_dates'] = dict(date_counts)
        _abode_status['last_backfill_existing_size'] = len(existing)
        _abode_status['last_backfill_page1'] = page1_raw
        _abode_status['last_backfill_skipped'] = skipped
        _abode_status['last_backfill_pages'] = page
        print(f'Abode backfill: {inserted} inserted, {len(rows_to_insert)} collected, {skipped} skipped ({days} days, {page} pages)')
        print(f'  Dates: {dict(date_counts)}')
        return inserted
    except Exception as exc:
        _abode_status['last_backfill_time'] = int(time.time())
        _abode_status['last_backfill_inserted'] = 0
        _abode_status['last_backfill_error'] = str(exc)
        print(f'Abode backfill error: {exc}')
        _log_system_error('abode', 'Backfill error', str(exc))
        return 0


def start_abode_listener():
    """Start abodepy websocket listener in a daemon thread."""
    global _abode_instance

    def _run():
        global _abode_instance
        try:
            from abodepy import Abode
        except ImportError:
            _abode_status['state'] = 'error'
            _abode_status['last_error'] = 'abodepy not installed'
            print('Abode: abodepy not installed — run: py -m pip install abodepy')
            return

        retry_delay = 60
        while True:
            # Check the toggle each iteration so we react to enable/disable
            if not get_setting_bool('abode_enabled', True):
                if _abode_status['state'] != 'disabled':
                    # Tear down existing connection if we were running
                    if _abode_instance is not None:
                        try:
                            _abode_instance.events.stop()
                        except Exception:
                            pass
                        _abode_instance = None
                    _abode_status['state'] = 'disabled'
                    print('Abode: disabled in settings')
                time.sleep(30)  # re-check toggle every 30s
                continue

            # Enabled and connected — safety net backfill every 2 hours
            if _abode_instance is not None:
                last_bf = _abode_status.get('last_backfill_time') or 0
                if time.time() - last_bf >= 7200:  # every 2 hours
                    try:
                        abode_backfill(_abode_instance, days=1)
                    except Exception as exc:
                        print(f'Abode periodic backfill error: {exc}')
                time.sleep(60)
                continue

            try:
                _abode_status['state'] = 'connecting'
                print('Abode: connecting…')
                abode = Abode(username=ABODE_EMAIL, password=ABODE_PASSWORD,
                              auto_login=True, get_devices=True)
                _abode_instance = abode
                _abode_status['state'] = 'connected'
                retry_delay = 60  # reset backoff on successful connect

                import abodepy.helpers.timeline as tl
                abode.events.add_timeline_callback(tl.ALL, _abode_write_event)
                abode.events.start()
                print('Abode: listener started.')

                # Backfill missed events on connect
                threading.Thread(
                    target=abode_backfill, args=(abode,), daemon=True
                ).start()

            except Exception as exc:
                _abode_instance = None
                _abode_status['state'] = 'error'
                _abode_status['last_error'] = str(exc)
                _abode_status['last_error_time'] = int(time.time())
                _abode_status['reconnect_count'] += 1
                is_429 = '429' in str(exc)
                print(f'Abode listener error: {exc} — retrying in {retry_delay}s')
                _log_system_error('abode', 'Listener error', f'{exc} — retrying in {retry_delay}s')
                time.sleep(retry_delay)
                # Back off aggressively on rate-limit; cap at 10 min
                retry_delay = min(retry_delay * 2 if is_429 else retry_delay, 600)

    t = threading.Thread(target=_run, daemon=True, name='abode-listener')
    t.start()


# ── Nest / Google SDM ────────────────────────────────────────────────────────
_nest_event_ts: float = 0.0
_nest_devices: dict = {}        # device_path -> display_name cache
_nest_devices_ts: float = 0.0
_NEST_DEVICE_CACHE_TTL = 3600   # 1 hour

NEST_EVENT_TYPE_MAP = {
    'sdm.devices.events.CameraMotion.Motion':  'motion_detected',
    'sdm.devices.events.CameraPerson.Person':  'person_detected',
    'sdm.devices.events.DoorbellChime.Chime':  'doorbell_press',
}

NEST_EVENT_TITLE_MAP = {
    'motion_detected': 'Motion Detected',
    'person_detected': 'Person Detected',
    'doorbell_press':  'Doorbell Pressed',
}


def _nest_ensure_token() -> str | None:
    """Return a valid access token, refreshing if expired. Returns None on failure."""
    access_token = get_setting('nest_access_token', '')
    expiry = get_setting_int('nest_token_expiry', 0)

    if access_token and time.time() < expiry:
        return access_token

    refresh_token = get_setting('nest_refresh_token', '')
    if not refresh_token:
        return None

    client_id     = get_setting('nest_client_id', '')
    client_secret = get_setting('nest_client_secret', '')

    try:
        resp = _requests.post('https://oauth2.googleapis.com/token', data={
            'client_id':     client_id,
            'client_secret': client_secret,
            'refresh_token': refresh_token,
            'grant_type':    'refresh_token',
        }, timeout=15)
        resp.raise_for_status()
        data = resp.json()

        new_token  = data['access_token']
        new_expiry = str(int(time.time()) + data.get('expires_in', 3600) - 60)

        with sqlite3.connect(DB_PATH) as c:
            c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                      ('nest_access_token', new_token))
            c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                      ('nest_token_expiry', new_expiry))
            c.commit()
        return new_token

    except Exception as exc:
        print(f'Nest token refresh error: {exc}')
        _log_system_error('nest', 'Token refresh failed', str(exc))
        return None


def _nest_get_device_name(device_path: str, token: str) -> str:
    """Return human-readable device name, using cache. Falls back to device ID fragment."""
    global _nest_devices, _nest_devices_ts

    if not _nest_devices or time.time() - _nest_devices_ts > _NEST_DEVICE_CACHE_TTL:
        project_id = get_setting('nest_project_id', '')
        try:
            resp = _requests.get(
                f'https://smartdevicemanagement.googleapis.com/v1/enterprises/{project_id}/devices',
                headers={'Authorization': f'Bearer {token}'},
                timeout=15,
            )
            resp.raise_for_status()
            devices = resp.json().get('devices', [])
            _nest_devices = {}
            for d in devices:
                name = d.get('name', '')
                traits = d.get('traits', {})
                custom = traits.get('sdm.devices.traits.Info', {}).get('customName', '')
                dev_type = d.get('type', '').rsplit('.', 1)[-1]
                display = custom or dev_type or 'Unknown'
                _nest_devices[name] = display
            _nest_devices_ts = time.time()
        except Exception as exc:
            print(f'Nest device list error: {exc}')

    if device_path in _nest_devices:
        return _nest_devices[device_path]
    return device_path.rsplit('/', 1)[-1][:6] if device_path else 'Unknown'


def fetch_nest_events() -> int:
    """Pull Nest camera/doorbell events from Pub/Sub and log new ones. Returns insert count."""
    import base64 as _b64

    if not get_setting_bool('nest_enabled', False):
        return 0

    subscription = get_setting('nest_pubsub_subscription', '')
    if not subscription:
        return 0

    token = _nest_ensure_token()
    if not token:
        return 0

    inserted = 0
    try:
        resp = _requests.post(
            f'https://pubsub.googleapis.com/v1/{subscription}:pull',
            headers={'Authorization': f'Bearer {token}'},
            json={'maxMessages': 50, 'returnImmediately': True},
            timeout=30,
        )
        resp.raise_for_status()
        messages = resp.json().get('receivedMessages', [])

        if not messages:
            return 0

        rows = []
        ack_ids = []

        for msg in messages:
            ack_ids.append(msg['ackId'])
            try:
                raw = _b64.b64decode(msg['message']['data']).decode('utf-8')
                payload = json.loads(raw)

                resource_update = payload.get('resourceUpdate', {})
                device_path = resource_update.get('name', '')
                events = resource_update.get('events', {})

                ts_str = payload.get('timestamp', '')
                if ts_str:
                    dt = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                    ts = int(dt.timestamp())
                else:
                    ts = int(time.time())

                event_id = payload.get('eventId', '')
                device_name = _nest_get_device_name(device_path, token)

                for sdm_event_key in events:
                    event_type = NEST_EVENT_TYPE_MAP.get(sdm_event_key)
                    if not event_type:
                        continue

                    title = f'{device_name}: {NEST_EVENT_TITLE_MAP.get(event_type, event_type)}'
                    session_id = events[sdm_event_key].get('eventSessionId', '')
                    detail = f'device: {device_name}  eventId: {event_id}  session: {session_id}'
                    rows.append((ts, 'nest', event_type, title, detail, 'info', 'live'))

            except Exception:
                continue

        # Batch deduplicate (same pattern as Rachio)
        if rows:
            with sqlite3.connect(DB_PATH, timeout=30) as c:
                existing = set(
                    c.execute(
                        'SELECT ts, title FROM event_log WHERE system = ?', ('nest',)
                    ).fetchall()
                )
                for row in rows:
                    ts_val, sys_, evt, title, detail, result, source = row
                    if (ts_val, title) not in existing:
                        c.execute(
                            'INSERT INTO event_log '
                            '(ts, system, event_type, title, detail, result, source) '
                            'VALUES (?,?,?,?,?,?,?)', row)
                        existing.add((ts_val, title))
                        inserted += 1

        # Always ack ALL messages to prevent redelivery
        if ack_ids:
            _requests.post(
                f'https://pubsub.googleapis.com/v1/{subscription}:acknowledge',
                headers={'Authorization': f'Bearer {token}'},
                json={'ackIds': ack_ids},
                timeout=15,
            )

        if inserted:
            print(f'Nest events: logged {inserted} new events')

    except _requests.exceptions.Timeout:
        print('Nest poll: timeout (no events)')
    except Exception as exc:
        print(f'Nest event poll error: {exc}')
        _log_system_error('nest', 'Event poll error', str(exc))

    return inserted


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
    _ai_cache['ts'] = 0  # invalidate AI insights cache
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
    _ai_cache['ts'] = 0  # invalidate AI insights cache
    return jsonify(_rule_row_to_dict(row, cond_list))


@app.route('/api/rules/<int:rid>', methods=['DELETE'])
def api_rules_delete(rid):
    with sqlite3.connect(DB_PATH) as c:
        c.execute('PRAGMA foreign_keys = ON')
        c.execute('DELETE FROM rules WHERE id=?', (rid,))
    _ai_cache['ts'] = 0
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


# ── Rules Insights engine ────────────────────────────────────────────────────
_HOLIDAY_NAMES = {
    (1, 1): "New Year's Day", (7, 4): 'Independence Day',
    (11, 11): "Veterans Day", (12, 25): 'Christmas Day',
}


def _holiday_name(d):
    """Return display name for an SDG&E holiday date."""
    key = (d.month, d.day)
    if key in _HOLIDAY_NAMES:
        return _HOLIDAY_NAMES[key]
    if d.month == 2 and d.weekday() == 0 and 15 <= d.day <= 21:
        return "Presidents' Day"
    if d.month == 5 and d.weekday() == 0 and d.day >= 25:
        return 'Memorial Day'
    if d.month == 9 and d.weekday() == 0 and d.day <= 7:
        return 'Labor Day'
    if d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28:
        return 'Thanksgiving'
    return 'SDG&E Holiday'


def _analyze_rules(rules, rates, holidays):
    """Deterministic analysis of Powerwall rules against EV-TOU-2 rate schedule."""
    insights = []
    now = datetime.now()
    today = now.date()

    enabled = [r for r in rules if r.get('enabled')]
    sop_winter = rates.get('winter_super_off_peak', 0.25)
    sop_summer = rates.get('summer_super_off_peak', 0.26)
    on_summer  = rates.get('summer_on_peak', 0.78)
    on_winter  = rates.get('winter_on_peak', 0.51)

    # ── 1. Grid charging window duration ─────────────────────────────────────
    charge_on  = [r for r in enabled if r.get('grid_charging') is True]
    charge_off = [r for r in enabled if r.get('grid_charging') is False]

    if charge_on:
        for on_r in charge_on:
            on_min  = on_r['hour'] * 60 + on_r['minute']
            on_days = set(on_r['days'])
            best_off = None
            for off_r in charge_off:
                off_min = off_r['hour'] * 60 + off_r['minute']
                if off_min > on_min and on_days & set(off_r['days']):
                    if best_off is None or off_min < best_off['hour'] * 60 + best_off['minute']:
                        best_off = off_r
            if best_off:
                window = (best_off['hour'] * 60 + best_off['minute']) - on_min
                if window < 180:
                    kwh = round(window * 5 / 60, 1)
                    insights.append({
                        'severity': 'warning',
                        'title':  f'Grid charging window is only {window} minutes',
                        'detail': (
                            f'"{on_r["name"]}" charges from {on_r["hour"]}:{on_r["minute"]:02d} '
                            f'until "{best_off["name"]}" stops it at {best_off["hour"]}:{best_off["minute"]:02d}. '
                            f'At ~5 kW that adds only ~{kwh} kWh to a 40.5 kWh battery bank (3× Powerwall 2). '
                            f'Super off-peak runs midnight\u20136 AM at ${sop_winter:.3f}/kWh.'
                        ),
                        'action': 'Start grid charging earlier (midnight or 1 AM) to fully charge at super off-peak rates.',
                        'rule_id': on_r['id'],
                    })
    else:
        insights.append({
            'severity': 'suggestion',
            'title':  'No grid charging rules configured',
            'detail': (
                f'Charging from grid during super off-peak (${sop_winter:.3f}/kWh) offsets '
                f'on-peak usage (${on_summer:.3f}/kWh) \u2014 a {on_summer / sop_winter:.1f}x saving.'
            ),
            'action': 'Add a rule to enable grid charging during midnight\u20136 AM (super off-peak).',
        })

    # ── 2. Sunday grid charging gap ──────────────────────────────────────────
    if charge_on:
        charge_days = set()
        for r in charge_on:
            charge_days.update(r['days'])
        if 6 not in charge_days:  # 6 = Sunday
            insights.append({
                'severity': 'suggestion',
                'title':  'Sunday excluded from grid charging',
                'detail': (
                    'Grid charging rules cover Mon\u2013Sat but skip Sunday. '
                    'The Powerwall may not be topped off for Sunday\u2019s on-peak hours.'
                ),
                'action': 'Add Sunday to an existing grid charging rule or create a Sunday-specific rule.',
            })

    # ── 3. Mar/Apr weekday super off-peak window ────────────────────────────
    mar_apr_tbc = [r for r in enabled
                   if r.get('mode') == 'autonomous'
                   and {3, 4} & set(r['months'])
                   and {0, 1, 2, 3, 4} & set(r['days'])
                   and 10 <= r['hour'] < 14]
    if not mar_apr_tbc:
        insights.append({
            'severity': 'suggestion',
            'title':  'Mar/Apr weekday super off-peak window not utilized',
            'detail': (
                'EV-TOU-2 has a bonus super off-peak window 10 AM\u20132 PM on weekdays in March & April '
                f'(${sop_winter:.3f}/kWh). Switching to Time-Based Control enables grid charging.'
            ),
            'action': 'Create rules: Time-Based Control at 10 AM and Self-Powered at 2 PM, weekdays, Mar\u2013Apr.',
        })

    # ── 4. No rule at 4 PM on-peak boundary ─────────────────────────────────
    at_4pm = [r for r in enabled if r['hour'] == 16 and r['minute'] <= 5]
    if not at_4pm:
        insights.append({
            'severity': 'suggestion',
            'title':  'No rule at 4 PM on-peak boundary',
            'detail': (
                f'On-peak starts at 4 PM (${on_summer:.3f}/kWh summer, ${on_winter:.3f}/kWh winter). '
                f'No rule adjusts Powerwall settings at this critical transition.'
            ),
            'action': 'Consider a 4 PM rule to set Self-Powered mode and verify reserve covers the 4\u20139 PM peak.',
        })

    # ── 5. Battery export starts late (season-aware) ────────────────────────
    # Summer sunset ~7:30-8 PM — starting at 7 PM+ means missing 3+ on-peak hours
    # Winter sunset ~5-5:30 PM — starting at 6 PM+ means missing 2+ on-peak hours
    summer = {6, 7, 8, 9, 10}
    for season_label, season_months, late_hour, rate_val in [
        ('summer', summer, 19, on_summer),
        ('winter', {1, 2, 3, 4, 5, 11, 12}, 18, on_winter),
    ]:
        season_export = [r for r in enabled
                         if r.get('grid_export') == 'battery_ok'
                         and season_months & set(r['months'])
                         and {0, 1, 2, 3, 4} & set(r['days'])]
        for r in season_export:
            if r['hour'] >= late_hour:
                missed = r['hour'] - 16
                insights.append({
                    'severity': 'suggestion',
                    'title':  f'Battery export starts at {r["hour"]}:{r["minute"]:02d} \u2014 on-peak begins 4 PM',
                    'detail': (
                        f'"{r["name"]}" enables battery export {missed}+ hours after on-peak starts. '
                        f'On-peak runs 4\u20139 PM at ${rate_val:.3f}/kWh ({season_label}).'
                    ),
                    'action': f'Consider starting export earlier to capture more {season_label} on-peak value.',
                    'rule_id': r['id'],
                })

    # ── 6. November in summer export rules ───────────────────────────────────
    nov_export = [r for r in enabled
                  if r.get('grid_export') == 'battery_ok'
                  and 11 in set(r['months'])
                  and summer & set(r['months'])]
    if nov_export:
        insights.append({
            'severity': 'info',
            'title':  'November grouped with summer in export rules',
            'detail': (
                f'SDG&E classifies November as winter (on-peak ${on_winter:.3f} vs summer ${on_summer:.3f}/kWh). '
                f'Export is still profitable but sunset is earlier \u2014 less solar by 7 PM.'
            ),
            'action': 'Consider separate November export rules with earlier timing for shorter daylight.',
        })

    # ── 7. Upcoming weekday holidays ─────────────────────────────────────────
    upcoming = sorted(d for d in holidays if today <= d <= today + timedelta(days=90))
    weekday_holidays = [d for d in upcoming if d.weekday() < 5]

    for hd in weekday_holidays:
        name = _holiday_name(hd)
        day_name = hd.strftime('%A')
        insights.append({
            'severity':     'warning',
            'title':        f'{name} ({hd.strftime("%b %d")}) falls on a {day_name}',
            'detail': (
                f'{name} uses the weekend/holiday TOU schedule: super off-peak midnight\u20132 PM, '
                f'on-peak 4\u20139 PM. Your weekday rules will fire but assume the regular schedule '
                f'(super off-peak only midnight\u20136 AM).'
            ),
            'action': (
                f'Disable weekday rules for {hd.strftime("%b %d")} or create holiday rules '
                f'that leverage the extended super off-peak window (midnight\u20132 PM).'
            ),
            'holiday_date': hd.isoformat(),
        })

    # ── 8. Holiday calendar health ───────────────────────────────────────────
    if not holidays:
        insights.append({
            'severity': 'warning',
            'title':  'No holiday dates configured',
            'detail': (
                'SDG&E holidays use a different TOU schedule (super off-peak midnight\u20132 PM). '
                'Without holiday dates, rules can\u2019t account for these changes.'
            ),
            'action': 'Refresh holiday dates via Settings.',
        })
    elif all(d < today for d in holidays):
        insights.append({
            'severity': 'warning',
            'title':  'All holiday dates have passed',
            'detail': (
                f'The last holiday was {max(holidays).isoformat()}. '
                f'Holiday dates need refreshing for upcoming holidays.'
            ),
            'action': 'Refresh holiday dates via Settings or wait for automatic refresh.',
        })

    # TOU schedule staleness check
    last_verified = get_setting('tou_periods_last_verified', '')
    try:
        stale = not last_verified or (date.today() - date.fromisoformat(last_verified)).days > 180
    except ValueError:
        stale = True
    if stale:
        insights.append({
            'severity': 'info',
            'title':  'TOU schedule not verified in 6+ months',
            'detail': (
                'The on-peak, off-peak, and super off-peak time windows are configured '
                'based on SDG&E EV-TOU-2 tariff Sheet 3. SDG&E occasionally adjusts these '
                'hours. Last verified: ' + (last_verified or 'never') + '.'
            ),
            'action': 'Check SDG&E EV-TOU-2 tariff schedule and update tou_periods_last_verified in Settings.',
        })

    return insights


@app.route('/api/rules/insights')
def api_rules_insights():
    with sqlite3.connect(DB_PATH) as c:
        rules = _load_all_rules(c)
    rates    = load_rates() or {}
    holidays = SDGE_HOLIDAYS
    insights = _analyze_rules(rules, rates, holidays)
    return jsonify(insights)


# ── Gemini AI Insights ───────────────────────────────────────────────────────
_GEMINI_SYSTEM = """\
You are an energy optimization advisor for a specific home in San Diego, CA.

## System
- 3× Tesla Powerwall 2 (40.5 kWh total usable capacity, ~90% round-trip efficiency)
- Rooftop solar — production varies seasonally (San Diego: ~10–14 kWh/day winter,
  ~22–30 kWh/day summer due to longer daylight hours June–October)
- SDG&E EV-TOU-2 rate plan — use the EXACT rate values from the rates object in
  the provided data, never guess or use generic values
- Annual true-up in January
- IMPORTANT: SDG&E does NOT pay out excess true-up credits — they pay close to nothing.
  The homeowner's goal is to land near net-zero with a small credit buffer ($100–$500).
  Overproducing credits is wasted energy. The current rules were intentionally tuned to
  be conservative on exports, but the additional grid import from overnight charging
  wasn't fully accounted for, resulting in the current projected deficit.
  Recommendations should close that gap without overshooting into excessive credits.
- Location: San Diego — mild winters, long sunny summers. June–October daylight runs
  ~13–14 hours vs ~10 hours in winter, meaning significantly more solar production
  and longer afternoon export windows

## Data conventions — read carefully
- battery_w: positive = charging, negative = discharging
- grid_w: positive = importing from grid, negative = exporting to grid
- on_peak_cost / off_peak_cost / super_off_peak_cost: signed net values —
  negative = net credit earned
- rule_based_insights: deterministic gaps already identified by a separate analysis
  engine — do NOT repeat these findings, go deeper or synthesize across them

## Rate structure
Use the exact summer_on_peak, summer_off_peak, summer_super_off_peak, winter_on_peak,
winter_off_peak, winter_super_off_peak values from the rates object provided.

Key EV-TOU-2 nuances:
- On-peak (4–9 PM) applies EVERY day including weekends and holidays — no exemptions
- Super off-peak bonus window: 10 AM–2 PM weekdays in March and April only
- Holidays follow weekend schedule: super off-peak all day except 4–9 PM on-peak
- November is WINTER season despite being adjacent to summer export months

## How to read the rules
The rules array defines the automation schedule. Each rule fires at hour:minute on
the specified days (0=Mon..6=Sun) and months (1=Jan..12=Dec). Rules change only the
fields they specify — null fields carry forward from the previous rule.

Key fields: mode (self_consumption | autonomous | backup), reserve (battery floor %),
grid_charging (true/false), grid_export (battery_ok | pv_only).

IMPORTANT: grid_export = battery_ok means ACTIVE continuous battery discharge to grid
at up to ~15 kW combined (3× Powerwall). At 1% reserve, nearly the full 40.5 kWh
is available to export. This is NOT passive solar overflow.

The design principles behind the rules (read the actual rules data for specific times):

- The system is tuned so the home NEVER buys expensive grid power. Every kWh comes
  from either super off-peak grid (cheapest), solar (free), or battery (charged from
  cheap sources).
- Overnight: home runs on grid at super off-peak. There is NO solar before ~6:30 AM
  in San Diego. Do NOT describe any pre-dawn hours as "solar time."
- Daytime (Self-Powered mode): solar does the heavy lifting — powers home, charges
  battery to 100%, exports excess. Grid is barely touched.
- Battery export starts around sunset when solar drops off. Before sunset, solar is
  still producing and covering everything for free.
- Export stops before the battery is fully drained — enough reserve is kept to power
  the home through to midnight, avoiding expensive grid imports. At midnight, grid
  takes over again at super off-peak.
- If the true-up shows a deficit, it means super off-peak imports exceed total exports.
  The fix is to INCREASE EXPORTS, not decrease imports — imports are already at the
  cheapest rates possible.
- When evaluating export timing: once the battery reaches 100% and solar is still
  producing, the battery is idle. Starting active battery export at that point would
  not reduce solar benefit because solar is already covering the home. Analyze the
  hourly readings to find when this window occurs and whether earlier export could
  close the deficit gap. Show the tradeoff with actual numbers.

## Prior year data — critical for projections
The `prior_year_monthly` array contains ACTUAL monthly performance from the previous
year. This is real measured data from the same house, same solar panels, same location.
It reflects real San Diego solar production, weather patterns, and consumption by month.

IMPORTANT: The prior year used a DIFFERENT automation strategy (Time-Based Control —
Tesla's automatic algorithm). The current year uses custom rules that deliberately
import more during super off-peak (grid charging) to build up battery for on-peak
export. This means:
- Winter months will show HIGHER imports in the current year (by design)
- Summer months should show HIGHER export credits (the payoff)
- Do NOT extrapolate from the most recent month to project summer — summer and
  winter behave fundamentally differently in San Diego

## Your analysis — cover all four areas:

**1. True-up trajectory**

The `trueup_projection_table` field contains a PRE-RENDERED markdown table.
These numbers were computed server-side with exact arithmetic.

The table is displayed separately in the UI — DO NOT reproduce it in your response.
DO NOT output a markdown table of the projection numbers.
Instead, reference the numbers directly in your analysis (e.g., "June shows -$234 credit").

Analyze:
- Is the full-year net positive (owe SDG&E) or negative (credit)?
- Which months drive the most credit? Which are the biggest costs?
- Is the current trajectory on track for net-zero or net-credit at true-up?
- What is the biggest risk to the projection?

The table MUST appear before any rule change recommendations.

**2. Seasonal transition impact**
Based on the current season and when the next season starts:
- Walk through what happens on a typical day in the upcoming season based on the
  current rules — what mode is the system in at each key time of day?
- How will the season shift affect solar production, electricity rates, and the
  opportunity to sell power back to the grid?
- What rule changes should be made BEFORE the transition?
- Address the battery export window timing given longer summer daylight hours

**3. Rule optimization**
Review the current rules against actual usage patterns. Focus on:
- Is the battery sitting fully charged during the expensive on-peak hours (4-9 PM)
  without actively exporting? How much money is being left on the table, and what
  would it cost in overnight charging to make up for earlier export?
- Is the overnight grid charging window long enough to fully recharge the battery?
  If not, how much longer does it need to be?
- Are there months where battery export rules are active but shouldn't be (like
  November, which is actually a winter month)?
- Are any days of the week missing from the export schedule?
For each suggestion, estimate the dollar impact per month using actual rates.

**4. Credit maximization**
Looking at the daily cost data:
- On days where little or no on-peak credit was earned, what likely went wrong?
  (cloudy day? battery not full? no export rule active?)
- Are there consistent patterns between high-credit days and low-credit days?
- Given the 40.5 kWh battery capacity that can actively discharge to the grid,
  where are the biggest untapped opportunities to earn more credits?

## Data quality awareness
The `data_quality` object tells you how reliable each projection input is:
- `actual_months`: months with real measured data — treat these as ground truth
- `projected_months`: months estimated from prior year patterns — flag as projections
- `period_weights_source`: per-season, tells you if TOU period distributions are from
  'current_year' (measured), 'prior_year' (historical), or 'default' (hardcoded estimate).
  If 'default', explicitly note that the import/export rate mix is estimated, not measured.
- `optimized_export_source`: 'actual_months' means the optimized scenario uses real export
  data from months with active rules. 'cross_season_estimate' or 'capacity_estimate' means
  it's hypothetical — frame it as "potential" or "estimated" savings, not guaranteed.
- `prior_year_daily_costs`: false means no historical baseline exists — projections for
  future months are less reliable. Note this limitation clearly.

When data sources are estimated rather than measured, hedge your language accordingly.

## Format
Use markdown. Use the actual rate values and cost figures from the data — no generic estimates.
Do not repeat findings already listed in rule_based_insights.

NEVER use JSON field names from the data in your output. The data contains technical
keys like on_peak_kwh, solar_w, grid_w, battery_pct, super_off_peak_kwh, import_kwh,
export_kwh, battery_w, home_w, etc. These are for your analysis only — always translate
to natural language in your response:
  on_peak_kwh → "on-peak export" or "on-peak usage"
  solar_w → "solar production"
  grid_w → "grid import" or "grid export"
  battery_w → "battery charging" or "battery discharging"
  battery_pct → "battery level"
  super_off_peak_kwh → "super off-peak usage"
  import_kwh → "grid imports"
  export_kwh → "grid exports"
  grid_export → "battery export to grid"
  self_consumption → "Self-Powered mode"
  autonomous → "Time-Based Control mode"
If the user sees a field name like `on_peak_kwh` or `solar_w` in your response, that
is a failure. Every technical term must be translated to plain English.

CRITICAL — Write for a homeowner, not an engineer:
- Use natural language for days: "Monday through Friday" or "Weekdays" or "Every day" — never arrays like [0,1,2,3,4].
- Use natural language for months: "June through October" — never arrays like [6,7,8,9,10].
- Use 12-hour time: "5:00 PM" — never "hour: 17" or "19:15".
- Instead of "daily_costs" or "hourly_readings", say "your daily cost data"
  or "your recent power readings".
- For rule recommendations: explain WHY the change helps and the expected dollar impact.
  Do NOT walk the user through how to create or edit a rule — they know how.
  Example: "Starting battery export at 5 PM instead of 7:15 PM would capture 2 extra hours
  of on-peak rates, adding approximately $X per month in credits toward net-zero."
- Never output JSON, arrays, code blocks, or raw data field names in recommendations.
- Use dollar amounts to justify every recommendation.

Keep the total response focused — depth over breadth.

After all rule recommendations, end with:

**5. Projected impact**
A pre-calculated "After Changes" projection table is displayed in the UI alongside
the baseline. It models adding winter on-peak battery export for months that currently
have no export rules. These numbers are computed server-side — DO NOT reproduce them.

Analyze:
- Does the optimized projection bring the full-year net into the $100–$500 credit range?
- If it overshoots, suggest scaling back (fewer months, higher reserve)
- If it falls short, suggest what additional changes could help
- Compare the baseline total vs optimized total and state the improvement\
"""


def _aggregate_monthly_power(c, year):
    """Aggregate solar_w and home_w from readings into monthly kWh."""
    result = {}
    for month in range(1, 13):
        start = int(datetime(year, month, 1).timestamp())
        end = int(datetime(year + (1 if month == 12 else 0),
                           (month % 12) + 1, 1).timestamp())
        row = c.execute(
            'SELECT COUNT(*), SUM(solar_w), SUM(home_w), '
            '       (MAX(timestamp) - MIN(timestamp)) / NULLIF(COUNT(*) - 1.0, 0) '
            'FROM readings WHERE timestamp >= ? AND timestamp < ? AND solar_w IS NOT NULL',
            (start, end)
        ).fetchone()
        count = row[0] or 0
        if count < 100:
            result[month] = {'solar_kwh': 0, 'home_kwh': 0}
            continue
        avg_interval_h = (row[3] or 300) / 3600.0
        result[month] = {
            'solar_kwh': round((row[1] or 0) * avg_interval_h / 1000, 1),
            'home_kwh': round((row[2] or 0) * avg_interval_h / 1000, 1),
        }
    return result


_PERIODS = ('on_peak', 'off_peak', 'super_off_peak')
_DEFAULT_WEIGHTS = {
    'winter': {
        'import': {'on_peak': 0.05, 'off_peak': 0.25, 'super_off_peak': 0.70},
        'export': {'on_peak': 0.30, 'off_peak': 0.50, 'super_off_peak': 0.20},
    },
    'summer': {
        'import': {'on_peak': 0.05, 'off_peak': 0.25, 'super_off_peak': 0.70},
        'export': {'on_peak': 0.55, 'off_peak': 0.40, 'super_off_peak': 0.05},
    },
}


def _compute_period_weights(c, year) -> dict:
    """Derive actual TOU period weights from daily_costs per-period data.

    Returns dict keyed by season ('winter'/'summer'), each containing
    'import' and 'export' sub-dicts with fractional weights per period.
    Falls back to _DEFAULT_WEIGHTS for seasons with insufficient data.
    """
    rows = c.execute(
        'SELECT date, on_peak_kwh, off_peak_kwh, super_off_peak_kwh '
        'FROM daily_costs WHERE date >= ? AND date < ?',
        (f'{year}-01-01', f'{year + 1}-01-01')
    ).fetchall()

    # Accumulate import/export kWh by season and period.
    # We split on kWh sign, not cost sign. This is intentional — weights are
    # multiplied by rates to get avg rate, so kWh gives the correct distribution.
    # Using cost would double-count rate differences between periods.
    buckets = {
        'winter': {'import': {p: 0.0 for p in _PERIODS}, 'export': {p: 0.0 for p in _PERIODS}},
        'summer': {'import': {p: 0.0 for p in _PERIODS}, 'export': {p: 0.0 for p in _PERIODS}},
    }
    for d, on_kwh, off_kwh, sop_kwh in rows:
        month = int(d[5:7])
        season = 'summer' if month in (6, 7, 8, 9, 10) else 'winter'
        for period, val in zip(_PERIODS, (on_kwh or 0, off_kwh or 0, sop_kwh or 0)):
            if val > 0:
                buckets[season]['import'][period] += val
            elif val < 0:
                buckets[season]['export'][period] += abs(val)

    # Normalize to fractions; fall back to defaults if no data
    result = {}
    for season in ('winter', 'summer'):
        result[season] = {}
        for direction in ('import', 'export'):
            totals = buckets[season][direction]
            total = sum(totals.values())
            if total > 0:
                result[season][direction] = {p: totals[p] / total for p in _PERIODS}
            else:
                result[season][direction] = dict(_DEFAULT_WEIGHTS[season][direction])
    return result


def _render_projection_table(projection):
    """Render a projection list as a markdown table."""
    lines = ['| Month | Label | Import kWh | Export kWh | Import Cost | Export Credit | Base Charge | Net |',
             '|---|---|---|---|---|---|---|---|']
    t_ikwh = t_ekwh = t_icost = t_ecred = t_base = t_net = 0
    for p in projection:
        lines.append(f'| {p["month"]} | {p["label"]} | {p["import_kwh"]:.1f} | {p["export_kwh"]:.1f} '
                     f'| ${p["import_cost"]:.2f} | ${p["export_credit"]:.2f} '
                     f'| ${p["base_charge"]:.2f} | ${p["net"]:.2f} |')
        t_ikwh += p['import_kwh']; t_ekwh += p['export_kwh']
        t_icost += p['import_cost']; t_ecred += p['export_credit']
        t_base += p['base_charge']; t_net += p['net']
    lines.append(f'| **Total** | | **{t_ikwh:.1f}** | **{t_ekwh:.1f}** '
                 f'| **${t_icost:.2f}** | **${t_ecred:.2f}** '
                 f'| **${t_base:.2f}** | **${t_net:.2f}** |')
    return '\n'.join(lines)


def _build_trueup_projection(c, rates, base_charge_per_day):
    """Pre-calculate baseline + optimized projection tables using solar-based approach."""
    import calendar
    now = datetime.now()
    this_year = now.year
    prior_year = this_year - 1
    CAPACITY = 40.5
    EFFICIENCY = 0.90

    # ── Gather data ──────────────────────────────────────────────────────────
    # Current year actuals from daily_costs
    cy_rows = c.execute(
        'SELECT substr(date,1,7) as m, SUM(import_kwh), SUM(export_kwh), '
        '       SUM(import_cost), SUM(export_credit), COUNT(date) '
        'FROM daily_costs WHERE date >= ? AND date < ? '
        'GROUP BY substr(date,1,7) ORDER BY 1',
        (f'{this_year}-01-01', f'{this_year + 1}-01-01')
    ).fetchall()
    cy_data = {}
    for row in cy_rows:
        cy_data[row[0]] = {
            'import_kwh': row[1] or 0, 'export_kwh': row[2] or 0,
            'import_cost': row[3] or 0, 'export_credit': row[4] or 0,
            'days': row[5],
        }

    # Prior year solar + home from readings (for context)
    py_power = _aggregate_monthly_power(c, prior_year)
    cy_power = _aggregate_monthly_power(c, this_year)

    # Prior year monthly import/export from daily_costs (for projection baseline)
    py_dc_rows = c.execute(
        'SELECT substr(date,1,7) as m, SUM(import_kwh), SUM(export_kwh) '
        'FROM daily_costs WHERE date >= ? AND date < ? '
        'GROUP BY substr(date,1,7) ORDER BY 1',
        (f'{prior_year}-01-01', f'{prior_year + 1}-01-01')
    ).fetchall()
    py_dc_data = {}
    for row in py_dc_rows:
        py_dc_data[f'{prior_year}-{row[0][5:7]}'] = {
            'import_kwh': row[1] or 0, 'export_kwh': row[2] or 0,
        }

    # Home consumption ratio — Q1 is winter, so ratio applies best to winter months.
    # Summer home usage is more sun-driven (AC, etc.) so cap the summer ratio at 1.1×
    cy_q1_home = sum(cy_power.get(m, {}).get('home_kwh', 0) for m in [1, 2, 3])
    py_q1_home = sum(py_power.get(m, {}).get('home_kwh', 0) for m in [1, 2, 3])
    winter_home_ratio = cy_q1_home / py_q1_home if py_q1_home > 0 else 1.0
    summer_home_ratio = min(winter_home_ratio, 1.10)  # cap summer at 10% increase

    # Rate periods
    rate_periods = c.execute(
        'SELECT effective_date, end_date, '
        '       summer_on_peak, summer_off_peak, summer_super_off_peak, '
        '       winter_on_peak, winter_off_peak, winter_super_off_peak, '
        '       COALESCE(base_services_charge_per_day, 0) '
        'FROM rate_history ORDER BY effective_date'
    ).fetchall()

    # Data-derived TOU period weights (current year, with prior year fallback)
    cy_weights = _compute_period_weights(c, this_year)
    py_weights = _compute_period_weights(c, prior_year)
    # For each season: prefer current year if it has real data, else prior year
    period_weights = {}
    weights_source = {}  # track source per season for data_quality
    for season in ('winter', 'summer'):
        period_weights[season] = {}
        if cy_weights[season]['import'] != _DEFAULT_WEIGHTS[season]['import']:
            weights_source[season] = 'current_year'
        elif py_weights[season]['import'] != _DEFAULT_WEIGHTS[season]['import']:
            weights_source[season] = 'prior_year'
        else:
            weights_source[season] = 'default'
        for direction in ('import', 'export'):
            if cy_weights[season][direction] == _DEFAULT_WEIGHTS[season][direction]:
                period_weights[season][direction] = py_weights[season][direction]
            else:
                period_weights[season][direction] = cy_weights[season][direction]

    # ── Estimate grid charging + export from current rules ───────────────────
    # Read rules to determine: which months have grid charging? which have export?
    rules = c.execute(
        'SELECT months, hour, minute, grid_charging, grid_export, days '
        'FROM rules WHERE enabled = 1 ORDER BY hour, minute'
    ).fetchall()

    def _rule_charging_hours(month):
        """Estimate daily grid charging hours for a given month."""
        charge_start = charge_end = None
        for months_j, hour, minute, gc, ge, days_j in rules:
            months = json.loads(months_j) if isinstance(months_j, str) else months_j
            if month not in months:
                continue
            if gc == 1:  # grid_charging ON
                charge_start = hour + minute / 60.0
            elif gc == 0 and charge_start is not None:  # grid_charging OFF
                charge_end = hour + minute / 60.0
        if charge_start is not None and charge_end is not None and charge_end > charge_start:
            return charge_end - charge_start
        return 0

    def _rule_export_hours(month):
        """Check if any export rules exist for a given month and estimate the window.

        Returns >0 if any rule enables battery_ok for this month (used as boolean
        by callers). Does not weight by days-of-week — actual export kWh comes from
        daily_costs data which reflects real-world day coverage.
        """
        # Find earliest battery_ok start and latest pv_only end for this month
        earliest_start = None
        latest_end = None
        for months_j, hour, minute, gc, ge, days_j in rules:
            months = json.loads(months_j) if isinstance(months_j, str) else months_j
            if month not in months:
                continue
            t = hour + minute / 60.0
            if ge == 'battery_ok':
                if earliest_start is None or t < earliest_start:
                    earliest_start = t
            elif ge == 'pv_only' and earliest_start is not None:
                if latest_end is None or t > latest_end:
                    latest_end = t
        if earliest_start is not None and latest_end is None:
            latest_end = 21.0  # on-peak ends at 9 PM
        if earliest_start is not None and latest_end is not None and latest_end > earliest_start:
            return latest_end - earliest_start
        return 0

    # ── Build baseline projection ────────────────────────────────────────────
    has_prior_year_data = bool(py_dc_data)
    actual_months = []
    projected_months = []
    projection_basis = []
    baseline = []
    for month_num in range(1, 13):
        m_key = f'{this_year}-{month_num:02d}'
        days_in_month = calendar.monthrange(this_year, month_num)[1]
        is_summer = month_num in (6, 7, 8, 9, 10)
        # Look up base charge from rate_history for this month; fall back to passed-in value
        mid_date = f'{this_year}-{month_num:02d}-15'
        month_rates = _rate_for_date(rate_periods, mid_date)
        month_base_per_day = (month_rates or {}).get('base_services_charge_per_day', 0) or base_charge_per_day
        base_charge = round(month_base_per_day * days_in_month, 2)

        if m_key in cy_data:
            d = cy_data[m_key]
            # Use calendar days for complete past months; recorded days for current month
            is_current_month = (month_num == now.month and this_year == now.year)
            base_days = d['days'] if is_current_month else days_in_month
            baseline.append({
                'month': m_key, 'label': 'actual',
                'import_kwh': round(d['import_kwh'], 1),
                'export_kwh': round(d['export_kwh'], 1),
                'import_cost': round(d['import_cost'], 2),
                'export_credit': round(d['export_credit'], 2),
                'base_charge': round(month_base_per_day * base_days, 2),
                'net': round(d['import_cost'] - d['export_credit']
                             + month_base_per_day * base_days, 2),
            })
            actual_months.append(m_key)
        else:
            # Use prior year's actual import/export from daily_costs as the baseline
            # (captures real solar overflow behavior that monthly solar/home can't)
            py_key = f'{prior_year}-{month_num:02d}'
            py_dc = py_dc_data.get(py_key, {'import_kwh': 0, 'export_kwh': 0})

            # Scale imports: winter uses home_ratio (higher consumption + grid charging),
            # summer uses a modest ratio (solar covers most, grid charging similar)
            if is_summer:
                proj_imp_kwh = py_dc['import_kwh'] * summer_home_ratio
                proj_exp_kwh = py_dc['export_kwh']  # solar exports stay ~same
            else:
                proj_imp_kwh = py_dc['import_kwh'] * winter_home_ratio
                proj_exp_kwh = py_dc['export_kwh']  # unscaled — exports driven by solar + rules, not consumption

            # Apply current rates with data-derived TOU period weights
            r = month_rates or rates
            season = 'summer' if is_summer else 'winter'
            w = period_weights[season]

            avg_imp_rate = sum(r[f'{season}_{p}'] * w['import'][p] for p in _PERIODS)
            avg_exp_rate = sum(r[f'{season}_{p}'] * w['export'][p] for p in _PERIODS)

            proj_imp_cost = round(proj_imp_kwh * avg_imp_rate, 2)
            proj_exp_credit = round(proj_exp_kwh * avg_exp_rate, 2)
            net = round(proj_imp_cost - proj_exp_credit + base_charge, 2)

            baseline.append({
                'month': m_key, 'label': 'projected',
                'import_kwh': round(proj_imp_kwh, 1),
                'export_kwh': round(proj_exp_kwh, 1),
                'import_cost': proj_imp_cost,
                'export_credit': proj_exp_credit,
                'base_charge': base_charge,
                'net': net,
            })
            projected_months.append(m_key)
            projection_basis.append({
                'month': m_key,
                'basis': 'prior_year' if py_dc['import_kwh'] > 0 else 'no_data',
                'py_import_kwh': round(py_dc['import_kwh'], 1),
                'py_export_kwh': round(py_dc['export_kwh'], 1),
                'home_ratio': round(summer_home_ratio if is_summer else winter_home_ratio, 3),
                'weights_source': weights_source.get(season, 'default'),
            })

    # ── Compute actual daily export from months with export rules ──────────────
    # Query per-day on-peak net export for months that have export rules active
    export_months = [m for m in range(1, 13) if _rule_export_hours(m) > 0]
    avg_daily_export = {'winter': 0.0, 'summer': 0.0}
    if export_months:
        # Build date range filters for months with export rules
        winter_export_months = [m for m in export_months if m not in (6, 7, 8, 9, 10)]
        summer_export_months = [m for m in export_months if m in (6, 7, 8, 9, 10)]
        for season, months in [('winter', winter_export_months), ('summer', summer_export_months)]:
            if not months:
                continue
            like_clauses = ' OR '.join(f"date LIKE '{this_year}-{m:02d}-%'" for m in months)
            row = c.execute(
                f'SELECT SUM(CASE WHEN on_peak_kwh < 0 THEN ABS(on_peak_kwh) ELSE 0 END), '
                f'       COUNT(DISTINCT date) '
                f'FROM daily_costs WHERE ({like_clauses})'
            ).fetchone()
            total_export = row[0] or 0
            day_count = row[1] or 0
            if day_count > 0:
                avg_daily_export[season] = total_export / day_count

    # Determine optimized export data source
    if avg_daily_export['winter'] > 0 or avg_daily_export['summer'] > 0:
        optimized_export_source = 'actual_months'
    else:
        optimized_export_source = 'capacity_estimate'

    # ── Build optimized projection (add battery export to months without rules) ─
    optimized = []
    for bp in baseline:
        month_num = int(bp['month'][5:7])
        is_summer = month_num in (6, 7, 8, 9, 10)
        season = 'summer' if is_summer else 'winter'
        days_in_month = calendar.monthrange(this_year, month_num)[1]

        has_export = _rule_export_hours(month_num) > 0
        if bp['label'] == 'actual' or has_export:
            # Actual months or months that already have export rules — no change
            optimized.append(dict(bp))
        else:
            # Month with no export rules — estimate what adding export rules could yield
            mid_date = f'{this_year}-{month_num:02d}-15'
            r = _rate_for_date(rate_periods, mid_date) or rates
            w = period_weights[season]

            # Use actual average daily export if available; prior-year seasonal fallback otherwise
            daily_exp = avg_daily_export[season]
            if daily_exp <= 0:
                # No current-year data — use prior year's same-season avg daily export
                py_season_months = [m for m in range(1, 13)
                                    if (m in (6, 7, 8, 9, 10)) == (season == 'summer')]
                py_total = sum(py_dc_data.get(f'{prior_year}-{m:02d}', {}).get('export_kwh', 0)
                               for m in py_season_months)
                py_days = sum(calendar.monthrange(prior_year, m)[1] for m in py_season_months)
                if py_total > 0 and py_days > 0:
                    daily_exp = py_total / py_days
                    optimized_export_source = 'prior_year_seasonal'
                else:
                    daily_exp = CAPACITY * 0.50
                    optimized_export_source = 'capacity_estimate'

            add_export_kwh = daily_exp * days_in_month
            add_charge_kwh = add_export_kwh / EFFICIENCY
            # Use data-derived export weights for credit, import weights for charge cost
            avg_exp_rate = sum(r[f'{season}_{p}'] * w['export'][p] for p in _PERIODS)
            avg_imp_rate = sum(r[f'{season}_{p}'] * w['import'][p] for p in _PERIODS)
            credit_gain = add_export_kwh * avg_exp_rate
            charge_cost = add_charge_kwh * avg_imp_rate

            new_imp_kwh = bp['import_kwh'] + add_charge_kwh
            new_exp_kwh = bp['export_kwh'] + add_export_kwh
            new_imp_cost = round(bp['import_cost'] + charge_cost, 2)
            new_exp_credit = round(bp['export_credit'] + credit_gain, 2)
            new_net = round(new_imp_cost - new_exp_credit + bp['base_charge'], 2)

            optimized.append({
                'month': bp['month'], 'label': 'optimized',
                'import_kwh': round(new_imp_kwh, 1),
                'export_kwh': round(new_exp_kwh, 1),
                'import_cost': new_imp_cost,
                'export_credit': new_exp_credit,
                'base_charge': bp['base_charge'],
                'net': new_net,
            })

    baseline_md = _render_projection_table(baseline)
    optimized_md = _render_projection_table(optimized)

    meta = {
        'prior_year_daily_costs': has_prior_year_data,
        'period_weights_source': weights_source,
        'optimized_export_source': optimized_export_source,
        'actual_months': actual_months,
        'projected_months': projected_months,
        'projection_basis': projection_basis,
    }
    return baseline, baseline_md, optimized, optimized_md, meta


def _build_prior_year_note(rules, prior_year, current_year):
    """Build a prior_year_note with the actual charging window from rules."""
    def _fmt_time(h, m):
        if h == 0 and m == 0:
            return 'midnight'
        period = 'AM' if h < 12 else 'PM'
        display_h = h if h <= 12 else h - 12
        if display_h == 0:
            display_h = 12
        return f'{display_h}:{m:02d} {period}' if m else f'{display_h} {period}'

    # Find earliest grid_charging ON and OFF from enabled rules
    gc_on = gc_off = None
    for r in rules:
        if not r.get('enabled'):
            continue
        gc = r.get('grid_charging')
        t = (r['hour'], r['minute'])
        if gc is True and (gc_on is None or t < gc_on):
            gc_on = t
        elif gc is False and (gc_off is None or t > gc_off):
            gc_off = t

    note = (f'{prior_year} used Time-Based Control (Tesla automatic algorithm). '
            f'Current {current_year} rules are custom')
    if gc_on is not None and gc_off is not None:
        window = f'{_fmt_time(*gc_on)}\u2013{_fmt_time(*gc_off)}'
        note += (f' \u2014 they deliberately import more during '
                 f'super off-peak (grid charging {window}) to store energy for on-peak export.')
    else:
        note += '.'
    note += (f' Q1 imports may be higher vs {prior_year} '
             f'but summer export credits should more than offset this.')
    return note


def _build_ai_context():
    """Gather all relevant data for the Gemini prompt."""
    now = datetime.now()
    today = now.date()
    rates = load_rates() or {}
    holidays = sorted(d.isoformat() for d in SDGE_HOLIDAYS if d >= today)

    with sqlite3.connect(DB_PATH) as c:
        rules = _load_all_rules(c)

        # Current year monthly summaries
        cy_monthly_rows = c.execute(
            'SELECT substr(date,1,7), SUM(import_kwh), SUM(export_kwh), '
            '       SUM(import_cost), SUM(export_credit), '
            '       SUM(on_peak_kwh), SUM(off_peak_kwh), SUM(super_off_peak_kwh) '
            'FROM daily_costs WHERE date >= ? AND date < ? '
            'GROUP BY substr(date,1,7) ORDER BY 1',
            (f'{now.year}-01-01', f'{now.year + 1}-01-01')
        ).fetchall()

        # Last 7 days of daily costs (for recent pattern analysis)
        d7 = (today - timedelta(days=7)).isoformat()
        cost_rows = c.execute(
            'SELECT date, import_kwh, export_kwh, import_cost, export_credit, '
            '       on_peak_kwh, off_peak_kwh, super_off_peak_kwh '
            'FROM daily_costs WHERE date >= ? ORDER BY date', (d7,)
        ).fetchall()

        # Prior year monthly summaries (2025) for seasonal baseline
        prior_year = now.year - 1
        py_rows = c.execute(
            'SELECT substr(date,1,7), SUM(import_kwh), SUM(export_kwh), '
            '       SUM(import_cost), SUM(export_credit), '
            '       SUM(on_peak_kwh), SUM(off_peak_kwh), SUM(super_off_peak_kwh) '
            'FROM daily_costs WHERE date >= ? AND date < ? '
            'GROUP BY substr(date,1,7) ORDER BY 1',
            (f'{prior_year}-01-01', f'{prior_year + 1}-01-01')
        ).fetchall()

        # Pre-calculated true-up projections (baseline + optimized)
        # Derive base_charge from rate_history first, then rates.json, then hardcoded fallback
        _rh = _load_rate_history()
        _today_rate = _rate_for_date(_rh, today.isoformat()) if _rh else None
        base_charge = float((_today_rate or {}).get('base_services_charge_per_day', 0)
                            or rates.get('base_services_charge_per_day', 0.79343))
        baseline, baseline_md, optimized, optimized_md, projection_meta = _build_trueup_projection(c, rates, base_charge)

        # Last 7 days of readings (sample every ~60 min)
        t7 = int((now - timedelta(days=7)).timestamp())
        reading_rows = c.execute(
            'SELECT timestamp, solar_w, home_w, battery_w, grid_w, battery_pct '
            'FROM readings WHERE timestamp >= ? ORDER BY timestamp', (t7,)
        ).fetchall()

    # Sample readings to ~3-hourly
    sampled = []
    last_ts = 0
    for row in reading_rows:
        if row[0] - last_ts >= 10800:
            sampled.append({
                'time': datetime.fromtimestamp(row[0]).strftime('%Y-%m-%d %H:%M'),
                'solar_w': round(row[1] or 0), 'home_w': round(row[2] or 0),
                'battery_w': round(row[3] or 0), 'grid_w': round(row[4] or 0),
                'battery_pct': round(row[5] or 0, 1),
            })
            last_ts = row[0]

    # Current year monthly summaries
    current_year_monthly = []
    for row in cy_monthly_rows:
        current_year_monthly.append({
            'month': row[0],
            'import_kwh': round(row[1] or 0, 1), 'export_kwh': round(row[2] or 0, 1),
            'import_cost': round(row[3] or 0, 2), 'export_credit': round(row[4] or 0, 2),
            'on_peak_kwh': round(row[5] or 0, 1), 'off_peak_kwh': round(row[6] or 0, 1),
            'super_off_peak_kwh': round(row[7] or 0, 1),
        })

    # Last 7 days of daily costs
    daily_costs_7d = []
    for row in cost_rows:
        daily_costs_7d.append({
            'date': row[0],
            'import_kwh': round(row[1] or 0, 2), 'export_kwh': round(row[2] or 0, 2),
            'import_cost': round(row[3] or 0, 2), 'export_credit': round(row[4] or 0, 2),
            'on_peak_kwh': round(row[5] or 0, 2), 'off_peak_kwh': round(row[6] or 0, 2),
            'super_off_peak_kwh': round(row[7] or 0, 2),
        })

    # Rule summaries
    rule_summaries = []
    for r in rules:
        rule_summaries.append({
            'name': r['name'], 'enabled': r['enabled'],
            'days': r['days'], 'months': r['months'],
            'hour': r['hour'], 'minute': r['minute'],
            'mode': r['mode'], 'reserve': r['reserve'],
            'grid_charging': r['grid_charging'], 'grid_export': r['grid_export'],
        })

    # Prior year monthly summaries
    prior_year_monthly = []
    for row in py_rows:
        prior_year_monthly.append({
            'month': row[0],
            'import_kwh': round(row[1] or 0, 1), 'export_kwh': round(row[2] or 0, 1),
            'import_cost': round(row[3] or 0, 2), 'export_credit': round(row[4] or 0, 2),
            'on_peak_kwh': round(row[5] or 0, 1), 'off_peak_kwh': round(row[6] or 0, 1),
            'super_off_peak_kwh': round(row[7] or 0, 1),
        })

    # Rule-based insights for additional context
    rule_insights = _analyze_rules(rules, rates, SDGE_HOLIDAYS)

    is_summer = now.month in (6, 7, 8, 9, 10)
    jan1_next = date(now.year + 1, 1, 1)
    days_until_trueup = (jan1_next - today).days

    with _lock:
        live_snapshot = dict(_live)

    return json.dumps({
        'current_date': today.isoformat(),
        'current_season': 'summer' if is_summer else 'winter',
        'next_season_change': 'June 1' if not is_summer else 'November 1',
        'days_until_trueup': days_until_trueup,
        'battery_capacity_kwh': 40.5,
        'powerwall_count': 3,
        'rates': {k: v for k, v in rates.items()},
        'upcoming_holidays': holidays,
        'rules': rule_summaries,
        'rule_based_insights': [{'title': i['title'], 'action': i['action']} for i in rule_insights],
        'trueup_projection_table': baseline_md,
        'optimized_projection_table': optimized_md,
        'prior_year_monthly': prior_year_monthly,
        'prior_year_note': _build_prior_year_note(rules, prior_year, now.year),
        'current_year_monthly': current_year_monthly,
        'daily_costs_last_7d': daily_costs_7d,
        'readings_last_7d': sampled,
        'live_now': {
            'battery_pct': round(live_snapshot.get('battery_pct', 0), 1),
            'solar_w': round(live_snapshot.get('solar_w', 0)),
            'home_w': round(live_snapshot.get('home_w', 0)),
            'grid_w': round(live_snapshot.get('grid_w', 0)),
            'mode': live_snapshot.get('mode', 'unknown'),
        },
        'data_quality': projection_meta,
    }, indent=None, default=str), baseline_md, optimized_md


_ai_cache = {'text': None, 'model': None, 'ts': 0, 'table': None}

_AI_CACHE_TTL = 300  # 5 minutes


@app.route('/api/rules/ai-insights', methods=['POST'])
def api_rules_ai_insights():
    # Return cached response if fresh
    if _ai_cache['text'] and (time.time() - _ai_cache['ts']) < _AI_CACHE_TTL:
        return jsonify({'ok': True, 'insights': _ai_cache['text'], 'model': _ai_cache['model'],
                        'projection_table': _ai_cache['table'],
                        'optimized_table': _ai_cache.get('optimized'), 'cached': True})

    api_key = get_setting('gemini_api_key', '')
    model   = get_setting('gemini_model', 'gemini-2.0-flash')
    if not api_key:
        return jsonify({'ok': False, 'error': 'Gemini API key not configured. Add it in Settings.'}), 400

    try:
        context, table_md, opt_md = _build_ai_context()
        url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
        payload = {
            'system_instruction': {'parts': [{'text': _GEMINI_SYSTEM}]},
            'contents': [{'parts': [{'text': f'Here is the current home energy data:\n\n{context}'}]}],
            'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 65536},
        }
        resp = _requests.post(url, json=payload, timeout=300)
        resp.raise_for_status()
        data = resp.json()

        # Extract text from Gemini response
        text = ''
        candidates = data.get('candidates', [])
        if candidates:
            parts = candidates[0].get('content', {}).get('parts', [])
            text = '\n'.join(p.get('text', '') for p in parts)

        if not text:
            return jsonify({'ok': False, 'error': 'Gemini returned an empty response.'}), 502

        _ai_cache['text'] = text
        _ai_cache['model'] = model
        _ai_cache['table'] = table_md
        _ai_cache['optimized'] = opt_md
        _ai_cache['ts'] = time.time()
        return jsonify({'ok': True, 'insights': text, 'model': model,
                        'projection_table': table_md, 'optimized_table': opt_md})

    except _requests.exceptions.Timeout:
        return jsonify({'ok': False, 'error': 'Gemini API timed out. Try again.'}), 504
    except _requests.exceptions.HTTPError as exc:
        status = exc.response.status_code if exc.response else 500
        body = ''
        try:
            body = exc.response.json().get('error', {}).get('message', str(exc))
        except Exception:
            body = str(exc)
        return jsonify({'ok': False, 'error': f'Gemini API error ({status}): {body}'}), 502
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


@app.route('/api/rules/ai-insights/debug')
def api_rules_ai_insights_debug():
    """Debug endpoint — returns full prompt, context, raw Gemini response, and token usage."""
    api_key = get_setting('gemini_api_key', '')
    model   = get_setting('gemini_model', 'gemini-2.0-flash')
    if not api_key:
        return jsonify({'ok': False, 'error': 'No API key'}), 400

    context, _, _ = _build_ai_context()
    user_msg = f'Here is the current home energy data:\n\n{context}'
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
    payload = {
        'system_instruction': {'parts': [{'text': _GEMINI_SYSTEM}]},
        'contents': [{'parts': [{'text': user_msg}]}],
        'generationConfig': {'temperature': 0.2, 'maxOutputTokens': 65536},
    }

    try:
        resp = _requests.post(url, json=payload, timeout=300)
        raw = resp.json()
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500

    # Extract parts
    text = ''
    candidates = raw.get('candidates', [])
    finish_reason = None
    if candidates:
        parts = candidates[0].get('content', {}).get('parts', [])
        text = '\n'.join(p.get('text', '') for p in parts)
        finish_reason = candidates[0].get('finishReason')

    usage = raw.get('usageMetadata', {})

    return jsonify({
        'ok': resp.status_code == 200,
        'model': model,
        'system_prompt_chars': len(_GEMINI_SYSTEM),
        'context_chars': len(context),
        'finish_reason': finish_reason,
        'usage': {
            'prompt_tokens': usage.get('promptTokenCount'),
            'output_tokens': usage.get('candidatesTokenCount'),
            'thinking_tokens': usage.get('thoughtsTokenCount'),
            'total_tokens': usage.get('totalTokenCount'),
        },
        'response_chars': len(text),
        'response_text': text,
        'system_prompt': _GEMINI_SYSTEM,
    })


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


@app.route('/api/costs/daily')
def api_costs_daily():
    # Support start/end date filters (default: current year) + pagination
    today = date.today()
    start = request.args.get('start', f'{today.year}-01-01')
    end   = request.args.get('end', today.isoformat())
    limit  = int(request.args.get('limit', 0))   # 0 = no limit
    offset = int(request.args.get('offset', 0))
    with sqlite3.connect(DB_PATH) as c:
        # Total count for pagination
        total = c.execute(
            'SELECT COUNT(*) FROM daily_costs WHERE date >= ? AND date <= ?',
            (start, end)
        ).fetchone()[0]
        sql = ('SELECT date, import_kwh, export_kwh, import_cost, export_credit, '
               '       on_peak_kwh, off_peak_kwh, super_off_peak_kwh, '
               '       on_peak_cost, off_peak_cost, super_off_peak_cost '
               'FROM daily_costs WHERE date >= ? AND date <= ? ORDER BY date DESC')
        params: list = [start, end]
        if limit > 0:
            sql += ' LIMIT ? OFFSET ?'
            params += [limit, offset]
        rows = c.execute(sql, params).fetchall()
    rates = load_rates()
    rates_as_of = (rates.get('updated') or '')[:10] if rates else ''
    days = [
        {
            'date':          r[0],
            'import_kwh':    round(r[1], 2),
            'export_kwh':    round(r[2], 2),
            'import_cost':   round(r[3], 2),
            'export_credit': round(r[4], 2),
            'net_cost':      round(r[3] - r[4], 2),
            'on_peak_kwh':        round(r[5] or 0, 2),
            'off_peak_kwh':       round(r[6] or 0, 2),
            'super_off_peak_kwh': round(r[7] or 0, 2),
            'on_peak_cost':        round(r[8] or 0, 2),
            'off_peak_cost':       round(r[9] or 0, 2),
            'super_off_peak_cost': round(r[10] or 0, 2),
        }
        for r in rows
    ]
    return jsonify({'start': start, 'end': end, 'total': total,
                    'rates_as_of': rates_as_of, 'days': days})


@app.route('/api/costs/rebuild', methods=['POST'])
def api_costs_rebuild():
    threading.Thread(target=rebuild_daily_costs, daemon=True).start()
    return jsonify({'ok': True})


@app.route('/api/rates')
def api_rates():
    data = load_rates() or {}
    data['holidays'] = sorted(d.isoformat() for d in SDGE_HOLIDAYS)
    data['tou_periods'] = _load_tou_periods()
    return jsonify(data)


@app.route('/api/rates/refresh', methods=['POST'])
def api_rates_refresh():
    try:
        rates = fetch_ev_tou2_rates()
        return jsonify({'ok': True, 'updated': rates.get('updated')})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


# ── Abode debug endpoint ─────────────────────────────────────────────────────
@app.route('/api/debug/abode/timeline')
def api_debug_abode_timeline():
    """Return first page of raw Abode timeline — use to verify field names."""
    if _abode_instance is None:
        return jsonify({'error': 'Abode not connected yet'}), 503
    try:
        resp = _abode_instance.send_request(
            'get', 'https://my.goabode.com/api/v1/timeline?size=5'
        )
        return jsonify(resp.json())
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/debug/abode/status')
def api_debug_abode_status():
    """Return Abode listener connection state and stats."""
    info = dict(_abode_status)
    info['connected'] = _abode_instance is not None
    return jsonify(info)


@app.route('/api/debug/abode/backfill', methods=['POST'])
def api_debug_abode_backfill():
    """Manually trigger Abode backfill and return result with diagnostics."""
    if _abode_instance is None:
        return jsonify({'error': 'Abode not connected'}), 503
    days = int(request.args.get('days', 30))

    # Collect diagnostics: fetch page 1 raw to show what we're getting
    diag = {}
    try:
        resp = _abode_instance.send_request(
            'get', f'https://my.goabode.com/api/v1/timeline?size=5')
        raw = resp.json()
        if isinstance(raw, list):
            diag['api_sample'] = [
                {'event_utc': e.get('event_utc'), 'event_name': e.get('event_name'),
                 'device_name': e.get('device_name'), 'date': e.get('date')}
                for e in raw[:3]
            ]
    except Exception:
        pass

    # Check existing row count before
    with sqlite3.connect(DB_PATH, timeout=10) as c:
        before = c.execute(
            "SELECT COUNT(*) FROM event_log WHERE system='abode'").fetchone()[0]

    inserted = abode_backfill(_abode_instance, days=days)

    with sqlite3.connect(DB_PATH, timeout=10) as c:
        after = c.execute(
            "SELECT COUNT(*) FROM event_log WHERE system='abode'").fetchone()[0]

    # Direct DB check: does a known Mar 28 event exist?
    spot_check = {}
    try:
        sample_ts = int(diag.get('api_sample', [{}])[0].get('event_utc', 0))
        sample_title = diag.get('api_sample', [{}])[0].get('event_name', '')
        with sqlite3.connect(DB_PATH, timeout=10) as c:
            spot_check['ts'] = sample_ts
            spot_check['title'] = sample_title
            spot_check['exact_match'] = c.execute(
                'SELECT COUNT(*) FROM event_log WHERE ts=? AND system=? AND title=?',
                (sample_ts, 'abode', sample_title)).fetchone()[0]
            spot_check['ts_only'] = c.execute(
                'SELECT COUNT(*) FROM event_log WHERE ts=?',
                (sample_ts,)).fetchone()[0]
            spot_check['db_path'] = DB_PATH
    except Exception as e:
        spot_check['error'] = str(e)

    return jsonify({
        'code_version': 'v8-page1log',
        'ok': True,
        'inserted': inserted,
        'collected': _abode_status.get('last_backfill_collected', 0),
        'days': days,
        'rows_before': before,
        'rows_after': after,
        'backfill_error': _abode_status.get('last_backfill_error'),
        'duplicates_skipped': _abode_status.get('last_backfill_dupes', 0),
        'spot_check': spot_check,
        'collected_dates': _abode_status.get('last_backfill_dates', {}),
        'existing_set_size': _abode_status.get('last_backfill_existing_size', 0),
        'skipped_no_ts': _abode_status.get('last_backfill_skipped', 0),
        'pages_fetched': _abode_status.get('last_backfill_pages', 0),
        'backfill_page1': _abode_status.get('last_backfill_page1'),
        'diagnostics': diag,
    })


@app.route('/api/debug/abode/dedup', methods=['POST'])
def api_debug_abode_dedup():
    """Remove duplicate abode events from event_log."""
    with sqlite3.connect(DB_PATH, timeout=30) as c:
        before = c.execute("SELECT COUNT(*) FROM event_log WHERE system='abode'").fetchone()[0]
        c.execute('''DELETE FROM event_log WHERE system='abode' AND id NOT IN (
            SELECT MIN(id) FROM event_log WHERE system='abode'
            GROUP BY ts, system, title)''')
        after = c.execute("SELECT COUNT(*) FROM event_log WHERE system='abode'").fetchone()[0]
    return jsonify({'before': before, 'after': after, 'removed': before - after})


@app.route('/api/debug/abode/test-event', methods=['POST'])
def api_debug_abode_test_event():
    """Insert a synthetic Abode event for UI testing."""
    import random
    samples = [
        ('door_open',    'Front Door Opened'),
        ('door_closed',  'Front Door Closed'),
        ('lock_locked',  'Garage Door Lock Locked'),
        ('lock_unlocked','Garage Door Lock Unlocked'),
        ('arm_away',     'System Armed Away'),
        ('arm_home',     'System Armed Home'),
        ('disarm',       'System Disarmed'),
        ('motion',       'Living Room Motion Detected'),
    ]
    evt, title = random.choice(samples)
    ts = int(time.time())
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            'INSERT INTO event_log '
            '(ts, system, event_type, title, detail, result, source) '
            'VALUES (?,?,?,?,?,?,?)',
            (ts, 'abode', evt, title, 'synthetic test event', 'info', 'test')
        )
    return jsonify({'ok': True, 'ts': ts, 'event_type': evt, 'title': title})


# ── Nest OAuth + debug ───────────────────────────────────────────────────────
@app.route('/nest/auth')
def nest_auth():
    """Redirect user to Google OAuth consent screen for Nest/SDM access."""
    import urllib.parse
    client_id  = get_setting('nest_client_id', '')
    project_id = get_setting('nest_project_id', '')
    if not client_id or not project_id:
        return jsonify({'error': 'Nest client_id or project_id not configured'}), 400

    redirect_uri = request.url_root.rstrip('/') + '/nest/callback'
    params = urllib.parse.urlencode({
        'client_id':     client_id,
        'redirect_uri':  redirect_uri,
        'response_type': 'code',
        'scope':         'https://www.googleapis.com/auth/sdm.service https://www.googleapis.com/auth/pubsub',
        'access_type':   'offline',
        'prompt':        'consent',
    })
    url = f'https://nestservices.google.com/partnerconnections/{project_id}/auth?{params}'
    return redirect(url)


@app.route('/nest/callback')
def nest_callback():
    """Exchange authorization code for tokens, store refresh_token."""
    code = request.args.get('code')
    error = request.args.get('error')
    if error:
        return f'<h2>Nest authorization failed</h2><p>{error}</p>', 400
    if not code:
        return '<h2>Missing authorization code</h2>', 400

    client_id     = get_setting('nest_client_id', '')
    client_secret = get_setting('nest_client_secret', '')
    redirect_uri  = request.url_root.rstrip('/') + '/nest/callback'

    try:
        resp = _requests.post('https://oauth2.googleapis.com/token', data={
            'client_id':     client_id,
            'client_secret': client_secret,
            'code':          code,
            'grant_type':    'authorization_code',
            'redirect_uri':  redirect_uri,
        }, timeout=15)
        resp.raise_for_status()
        tokens = resp.json()
    except Exception as exc:
        return f'<h2>Token exchange failed</h2><pre>{exc}</pre>', 500

    refresh_token = tokens.get('refresh_token', '')
    access_token  = tokens.get('access_token', '')
    expires_in    = tokens.get('expires_in', 3600)

    with sqlite3.connect(DB_PATH) as c:
        for k, v in [
            ('nest_refresh_token', refresh_token),
            ('nest_access_token',  access_token),
            ('nest_token_expiry',  str(int(time.time()) + expires_in - 60)),
        ]:
            c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (k, v))
        c.commit()

    return ('<h2>Nest connected successfully!</h2>'
            '<p>You can close this tab and return to the dashboard.</p>'
            '<p>Enable the Nest connector in Settings to start receiving events.</p>')


@app.route('/api/debug/nest/status')
def api_debug_nest_status():
    token = get_setting('nest_access_token', '')
    expiry = get_setting_int('nest_token_expiry', 0)
    return jsonify({
        'enabled': get_setting_bool('nest_enabled', False),
        'has_refresh_token': bool(get_setting('nest_refresh_token', '')),
        'token_valid': bool(token and time.time() < expiry),
        'token_expiry': expiry,
        'subscription': get_setting('nest_pubsub_subscription', ''),
        'cached_devices': _nest_devices,
        'devices_cache_age': int(time.time() - _nest_devices_ts) if _nest_devices_ts else None,
    })


# ── Event Log endpoint ────────────────────────────────────────────────────────
@app.route('/api/events')
def api_events():
    limit  = min(int(request.args.get('limit', 50)), 500)
    offset = max(int(request.args.get('offset', 0)), 0)
    system = request.args.get('system', 'all')
    etype  = request.args.get('type')

    # Date range: accept start/end unix timestamps, fall back to days param
    start_ts = request.args.get('start')
    end_ts   = request.args.get('end')
    if start_ts:
        start_ts = int(start_ts)
    else:
        days = min(int(request.args.get('days', 7)), 365)
        start_ts = int(time.time()) - days * 86400
    if end_ts:
        end_ts = int(end_ts)

    query  = 'SELECT id,ts,system,event_type,title,detail,result,source,battery_pct FROM event_log WHERE ts >= ?'
    params: list = [start_ts]

    if end_ts:
        query += ' AND ts <= ?'
        params.append(end_ts)
    if system == 'errors':
        query += " AND (result = 'failed' OR event_type = 'error')"
    elif system != 'all':
        query += ' AND system = ?'
        params.append(system)
    if etype:
        query += ' AND event_type = ?'
        params.append(etype)

    query += ' ORDER BY ts DESC LIMIT ? OFFSET ?'
    params.append(limit + 1)   # fetch one extra to detect has_more
    params.append(offset)

    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(query, params).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]

    results = []
    for row in rows:
        rid, ts, sys_, evt, title, detail, result, source, batt = row
        d = datetime.fromtimestamp(ts)
        ts_display = (
            d.strftime('%b %#d  %#I:%M %p') if os.name == 'nt'
            else d.strftime('%b %-d  %-I:%M %p')
        )
        results.append({
            'id':          rid,
            'ts':          ts,
            'ts_display':  ts_display,
            'system':      sys_,
            'event_type':  evt,
            'title':       title,
            'detail':      detail,
            'result':      result,
            'source':      source,
            'battery_pct': batt,
        })
    return jsonify({'events': results, 'has_more': has_more})


# ── Settings endpoints ────────────────────────────────────────────────────────
@app.route('/api/settings')
def api_settings():
    settings = load_settings()
    # Add runtime info for each connector
    connectors = [
        {
            'key': 'powerwall',
            'label': 'Powerwall',
            'type': 'continuous',
            'enabled_key': 'powerwall_enabled',
            'intervals': [
                {'key': 'powerwall_poll_interval', 'label': 'Poll interval', 'unit': 's'},
                {'key': 'powerwall_db_write_interval', 'label': 'DB write interval', 'unit': 's'},
            ],
        },
        {
            'key': 'pool',
            'label': 'Pool (ScreenLogic)',
            'type': 'on-demand',
            'enabled_key': 'pool_enabled',
            'intervals': [
                {'key': 'pool_poll_interval', 'label': 'Poll interval', 'unit': 's'},
            ],
        },
        {
            'key': 'rachio',
            'label': 'Rachio / Sprinklers',
            'type': 'on-demand',
            'enabled_key': 'rachio_enabled',
            'intervals': [
                {'key': 'rachio_poll_interval',      'label': 'Schedule poll',  'unit': 's'},
                {'key': 'rachio_event_poll_interval', 'label': 'Event log poll', 'unit': 's'},
            ],
        },
        {
            'key': 'rain_skip',
            'label': 'Smart Rain Skip',
            'type': 'on-demand',
            'enabled_key': 'rain_skip_enabled',
            'intervals': [
                {'key': 'rain_skip_check_interval', 'label': 'Check interval',  'unit': 's'},
                {'key': 'rain_lookback_days',       'label': 'Rain lookback',   'unit': 'days'},
                {'key': 'rain_mm_per_skip_day',     'label': 'mm per skip day', 'unit': 'text'},
                {'key': 'rain_skip_max_days',       'label': 'Max skip days',   'unit': 'days'},
            ],
        },
        {
            'key': 'abode',
            'label': 'Abode',
            'type': 'websocket',
            'enabled_key': 'abode_enabled',
            'intervals': [],
        },
        {
            'key': 'nest',
            'label': 'Nest (Camera/Doorbell)',
            'type': 'on-demand',
            'enabled_key': 'nest_enabled',
            'intervals': [
                {'key': 'nest_poll_interval',       'label': 'Poll interval',        'unit': 's'},
                {'key': 'nest_pubsub_subscription', 'label': 'Pub/Sub subscription', 'unit': 'text'},
                {'key': 'nest_client_id',           'label': 'OAuth Client ID',      'unit': 'text'},
                {'key': 'nest_client_secret',       'label': 'OAuth Client Secret',  'unit': 'text'},
                {'key': 'nest_project_id',          'label': 'Device Access Project', 'unit': 'text'},
            ],
        },
        {
            'key': 'maintenance',
            'label': 'Maintenance',
            'type': 'scheduled',
            'intervals': [
                {'key': 'cost_rebuild_days', 'label': 'Cost rebuild', 'unit': 'days'},
                {'key': 'refresh_start_date', 'label': 'Refresh start date', 'unit': 'date'},
                {'key': 'holidays_poll_months', 'label': 'Holiday refresh', 'unit': 'months'},
                {'key': 'rates_poll_months', 'label': 'Energy Rate refresh', 'unit': 'months'},
            ],
        },
        {
            'key': 'sdge',
            'label': 'SDG\u0026E Rates',
            'type': 'configurable',
            'intervals': [
                {'key': 'rates_page_url', 'label': 'Rates page URL', 'unit': 'url'},
                {'key': 'rate_schedule_name', 'label': 'Schedule name', 'unit': 'text'},
            ],
        },
        {
            'key': 'gemini',
            'label': 'Gemini AI',
            'type': 'configurable',
            'intervals': [
                {'key': 'gemini_api_key', 'label': 'API Key', 'unit': 'text'},
                {'key': 'gemini_model', 'label': 'Model', 'unit': 'text'},
            ],
        },
        {
            'key': 'frontend',
            'label': 'Dashboard Refresh',
            'type': 'frontend',
            'intervals': [
                {'key': 'fe_poll_interval', 'label': 'Live power', 'unit': 'ms'},
                {'key': 'fe_chart_interval', 'label': 'Chart', 'unit': 'ms'},
                {'key': 'fe_weather_interval', 'label': 'Weather', 'unit': 'ms'},
                {'key': 'fe_automations_interval', 'label': 'Automations', 'unit': 'ms'},
                {'key': 'fe_pool_interval', 'label': 'Pool tile', 'unit': 'ms'},
                {'key': 'fe_costs_interval', 'label': 'Costs tile', 'unit': 'ms'},
                {'key': 'fe_rates_interval', 'label': 'Rates', 'unit': 'ms'},
                {'key': 'fe_events_interval', 'label': 'Event log', 'unit': 'ms'},
            ],
        },
    ]
    return jsonify({'settings': settings, 'connectors': connectors})


@app.route('/api/settings', methods=['PUT'])
def api_settings_update():
    data = request.get_json() or {}
    valid_keys = set(_SETTINGS_DEFAULTS.keys())
    with sqlite3.connect(DB_PATH) as c:
        for key, value in data.items():
            if key in valid_keys:
                c.execute(
                    'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                    (key, str(value))
                )
        c.commit()
    return jsonify({'ok': True})


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
    backfill_history()
    threading.Thread(target=rebuild_daily_costs, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    start_abode_listener()
    print('Dashboard \u2192 http://localhost:5000')
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
