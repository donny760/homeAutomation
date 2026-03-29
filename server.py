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
    tou_period, load_or_generate_holidays, SDGE_HOLIDAYS,
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
ABODE_EMAIL         = 'don@nsdsolutions.com'
ABODE_PASSWORD      = 'RKf3^KH^'

app = Flask(__name__)

# Shared live-data cache
_live: dict = {}
_lock = threading.Lock()

# Pool cache
_pool: dict    = {}
_pool_ts: float = 0.0
_pool_prev: dict = {}  # previous state for change detection

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
    'security_poll_interval':      '30',      # backend cache TTL
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
        ' winter_on_peak, winter_off_peak, winter_super_off_peak, source_url, fetched_at) '
        'VALUES (?,?,?,?,?,?,?,?,?)',
        (eff_date,
         rates.get('summer_on_peak', 0), rates.get('summer_off_peak', 0),
         rates.get('summer_super_off_peak', 0),
         rates.get('winter_on_peak', 0), rates.get('winter_off_peak', 0),
         rates.get('winter_super_off_peak', 0),
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
            '       winter_on_peak, winter_off_peak, winter_super_off_peak '
            'FROM rate_history ORDER BY effective_date'
        ).fetchall()


def _rate_for_date(rate_periods, d_iso: str) -> dict | None:
    """Find the rate dict applicable to a given date string 'YYYY-MM-DD'."""
    for eff, end, s_on, s_off, s_sup, w_on, w_off, w_sup in reversed(rate_periods):
        if d_iso >= eff:
            return {
                'summer_on_peak': s_on, 'summer_off_peak': s_off,
                'summer_super_off_peak': s_sup,
                'winter_on_peak': w_on, 'winter_off_peak': w_off,
                'winter_super_off_peak': w_sup,
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
    cutoff = int(time.time()) - PURGE_DAYS * 86400
    with sqlite3.connect(DB_PATH) as c:
        c.execute('DELETE FROM readings WHERE timestamp < ?', (cutoff,))
        c.execute("DELETE FROM event_log WHERE ts < ? AND source != 'import'", (cutoff,))


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
    last_holidays_check = 0
    last_rates_check = 0
    last_rachio_event_poll = 0
    last_rain_skip_check = 0

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
        c508   = _key(circuit, 508, '508') or {}

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
        cleaner_on      = bool(_nested(c508, 'value'))

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
}


def _log_pool_changes(new: dict) -> None:
    """Compare new pool state against previous and log any changes."""
    global _pool_prev
    if not _pool_prev:
        # First fetch — seed state, don't log
        _pool_prev = {k: new.get(k) for k in _POOL_EVENT_FIELDS}
        return
    now = int(time.time())
    try:
        with sqlite3.connect(DB_PATH) as c:
            for field, (event_type, label) in _POOL_EVENT_FIELDS.items():
                old_val = _pool_prev.get(field)
                new_val = new.get(field)
                if old_val == new_val:
                    continue
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
    except Exception as exc:
        print(f'Pool event log error: {exc}')
    _pool_prev = {k: new.get(k) for k in _POOL_EVENT_FIELDS}


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


@app.route('/api/costs/daily')
def api_costs_daily():
    year = int(request.args.get('year', date.today().year))
    jan1  = f'{year}-01-01'
    dec31 = f'{year}-12-31'
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            'SELECT date, import_kwh, export_kwh, import_cost, export_credit, '
            '       on_peak_kwh, off_peak_kwh, super_off_peak_kwh, '
            '       on_peak_cost, off_peak_cost, super_off_peak_cost '
            'FROM daily_costs WHERE date >= ? AND date <= ? ORDER BY date DESC',
            (jan1, dec31)
        ).fetchall()
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
    return jsonify({'year': year, 'rates_as_of': rates_as_of, 'days': days})


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


# ── Event Log endpoint ────────────────────────────────────────────────────────
@app.route('/api/events')
def api_events():
    limit  = min(int(request.args.get('limit', 50)), 500)
    system = request.args.get('system', 'all')
    days   = min(int(request.args.get('days', 7)), 90)
    etype  = request.args.get('type')

    cutoff = int(time.time()) - days * 86400
    query  = 'SELECT id,ts,system,event_type,title,detail,result,source,battery_pct FROM event_log WHERE ts >= ?'
    params = [cutoff]

    if system != 'all':
        query += ' AND system = ?'
        params.append(system)
    if etype:
        query += ' AND event_type = ?'
        params.append(etype)

    query += ' ORDER BY ts DESC LIMIT ?'
    params.append(limit)

    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(query, params).fetchall()

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
    return jsonify(results)


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
