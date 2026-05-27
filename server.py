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
import traceback
import requests as _requests
from datetime import datetime, date, timedelta, timezone

import asyncio

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

from flask import Flask, jsonify, send_file, send_from_directory, request, redirect
import pypowerwall
from rules import seed_default_rules as _seed_rules
from fetch_rates import (
    load_rates, rates_are_stale, fetch_ev_tou2_rates,
    tou_period, load_or_generate_holidays, SDGE_HOLIDAYS,
    HOLIDAYS_PATH, RATES_PATH,
    holiday_name, is_sdge_holiday,
)

# ── Config ────────────────────────────────────────────────────────────────────
PW_EMAIL          = 'don@nsdsolutions.com'
PW_CAPACITY_KWH   = 40.5          # 3× Powerwall 2 usable capacity (3 × 13.5 kWh)
BASE_DIR          = os.path.dirname(os.path.abspath(__file__))
DB_PATH           = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'powerwall.db'))
POLL_INTERVAL     = 10            # seconds between pypowerwall polls
DB_WRITE_EVERY    = 30            # seconds between DB writes
PURGE_DAYS        = 0             # disabled — keep all readings forever
POOL_POLL_INTERVAL  = 30           # seconds between pool polls
RACHIO_API_KEY      = os.environ.get('RACHIO_API_KEY', '')
RACHIO_BASE         = 'https://api.rach.io/1/public'
RACHIO_TTL          = 300          # 5-minute cache for Rachio schedule
ABODE_EMAIL         = os.environ.get('ABODE_EMAIL', '')
ABODE_PASSWORD      = os.environ.get('ABODE_PASSWORD', '')

app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # no browser caching of static files

@app.errorhandler(Exception)
def handle_exception(e):
    import traceback
    app.logger.error(traceback.format_exc())
    return jsonify(error=str(e)), 500

# Shared live-data cache
_live: dict = {}
_lock = threading.Lock()

# Pool cache
_pool: dict    = {}
_pool_ts: float = 0.0
_pool_prev: dict = {}       # previous state for change detection
_pool_pending: dict = {}    # pending state changes (debounce — must persist 2 consecutive polls)
_pool_gallons_today: float = 0.0
_pool_gallons_date:  str   = ''
_pool_last_accum_ts: float = 0.0
_pool_normal_gpm:    float = 0.0   # last pump_gpm observed while cleaner was OFF
_pool_cleaner_gpm:   float = 0.0   # last pump_gpm observed while cleaner was ON
_pool_edge_gpm:      float = 0.0   # last edge_pump_gpm observed while edge was ON

# Security cache
_security: dict    = {}
_security_ts: float = 0.0

# Rachio cache
_rachio_schedule: list = []
_rachio_ts: float      = 0.0

# Switches (Kasa/Nest-thermostat/Tuya) caches
_kasa_devices: dict     = {}   # mac -> {alias, ip, on, last_seen, host_obj}
_kasa_ts: float         = 0.0
_nest_thermostats: dict = {}   # device_name -> {...thermostat traits...}
_tuya_devices: dict     = {}   # dev_id -> {name, ip, local_key, version, on, last_seen}
_tuya_ts: float         = 0.0
_tuya_connections: dict = {}   # dev_id -> OutletDevice (persistent socket)
_tuya_failures: dict    = {}   # dev_id -> consecutive failure count
_tuya_quarantine: dict  = {}   # dev_id -> unix ts at which quarantine ends
_switches_lock          = threading.Lock()

# Persistent asyncio loop for Kasa — needed so Device instances (which are
# bound to the event loop that created them) survive across calls. Without
# this, every call to asyncio.run() would create a new loop and drop any
# cached connections, forcing a fresh KLAP handshake every time — which was
# knocking some dimmers off WiFi.
_kasa_loop: "asyncio.AbstractEventLoop | None" = None
_kasa_loop_thread: "threading.Thread | None" = None
_kasa_connections: dict = {}   # mac -> Device (alive on _kasa_loop)
_kasa_failures: dict    = {}   # mac -> consecutive failure count
_kasa_quarantine: dict  = {}   # mac -> unix ts at which quarantine ends



# ── Database ──────────────────────────────────────────────────────────────────
def init_db() -> None:
    with sqlite3.connect(DB_PATH) as c:
        # WAL allows the rules.py process to read while server.py writes
        # (and vice-versa) without blocking. busy_timeout absorbs any brief
        # contention during cost rebuilds.
        c.execute('PRAGMA journal_mode=WAL')
        c.execute('PRAGMA busy_timeout=10000')
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
        # Switches drawer metadata — Kasa/Pool/Nest/Tuya devices surfaced as tiles
        c.execute('''
            CREATE TABLE IF NOT EXISTS switches_meta (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                provider    TEXT NOT NULL,
                external_id TEXT NOT NULL,
                kind        TEXT NOT NULL,
                name        TEXT NOT NULL,
                room        TEXT DEFAULT '',
                sort_order  INTEGER DEFAULT 0,
                hidden      INTEGER DEFAULT 0,
                UNIQUE(provider, external_id)
            )
        ''')
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
        # Migration: add sort_order column if not present
        try:
            c.execute('ALTER TABLE rules ADD COLUMN sort_order INTEGER DEFAULT 0')
            c.execute('UPDATE rules SET sort_order = id')
        except Exception:
            pass  # column already exists
        # Migration: add notes column if not present
        try:
            c.execute('ALTER TABLE rules ADD COLUMN notes TEXT')
        except Exception:
            pass  # column already exists
        # Migration: force-update tou_periods to year-round weekday super off-peak (effective 2026-05-01)
        correct_tou = json.dumps({
            'weekday':         {'on_peak': [[16, 21]], 'super_off_peak': [[0, 6], [10, 14]]},
            'weekend_holiday': {'on_peak': [[16, 21]], 'super_off_peak': [[0, 14]]},
        })
        c.execute("INSERT INTO settings (key,value) VALUES ('tou_periods',?) ON CONFLICT(key) DO UPDATE SET value=?",
                  (correct_tou, correct_tou))


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
    'rain_lookback_days':          '5',       # days of precipitation history to check
    'rain_mm_per_skip_day':        '1',       # mm of accumulated rain per skip day
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
    # TOU period definitions (JSON) — per official SDG&E EV-TOU-2 tariff (effective 2026-05-01)
    'tou_periods':                 json.dumps({
        'weekday': {
            'on_peak':        [[16, 21]],
            'super_off_peak': [[0, 6], [10, 14]],
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
    # Azure OpenAI (fallback when Gemini fails)
    'azure_openai_endpoint':       '',  # e.g., https://myresource.openai.azure.com
    'azure_openai_api_key':        '',
    'azure_openai_deployment':     '',  # deployment name, e.g. "gpt-4o-mini"
    'azure_openai_api_version':    '2024-10-21',
    # Nest / Google SDM
    'nest_enabled':                '0',
    'nest_poll_interval':          '60',
    'nest_client_id':              os.environ.get('NEST_CLIENT_ID', ''),
    'nest_client_secret':          os.environ.get('NEST_CLIENT_SECRET', ''),
    'nest_project_id':             os.environ.get('NEST_PROJECT_ID', ''),
    'nest_pubsub_subscription':    '',
    'nest_refresh_token':          '',
    'nest_access_token':           '',
    'nest_token_expiry':           '0',
    'nest_thermostat_enabled':     '0',
    # Kasa (TP-Link smart plugs, LAN discovery)
    'kasa_enabled':                '0',
    'kasa_poll_interval':          '10',
    # When '0', the periodic state poll is skipped — drawer tiles still
    # appear (from last discovery) and toggling still works, but we stop
    # hammering the devices every 10s. Useful as a diagnostic when Kasa
    # schedules misbehave and we suspect connection churn is the cause.
    'kasa_state_poll_enabled':     '1',
    # Pool control (write path; read path is pool_enabled)
    'pool_control_enabled':        '0',
    # Tuya (tinytuya, LAN control of Smart Life / Tuya-platform devices)
    'tuya_enabled':                '0',
    'tuya_poll_interval':          '15',
    # Network devices (LRT224 router + DD-WRT APs, basic auth web scrape)
    'network_enabled':             '0',
    'network_poll_interval':       '60',
    'network_router_url':          '',
    'network_router_user':         '',
    'network_router_pass':         '',
    # SNMP path for the LRT224 (its web UI is JS-rendered, so SNMP is the
    # only practical way to get its ARP table). Leave snmp_host blank to
    # fall back to web scraping.
    'network_router_snmp_host':      '',
    'network_router_snmp_community': 'public',
    'network_router_snmp_port':      '161',
    # Local LAN scan (ping-sweep + arp -a from the dashboard host).
    # The LRT224's web UI is JS-shell and its SNMP agent doesn't expose ARP,
    # so this is how we get the full LAN device list.
    'network_local_subnet':        '10.0.0.0/24',
    # JSON list: [{"name":"Living Room","url":"http://192.168.1.2","user":"root","pass":"…"}, …]
    'network_aps':                 '[]',
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


def _log_success(system: str, event_type: str, title: str, detail: str = None) -> None:
    """Log a successful system event to the event_log table."""
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                'INSERT INTO event_log '
                '(ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), system, event_type, title, detail, 'ok', 'live')
            )
    except Exception:
        pass


def _backfill_rates_event_url() -> None:
    """One-time: append source_url to existing rates_updated events that don't have one."""
    try:
        if not os.path.exists(RATES_PATH):
            return
        with open(RATES_PATH) as f:
            src_url = json.load(f).get('source_url')
        if not src_url:
            return
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                "UPDATE event_log SET detail = detail || '  ' || ? "
                "WHERE system='rates' AND event_type='rates_updated' "
                "AND (detail IS NULL OR detail NOT LIKE '%http%')",
                (src_url,)
            )
    except Exception:
        pass


def _read_year_from_json(path):
    """Tolerantly read {"year": N} from a JSON file. Returns None if the file
    is missing, was deleted between exists() and open(), or is corrupt."""
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f).get('year')
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def write_reading(solar_w, home_w, battery_w, grid_w, battery_pct) -> None:
    # INSERT OR IGNORE: if the poll interval ever drops below 1s or two writes
    # land in the same second, we keep the first reading rather than letting a
    # second write clobber a battery_pct populated by the backfill path.
    with sqlite3.connect(DB_PATH) as c:
        c.execute(
            'INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?)',
            (int(time.time()), solar_w, home_w, battery_w, grid_w, battery_pct)
        )


def purge_old() -> None:
    """Disabled — keep all readings forever."""
    pass


_cost_rebuild_lock = threading.Lock()


def _spawn_rebuild_daily_costs(from_date=None) -> bool:
    """Spawn a daemon thread for rebuild_daily_costs unless one is already
    running. Returns True if started, False if skipped due to overlap."""
    if not _cost_rebuild_lock.acquire(blocking=False):
        return False

    def _run():
        try:
            rebuild_daily_costs(from_date=from_date)
        finally:
            _cost_rebuild_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


def _rebuild_today() -> None:
    """Recompute and upsert daily_costs for today only."""
    today_dt = date.today()
    today_str = today_dt.isoformat()
    midnight = int(datetime(today_dt.year, today_dt.month, today_dt.day).timestamp())
    tomorrow = midnight + 86400

    rate_periods = _load_rate_history()
    fallback_rates = load_rates() if not rate_periods else None
    if not rate_periods and not fallback_rates:
        return

    tou_cfg = _load_tou_periods()
    day_rate = (_rate_for_date(rate_periods, today_str) if rate_periods else None) or fallback_rates or {}

    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            'SELECT timestamp, grid_w FROM readings '
            'WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp',
            (midnight, tomorrow)
        ).fetchall()

        v = {
            'import_kwh': 0.0, 'export_kwh': 0.0,
            'import_cost': 0.0, 'export_credit': 0.0,
            'on_peak_kwh': 0.0, 'off_peak_kwh': 0.0, 'super_off_peak_kwh': 0.0,
            'on_peak_cost': 0.0, 'off_peak_cost': 0.0, 'super_off_peak_cost': 0.0,
        }
        for i in range(1, len(rows)):
            ts0, g0 = rows[i - 1]
            ts1, g1 = rows[i]
            dt_h = (ts1 - ts0) / 3600
            if dt_h > 1:
                continue
            dt = datetime.fromtimestamp(ts1)
            avg_grid = ((g0 or 0) + (g1 or 0)) / 2
            kwh = avg_grid * dt_h / 1000
            season, period = tou_period(dt, tou_cfg)
            rate = day_rate.get(f'{season}_{period}', 0.0)
            if kwh > 0:
                v['import_kwh'] += kwh
                v['import_cost'] += kwh * rate
            elif kwh < 0:
                v['export_kwh'] += abs(kwh)
                v['export_credit'] += abs(kwh) * rate
            v[f'{period}_kwh'] += kwh
            v[f'{period}_cost'] += kwh * rate

        c.execute(
            'INSERT OR REPLACE INTO daily_costs '
            '(date, import_kwh, export_kwh, import_cost, export_credit, '
            ' on_peak_kwh, off_peak_kwh, super_off_peak_kwh, '
            ' on_peak_cost, off_peak_cost, super_off_peak_cost) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (today_str,
             round(v['import_kwh'], 4), round(v['export_kwh'], 4),
             round(v['import_cost'], 4), round(v['export_credit'], 4),
             round(v['on_peak_kwh'], 4), round(v['off_peak_kwh'], 4), round(v['super_off_peak_kwh'], 4),
             round(v['on_peak_cost'], 4), round(v['off_peak_cost'], 4), round(v['super_off_peak_cost'], 4))
        )


def rebuild_daily_costs(year: int = None, from_date=None) -> None:
    """Rebuild daily_costs from readings for a given year (default: current year).

    from_date: optional date object; if provided, only readings on or after this
    date are processed. Useful to avoid applying new TOU configs to historical
    periods where different rates applied.
    """
    # Load rate history — fall back to rates.json if empty
    rate_periods = _load_rate_history()
    fallback_rates = load_rates() if not rate_periods else None
    if not rate_periods and not fallback_rates:
        print('rebuild_daily_costs: no rate data available, skipping')
        return

    target_year = year or date.today().year
    jan1 = int(datetime(target_year, 1, 1).timestamp())
    dec31_end = int(datetime(target_year + 1, 1, 1).timestamp())
    start_ts = max(jan1, int(datetime(from_date.year, from_date.month, from_date.day).timestamp())) \
               if from_date else jan1

    # Load TOU period definitions from DB setting
    tou_cfg = _load_tou_periods()

    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            'SELECT timestamp, grid_w FROM readings '
            'WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp',
            (start_ts, dec31_end)
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


def _fetch_rows_range(start_ts: int, end_ts: int) -> list:
    with sqlite3.connect(DB_PATH) as c:
        return c.execute(
            'SELECT timestamp, solar_w, home_w, battery_w, grid_w '
            'FROM readings WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp',
            (start_ts, end_ts)
        ).fetchall()


def today_rows() -> list:
    start = int(datetime.combine(date.today(), datetime.min.time()).timestamp())
    return _fetch_rows(start)


def day_rows(target: date) -> list:
    start = int(datetime.combine(target, datetime.min.time()).timestamp())
    end   = int(datetime.combine(target + timedelta(days=1), datetime.min.time()).timestamp())
    return _fetch_rows_range(start, end)


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


# ── Poller thread ─────────────────────────────────────────────────────────────
def poller() -> None:
    pw = None
    last_write = 0
    last_purge = 0
    last_cost_rebuild = 0
    last_today_rebuild = 0
    last_holidays_check = 0
    last_rates_check = 0
    last_rachio_event_poll = 0
    last_rain_skip_check = 0
    last_nest_event_poll = 0
    last_pool_poll = 0
    last_kasa_poll = 0
    last_tuya_poll = 0

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

            cost_interval = get_setting_int('cost_rebuild_days', 7) * 86400
            if now - last_cost_rebuild >= cost_interval:
                _spawn_rebuild_daily_costs()
                last_cost_rebuild = now

            if now - last_today_rebuild >= 3600:
                threading.Thread(target=_rebuild_today, daemon=True).start()
                last_today_rebuild = now

            # Holidays + Rates refresh (calendar-driven from shared start date)
            refresh_start = get_setting('refresh_start_date', '')

            # Holidays
            holidays_months = get_setting_int('holidays_poll_months', 1)
            if _is_refresh_due(refresh_start, holidays_months) and now - last_holidays_check >= 86400:
                try:
                    old_year = _read_year_from_json(HOLIDAYS_PATH)
                    load_or_generate_holidays()
                    new_year = _read_year_from_json(HOLIDAYS_PATH)
                    if old_year and new_year and new_year != old_year:
                        _log_success('holidays', 'holidays_updated',
                                     f'Holidays regenerated for {new_year}')
                    print('Holidays refreshed')
                except Exception as exc:
                    print(f'Holidays refresh error: {exc}')
                    _log_system_error('holidays', 'Holiday refresh failed', str(exc))
                last_holidays_check = now

            # Energy rates
            rates_months = get_setting_int('rates_poll_months', 1)
            if _is_refresh_due(refresh_start, rates_months) and now - last_rates_check >= 86400:
                try:
                    old_rates = {}
                    try:
                        with open(RATES_PATH, encoding='utf-8') as f:
                            old_rates = json.load(f)
                    except (FileNotFoundError, json.JSONDecodeError, OSError):
                        pass
                    page_url = get_setting('rates_page_url',
                                           'https://www.sdge.com/total-electric-rates')
                    schedule = get_setting('rate_schedule_name', 'EV-TOU')
                    new_rates = fetch_ev_tou2_rates(page_url=page_url,
                                                    schedule_name=schedule,
                                                    db_path=DB_PATH)
                    rate_keys = ['summer_on_peak', 'summer_off_peak', 'summer_super_off_peak',
                                 'winter_on_peak', 'winter_off_peak', 'winter_super_off_peak',
                                 'base_services_charge_per_day']
                    changes = []
                    for k in rate_keys:
                        old_v = old_rates.get(k)
                        new_v = new_rates.get(k)
                        if old_v is not None and new_v is not None and old_v != new_v:
                            changes.append(f'{k}: {old_v}\u2192{new_v}')
                    if changes:
                        detail_parts = [', '.join(changes)]
                        src_url = new_rates.get('source_url')
                        if src_url:
                            detail_parts.append(src_url)
                        _log_success('rates', 'rates_updated',
                                     f'Rates updated (eff. {new_rates.get("effective_date", "?")})',
                                     detail='  '.join(detail_parts))
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

            # Nest camera/doorbell events (Pub/Sub pull) + thermostat refresh
            nest_event_interval = get_setting_int('nest_poll_interval', 60)
            if now - last_nest_event_poll >= nest_event_interval:
                if get_setting_bool('nest_enabled', False):
                    try:
                        fetch_nest_events()
                    except Exception as exc:
                        print(f'Nest event poll error: {exc}')
                        _log_system_error('nest', 'Event poll error', str(exc))
                    if get_setting_bool('nest_thermostat_enabled', False):
                        try:
                            token = _nest_ensure_token()
                            if token:
                                _nest_refresh_devices(token)
                        except Exception as exc:
                            print(f'Nest thermostat poll error: {exc}')
                last_nest_event_poll = now

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

            # Kasa smart plug state polling (LAN, fast)
            kasa_poll_interval = get_setting_int('kasa_poll_interval', 10)
            if now - last_kasa_poll >= kasa_poll_interval:
                if get_setting_bool('kasa_enabled', False):
                    try:
                        # First time after enable: discover; otherwise just poll state
                        if not _kasa_devices:
                            _kasa_refresh_devices()
                        elif get_setting_bool('kasa_state_poll_enabled', True):
                            _kasa_poll_state()
                    except Exception as exc:
                        print(f'Kasa poll error: {exc}')
                        _log_system_error('kasa', 'State poll error', str(exc))
                last_kasa_poll = now

            # Tuya state polling (LAN, sync socket calls)
            tuya_poll_interval = get_setting_int('tuya_poll_interval', 15)
            if now - last_tuya_poll >= tuya_poll_interval:
                if get_setting_bool('tuya_enabled', False):
                    try:
                        if not _tuya_devices:
                            _tuya_refresh_devices()
                        else:
                            _tuya_poll_state()
                    except Exception as exc:
                        print(f'Tuya poll error: {exc}')
                        _log_system_error('tuya', 'State poll error', str(exc))
                last_tuya_poll = now

        except Exception as exc:
            print(f'Poller error: {type(exc).__name__}: {exc}')
            traceback.print_exc()
            _log_system_error('powerwall', 'Poller error',
                              f'{type(exc).__name__}: {exc}')
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


_device_coords: tuple | None = None  # (lat, lng) cached after first successful Rachio lookup


def _get_device_coords() -> tuple[float, float]:
    """Read lat/long from the Rachio device; cache module-level. Falls back to SD downtown."""
    global _device_coords
    if _device_coords is not None:
        return _device_coords
    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')
        for device in person.get('devices', []):
            lat, lng = device.get('latitude'), device.get('longitude')
            if lat and lng:
                _device_coords = (float(lat), float(lng))
                print(f'Weather: using device coords ({lat}, {lng})')
                return _device_coords
    except Exception as exc:
        print(f'Weather coord lookup error: {exc}')
    _device_coords = (33.09924225276156, -117.06278850408303)
    return _device_coords


def fetch_weather() -> dict:
    global _wx_cache, _wx_ts
    if time.time() - _wx_ts < WX_TTL:
        return _wx_cache

    lookback = get_setting_int('rain_lookback_days', 5)
    lat, lng = _get_device_coords()
    url = (
        'https://api.open-meteo.com/v1/forecast'
        f'?latitude={lat}&longitude={lng}'
        '&current_weather=true'
        '&daily=precipitation_sum,cloudcover_mean'
        f'&past_days={lookback}'
        '&forecast_days=3&timezone=America%2FLos_Angeles'
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        cw    = data.get('current_weather', {})
        daily = data.get('daily', {})
        dates    = daily.get('time', [])
        precip   = daily.get('precipitation_sum', [])
        clouds   = daily.get('cloudcover_mean', [])

        # With past_days=lookback + forecast_days=3:
        # indices [0 .. lookback-1] = past days
        # indices [lookback .. lookback+2] = today, tomorrow, day-after
        n = len(dates)
        today_idx    = lookback if n > lookback else None
        tomorrow_idx = lookback + 1 if n > lookback + 1 else None

        clouds_tm = clouds[tomorrow_idx] if tomorrow_idx is not None else None
        rain_tm   = precip[tomorrow_idx] if tomorrow_idx is not None else None

        # Rain history: past N days + today (saturation matters now); exclude future forecast
        rain_history = []
        for i, (d, mm) in enumerate(zip(dates, precip)):
            if today_idx is not None and i <= today_idx:
                rain_history.append({'date': d, 'mm': mm or 0})

        # Forecast lookup by ISO date — covers today, tomorrow, day-after
        rain_forecast = {}
        if today_idx is not None:
            for i in range(today_idx, n):
                rain_forecast[dates[i]] = precip[i] or 0

        _wx_cache = {
            'temp_f':          round(cw.get('temperature', 0) * 9 / 5 + 32, 1),
            'desc':            WMO.get(cw.get('weathercode', 0), ''),
            'weathercode':     cw.get('weathercode', 0),
            'tomorrow_cloud':  clouds_tm,
            'tomorrow_rain':   rain_tm,
            'bad_forecast':    (clouds_tm or 0) > 60 or (rain_tm or 0) > 1,
            'rain_history':    rain_history,
            'rain_forecast':   rain_forecast,
        }

        # Fetch AQI from Open-Meteo Air Quality API
        try:
            aqi_url = (
                'https://air-quality-api.open-meteo.com/v1/air-quality'
                f'?latitude={lat}&longitude={lng}'
                '&current=us_aqi&timezone=America%2FLos_Angeles'
            )
            with urllib.request.urlopen(aqi_url, timeout=10) as aq:
                aqi_data = json.loads(aq.read())
            _wx_cache['aqi'] = aqi_data.get('current', {}).get('us_aqi')
        except Exception:
            _wx_cache['aqi'] = None

        _wx_ts = time.time()
    except Exception as exc:
        print(f'Weather error: {exc}')
        if not _wx_cache:
            _wx_cache = {}

    return _wx_cache


# ── Solar Forecast ────────────────────────────────────────────────────────────
_sf_cache: dict  = {}
_sf_ts: float    = 0.0
_stf_cache: dict = {}
_stf_ts: float   = 0.0
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
        '?latitude=33.09924225276156&longitude=-117.06278850408303'
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


def fetch_tomorrow_solar_forecast() -> dict:
    """Return tomorrow's hourly solar estimate and total kWh.

    Result: {date, hours: {0..23: watts}, kwh_by_hour: {0..23: float}, total_kwh: float}
    Each kwh_by_hour value is watts / 1000 (each slot = 1 hour).
    Cached for 1 hour.
    """
    global _stf_cache, _stf_ts
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()

    if _stf_cache.get('date') == tomorrow_str and time.time() - _stf_ts < SF_TTL:
        return _stf_cache

    url = (
        'https://api.open-meteo.com/v1/forecast'
        '?latitude=33.09924225276156&longitude=-117.06278850408303'
        '&hourly=shortwave_radiation'
        '&forecast_days=2&timezone=America%2FLos_Angeles'
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())

        hourly = data.get('hourly', {})
        times  = hourly.get('time', [])
        rads   = hourly.get('shortwave_radiation', [])

        peak_solar = _peak_solar_w()
        scale = peak_solar / PEAK_RAD_WM2

        hours: dict[int, int] = {}
        for t_str, rad in zip(times, rads):
            day_part, hour_part = t_str.split('T')
            if day_part != tomorrow_str:
                continue
            h = int(hour_part.split(':')[0])
            hours[h] = round(max(0, (rad or 0) * scale))

        kwh_by_hour = {h: round(w / 1000, 2) for h, w in hours.items()}
        total_kwh   = round(sum(kwh_by_hour.values()), 2)

        _stf_cache = {
            'date':        tomorrow_str,
            'hours':       hours,         # watts per hour
            'kwh_by_hour': kwh_by_hour,   # kWh per hour
            'total_kwh':   total_kwh,     # estimated day total
        }
        _stf_ts = time.time()

        # Persist so rules.py can use it as a condition without calling the API
        try:
            with sqlite3.connect(DB_PATH) as c:
                c.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('tomorrow_solar_kwh', ?)",
                    (str(total_kwh),)
                )
        except Exception:
            pass

    except Exception as exc:
        print(f'Tomorrow solar forecast error: {exc}')
        if _stf_cache.get('date') != tomorrow_str:
            _stf_cache = {'date': tomorrow_str, 'hours': {}, 'kwh_by_hour': {}, 'total_kwh': 0.0}

    return _stf_cache


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

        # Pump 1 = pool pump; edge pump via circuit 506 (pump 0 is unreliable for state)
        pool_pump_on    = bool(_nested(pump1, 'state', 'value'))
        pool_pump_watts = _nested(pump1, 'watts_now', 'value')
        pool_pump_rpm   = _nested(pump1, 'rpm_now', 'value')
        pool_pump_gpm   = _nested(pump1, 'gpm_now', 'value')
        edge_pump_on    = bool(_nested(c506, 'value'))
        edge_pump_watts = _nested(pump0, 'watts_now', 'value')
        edge_pump_rpm   = _nested(pump0, 'rpm_now', 'value')
        edge_pump_gpm   = _nested(pump0, 'gpm_now', 'value')

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
            'pump_rpm':        int(pool_pump_rpm)   if pool_pump_rpm   is not None else None,
            'pump_gpm':        int(pool_pump_gpm)   if pool_pump_gpm   is not None else None,
            'edge_pump_on':    edge_pump_on,
            'edge_pump_watts': int(edge_pump_watts) if edge_pump_watts is not None else None,
            'edge_pump_rpm':   int(edge_pump_rpm)   if edge_pump_rpm   is not None else None,
            'edge_pump_gpm':   int(edge_pump_gpm)   if edge_pump_gpm   is not None else None,
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


def _accumulate_pool_gallons(pool: dict) -> None:
    global _pool_gallons_today, _pool_gallons_date, _pool_last_accum_ts
    global _pool_normal_gpm, _pool_cleaner_gpm, _pool_edge_gpm
    now   = time.time()
    today = time.strftime('%Y-%m-%d', time.localtime(now))
    if today != _pool_gallons_date:
        _pool_gallons_today = 0.0
        _pool_gallons_date  = today
        _pool_last_accum_ts = now
        threading.Thread(target=_recalc_pool_target, daemon=True).start()
        return
    if _pool_last_accum_ts == 0.0:
        _pool_last_accum_ts = now
        return
    elapsed_min = (now - _pool_last_accum_ts) / 60.0
    _pool_last_accum_ts = now
    if elapsed_min <= 0:
        return
    pool_gpm      = pool.get('pump_gpm')
    edge_gpm      = pool.get('edge_pump_gpm')
    cleaner_is_on = bool(pool.get('cleaner_on'))
    gpm_updates: list = []
    if pool.get('pump_on') and pool_gpm:
        _pool_gallons_today += pool_gpm * elapsed_min
        if cleaner_is_on:
            if float(pool_gpm) != _pool_cleaner_gpm:
                _pool_cleaner_gpm = float(pool_gpm)
                gpm_updates.append(('pool_cached_cleaner_gpm', str(_pool_cleaner_gpm)))
        else:
            if float(pool_gpm) != _pool_normal_gpm:
                _pool_normal_gpm = float(pool_gpm)
                gpm_updates.append(('pool_cached_normal_gpm', str(_pool_normal_gpm)))
    if pool.get('edge_pump_on') and edge_gpm:
        _pool_gallons_today += edge_gpm * elapsed_min
        if float(edge_gpm) != _pool_edge_gpm:
            _pool_edge_gpm = float(edge_gpm)
            gpm_updates.append(('pool_cached_edge_gpm', str(_pool_edge_gpm)))
    if gpm_updates:
        try:
            with sqlite3.connect(DB_PATH) as conn:
                conn.execute('PRAGMA busy_timeout=5000')
                conn.executemany(
                    'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                    gpm_updates
                )
                conn.commit()
        except Exception as exc:
            print(f'Pool GPM cache write error: {exc}')


_CLEANER_PRESET_RPM = 2950  # pump gateway preset RPM for cleaner mode
_MAX_SEGMENT_HOURS  = 18    # cap segments to guard against missed off-events


def _recalc_pool_target() -> None:
    """Derive weekday/weekend daily gallons targets from 30 days of event_log.
    Called in a daemon thread at midnight rollover and on server startup.
    """
    global _pool_normal_gpm, _pool_cleaner_gpm, _pool_edge_gpm

    normal_gpm = _pool_normal_gpm if _pool_normal_gpm > 0 else 23.0
    edge_gpm   = _pool_edge_gpm   if _pool_edge_gpm   > 0 else 34.0
    if _pool_cleaner_gpm > 0:
        cleaner_gpm = _pool_cleaner_gpm
    elif _pool_normal_gpm > 0:
        normal_rpm  = _pool.get('pump_rpm') or 1770
        cleaner_gpm = normal_gpm * (_CLEANER_PRESET_RPM / normal_rpm)
    else:
        cleaner_gpm = 38.3

    cutoff_ts    = int(time.time()) - 30 * 86400
    MAX_SEG_SECS = _MAX_SEGMENT_HOURS * 3600

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            conn.execute('PRAGMA busy_timeout=5000')
            rows = conn.execute(
                '''SELECT ts, event_type, title FROM event_log
                   WHERE  system = 'pool'
                     AND  event_type IN ('pump_changed','edge_pump_changed','cleaner_changed')
                     AND  ts >= ?
                   ORDER BY ts''',
                (cutoff_ts,)
            ).fetchall()
    except Exception as exc:
        print(f'_recalc_pool_target DB error: {exc}')
        return

    from collections import defaultdict
    by_date: dict = defaultdict(list)
    for ts, event_type, title in rows:
        by_date[time.strftime('%Y-%m-%d', time.localtime(ts))].append((ts, event_type, title))

    today = time.strftime('%Y-%m-%d', time.localtime(time.time()))

    def build_segments(events, day, etype):
        segs, start = [], None
        day_start = int(time.mktime(time.strptime(day, '%Y-%m-%d')))
        for ts, et, title in events:
            if et != etype:
                continue
            if 'turned on' in title and start is None:
                start = ts
            elif 'turned off' in title and start is not None:
                segs.append((start, start + min(ts - start, MAX_SEG_SECS)))
                start = None
        if start is not None:
            segs.append((start, start + min(day_start + 86400 - start, MAX_SEG_SECS)))
        return segs

    def intersect_min(a_segs, b_segs):
        total = 0.0
        for as_, ae in a_segs:
            for bs, be in b_segs:
                lo, hi = max(as_, bs), min(ae, be)
                if hi > lo:
                    total += hi - lo
        return total / 60.0

    weekday_vals: list = []
    weekend_vals: list = []

    for day, events in sorted(by_date.items()):
        if day == today:
            continue
        pump_segs    = build_segments(events, day, 'pump_changed')
        cleaner_segs = build_segments(events, day, 'cleaner_changed')
        edge_segs    = build_segments(events, day, 'edge_pump_changed')
        if not pump_segs:
            continue
        pump_min    = sum(e - s for s, e in pump_segs) / 60.0
        cleaner_min = intersect_min(cleaner_segs, pump_segs)
        normal_min  = pump_min - cleaner_min
        edge_min    = sum(e - s for s, e in edge_segs) / 60.0
        gallons     = normal_min * normal_gpm + cleaner_min * cleaner_gpm + edge_min * edge_gpm
        dow = time.localtime(time.mktime(time.strptime(day, '%Y-%m-%d'))).tm_wday
        (weekday_vals if dow < 5 else weekend_vals).append(gallons)

    # Seed today's partial gallons — same logic, open segments cap at now
    now_ts = time.time()
    today_events = by_date.get(today, [])
    if today_events:
        def build_segments_now(events, etype):
            segs, start = [], None
            for ts, et, title in events:
                if et != etype:
                    continue
                if 'turned on' in title and start is None:
                    start = ts
                elif 'turned off' in title and start is not None:
                    segs.append((start, start + min(ts - start, MAX_SEG_SECS)))
                    start = None
            if start is not None:
                segs.append((start, start + min(now_ts - start, MAX_SEG_SECS)))
            return segs

        t_pump    = build_segments_now(today_events, 'pump_changed')
        t_cleaner = build_segments_now(today_events, 'cleaner_changed')
        t_edge    = build_segments_now(today_events, 'edge_pump_changed')
        t_pump_min    = sum(e - s for s, e in t_pump)    / 60.0
        t_cleaner_min = intersect_min(t_cleaner, t_pump)
        t_normal_min  = t_pump_min - t_cleaner_min
        t_edge_min    = sum(e - s for s, e in t_edge)    / 60.0
        seeded = t_normal_min * normal_gpm + t_cleaner_min * cleaner_gpm + t_edge_min * edge_gpm
        global _pool_gallons_today, _pool_gallons_date, _pool_last_accum_ts
        _pool_gallons_today = seeded
        _pool_gallons_date  = today
        _pool_last_accum_ts = now_ts

    if not weekday_vals and not weekend_vals:
        return

    def _avg(vals, fallback=21500):
        return int(sum(vals) / len(vals)) if vals else fallback

    target_wd = _avg(weekday_vals[-2:])
    target_we = _avg(weekend_vals[-2:], target_wd)

    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('PRAGMA busy_timeout=5000')
            conn.executemany(
                'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                [('pool_gallons_target_weekday', str(target_wd)),
                 ('pool_gallons_target_weekend', str(target_we))]
            )
            conn.commit()
        print(f'Pool target recalc: weekday={target_wd} gal, weekend={target_we} gal '
              f'({len(weekday_vals)} weekdays, {len(weekend_vals)} weekends)')
    except Exception as exc:
        print(f'_recalc_pool_target write error: {exc}')


def fetch_pool() -> dict:
    global _pool, _pool_ts, _pool_gallons_today, _pool_gallons_date, _pool_last_accum_ts
    if not get_setting_bool('pool_enabled', True):
        return _pool or {'temp_f': None, 'pump_on': None, 'spa_temp_f': None}
    pool_ttl = get_setting_int('pool_poll_interval', POOL_POLL_INTERVAL)
    # Clock-aligned polling: fetch when we enter a new interval window
    # e.g. 900s → :00, :15, :30, :45 regardless of server start time
    now = time.time()
    if _pool_ts and int(now) // pool_ttl == int(_pool_ts) // pool_ttl:
        return _pool
    # asyncio.run() spins a fresh event loop per call. Safe today because
    # ScreenLogic uses one-shot UDP discovery with no persistent socket — a
    # new loop per poll has no state to lose. If we ever cache a gateway
    # connection, switch to a persistent loop on the Kasa pattern
    # (_kasa_loop / _kasa_submit) to avoid handshake churn.
    try:
        _pool    = asyncio.run(_pool_fetch_async())
        _pool_ts = time.time()
        _log_pool_changes(_pool)
        _accumulate_pool_gallons(_pool)
        _pool['gallons_today']  = int(_pool_gallons_today)
        is_weekday = datetime.today().weekday() < 5
        key = 'pool_gallons_target_weekday' if is_weekday else 'pool_gallons_target_weekend'
        _pool['gallons_target'] = get_setting_int(key, 21500)
    except Exception as exc:
        print(f'Pool error: {exc}')
        _log_system_error('pool', 'Pool fetch error', str(exc))
        if not _pool:
            _pool = {'temp_f': None, 'pump_on': None, 'spa_temp_f': None}
    return _pool


# ── Pool circuit control (screenlogicpy async_set_circuit) ────────────────────
# External IDs are stored in switches_meta so user-edited name/room survive
# reboots + rediscovery. For most circuits the external_id is the numeric
# circuit ID as a string. Feature 1's ScreenLogic circuit ID is assigned
# dynamically — we resolve it by name at toggle time.
POOL_CIRCUITS = [
    # (external_id, circuit_id, default_name,  pool_cache_field)
    ('500',   500,  'Spa',         'spa_circuit_on'),
    ('501',   501,  'Pool Light',  'pool_light_on'),
    ('502',   502,  'Water Light', 'water_light_on'),
    ('503',   503,  'Spa Light',   'spa_light_on'),
    ('504',   504,  'Waterfall',   'waterfall_on'),
    ('505',   505,  'Pool',        'pool_circuit_on'),
    ('506',   506,  'Edge Pump',   'edge_pump_on'),
    ('507',   507,  'Spillway',    'spillway_on'),
    ('508',   508,  'Cleaner',     'cleaner_on'),
    ('feat1', None, 'Feature 1',   'feature1_on'),
]
POOL_EXT_TO_FIELD = {ext: field for ext, _, _, field in POOL_CIRCUITS}


def _pool_discover_circuits() -> int:
    """Upsert the known pool circuits into switches_meta. Safe to call on
    every startup — existing user edits (name, room, hidden) are preserved."""
    with sqlite3.connect(DB_PATH) as c:
        for ext_id, _, default_name, _ in POOL_CIRCUITS:
            row = c.execute(
                'SELECT id FROM switches_meta WHERE provider=? AND external_id=?',
                ('pool', ext_id)
            ).fetchone()
            if row is None:
                c.execute(
                    'INSERT INTO switches_meta (provider, external_id, kind, name) '
                    'VALUES (?,?,?,?)',
                    ('pool', ext_id, 'circuit', default_name)
                )
    return len(POOL_CIRCUITS)


async def _pool_resolve_feature1_id(gateway) -> int:
    """Scan gateway data for the 'Feature 1' circuit and return its circuit ID."""
    data = gateway.get_data()
    circuit = data.get('circuit') or data.get(b'circuit') or {}
    for cid, cdata in circuit.items():
        if not isinstance(cdata, dict):
            continue
        name = cdata.get('name') or cdata.get(b'name')
        if isinstance(name, dict):
            name = name.get('value')
        if isinstance(name, bytes):
            name = name.decode('utf-8', errors='ignore')
        if isinstance(name, str) and name.strip() == 'Feature 1':
            try:
                return int(cid)
            except (TypeError, ValueError):
                continue
    raise ValueError('Feature 1 circuit not found on ScreenLogic gateway')


async def _pool_set_circuit_async(ext_id: str, on: bool) -> bool:
    """Set a pool circuit on/off via screenlogicpy. Returns the new state."""
    from screenlogicpy import ScreenLogicGateway
    from screenlogicpy.discovery import async_discover
    gateways = await async_discover()
    if not gateways:
        raise RuntimeError('No ScreenLogic gateway found via UDP discovery')
    gw      = gateways[0]
    gateway = ScreenLogicGateway()
    await gateway.async_connect(ip=gw['ip'], port=gw.get('port', 80))
    try:
        if ext_id == 'feat1':
            await gateway.async_update()
            circuit_id = await _pool_resolve_feature1_id(gateway)
        else:
            try:
                circuit_id = int(ext_id)
            except ValueError:
                raise ValueError(f'Invalid pool circuit ext_id: {ext_id}')
        await gateway.async_set_circuit(circuit_id, 1 if on else 0)
    finally:
        await gateway.async_disconnect()
    return bool(on)


def pool_set_circuit(ext_id: str, on: bool) -> bool:
    """Sync wrapper for pool circuit toggle. Returns the new state."""
    result = asyncio.run(_pool_set_circuit_async(ext_id, on))
    # Nudge the cache so /api/switches reflects the new state immediately and
    # the next _log_pool_changes doesn't double-log (it compares against
    # _pool_prev, which we advance here).
    field = POOL_EXT_TO_FIELD.get(ext_id)
    if field:
        _pool[field] = on
        _pool_prev[field] = on
        _pool_pending.pop(field, None)
    return result


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


@app.route('/<path:filename>')
def frontend_static(filename):
    return send_from_directory(os.path.join('static', 'frontend'), filename)


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


def _filter_chart_rows(raw: list) -> list:
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
        out.append({'ts': r[0], 'solar_w': r[1], 'home_w': r[2], 'grid_w': r[4]})
    return out


@app.route('/api/today')
def api_today():
    return jsonify(_filter_chart_rows(today_rows()))


@app.route('/api/day')
def api_day():
    date_str = request.args.get('date', '')
    try:
        target = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'invalid date, use YYYY-MM-DD'}), 400
    return jsonify(_filter_chart_rows(day_rows(target)))


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


@app.route('/api/solar-forecast/tomorrow')
def api_solar_forecast_tomorrow():
    return jsonify(fetch_tomorrow_solar_forecast())


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


@app.route('/api/debug/rachio/full')
def api_debug_rachio_full():
    """Dump everything we can pull from Rachio for each device — shows all available fields.
    Tries documented + commonly-undocumented endpoints to find skip prediction data."""
    def _try(path):
        try:
            return _rachio_get(path)
        except Exception as exc:
            return {'__error__': str(exc)}

    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')
        result = {
            'person_info':     _try('/person/info'),
            'person':          person,
            'devices_full':    {},
        }
        for device in person.get('devices', []):
            did = device['id']
            zone_ids = [z.get('id') for z in (device.get('zones') or []) if z.get('id')]
            schedule_ids = [r.get('id') for r in (device.get('scheduleRules') or []) if r.get('id')]
            flex_ids = [r.get('id') for r in (device.get('flexScheduleRules') or []) if r.get('id')]

            now_ms = int(time.time() * 1000)
            future_ms = now_ms + 7 * 86400 * 1000

            result['devices_full'][did] = {
                'name':                       device.get('name'),
                'device_keys':                sorted(device.keys()),
                'rainDelayExpirationDate':    device.get('rainDelayExpirationDate'),
                'rainDelayStartDate':         device.get('rainDelayStartDate'),
                # Try various device sub-endpoints
                'current_schedule':           _try(f'/device/{did}/current_schedule'),
                'forecast':                   _try(f'/device/{did}/forecast'),
                'forecast_summary':           _try(f'/device/{did}/forecast_summary'),
                'state':                      _try(f'/device/{did}/state'),
                # Future-window event probes — does the events endpoint expose scheduled future skips?
                'events_future':              _try(f'/device/{did}/event?startTime={now_ms}&endTime={future_ms}'),
                # Possibly-scheduled / upcoming endpoints (undocumented; mostly likely 404)
                'upcoming':                   _try(f'/device/{did}/upcoming'),
                'upcoming_runs':              _try(f'/device/{did}/upcoming_runs'),
                'scheduled_runs':             _try(f'/device/{did}/scheduled_runs'),
                'scheduled_events':           _try(f'/device/{did}/scheduled_events'),
                'calendar':                   _try(f'/device/{did}/calendar'),
                'planned':                    _try(f'/device/{did}/planned'),
                # Per schedule rule (full detail)
                'scheduleRule_detail':        {sid: _try(f'/schedulerule/{sid}') for sid in schedule_ids},
                'scheduleRule_skip':          {sid: _try(f'/schedulerule/{sid}/skip') for sid in schedule_ids},
                'scheduleRule_next':          {sid: _try(f'/schedulerule/{sid}/next_run') for sid in schedule_ids},
                'scheduleRule_skipped':       {sid: _try(f'/schedulerule/{sid}/skipped') for sid in schedule_ids},
                'scheduleRule_upcoming':      {sid: _try(f'/schedulerule/{sid}/upcoming') for sid in schedule_ids},
                'flexScheduleRule_detail':    {fid: _try(f'/flexschedulerule/{fid}') for fid in flex_ids},
                # Per zone
                'zone_detail':                {zid: _try(f'/zone/{zid}') for zid in zone_ids[:3]},  # first 3 only
            }
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


def _rachio_runs_in_window(start_h: int, start_m: int, rachio_days: set,
                           hours: int = 48, past_hours: int = 0):
    """Return all local datetimes in [now - past_hours, now + hours] matching hour/minute and day set."""
    from datetime import time as dt_time
    run_t  = dt_time(hour=int(start_h), minute=int(start_m))
    now    = datetime.now()
    start  = now - timedelta(hours=past_hours)
    cutoff = now + timedelta(hours=hours)
    runs   = []
    total_days = (hours + past_hours) // 24 + 2
    for delta in range(-((past_hours // 24) + 1), total_days):
        cdate      = (now + timedelta(days=delta)).date()
        rachio_dow = (cdate.weekday() + 1) % 7
        if rachio_dow in rachio_days:
            cdt = datetime.combine(cdate, run_t)
            if start <= cdt <= cutoff:
                runs.append(cdt)
    return runs


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


RAIN_MIN_MM = 1.0  # below this, rain is treated as noise (no qualifying event)


def evaluate_rain_skip() -> None:
    """Apply a post-rain saturation buffer that extends ONE+ days past Rachio's last skip.

    Behavior:
    - Skip end is anchored to max(last_rain_date, last_rachio_wi_skip_date) + skip_days.
    - skip_days = floor(accumulated_rain_mm / mm_per_skip_day), capped at max_days.
    - Defers entirely if Rachio's own forecast covers the next 24-48h (Rachio will handle).
    - Never shortens an active rainDelayExpirationDate.
    """
    global _rain_skip_ts
    if not get_setting_bool('rain_skip_enabled', False):
        return
    if not get_setting_bool('rachio_enabled', True):
        return

    import math
    mm_per_day    = get_setting_int('rain_mm_per_skip_day', 1)
    max_days      = get_setting_int('rain_skip_max_days', 7)
    lookback_days = get_setting_int('rain_lookback_days', 5)

    wx = fetch_weather()
    rain_history = wx.get('rain_history', [])
    if not rain_history:
        return

    # Need at least one qualifying rain day in window
    rainy = [(e['date'], e['mm']) for e in rain_history if (e['mm'] or 0) >= RAIN_MIN_MM]
    if not rainy:
        _rain_skip_ts = time.time()
        return

    last_rain_date_str = max(rainy, key=lambda r: r[0])[0]
    last_rain_date     = datetime.strptime(last_rain_date_str, '%Y-%m-%d').date()

    accumulated = sum((e['mm'] or 0) for e in rain_history)
    skip_days   = min(int(math.floor(accumulated / mm_per_day)), max_days) if mm_per_day > 0 else 0
    if skip_days <= 0:
        _rain_skip_ts = time.time()
        return

    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')

        for device in person.get('devices', []):
            did   = device['id']
            dname = device.get('name', '?')

            # Anchor: latest of (last rain day) or (Rachio's most recent WI skip date)
            wi_skips, threshold_in = _rachio_wi_skip_info(did, lookback_hours=lookback_days * 24)
            last_skip_date = None
            for _sched_id, run_dt in wi_skips:
                d = run_dt.date()
                if last_skip_date is None or d > last_skip_date:
                    last_skip_date = d
            anchor_date = max(last_rain_date, last_skip_date) if last_skip_date else last_rain_date

            # Fixed end-of-skip — never drags forward across hourly evaluations
            end_dt     = datetime.combine(anchor_date, datetime.min.time()) + timedelta(days=skip_days)
            our_end_ts = end_dt.timestamp()
            now_ts     = time.time()
            if our_end_ts <= now_ts:
                continue  # buffer has already passed

            # Existing rainDelayExpirationDate guard — don't shorten manual / prior delay
            existing_end_ts = 0
            rd_exp = device.get('rainDelayExpirationDate')
            if rd_exp and isinstance(rd_exp, (int, float)) and rd_exp > 0:
                existing_end_ts = rd_exp / 1000
            if existing_end_ts >= our_end_ts:
                existing_dt = datetime.fromtimestamp(existing_end_ts).strftime('%Y-%m-%d %H:%M')
                print(f'Rain skip: {dname} — existing delay until {existing_dt} is longer, skipping')
                continue

            # Forecast deferral — if Rachio's forecast says rain in next 24-48h, defer
            try:
                forecast_by_date = _rachio_device_forecast(did)
                today_d     = datetime.now().date()
                tomorrow_d  = today_d + timedelta(days=1)
                today_in    = forecast_by_date.get(today_d.isoformat(), 0)
                tomorrow_in = forecast_by_date.get(tomorrow_d.isoformat(), 0)
                if today_in >= threshold_in or tomorrow_in >= threshold_in:
                    print(f'Rain skip: {dname} — Rachio forecast covers next 48h '
                          f'(today={today_in:.2f}", tomorrow={tomorrow_in:.2f}", '
                          f'threshold={threshold_in:.2f}"), deferring')
                    continue
            except Exception as exc:
                print(f'Rain skip forecast check error: {exc}')
                continue  # fail safe — defer to Rachio

            # Apply rain delay anchored to the buffer end
            duration_secs = int(our_end_ts - now_ts)
            _rachio_put('/device/rain_delay', {'id': did, 'duration': duration_secs})

            # Invalidate the schedule cache so /api/schedule reflects the new delay
            # immediately on next fetch (instead of waiting for the 3-hour TTL).
            global _rachio_ts
            _rachio_ts = 0.0

            existing_info = ''
            if existing_end_ts > now_ts:
                existing_dt = datetime.fromtimestamp(existing_end_ts).strftime('%Y-%m-%d %H:%M')
                existing_info = f'  existing_delay_until: {existing_dt}'
            anchor_info = (f'rachio_last_skip: {last_skip_date}'
                           if last_skip_date else f'last_rain: {last_rain_date}')
            detail = (f'device: {dname}  accumulated: {accumulated:.1f}mm  '
                      f'lookback: {len(rain_history)} days  skip: {skip_days} days  '
                      f'anchor: {anchor_date} ({anchor_info})  '
                      f'delay_until: {end_dt.strftime("%Y-%m-%d %H:%M")}{existing_info}')

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
            print(f'Rain skip applied: {dname} — {skip_days} days '
                  f'(anchor {anchor_date}, end {end_dt.strftime("%Y-%m-%d %H:%M")})')

        _rain_skip_ts = time.time()
    except Exception as exc:
        import traceback
        err = f'{type(exc).__name__}: {exc}\n' + traceback.format_exc()[-400:]
        print(f'Rain skip error: {err}')
        _log_system_error('rachio', 'Rain skip evaluation error', err)


def _rachio_device_forecast(device_id: str) -> dict:
    """Return {ISO_date: calculated_precip_in} from Rachio's own forecast endpoint.

    Same data source Rachio uses internally for Weather Intelligence decisions —
    far more accurate for predicting skips than Open-Meteo.
    """
    forecast_by_date = {}
    try:
        data = _rachio_get(f'/device/{device_id}/forecast')
        if not isinstance(data, dict):
            return forecast_by_date
        # Combine 'current' + 'forecast' entries
        entries = []
        cur = data.get('current')
        if isinstance(cur, dict):
            entries.append(cur)
        fc = data.get('forecast')
        if isinstance(fc, list):
            entries.extend(fc)
        for entry in entries:
            ts_ms = entry.get('localizedTimeStamp') or (entry.get('time') and entry.get('time') * 1000)
            if not ts_ms:
                continue
            d = datetime.fromtimestamp(ts_ms / 1000).date().isoformat()
            precip_in = entry.get('calculatedPrecip')
            if precip_in is None:
                precip_in = entry.get('precipIntensity', 0)
            forecast_by_date[d] = float(precip_in or 0)
    except Exception as exc:
        print(f'Rachio device forecast error: {exc}')
    return forecast_by_date


def _rachio_wi_skip_info(device_id: str, lookback_hours: int = 48):
    """Fetch device events; return (skip_set, threshold_in).

    skip_set: set of (scheduleId, run_dt_local) tuples for past WEATHER_INTELLIGENCE_SKIP events
    threshold_in: most recent per-schedule rain threshold parsed from event summary (default 0.06)
    """
    import re
    skip_set = set()
    threshold_in = 0.06  # Rachio's default per-schedule WI threshold
    try:
        end_ms   = int(time.time() * 1000)
        start_ms = end_ms - lookback_hours * 3600 * 1000
        raw = _rachio_get(f'/device/{device_id}/event?startTime={start_ms}&endTime={end_ms}')
        if not isinstance(raw, list):
            return skip_set, threshold_in
        for ev in raw:
            if ev.get('subType') != 'WEATHER_INTELLIGENCE_SKIP':
                continue
            sched_id = ev.get('scheduleId') or ''
            summary  = ev.get('summary') or ''

            # Parse "scheduled for M/D at HH:MM AM/PM" from summary
            m = re.search(r'scheduled for (\d+)/(\d+) at (\d+):(\d+)\s*([AP])M', summary)
            run_dt = None
            if m:
                mo, dy, hh, mm, ap = m.groups()
                hh = int(hh) % 12
                if ap == 'P':
                    hh += 12
                # Year: assume current year; if month is in the future relative to today, assume previous year
                year = datetime.now().year
                try:
                    candidate = datetime(year, int(mo), int(dy), hh, int(mm))
                    if candidate > datetime.now() + timedelta(days=1):
                        candidate = candidate.replace(year=year - 1)
                    run_dt = candidate
                except Exception:
                    run_dt = None
            # Fallback: use eventDate (within ~3 minutes of the actual run start)
            if run_dt is None:
                ed = ev.get('eventDate')
                if isinstance(ed, (int, float)):
                    run_dt = datetime.fromtimestamp(ed / 1000)

            if run_dt is not None:
                skip_set.add((sched_id, run_dt))

            # Extract threshold ("threshold of 0.06 in")
            tm = re.search(r'threshold of (\d+\.\d+)\s*in', summary)
            if tm:
                try:
                    threshold_in = float(tm.group(1))
                except Exception:
                    pass
    except Exception as exc:
        print(f'Rachio WI skip fetch error: {exc}')
    return skip_set, threshold_in


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
        events    = []
        now_local = datetime.now()

        for device in person.get('devices', []):
            rules = device.get('scheduleRules', [])
            print(f'Rachio device {device.get("name","?")}: {len(rules)} schedule rules')

            # Active rain delay window for this device (None if no delay)
            rd_until = None
            rd_exp = device.get('rainDelayExpirationDate')
            if rd_exp and isinstance(rd_exp, (int, float)) and rd_exp > 0:
                rd_dt = datetime.fromtimestamp(rd_exp / 1000, tz=timezone.utc).astimezone().replace(tzinfo=None)
                if rd_dt > now_local:
                    rd_until = rd_dt

            # Rachio's per-schedule WI threshold (parsed from past skip summaries)
            _wi_skip_set, threshold_in = _rachio_wi_skip_info(device['id'], lookback_hours=48)

            # Rachio's own forecast — same source it uses for WI skip decisions
            forecast_by_date = _rachio_device_forecast(device['id'])

            for rule in rules:
                if not rule.get('enabled', True):
                    continue
                try:
                    h    = rule.get('startHour', 0)
                    m    = rule.get('startMinute', 0)
                    days = _rachio_days_from_job_types(rule.get('scheduleJobTypes', []))
                    duration_min = round(rule.get('totalDuration', rule.get('duration', 0)) / 60)
                    name         = rule.get('name', rule.get('externalName', 'Irrigation'))

                    # Enumerate future runs only (past runs belong in Event Log)
                    for run_dt in _rachio_runs_in_window(h, m, days, hours=48):
                        evt = {
                            'fire_time':    run_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                            'name':         name,
                            'duration_min': duration_min,
                            'source':       'rachio',
                        }

                        skip_reason = None

                        # 1. Active rain delay (manual or smart skip)
                        if rd_until is not None and run_dt <= rd_until:
                            skip_reason = 'Skipped due to Rain'

                        # 2. Future run with Rachio's own forecast above threshold
                        if skip_reason is None:
                            forecast_in = forecast_by_date.get(run_dt.date().isoformat(), 0)
                            if forecast_in >= threshold_in:
                                skip_reason = f'Likely skipped — {forecast_in:.2f}" forecast'

                        if skip_reason:
                            evt['skip']        = True
                            evt['skip_reason'] = skip_reason
                        events.append(evt)
                except Exception as exc:
                    print(f'Rachio rule skip: {exc}')
                    continue

        events.sort(key=lambda e: e['fire_time'])
        _rachio_schedule = events
        _rachio_ts = time.time()
        skipped_count = sum(1 for e in events if e.get('skip'))
        print(f'Rachio: fetched {len(events)} upcoming events ({skipped_count} skipped)')
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
_abode_status_lock = threading.Lock()
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
        with _abode_status_lock:
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
        with _abode_status_lock:
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
        with _abode_status_lock:
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
            with _abode_status_lock:
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
                    with _abode_status_lock:
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
                with _abode_status_lock:
                    _abode_status['state'] = 'connecting'
                print('Abode: connecting…')
                abode = Abode(username=ABODE_EMAIL, password=ABODE_PASSWORD,
                              auto_login=True, get_devices=True)
                _abode_instance = abode
                with _abode_status_lock:
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
                with _abode_status_lock:
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
_nest_devices_raw: list = []    # last raw device list from SDM API (for debug)
_nest_devices_ts: float = 0.0
_NEST_DEVICE_CACHE_TTL = 3600   # 1 hour
_nest_event_counters: dict = {} # running tally of event types seen over time
_nest_poll_stats: dict = {'calls': 0, 'last_call_ts': None, 'last_pull_count': None, 'last_error': None, 'pull_count_total': 0}

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


NEST_OAUTH_TOKEN_URL = 'https://oauth2.googleapis.com/token'


def _nest_oauth_exchange(extra: dict) -> dict:
    """POST to Google's OAuth2 token endpoint with client credentials + the
    caller-supplied grant fields (grant_type, refresh_token or code+redirect_uri).
    Returns the parsed token response. Raises on HTTP/network error."""
    client_id     = get_setting('nest_client_id', '')
    client_secret = get_setting('nest_client_secret', '')
    data = {'client_id': client_id, 'client_secret': client_secret, **extra}
    resp = _requests.post(NEST_OAUTH_TOKEN_URL, data=data, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _nest_save_tokens(tokens: dict, *, save_refresh: bool) -> None:
    """Persist access_token + expiry (and refresh_token if requested) to settings."""
    expires_in = tokens.get('expires_in', 3600)
    rows = [
        ('nest_access_token', tokens.get('access_token', '')),
        ('nest_token_expiry', str(int(time.time()) + expires_in - 60)),
    ]
    if save_refresh:
        rows.append(('nest_refresh_token', tokens.get('refresh_token', '')))
    with sqlite3.connect(DB_PATH) as c:
        c.executemany(
            'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', rows
        )
        c.commit()


def _nest_ensure_token() -> str | None:
    """Return a valid access token, refreshing if expired. Returns None on failure."""
    access_token = get_setting('nest_access_token', '')
    expiry = get_setting_int('nest_token_expiry', 0)

    if access_token and time.time() < expiry:
        return access_token

    refresh_token = get_setting('nest_refresh_token', '')
    if not refresh_token:
        return None

    try:
        tokens = _nest_oauth_exchange({
            'refresh_token': refresh_token,
            'grant_type':    'refresh_token',
        })
        _nest_save_tokens(tokens, save_refresh=False)
        return tokens.get('access_token')
    except Exception as exc:
        print(f'Nest token refresh error: {exc}')
        _log_system_error('nest', 'Token refresh failed', str(exc))
        return None


def _c_to_f(c):
    """Celsius to Fahrenheit, rounded to 0.1."""
    if c is None:
        return None
    try:
        return round(float(c) * 9.0 / 5.0 + 32.0, 1)
    except (TypeError, ValueError):
        return None


def _f_to_c(f):
    """Fahrenheit to Celsius."""
    if f is None:
        return None
    try:
        return round((float(f) - 32.0) * 5.0 / 9.0, 2)
    except (TypeError, ValueError):
        return None


def _nest_refresh_devices(token):
    """Refresh the device cache. Parses cameras/doorbells + thermostats.

    Stores camera/doorbell metadata in `_nest_devices` (unchanged for the
    existing camera event flow) and thermostat state in `_nest_thermostats`,
    upserting a switches_meta row for each thermostat.
    """
    global _nest_devices, _nest_devices_ts, _nest_devices_raw, _nest_thermostats
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
        _nest_devices_raw = devices
        thermostats = {}
        for d in devices:
            name = d.get('name', '')
            traits = d.get('traits', {})
            custom = traits.get('sdm.devices.traits.Info', {}).get('customName', '')
            dev_type_full = d.get('type', '')
            dev_type = dev_type_full.rsplit('.', 1)[-1]
            # Fall back to parentRelations.displayName — that's the room name
            # from Google Home (e.g. "Entryway") when customName is empty.
            room_name = ''
            for pr in d.get('parentRelations', []) or []:
                dn = pr.get('displayName')
                if isinstance(dn, str) and dn.strip():
                    room_name = dn.strip()
                    break
            display = custom or room_name or dev_type or 'Unknown'
            _nest_devices[name] = {
                'name': display,
            }
            if dev_type_full == 'sdm.devices.types.THERMOSTAT':
                tmode    = traits.get('sdm.devices.traits.ThermostatMode', {}) or {}
                setpt    = traits.get('sdm.devices.traits.ThermostatTemperatureSetpoint', {}) or {}
                hvac     = traits.get('sdm.devices.traits.ThermostatHvac', {}) or {}
                ambient  = traits.get('sdm.devices.traits.Temperature', {}) or {}
                humidity = traits.get('sdm.devices.traits.Humidity', {}) or {}
                eco      = traits.get('sdm.devices.traits.ThermostatEco', {}) or {}
                thermostats[name] = {
                    'display_name':   display,
                    'mode':           tmode.get('mode'),
                    'available_modes': tmode.get('availableModes', []),
                    'setpoint_heat_c': setpt.get('heatCelsius'),
                    'setpoint_cool_c': setpt.get('coolCelsius'),
                    'ambient_c':      ambient.get('ambientTemperatureCelsius'),
                    'humidity':       humidity.get('ambientHumidityPercent'),
                    'hvac_status':    hvac.get('status'),
                    'eco_mode':       eco.get('mode'),
                }
        _nest_thermostats = thermostats
        _nest_devices_ts = time.time()
        # Upsert thermostat metadata rows. If an existing row still has the
        # un-edited default name ('THERMOSTAT'), update it to the better name
        # now available (customName or room displayName). Preserve any name
        # the user has edited.
        if thermostats:
            with sqlite3.connect(DB_PATH) as c:
                for dev_path, info in thermostats.items():
                    new_name = info['display_name']
                    row = c.execute(
                        'SELECT id, name FROM switches_meta WHERE provider=? AND external_id=?',
                        ('nest', dev_path)
                    ).fetchone()
                    if row is None:
                        c.execute(
                            'INSERT INTO switches_meta (provider, external_id, kind, name) '
                            'VALUES (?,?,?,?)',
                            ('nest', dev_path, 'thermostat', new_name)
                        )
                    elif row[1] in ('THERMOSTAT', 'Unknown') and new_name != row[1]:
                        c.execute(
                            'UPDATE switches_meta SET name=? WHERE id=?',
                            (new_name, row[0])
                        )
    except Exception as exc:
        print(f'Nest device list error: {exc}')


def _nest_thermostat_command(device_path: str, command: str, params: dict) -> dict:
    """Send a SDM executeCommand to a thermostat. Returns API response body or raises."""
    token = _nest_ensure_token()
    if not token:
        raise RuntimeError('Nest not authenticated')
    url = (
        'https://smartdevicemanagement.googleapis.com/v1/'
        f'{device_path}:executeCommand'
    )
    resp = _requests.post(
        url,
        headers={
            'Authorization': f'Bearer {token}',
            'Content-Type':  'application/json',
        },
        json={'command': command, 'params': params},
        timeout=15,
    )
    if resp.status_code >= 400:
        detail = _extract_api_error(resp, max_len=len(resp.text or ''))
        raise RuntimeError(f'SDM command {command} failed ({resp.status_code}): {detail}')
    return resp.json() if resp.content else {}


def nest_set_thermostat(device_path: str, *, mode: str = None,
                        setpoint_f: float = None,
                        setpoint_heat_f: float = None,
                        setpoint_cool_f: float = None) -> dict:
    """Apply one or more thermostat changes. Valid mode values: OFF/HEAT/COOL/HEATCOOL.

    setpoint_f is used when current mode is HEAT or COOL (sets that single
    setpoint). For HEATCOOL/Auto, pass both setpoint_heat_f and setpoint_cool_f.
    Refreshes the cache afterward so /api/switches reflects the change.
    """
    info = _nest_thermostats.get(device_path)
    if info is None:
        raise ValueError(f'Unknown Nest thermostat: {device_path}')
    # Mode change
    if mode:
        mode = mode.upper()
        valid = {'OFF', 'HEAT', 'COOL', 'HEATCOOL'}
        if mode not in valid:
            raise ValueError(f'Invalid mode: {mode}')
        _nest_thermostat_command(
            device_path,
            'sdm.devices.commands.ThermostatMode.SetMode',
            {'mode': mode},
        )
        info['mode'] = mode  # optimistic
    # Setpoint(s) — decide which command based on current (or just-set) mode
    current_mode = (mode or info.get('mode') or '').upper()
    if setpoint_heat_f is not None and setpoint_cool_f is not None:
        _nest_thermostat_command(
            device_path,
            'sdm.devices.commands.ThermostatTemperatureSetpoint.SetRange',
            {
                'heatCelsius': _f_to_c(setpoint_heat_f),
                'coolCelsius': _f_to_c(setpoint_cool_f),
            },
        )
        info['setpoint_heat_c'] = _f_to_c(setpoint_heat_f)
        info['setpoint_cool_c'] = _f_to_c(setpoint_cool_f)
    elif setpoint_f is not None:
        if current_mode == 'HEAT':
            _nest_thermostat_command(
                device_path,
                'sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat',
                {'heatCelsius': _f_to_c(setpoint_f)},
            )
            info['setpoint_heat_c'] = _f_to_c(setpoint_f)
        elif current_mode == 'COOL':
            _nest_thermostat_command(
                device_path,
                'sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool',
                {'coolCelsius': _f_to_c(setpoint_f)},
            )
            info['setpoint_cool_c'] = _f_to_c(setpoint_f)
        else:
            raise ValueError(
                f'setpoint_f requires mode=HEAT or COOL (current: {current_mode}). '
                'For HEATCOOL/Auto, pass setpoint_heat_f AND setpoint_cool_f.'
            )
    # Pull fresh state so the next /api/switches query has authoritative
    # mode + setpoints. Mode transitions change which setpoints SDM returns
    # (OFF → HEAT reveals heatCelsius, etc.).
    try:
        token = _nest_ensure_token()
        if token:
            _nest_refresh_devices(token)
    except Exception as exc:
        print(f'Nest post-command refresh failed: {exc}')
    return dict(_nest_thermostats.get(device_path, info))


def _nest_get_device_name(device_path: str, token: str) -> str:
    """Return human-readable device name, using cache. Falls back to device ID fragment."""
    if not _nest_devices or time.time() - _nest_devices_ts > _NEST_DEVICE_CACHE_TTL:
        _nest_refresh_devices(token)

    if device_path in _nest_devices:
        return _nest_devices[device_path].get('name', 'Unknown')
    return device_path.rsplit('/', 1)[-1][:6] if device_path else 'Unknown'


def fetch_nest_events() -> int:
    """Pull Nest camera/doorbell events from Pub/Sub and log new ones. Returns insert count."""
    import base64 as _b64

    _nest_poll_stats['calls'] += 1
    _nest_poll_stats['last_call_ts'] = int(time.time())

    if not get_setting_bool('nest_enabled', False):
        _nest_poll_stats['last_error'] = 'disabled'
        return 0

    subscription = get_setting('nest_pubsub_subscription', '')
    if not subscription:
        _nest_poll_stats['last_error'] = 'no subscription'
        return 0

    token = _nest_ensure_token()
    if not token:
        _nest_poll_stats['last_error'] = 'no token'
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
        _nest_poll_stats['last_pull_count'] = len(messages)
        _nest_poll_stats['pull_count_total'] += len(messages)
        _nest_poll_stats['last_error'] = None

        if not messages:
            return 0

        # Ensure device cache is warm (for trait lookups)
        if not _nest_devices or time.time() - _nest_devices_ts > _NEST_DEVICE_CACHE_TTL:
            _nest_refresh_devices(token)

        # Parse each message, attempt snapshot capture immediately (URL expires fast)
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

                outer_event_id = payload.get('eventId', '')
                device_name = _nest_get_device_name(device_path, token)

                for sdm_event_key, edata in events.items():
                    # Running counter across all calls (for debugging)
                    _nest_event_counters[sdm_event_key] = _nest_event_counters.get(sdm_event_key, 0) + 1

                    event_type = NEST_EVENT_TYPE_MAP.get(sdm_event_key)
                    if not event_type:
                        continue

                    session_id = edata.get('eventSessionId', '')
                    title = f'{device_name}: {NEST_EVENT_TITLE_MAP.get(event_type, event_type)}'
                    detail = f'device: {device_name}  eventId: {outer_event_id}  session: {session_id}'
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
        _nest_poll_stats['last_error'] = 'timeout'
        print('Nest poll: timeout (no events)')
    except Exception as exc:
        _nest_poll_stats['last_error'] = str(exc)[:200]
        print(f'Nest event poll error: {exc}')
        _log_system_error('nest', 'Event poll error', str(exc))

    return inserted


# ── Kasa (TP-Link smart plugs + dimmers + bulbs, LAN discovery) ──────────────
def _kasa_start_loop() -> None:
    """Start a dedicated asyncio loop in a daemon thread if not already running.
    All Kasa coroutines must be submitted to this loop (via _kasa_submit) so
    persistent Device connections aren't tied to short-lived loops."""
    global _kasa_loop, _kasa_loop_thread
    if _kasa_loop is not None and _kasa_loop.is_running():
        return
    _kasa_loop = asyncio.new_event_loop()
    def _run():
        asyncio.set_event_loop(_kasa_loop)
        try:
            _kasa_loop.run_forever()
        except Exception as exc:
            print(f'Kasa loop crashed: {exc}')
    _kasa_loop_thread = threading.Thread(
        target=_run, daemon=True, name='kasa-asyncio-loop'
    )
    _kasa_loop_thread.start()


def _kasa_submit(coro, timeout: float = 30.0):
    """Run a coroutine on the persistent Kasa loop and wait for its result."""
    if _kasa_loop is None or not _kasa_loop.is_running():
        _kasa_start_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, _kasa_loop)
    return fut.result(timeout=timeout)


def _device_mark_failure(*, key: str, name: str, reason: str,
                         failures: dict, devices: dict, quarantine: dict,
                         log_fn, offline_field: str, offline_value) -> None:
    """Shared offline/quarantine tracking for Kasa & Tuya plugs.
    After 3 consecutive failures, quarantines the device for 5 minutes so we
    stop hammering a stuck/offline plug. `offline_field`/`offline_value` are
    the per-driver flag set on the device info dict (e.g. 'on'=None for Kasa,
    'online'=False for Tuya)."""
    count = failures.get(key, 0) + 1
    failures[key] = count
    info = devices.get(key) or {}
    if not info.get('was_offline'):
        log_fn(name, 'offline', reason[:200])
        info['was_offline'] = True
    info[offline_field] = offline_value
    if count >= 3 and key not in quarantine:
        quarantine[key] = time.time() + 300  # 5 min
        log_fn(name, 'quarantined',
               f'{count} consecutive failures; backing off 5 min')


def _kasa_mark_failure(mac: str, name: str, reason: str) -> None:
    _device_mark_failure(
        key=mac, name=name, reason=reason,
        failures=_kasa_failures, devices=_kasa_devices, quarantine=_kasa_quarantine,
        log_fn=_log_kasa_reachability,
        offline_field='on', offline_value=None,
    )


async def _kasa_close_all_async() -> None:
    """Close all persistent Device connections. Called on rediscover."""
    for mac, dev in list(_kasa_connections.items()):
        try:
            # python-kasa's Device has a disconnect() in recent versions
            if hasattr(dev, 'disconnect'):
                await dev.disconnect()
        except Exception as exc:
            print(f'Kasa close failed for {mac}: {exc}')
    _kasa_connections.clear()



def _kasa_read_brightness(dev) -> int:
    """Return current brightness 0-100 or None if device is not dimmable."""
    # Path 1: features dict (python-kasa 0.7+)
    try:
        feat = getattr(dev, 'features', {}) or {}
        if 'brightness' in feat:
            val = getattr(feat['brightness'], 'value', None)
            if val is not None:
                return int(val)
    except Exception:
        pass
    # Path 2: Light module
    try:
        from kasa import Module  # type: ignore
        mods = getattr(dev, 'modules', {}) or {}
        light = mods.get(Module.Light) if hasattr(Module, 'Light') else None
        if light is not None and hasattr(light, 'brightness'):
            return int(light.brightness)
    except Exception:
        pass
    # Path 3: direct attribute (older API)
    try:
        b = getattr(dev, 'brightness', None)
        if b is not None and getattr(dev, 'is_dimmable', False):
            return int(b)
    except Exception:
        pass
    return None


async def _kasa_set_brightness_on_device(dev, brightness: int) -> None:
    """Set brightness on an already-connected device.  Raises if not dimmable."""
    b = max(0, min(100, int(brightness)))
    # Path 1: features
    feat = getattr(dev, 'features', {}) or {}
    if 'brightness' in feat:
        await feat['brightness'].set_value(b)
        return
    # Path 2: Light module
    try:
        from kasa import Module  # type: ignore
        mods = getattr(dev, 'modules', {}) or {}
        light = mods.get(Module.Light) if hasattr(Module, 'Light') else None
        if light is not None and hasattr(light, 'set_brightness'):
            await light.set_brightness(b)
            return
    except Exception:
        pass
    # Path 3: direct method
    if hasattr(dev, 'set_brightness'):
        await dev.set_brightness(b)
        return
    raise ValueError('Device is not dimmable')


async def _kasa_discover_async() -> dict:
    """Run LAN discovery, store Device instances in _kasa_connections for
    persistent reuse across polls/toggles. Returns {mac: {alias, ip, on,
    model, dimmable, brightness}}."""
    from kasa import Discover
    # Close any stale connections from a prior run
    await _kasa_close_all_async()
    _kasa_failures.clear()
    _kasa_quarantine.clear()
    try:
        discovered = await Discover.discover()
    except Exception as exc:
        print(f'Kasa discovery error: {exc}')
        raise
    out = {}
    for ip, dev in discovered.items():
        try:
            await dev.update()
        except Exception as exc:
            print(f'Kasa update failed for {ip}: {exc}')
            continue
        mac = (getattr(dev, 'mac', None) or '').upper()
        if not mac:
            continue
        brightness = _kasa_read_brightness(dev)
        out[mac] = {
            'alias':      getattr(dev, 'alias', None) or f'Kasa {mac[-5:]}',
            'ip':         ip,
            'on':         bool(getattr(dev, 'is_on', False)),
            'model':      getattr(dev, 'model', ''),
            'dimmable':   brightness is not None,
            'brightness': brightness,
        }
        # Keep the Device alive for future poll/toggle — skips the handshake
        # hit we saw knocking some dimmers off WiFi.
        _kasa_connections[mac] = dev
    return out


def _kasa_refresh_devices() -> int:
    """Discover Kasa devices on the LAN, cache state, upsert metadata to DB.

    Upserts on (provider, external_id). Preserves user edits to name/room/
    sort_order/hidden.  Updates kind if dimmable flag changes between runs
    (e.g. new firmware reveals a feature, or a bulb replaces a plug).
    Uses the persistent Kasa asyncio loop so the Device connections created
    during discovery survive for later polls.
    """
    global _kasa_devices, _kasa_ts
    try:
        devices = _kasa_submit(_kasa_discover_async(), timeout=60)
    except Exception as exc:
        print(f'Kasa refresh error: {exc}')
        _log_system_error('kasa', 'Discovery failed', str(exc))
        return 0
    now = time.time()
    with _switches_lock:
        _kasa_devices = {mac: {**info, 'last_seen': now} for mac, info in devices.items()}
        _kasa_ts = now
    with sqlite3.connect(DB_PATH) as c:
        for mac, info in devices.items():
            kind = 'dimmer' if info.get('dimmable') else 'plug'
            existing = c.execute(
                'SELECT id, kind FROM switches_meta WHERE provider=? AND external_id=?',
                ('kasa', mac)
            ).fetchone()
            if existing is None:
                c.execute(
                    'INSERT INTO switches_meta (provider, external_id, kind, name) '
                    'VALUES (?,?,?,?)',
                    ('kasa', mac, kind, info['alias'])
                )
            elif existing[1] != kind:
                c.execute('UPDATE switches_meta SET kind=? WHERE id=?', (kind, existing[0]))
    return len(devices)


def _log_kasa_external_change(name: str, new_on: bool, detail: str = None) -> None:
    """Log a Kasa state change detected by polling — meaning a physical
    switch press, Kasa schedule fire, or Kasa-app toggle changed the state
    without our UI being involved. Logged under system='kasa' with
    source='live' to distinguish from home_control/ui actions."""
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                'INSERT INTO event_log '
                '(ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), 'kasa',
                 'plug_turned_on' if new_on else 'plug_turned_off',
                 f'{name} turned {"on" if new_on else "off"}',
                 detail or 'external (switch or schedule)', 'ok', 'live')
            )
    except Exception as exc:
        print(f'Kasa external-change log error: {exc}')


def _kasa_uptime_hint(dev) -> str:
    """Return a short description of device uptime if available, else ''.

    Kasa devices expose uptime via sys_info[on_time] (seconds since last
    power-on). Very short uptime on an OFF event suggests the device just
    rebooted (which defaults to OFF on most dimmer firmware)."""
    try:
        sys_info = getattr(dev, 'sys_info', None) or {}
        # Different firmware versions nest this differently
        on_time = sys_info.get('on_time')
        if on_time is None and 'system' in sys_info:
            on_time = (sys_info.get('system', {}).get('get_sysinfo', {})
                       .get('on_time'))
        if on_time is None:
            return ''
        on_time = int(on_time)
        if on_time < 60:
            return f'uptime {on_time}s (likely reboot)'
        if on_time < 3600:
            return f'uptime {on_time // 60}m'
        return f'uptime {on_time // 3600}h{(on_time % 3600) // 60}m'
    except Exception:
        return ''


def _log_kasa_reachability(name: str, event: str, detail: str = None) -> None:
    """Log a Kasa device going offline or coming back online. System='kasa',
    source='live'."""
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                'INSERT INTO event_log '
                '(ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), 'kasa', event, f'{name} {event}',
                 detail, 'ok', 'live')
            )
    except Exception as exc:
        print(f'Kasa reachability log error: {exc}')


async def _kasa_ensure_connection(mac: str, info: dict):
    """Return the persistent Device for mac. Reconnects via Discover.discover_single
    if we don't have a live connection yet (first poll after startup, or after
    a previous failure)."""
    from kasa import Discover
    dev = _kasa_connections.get(mac)
    if dev is not None:
        return dev
    ip = info.get('ip')
    if not ip:
        raise RuntimeError(f'No IP for Kasa {mac}')
    dev = await Discover.discover_single(ip)
    await dev.update()
    _kasa_connections[mac] = dev
    return dev


async def _kasa_update_state_async() -> None:
    """Poll each cached Kasa device using its persistent connection. No
    per-poll reconnects — this avoids the KLAP handshake churn that was
    knocking sensitive dimmers off WiFi. Respects per-device quarantine
    after 3 consecutive failures. Log:
      - State changes vs. the LAST KNOWN state (survives poll failures)
      - Offline/online + quarantine transitions"""
    now = time.time()
    for mac, info in list(_kasa_devices.items()):
        if not info.get('ip'):
            continue
        # Honor quarantine backoff
        q = _kasa_quarantine.get(mac, 0)
        if q and now < q:
            continue
        name = info.get('alias') or f'Kasa {mac[-5:]}'
        try:
            dev = await _kasa_ensure_connection(mac, info)
            await dev.update()
            new_on     = bool(getattr(dev, 'is_on', False))
            last_known = info.get('last_known_on')
            # Released from quarantine
            if mac in _kasa_quarantine:
                _kasa_quarantine.pop(mac, None)
                _log_kasa_reachability(name, 'online',
                                       'released from quarantine')
            # Came back online after a gap
            elif info.get('was_offline'):
                _log_kasa_reachability(name, 'online',
                                       f'state={"on" if new_on else "off"}')
            # State change vs last known (bridges across poll failures)
            if last_known is not None and new_on != last_known:
                uptime_hint = _kasa_uptime_hint(dev)
                detail = 'external (switch or schedule)'
                if uptime_hint:
                    detail = f'{detail} · {uptime_hint}'
                _log_kasa_external_change(name, new_on, detail)
            b = _kasa_read_brightness(dev) if info.get('dimmable') else None
            with _switches_lock:
                info['was_offline']   = False
                info['on']            = new_on
                info['last_known_on'] = new_on
                info['last_seen']     = time.time()
                if b is not None:
                    info['brightness'] = b
            _kasa_failures.pop(mac, None)
        except Exception as exc:
            # Lost connection — drop the stale Device so next cycle reconnects
            old = _kasa_connections.pop(mac, None)
            if old is not None:
                try:
                    if hasattr(old, 'disconnect'):
                        await old.disconnect()
                except Exception:
                    pass
            _kasa_mark_failure(mac, name, str(exc))


def _kasa_poll_state() -> None:
    """Refresh cached state for all known Kasa devices.  Called from poller."""
    if not _kasa_devices:
        return
    try:
        _kasa_submit(_kasa_update_state_async(), timeout=60)
    except Exception as exc:
        print(f'Kasa poll error: {exc}')


async def _kasa_set_async(mac: str, on: bool) -> bool:
    """Set Kasa device on/off via its persistent connection. Returns new is_on state."""
    info = _kasa_devices.get(mac)
    if not info:
        raise ValueError(f'Unknown Kasa MAC: {mac}')
    dev = await _kasa_ensure_connection(mac, info)
    if on:
        await dev.turn_on()
    else:
        await dev.turn_off()
    await dev.update()
    new_state = bool(getattr(dev, 'is_on', False))
    b = _kasa_read_brightness(dev) if info.get('dimmable') else None
    with _switches_lock:
        info['on']            = new_state
        info['last_known_on'] = new_state
        info['last_seen']     = time.time()
        if b is not None:
            info['brightness'] = b
    _kasa_failures.pop(mac, None)
    return new_state


async def _kasa_set_brightness_async(mac: str, brightness: int) -> dict:
    """Set brightness 0-100 on a Kasa dimmer via its persistent connection.
    Turns device on if b>0 and off, turns off if b=0 and on."""
    info = _kasa_devices.get(mac)
    if not info:
        raise ValueError(f'Unknown Kasa MAC: {mac}')
    if not info.get('dimmable'):
        raise ValueError(f'Kasa {mac} is not dimmable')
    b = max(0, min(100, int(brightness)))
    dev = await _kasa_ensure_connection(mac, info)
    was_on = bool(getattr(dev, 'is_on', False))
    if b > 0:
        if not was_on:
            await dev.turn_on()
            await dev.update()
        await _kasa_set_brightness_on_device(dev, b)
    else:
        if was_on:
            await dev.turn_off()
    await dev.update()
    new_on = bool(getattr(dev, 'is_on', False))
    new_b  = _kasa_read_brightness(dev)
    final_b = new_b if new_b is not None else b
    with _switches_lock:
        info['on']            = new_on
        info['last_known_on'] = new_on
        info['brightness']    = final_b
        info['last_seen']     = time.time()
    _kasa_failures.pop(mac, None)
    return {'on': new_on, 'brightness': final_b}


def kasa_set(mac: str, on: bool) -> bool:
    """Sync wrapper for Kasa on/off.  Returns new state."""
    try:
        return _kasa_submit(_kasa_set_async(mac, on), timeout=20)
    except Exception:
        # Connection may have gone stale — drop it so next attempt reconnects
        _kasa_connections.pop(mac, None)
        raise


def kasa_set_brightness(mac: str, brightness: int) -> dict:
    """Sync wrapper for Kasa dimmer brightness.  Returns {on, brightness}."""
    try:
        return _kasa_submit(_kasa_set_brightness_async(mac, brightness), timeout=20)
    except Exception:
        _kasa_connections.pop(mac, None)
        raise


# ── Tuya (tinytuya, LAN control of Smart Life / Tuya-platform devices) ───────
# Requires the user to run `python -m tinytuya wizard` once — that reaches out
# to the Tuya Cloud developer portal and produces devices.json with local keys
# for every device on the account. Our integration is then fully LAN-based.
_TUYA_DEVICEFILE = os.path.join(BASE_DIR, 'devices.json')


def _tuya_load_devicefile() -> list:
    """Load devices.json produced by tinytuya wizard. Returns list of dicts."""
    if not os.path.exists(_TUYA_DEVICEFILE):
        return []
    try:
        with open(_TUYA_DEVICEFILE, encoding='utf-8') as f:
            data = json.load(f)
        # Wizard output is a list of device dicts
        if isinstance(data, list):
            return data
        # Some versions wrap it
        if isinstance(data, dict) and 'devices' in data:
            return data['devices']
        return []
    except Exception as exc:
        print(f'Tuya devicefile load error: {exc}')
        return []


def _tuya_make_outlet(dev_id: str, info: dict):
    """Construct a tinytuya OutletDevice with a persistent socket. The socket
    is kept open between calls so we don't pay the handshake cost on every
    poll — same fix we did for Kasa, just simpler because tinytuya is sync
    and exposes a `persist` flag directly."""
    import tinytuya
    ip = info.get('ip')
    if not ip:
        raise RuntimeError(f'Tuya device {dev_id} has no IP; run rediscover')
    try:
        version = float(info.get('version', '3.3'))
    except (TypeError, ValueError):
        version = 3.3
    dev = tinytuya.OutletDevice(
        dev_id=dev_id,
        address=ip,
        local_key=info.get('local_key', ''),
        version=version,
        persist=True,
    )
    dev.set_socketTimeout(5)
    return dev


def _tuya_get_or_connect(dev_id: str, info: dict):
    """Return the cached persistent OutletDevice for dev_id, constructing it
    on first use or after a prior failure."""
    dev = _tuya_connections.get(dev_id)
    if dev is not None:
        return dev
    dev = _tuya_make_outlet(dev_id, info)
    _tuya_connections[dev_id] = dev
    return dev


def _tuya_close_connection(dev_id: str) -> None:
    """Close + drop the persistent connection for dev_id (on error or
    rediscover). Next access will reconnect."""
    dev = _tuya_connections.pop(dev_id, None)
    if dev is None:
        return
    try:
        if hasattr(dev, 'close'):
            dev.close()
    except Exception:
        pass


def _tuya_close_all() -> None:
    """Close every persistent Tuya connection (called on rediscover)."""
    for dev_id in list(_tuya_connections.keys()):
        _tuya_close_connection(dev_id)


def _log_tuya_reachability(name: str, event: str, detail: str = None) -> None:
    """Log a Tuya device going offline / online / into quarantine. Mirrors
    the Kasa reachability logger. System='tuya', source='live'."""
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                'INSERT INTO event_log '
                '(ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), 'tuya', event, f'{name} {event}',
                 detail, 'ok', 'live')
            )
    except Exception as exc:
        print(f'Tuya reachability log error: {exc}')


def _tuya_mark_failure(dev_id: str, name: str, reason: str) -> None:
    _device_mark_failure(
        key=dev_id, name=name, reason=reason,
        failures=_tuya_failures, devices=_tuya_devices, quarantine=_tuya_quarantine,
        log_fn=_log_tuya_reachability,
        offline_field='online', offline_value=False,
    )


def _parse_tuya_ext_id(ext_id: str):
    """Extract (dev_id, dp_idx) from switches_meta external_id.
    Format: '<dev_id>:<dp>' (multi-outlet) or '<dev_id>' (single-outlet back-compat)."""
    if ':' in ext_id:
        a, b = ext_id.split(':', 1)
        return a, b
    return ext_id, '1'


def _tuya_probe_dps(dev_id: str, info: dict) -> dict:
    """Connect to a device and return its current dps dict, or {} on error.
    Uses the persistent connection registry so we don't reconnect just to probe."""
    try:
        dev = _tuya_get_or_connect(dev_id, info)
        status = dev.status()
        if isinstance(status, dict):
            return status.get('dps') or {}
    except Exception as exc:
        _tuya_close_connection(dev_id)
        print(f'Tuya probe failed for {dev_id}: {exc}')
    return {}


def _tuya_refresh_devices() -> int:
    """Load devices.json, scan LAN, probe each device for its switch DPs,
    upsert one switches_meta row per outlet (multi-outlet strips split).
    Closes any stale persistent connections first."""
    global _tuya_devices, _tuya_ts
    # Tear down any existing persistent connections before rebuilding
    _tuya_close_all()
    _tuya_failures.clear()
    _tuya_quarantine.clear()
    file_devs = _tuya_load_devicefile()
    if not file_devs:
        print('Tuya: no devices.json found. Run `python -m tinytuya wizard`.')
        _log_system_error('tuya', 'devices.json missing',
                          f'Expected at {_TUYA_DEVICEFILE}. Run tinytuya wizard.')
        return 0

    # LAN scan (~18s UDP listen) so we learn each device's current IP + version.
    import tinytuya
    try:
        scan = tinytuya.deviceScan(verbose=False, color=False, poll=False) or {}
    except Exception as exc:
        print(f'Tuya LAN scan error: {exc}')
        scan = {}

    scan_by_id: dict = {}
    for ip_key, sinfo in (scan.items() if isinstance(scan, dict) else []):
        if isinstance(sinfo, dict):
            dev_id = sinfo.get('gwId') or sinfo.get('id')
            ip     = sinfo.get('ip') or ip_key
            ver    = sinfo.get('version', '3.3')
            if dev_id:
                scan_by_id[dev_id] = {'ip': ip, 'version': ver}

    new: dict = {}
    for d in file_devs:
        dev_id = d.get('id')
        if not dev_id:
            continue
        name = (d.get('name') or '').strip() or f'Tuya {dev_id[-5:]}'
        key  = d.get('key') or d.get('local_key') or ''
        if not key:
            continue
        lan = scan_by_id.get(dev_id, {})
        ip  = lan.get('ip') or d.get('ip') or ''
        ver = lan.get('version') or d.get('version') or '3.3'
        new[dev_id] = {
            'name':      name,
            'ip':        ip,
            'local_key': key,
            'version':   str(ver),
            'category':  d.get('category', ''),
            'online':    bool(ip),
            'last_seen': time.time() if ip else 0,
            'dp_state':  {},   # filled by probe
            'switch_dps': ['1'],  # DPs we consider togglable; populated by probe
        }

    # Probe each reachable device to discover its switch DPs.
    for dev_id, info in new.items():
        if not info.get('ip'):
            continue
        dps = _tuya_probe_dps(dev_id, info)
        info['dp_state'] = dps
        switch_dps = []
        for dp_idx, val in dps.items():
            if isinstance(val, bool):
                switch_dps.append(dp_idx)
        if switch_dps:
            # Sort numerically when possible (DP '1' before '10' etc.)
            info['switch_dps'] = sorted(
                switch_dps, key=lambda s: int(s) if s.isdigit() else 999
            )

    with _switches_lock:
        _tuya_devices = new
        _tuya_ts = time.time()

    # Upsert one row per switch DP per device.
    total_tiles = 0
    with sqlite3.connect(DB_PATH) as c:
        for dev_id, info in new.items():
            dps_list = info['switch_dps']
            for idx, dp_idx in enumerate(dps_list):
                ext_id = f'{dev_id}:{dp_idx}'
                default_name = (
                    f'{info["name"]} {idx + 1}' if len(dps_list) > 1
                    else info['name']
                )
                row = c.execute(
                    'SELECT id FROM switches_meta WHERE provider=? AND external_id=?',
                    ('tuya', ext_id)
                ).fetchone()
                if row is None:
                    c.execute(
                        'INSERT INTO switches_meta (provider, external_id, kind, name) '
                        'VALUES (?,?,?,?)',
                        ('tuya', ext_id, 'plug', default_name)
                    )
                total_tiles += 1
    return total_tiles


def _tuya_poll_state() -> None:
    """Refresh full dps dict for each cached Tuya device using its persistent
    socket. Blocking; runs in the poller thread. Honors per-device quarantine
    after 3 consecutive failures, mirroring the Kasa refactor."""
    if not _tuya_devices:
        return
    now = time.time()
    for dev_id, info in list(_tuya_devices.items()):
        if not info.get('ip'):
            continue
        # Quarantine backoff
        q = _tuya_quarantine.get(dev_id, 0)
        if q and now < q:
            continue
        name = info.get('name') or f'Tuya {dev_id[-5:]}'
        try:
            dev = _tuya_get_or_connect(dev_id, info)
            status = dev.status()
            if not isinstance(status, dict) or 'Error' in status:
                raise RuntimeError(
                    status.get('Error') if isinstance(status, dict)
                    else f'unexpected status: {status}'
                )
            dps = status.get('dps') or {}
            # Released from quarantine
            if dev_id in _tuya_quarantine:
                _tuya_quarantine.pop(dev_id, None)
                _log_tuya_reachability(name, 'online',
                                       'released from quarantine')
            elif info.get('was_offline'):
                _log_tuya_reachability(name, 'online', 'reconnected')
            info['was_offline'] = False
            info['dp_state']    = dps
            info['last_seen']   = time.time()
            info['online']      = True
            _tuya_failures.pop(dev_id, None)
        except Exception as exc:
            _tuya_close_connection(dev_id)
            _tuya_mark_failure(dev_id, name, str(exc))


# ── Abode alarm control (uses existing _abode_instance) ──────────────────────
# Abode is already fully read-integrated (websocket listener + fetch_security).
# This adds a single "Arm Home" action in the Home Control drawer — the
# user's common bedtime use case. Disarm stays on the physical keypad /
# Abode app (both enforce a real PIN). Away isn't surfaced.
ABODE_MODE_DISPLAY = {'standby': 'Disarmed', 'home': 'Armed Home', 'away': 'Armed Away'}


def _abode_seed_alarm_row() -> int:
    """Idempotent upsert of the alarm row in switches_meta. Returns 1."""
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            'SELECT id FROM switches_meta WHERE provider=? AND external_id=?',
            ('abode', 'alarm')
        ).fetchone()
        if row is None:
            c.execute(
                'INSERT INTO switches_meta (provider, external_id, kind, name) '
                'VALUES (?,?,?,?)',
                ('abode', 'alarm', 'alarm', 'Security')
            )
    return 1


def abode_arm_home() -> str:
    """Arm the Abode system to Home mode. Refreshes _security immediately so
    the next /api/switches reflects the change."""
    global _security, _security_ts
    if _abode_instance is None:
        raise RuntimeError('Abode not connected')
    alarm = _abode_instance.get_alarm()
    if alarm is None:
        raise RuntimeError('Abode alarm device not available')
    alarm.set_mode('home')
    _security = {
        **(_security or {}),
        'mode':         'home',
        'mode_display': ABODE_MODE_DISPLAY['home'],
    }
    _security_ts = time.time()
    return 'home'


def tuya_set(ext_id: str, on: bool) -> bool:
    """Toggle a single Tuya outlet (identified by 'dev_id:dp_idx' external_id).
    Uses the persistent connection; drops + reconnects on failure."""
    dev_id, dp_idx = _parse_tuya_ext_id(ext_id)
    info = _tuya_devices.get(dev_id)
    if not info:
        raise ValueError(f'Unknown Tuya device: {dev_id}')
    if not info.get('ip'):
        raise RuntimeError(f'Tuya {dev_id} has no LAN IP (may be offline)')
    try:
        dp_as_int = int(dp_idx)
    except ValueError:
        dp_as_int = dp_idx  # some devices use string codes
    try:
        dev = _tuya_get_or_connect(dev_id, info)
        # set_value handles plugs, wall switches, USB ports, multi-gang strips.
        result = dev.set_value(dp_as_int, bool(on))
        if isinstance(result, dict) and result.get('Error'):
            raise RuntimeError(f'Tuya error: {result.get("Error")}')
    except Exception:
        # Drop the socket so the next op reconnects cleanly.
        _tuya_close_connection(dev_id)
        raise
    dp_state = info.setdefault('dp_state', {})
    dp_state[dp_idx] = bool(on)
    info['last_seen'] = time.time()
    _tuya_failures.pop(dev_id, None)
    return bool(on)


# ── Switches (unified dispatch across providers) ─────────────────────────────
def _switches_rediscover_all() -> dict:
    """Run discovery across every enabled provider.  Returns per-provider counts."""
    counts = {'kasa': 0, 'pool': 0, 'nest_thermostat': 0, 'tuya': 0}
    if get_setting_bool('kasa_enabled', False):
        counts['kasa'] = _kasa_refresh_devices()
    if get_setting_bool('pool_enabled', True):
        counts['pool'] = _pool_discover_circuits()
    # Nest: refresh all devices (cameras + doorbells + thermostats). The
    # camera cache is the pre-existing behavior; thermostats are new.
    if get_setting_bool('nest_enabled', False):
        token = _nest_ensure_token()
        if token:
            _nest_refresh_devices(token)
            counts['nest_thermostat'] = len(_nest_thermostats)
    if get_setting_bool('tuya_enabled', False):
        counts['tuya'] = _tuya_refresh_devices()
    if get_setting_bool('abode_enabled', True):
        counts['abode'] = _abode_seed_alarm_row()
    return counts


def _get_all_switches() -> list:
    """Return merged switch list with DB metadata + live state per provider."""
    out = []
    with sqlite3.connect(DB_PATH) as c:
        rows = c.execute(
            'SELECT id, provider, external_id, kind, name, room, sort_order, hidden '
            'FROM switches_meta ORDER BY room, sort_order, name'
        ).fetchall()
    for rid, provider, ext_id, kind, name, room, sort_order, hidden in rows:
        state     = None
        detail    = {}
        reachable = True
        if provider == 'kasa':
            info = _kasa_devices.get(ext_id)
            if info is None:
                reachable = False
            else:
                state  = info.get('on')
                detail = {
                    'ip':         info.get('ip'),
                    'model':      info.get('model'),
                    'dimmable':   bool(info.get('dimmable')),
                    'brightness': info.get('brightness'),
                }
        elif provider == 'pool':
            if not get_setting_bool('pool_enabled', True):
                reachable = False
            else:
                field = POOL_EXT_TO_FIELD.get(ext_id)
                val = _pool.get(field) if field else None
                if val is None:
                    # _pool cache not populated yet, or field unknown
                    reachable = bool(_pool)
                    state = None
                else:
                    state = bool(val)
                detail = {'circuit_id': ext_id}
        elif provider == 'abode':
            if not get_setting_bool('abode_enabled', True):
                reachable = False
            else:
                mode = (_security.get('mode') or 'standby').lower()
                state = (mode != 'standby')
                detail = {
                    'mode':         mode,
                    'mode_display': ABODE_MODE_DISPLAY.get(mode, mode),
                    'connected':    _security.get('connected', False),
                }
                reachable = bool(_abode_instance is not None)
        elif provider == 'tuya':
            if not get_setting_bool('tuya_enabled', False):
                reachable = False
            else:
                dev_id, dp_idx = _parse_tuya_ext_id(ext_id)
                info = _tuya_devices.get(dev_id)
                if info is None or not info.get('ip'):
                    reachable = False
                    state = None
                else:
                    dps = info.get('dp_state') or {}
                    val = dps.get(dp_idx)
                    state = bool(val) if isinstance(val, bool) else None
                    detail = {
                        'ip':       info.get('ip'),
                        'category': info.get('category'),
                        'online':   info.get('online', True),
                        'dp':       dp_idx,
                    }
        elif provider == 'nest':
            if kind == 'thermostat':
                info = _nest_thermostats.get(ext_id)
                if info is None:
                    reachable = False
                else:
                    mode = (info.get('mode') or 'OFF').upper()
                    state = mode != 'OFF'
                    detail = {
                        'mode':            mode,
                        'available_modes': info.get('available_modes', []),
                        'ambient_f':       _c_to_f(info.get('ambient_c')),
                        'humidity':        info.get('humidity'),
                        'setpoint_heat_f': _c_to_f(info.get('setpoint_heat_c')),
                        'setpoint_cool_f': _c_to_f(info.get('setpoint_cool_c')),
                        'hvac_status':     info.get('hvac_status'),
                        'eco_mode':        info.get('eco_mode'),
                    }
            else:
                reachable = False
        out.append({
            'id':         rid,
            'provider':   provider,
            'external_id': ext_id,
            'kind':       kind,
            'name':       name,
            'room':       room or '',
            'sort_order': sort_order,
            'hidden':     bool(hidden),
            'state':      state,
            'reachable':  reachable,
            'detail':     detail,
        })
    return out


def _switches_lookup(row_id: int):
    with sqlite3.connect(DB_PATH) as c:
        row = c.execute(
            'SELECT id, provider, external_id, kind, name FROM switches_meta WHERE id=?',
            (row_id,)
        ).fetchone()
    return row  # (id, provider, external_id, kind, name) or None


_HOME_CONTROL_TITLE_SUFFIX = {
    'plug_turned_on':             'turned on',
    'plug_turned_off':             'turned off',
    'circuit_on':                  'turned on',
    'circuit_off':                 'turned off',
    'brightness_changed':          'brightness changed',
    'routine_triggered':           'routine triggered',
    'alarm_armed':                 'armed',
    'thermostat_mode_changed':     'mode changed',
    'thermostat_setpoint_changed': 'setpoint changed',
}


def _switches_log_event(provider: str, event_type: str, title: str, detail: str = None,
                        result: str = 'ok') -> None:
    """Log a drawer-initiated action. All entries go under system='home_control'
    so the Event Log can filter them as one group. The provider (kasa / pool /
    nest / abode / tuya) is preserved in the detail field so you still know
    which integration drove the change. The title gets a human-readable action
    suffix ('turned on', 'armed', etc.) so the event row is self-describing."""
    provider_label = (provider or '').strip()
    prefixed_detail = detail
    if provider_label:
        tag = f'[{provider_label}]'
        prefixed_detail = tag if not detail else f'{tag} {detail}'
    suffix = _HOME_CONTROL_TITLE_SUFFIX.get(event_type)
    composed_title = f'{title} {suffix}' if suffix else title
    try:
        with sqlite3.connect(DB_PATH) as c:
            c.execute(
                'INSERT INTO event_log (ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), 'home_control', event_type, composed_title,
                 prefixed_detail, result, 'ui')
            )
    except Exception as exc:
        print(f'Switch event log error: {exc}')


def switch_set_state(row_id: int, on: bool) -> dict:
    """Dispatch set-state to the right provider.  Returns result dict."""
    row = _switches_lookup(row_id)
    if row is None:
        return {'error': 'not found', 'code': 404}
    _, provider, ext_id, kind, name = row
    try:
        if provider == 'kasa':
            new_state = kasa_set(ext_id, on)
            _switches_log_event(
                'kasa',
                'plug_turned_on' if new_state else 'plug_turned_off',
                name
            )
            return {'ok': True, 'state': new_state}
        if provider == 'abode':
            return {'error': 'use /api/switches/alarm/arm-home for abode',
                    'code': 400}
        if provider == 'tuya':
            new_state = tuya_set(ext_id, on)
            _switches_log_event(
                'tuya',
                'plug_turned_on' if new_state else 'plug_turned_off',
                name
            )
            return {'ok': True, 'state': new_state}
        if provider == 'pool':
            pool_set_circuit(ext_id, on)
            _switches_log_event(
                'pool',
                'circuit_on' if on else 'circuit_off',
                name
            )
            return {'ok': True, 'state': on}
        if provider == 'nest':
            return {'error': 'use /api/switches/thermostat for nest', 'code': 400}
        return {'error': f'unknown provider: {provider}', 'code': 400}
    except Exception as exc:
        _switches_log_event(provider, 'error', f'{name}: set-state failed',
                            str(exc), 'failed')
        return {'error': str(exc), 'code': 500}


def switch_toggle(row_id: int) -> dict:
    """Flip the current state of a plug/circuit, or fire a routine."""
    row = _switches_lookup(row_id)
    if row is None:
        return {'error': 'not found', 'code': 404}
    _, provider, ext_id, kind, _ = row
    # Routines are stateless — tap = fire. Value of `on` is ignored downstream.
    if kind == 'routine':
        return switch_set_state(row_id, True)
    # Alarm uses its own endpoint — reject plain toggle so the frontend
    # explicitly calls /api/switches/alarm/arm-home.
    if kind == 'alarm':
        return {'error': 'use /api/switches/alarm/arm-home for abode alarm',
                'code': 400}
    current = None
    if provider == 'kasa':
        current = (_kasa_devices.get(ext_id) or {}).get('on')
    elif provider == 'pool':
        field = POOL_EXT_TO_FIELD.get(ext_id)
        current = _pool.get(field) if field else None
    elif provider == 'tuya':
        dev_id, dp_idx = _parse_tuya_ext_id(ext_id)
        info = _tuya_devices.get(dev_id) or {}
        val = (info.get('dp_state') or {}).get(dp_idx)
        current = bool(val) if isinstance(val, bool) else None
    # nest toggle semantics: Phase 4 (use /api/switches/thermostat instead)
    if current is None:
        return {'error': 'current state unknown', 'code': 409}
    return switch_set_state(row_id, not current)


def switch_set_thermostat(row_id: int, **fields) -> dict:
    """Dispatch thermostat command to SDM API. fields: mode / setpoint_f /
    setpoint_heat_f / setpoint_cool_f. Any combination is allowed; order is
    mode first, then setpoints."""
    row = _switches_lookup(row_id)
    if row is None:
        return {'error': 'not found', 'code': 404}
    _, provider, ext_id, kind, name = row
    if provider != 'nest' or kind != 'thermostat':
        return {'error': 'not a nest thermostat', 'code': 400}
    try:
        result = nest_set_thermostat(ext_id, **fields)
        changes = []
        if 'mode' in fields:
            changes.append(f'mode={fields["mode"]}')
        if fields.get('setpoint_f') is not None:
            changes.append(f'setpoint={fields["setpoint_f"]}°F')
        if fields.get('setpoint_heat_f') is not None:
            changes.append(f'heat={fields["setpoint_heat_f"]}°F')
        if fields.get('setpoint_cool_f') is not None:
            changes.append(f'cool={fields["setpoint_cool_f"]}°F')
        event_type = ('thermostat_mode_changed' if 'mode' in fields
                      else 'thermostat_setpoint_changed')
        _switches_log_event('nest', event_type, name, detail=', '.join(changes))
        return {'ok': True, **result}
    except Exception as exc:
        _switches_log_event('nest', 'error', f'{name}: thermostat set failed',
                            str(exc), 'failed')
        return {'error': str(exc), 'code': 500}


def switch_set_brightness(row_id: int, brightness: int) -> dict:
    """Dispatch brightness change to the right provider."""
    row = _switches_lookup(row_id)
    if row is None:
        return {'error': 'not found', 'code': 404}
    _, provider, ext_id, kind, name = row
    if kind != 'dimmer':
        return {'error': 'not a dimmer', 'code': 400}
    try:
        if provider == 'kasa':
            result = kasa_set_brightness(ext_id, brightness)
            _switches_log_event(
                'kasa', 'brightness_changed', name,
                detail=f'brightness={result["brightness"]}%'
            )
            return {'ok': True, **result}
        return {'error': f'brightness not supported for {provider}', 'code': 501}
    except Exception as exc:
        _switches_log_event(provider, 'error', f'{name}: brightness failed',
                            str(exc), 'failed')
        return {'error': str(exc), 'code': 500}


def switch_update_meta(row_id: int, fields: dict) -> dict:
    allowed = {'name', 'room', 'sort_order', 'hidden'}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return {'error': 'no valid fields', 'code': 400}
    if 'hidden' in updates:
        updates['hidden'] = 1 if updates['hidden'] else 0
    if 'sort_order' in updates:
        try:
            updates['sort_order'] = int(updates['sort_order'])
        except (TypeError, ValueError):
            return {'error': 'sort_order must be int', 'code': 400}
    sets = ', '.join(f'{k}=?' for k in updates)
    vals = list(updates.values()) + [row_id]
    with sqlite3.connect(DB_PATH) as c:
        cur = c.execute(f'UPDATE switches_meta SET {sets} WHERE id=?', vals)
        if cur.rowcount == 0:
            return {'error': 'not found', 'code': 404}
    return {'ok': True}


# ── Rules helpers ────────────────────────────────────────────────────────────
def _rule_row_to_dict(row, conditions):
    rid, name, enabled, days_j, months_j, hour, minute, mode, reserve, gc, ge, notes = row
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
        'notes':        notes,
        'conditions':   conditions,
    }


def _load_all_rules(c):
    rows = c.execute(
        'SELECT id,name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export,notes FROM rules ORDER BY sort_order, id'
    ).fetchall()
    cond_rows = c.execute('SELECT rule_id,logic,type,operator,value FROM rule_conditions').fetchall()
    cond_map = {}
    for rule_id, logic, ctype, op, val in cond_rows:
        cond_map.setdefault(rule_id, []).append(
            {'logic': logic, 'type': ctype, 'operator': op, 'value': val}
        )
    return [_rule_row_to_dict(r, cond_map.get(r[0], [])) for r in rows]


def _rule_fires_at(rule, d):
    weekday     = d.weekday()
    days_set    = set(rule['days'])
    is_holiday  = is_sdge_holiday(d)
    has_weekend = bool(days_set & {5, 6})

    if is_holiday:
        # Treat holiday like a weekend: only weekend rules fire
        if not has_weekend:
            return None
    else:
        if weekday not in days_set:
            return None

    if d.month not in set(rule['months']):
        return None
    return datetime(d.year, d.month, d.day, rule['hour'], rule['minute'])


def _upcoming_firings(rules, hours=48):
    now = datetime.now()
    cutoff = now + timedelta(hours=hours)
    events = []
    paused_shown: set = set()  # track disabled rules already added (show next occurrence only)
    tou = _load_tou_periods()
    for delta_days in (0, 1, 2):
        d = now.date() + timedelta(days=delta_days)
        # Informational holiday entry — weekend rules apply on holidays
        if is_sdge_holiday(d):
            fire_dt = datetime(d.year, d.month, d.day, 0, 0)
            if fire_dt <= cutoff:
                events.append({
                    'fire_time':     fire_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                    'source':        'powerwall',
                    'name':          f'Holiday: {holiday_name(d)} — weekend rules apply',
                    'holiday_info':  True,
                    'mode':          None,
                    'reserve':       None,
                    'grid_charging': None,
                    'grid_export':   None,
                    'conditions':    [],
                })
        for rule in rules:
            if not rule['enabled'] and rule['id'] in paused_shown:
                continue
            fire_dt = _rule_fires_at(rule, d)
            if fire_dt and now < fire_dt <= cutoff:
                events.append({
                    'fire_time':     fire_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                    'source':        'powerwall',
                    'rule_id':       rule['id'],
                    'enabled':       bool(rule['enabled']),
                    'name':          rule['name'],
                    'mode':          rule['mode'],
                    'reserve':       rule['reserve'],
                    'grid_charging': rule['grid_charging'],
                    'grid_export':   rule['grid_export'],
                    'conditions':    rule['conditions'],
                })
                if not rule['enabled']:
                    paused_shown.add(rule['id'])
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


_RULE_REQUIRED_FIELDS = ('name', 'days', 'months', 'hour', 'minute')


def _validate_rule_body(body):
    if not isinstance(body, dict):
        return 'JSON object required'
    missing = [k for k in _RULE_REQUIRED_FIELDS if k not in body]
    if missing:
        return f'missing fields: {", ".join(missing)}'
    return None


@app.route('/api/rules', methods=['POST'])
def api_rules_post():
    body = request.get_json(silent=True)
    err = _validate_rule_body(body)
    if err:
        return jsonify({'error': err}), 400
    days_j   = json.dumps(body['days'])
    months_j = json.dumps(body['months'])
    gc = body.get('grid_charging')
    gc_val = None if gc is None else (1 if gc else 0)
    with sqlite3.connect(DB_PATH) as c:
        c.execute('PRAGMA foreign_keys = ON')
        max_order = c.execute('SELECT COALESCE(MAX(sort_order), 0) FROM rules').fetchone()[0]
        cur = c.execute(
            'INSERT INTO rules (name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export,sort_order,notes) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (body['name'], 1 if body.get('enabled', True) else 0,
             days_j, months_j, body['hour'], body['minute'],
             body.get('mode'), body.get('reserve'), gc_val, body.get('grid_export'),
             max_order + 1, body.get('notes') or None)
        )
        rid = cur.lastrowid
        for cond in body.get('conditions', []):
            c.execute(
                'INSERT INTO rule_conditions (rule_id,logic,type,operator,value) VALUES (?,?,?,?,?)',
                (rid, cond['logic'], cond['type'], cond['operator'], cond['value'])
            )
        row = c.execute(
            'SELECT id,name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export,notes FROM rules WHERE id=?', (rid,)
        ).fetchone()
        conds = c.execute('SELECT rule_id,logic,type,operator,value FROM rule_conditions WHERE rule_id=?', (rid,)).fetchall()
    cond_list = [{'logic': r[1], 'type': r[2], 'operator': r[3], 'value': r[4]} for r in conds]
    _ai_cache['ts'] = 0  # invalidate AI insights cache
    return jsonify(_rule_row_to_dict(row, cond_list)), 201


@app.route('/api/rules/<int:rid>', methods=['PUT'])
def api_rules_put(rid):
    body = request.get_json(silent=True)
    err = _validate_rule_body(body)
    if err:
        return jsonify({'error': err}), 400
    days_j   = json.dumps(body['days'])
    months_j = json.dumps(body['months'])
    gc = body.get('grid_charging')
    gc_val = None if gc is None else (1 if gc else 0)
    with sqlite3.connect(DB_PATH) as c:
        c.execute('PRAGMA foreign_keys = ON')
        c.execute(
            'UPDATE rules SET name=?,enabled=?,days=?,months=?,hour=?,minute=?,mode=?,reserve=?,grid_charging=?,grid_export=?,notes=? WHERE id=?',
            (body['name'], 1 if body.get('enabled', True) else 0,
             days_j, months_j, body['hour'], body['minute'],
             body.get('mode'), body.get('reserve'), gc_val, body.get('grid_export'),
             body.get('notes') or None, rid)
        )
        c.execute('DELETE FROM rule_conditions WHERE rule_id=?', (rid,))
        for cond in body.get('conditions', []):
            c.execute(
                'INSERT INTO rule_conditions (rule_id,logic,type,operator,value) VALUES (?,?,?,?,?)',
                (rid, cond['logic'], cond['type'], cond['operator'], cond['value'])
            )
        row = c.execute(
            'SELECT id,name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export,notes FROM rules WHERE id=?', (rid,)
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


@app.route('/api/rules/reorder', methods=['POST'])
def api_rules_reorder():
    """Accept an ordered list of rule IDs and update sort_order accordingly."""
    body = request.get_json(silent=True)
    ids = body.get('ids') if body else None
    if not ids or not isinstance(ids, list):
        return jsonify({'error': 'ids list required'}), 400
    with sqlite3.connect(DB_PATH) as c:
        for pos, rid in enumerate(ids):
            c.execute('UPDATE rules SET sort_order=? WHERE id=?', (pos, rid))
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
# holiday_name() imported from fetch_rates


def _fmt_hour(h):
    """Format hour int as '2 PM', '12 AM', etc."""
    ampm = 'AM' if h < 12 else 'PM'
    h12 = h % 12 or 12
    if h == 0:
        return 'midnight'
    return f'{h12} {ampm}'


def _analyze_rules(rules, rates, holidays, tou_periods=None):
    """Deterministic analysis of Powerwall rules against EV-TOU-2 rate schedule."""
    insights = []
    now = datetime.now()
    today = now.date()

    enabled = [r for r in rules if r.get('enabled')]
    sop_winter = rates.get('winter_super_off_peak', 0.25)
    sop_summer = rates.get('summer_super_off_peak', 0.26)
    on_summer  = rates.get('summer_on_peak', 0.78)
    on_winter  = rates.get('winter_on_peak', 0.51)

    # Derive TOU time boundaries from configurable periods
    _tp = tou_periods or {}
    _wd = _tp.get('weekday', {})
    _wh = _tp.get('weekend_holiday', {})

    # Split weekday super off-peak into overnight (<8 AM) and daytime (>=8 AM) windows
    wd_sop_ranges    = _wd.get('super_off_peak', [[0, 6], [10, 14]])
    wd_sop_overnight = [r for r in wd_sop_ranges if r[0] < 8] or [[0, 6]]
    wd_sop_daytime   = [r for r in wd_sop_ranges if r[0] >= 8]
    wd_sop_start     = _fmt_hour(min(s for s, _ in wd_sop_overnight))
    wd_sop_end       = _fmt_hour(max(e for _, e in wd_sop_overnight))
    day_sop_start_h  = min(s for s, _ in wd_sop_daytime) if wd_sop_daytime else 10
    day_sop_end_h    = max(e for _, e in wd_sop_daytime) if wd_sop_daytime else 14
    day_sop_start    = _fmt_hour(day_sop_start_h)
    day_sop_end      = _fmt_hour(day_sop_end_h)

    # On-peak window (e.g. 4 PM–9 PM)
    on_peak_ranges = _wd.get('on_peak', [[16, 21]])
    on_start_h = min(s for s, _ in on_peak_ranges)
    on_end_h   = max(e for _, e in on_peak_ranges)
    on_start   = _fmt_hour(on_start_h)
    on_end     = _fmt_hour(on_end_h)

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
                            f'Super off-peak runs {wd_sop_start}\u2013{wd_sop_end} at ${sop_winter:.3f}/kWh.'
                        ),
                        'action': 'Start grid charging earlier to fully charge at super off-peak rates.',
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
            'action': f'Add a rule to enable grid charging during {wd_sop_start}\u2013{wd_sop_end} (super off-peak).',
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

    # ── 3. Weekday daytime super off-peak window ─────────────────────────────
    if wd_sop_daytime:
        day_tbc = [r for r in enabled
                   if r.get('mode') == 'autonomous'
                   and {0, 1, 2, 3, 4} & set(r['days'])
                   and day_sop_start_h <= r['hour'] < day_sop_end_h]
        if not day_tbc:
            insights.append({
                'severity': 'suggestion',
                'title':  f'Weekday daytime super off-peak window ({day_sop_start}–{day_sop_end}) not utilized',
                'detail': (
                    f'EV-TOU-2 has a daytime super off-peak window {day_sop_start}–{day_sop_end} on weekdays year-round '
                    f'(${sop_winter:.3f}/kWh winter, ${sop_summer:.3f}/kWh summer). '
                    f'Switching to Time-Based Control means the home draws from the grid at the cheapest rate '
                    f'while solar (if available) charges the battery instead of exporting at the low super off-peak credit rate.'
                ),
                'action': f'Create rules: Time-Based Control at {day_sop_start} and Self-Powered at {day_sop_end}, weekdays.',
            })
    # ── 4. No rule at on-peak boundary ──────────────────────────────────────
    at_on_start = [r for r in enabled if r['hour'] == on_start_h and r['minute'] <= 5]
    if not at_on_start:
        insights.append({
            'severity': 'suggestion',
            'title':  f'No rule at {on_start} on-peak boundary',
            'detail': (
                f'On-peak starts at {on_start} (${on_summer:.3f}/kWh summer, ${on_winter:.3f}/kWh winter). '
                f'No rule adjusts Powerwall settings at this critical transition.'
            ),
            'action': f'Consider a {on_start} rule to set Self-Powered mode and verify reserve covers the {on_start}\u2013{on_end} peak.',
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
                missed = r['hour'] - on_start_h
                insights.append({
                    'severity': 'suggestion',
                    'title':  f'Battery export starts at {r["hour"]}:{r["minute"]:02d} \u2014 on-peak begins {on_start}',
                    'detail': (
                        f'"{r["name"]}" enables battery export {missed}+ hours after on-peak starts. '
                        f'On-peak runs {on_start}\u2013{on_end} at ${rate_val:.3f}/kWh ({season_label}).'
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

    # Derive holiday super off-peak end hour from TOU periods
    _p = tou_periods or {}
    _hol_sop = _p.get('weekend_holiday', {}).get('super_off_peak', [[0, 14]])
    _hol_sop_end = _fmt_hour(max(e for _, e in _hol_sop))
    _hol_on  = _p.get('weekend_holiday', {}).get('on_peak', [[16, 21]])
    _hol_on_start = _fmt_hour(min(s for s, _ in _hol_on))
    _hol_on_end   = _fmt_hour(max(e for _, e in _hol_on))

    for hd in weekday_holidays:
        name = holiday_name(hd)
        day_name = hd.strftime('%A')
        insights.append({
            'severity':     'info',
            'title':        f'{name} ({hd.strftime("%b %d")}) — weekend rules apply',
            'detail': (
                f'{name} falls on a {day_name} and uses the holiday TOU schedule: '
                f'super off-peak midnight\u2013{_hol_sop_end}, on-peak {_hol_on_start}\u2013{_hol_on_end}. '
                f'Weekend rules will fire automatically on this day. '
                f'Use a holiday condition to create holiday-specific rules (e.g. battery hold).'
            ),
            'action': 'Review your weekend rules to ensure they cover holiday behavior.',
            'holiday_date': hd.isoformat(),
        })

    # ── 8. Holiday calendar health ───────────────────────────────────────────
    if not holidays:
        insights.append({
            'severity': 'warning',
            'title':  'No holiday dates configured',
            'detail': (
                f'SDG&E holidays use a different TOU schedule (super off-peak midnight\u2013{_hol_sop_end}). '
                'Without holiday dates, weekend rules cannot activate on holidays and '
                'holiday conditions will not work.'
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
    insights = _analyze_rules(rules, rates, holidays, _load_tou_periods())
    return jsonify(insights)


# ── Gemini AI Insights ───────────────────────────────────────────────────────
def _gemini_system_prompt(tou_periods=None):
    """Build the Gemini system prompt with TOU times derived from settings."""
    _tp = tou_periods or {}
    _wd = _tp.get('weekday', {})
    _wh = _tp.get('weekend_holiday', {})

    wd_sop = _wd.get('super_off_peak', [[0, 6], [10, 14]])
    wd_sop_overnight = [r for r in wd_sop if r[0] < 8] or [[0, 6]]
    wd_sop_daytime   = [r for r in wd_sop if r[0] >= 8] or [[10, 14]]
    wd_sop_night_end = _fmt_hour(max(e for _, e in wd_sop_overnight))
    wd_day_start     = _fmt_hour(min(s for s, _ in wd_sop_daytime))
    wd_day_end       = _fmt_hour(max(e for _, e in wd_sop_daytime))

    on_pk = _wd.get('on_peak', [[16, 21]])
    on_start = _fmt_hour(min(s for s, _ in on_pk))
    on_end   = _fmt_hour(max(e for _, e in on_pk))

    hol_sop = _wh.get('super_off_peak', [[0, 14]])
    hol_sop_end = _fmt_hour(max(e for _, e in hol_sop))

    hol_on = _wh.get('on_peak', [[16, 21]])
    hol_on_start = _fmt_hour(min(s for s, _ in hol_on))
    hol_on_end   = _fmt_hour(max(e for _, e in hol_on))

    return f"""\
You are an energy optimization advisor for a specific home in San Diego, CA.

## System
- 3× Tesla Powerwall 2 (40.5 kWh total usable capacity, ~90% round-trip efficiency)
- Rooftop solar — production varies seasonally (summer months have longer daylight
  and higher production than winter months)
- SDG&E EV-TOU-2 rate plan — use the EXACT rate values from the rates object in
  the provided data, never guess or use generic values
- Annual true-up in January
- IMPORTANT: SDG&E does NOT pay out excess true-up credits at a meaningful rate.
  The homeowner's goal is to land in a small credit range ($100–$500 credit).
  **Overproducing credits is wasted energy** — do not recommend maximizing exports.
  The strategy is a balance: capture enough on-peak credit to offset winter imports,
  while preserving battery for post-sunset self-consumption to avoid expensive
  grid imports during the remaining evening hours.
- Location: San Diego — mild winters, long sunny summers. Use actual rule times
  and rate period boundaries from the data; do not hardcode specific times.

## Data conventions — read carefully
- Battery (W): positive = charging, negative = discharging
- Grid (W): positive = importing from grid, negative = exporting to grid
- On-Peak Net / Off-Peak Net / Super Off-Peak Net (kWh): signed net values —
  negative = net export credit earned during that period
- **CRITICAL — projection Net column sign convention:**
  - POSITIVE Net = deficit (homeowner OWES SDG&E this amount)
  - NEGATIVE Net = credit (SDG&E OWES the homeowner this amount)
  - Example: Net = -$328.15 means a CREDIT of $328.15 (good outcome, within $100-$500 target)
  - Example: Net = +$328.15 means a DEFICIT of $328.15 (bad outcome, need more exports)
  - Never describe a negative number as a "deficit" — a negative Net is ALWAYS a credit
  - Always check the sign before labeling the outcome
- Rule-Based Findings: deterministic gaps already identified by a separate analysis
  engine — do NOT repeat these findings, go deeper or synthesize across them
- Rule names are DESCRIPTIVE, not authoritative. If a rule's name disagrees with
  its actual values, the values are what the system executes — but flag the
  disagreement as a likely bug for the user to review.

## Rate structure
Use the exact summer_on_peak, summer_off_peak, summer_super_off_peak, winter_on_peak,
winter_off_peak, winter_super_off_peak values from the rates object provided.

Key EV-TOU-2 nuances:
- On-peak ({on_start}–{on_end}) applies EVERY day including weekends and holidays — no exemptions
- Weekday super off-peak: midnight–{wd_sop_night_end} and {wd_day_start}–{wd_day_end} year-round
- Holidays follow weekend schedule: super off-peak midnight–{hol_sop_end}, on-peak {hol_on_start}–{hol_on_end}, off-peak fills the remaining hours
- Summer rates apply June–October; winter rates apply November–May

## How to read the rules
The rules array defines the automation schedule. Each rule fires at hour:minute on
the specified days (0=Mon..6=Sun) and months (1=Jan..12=Dec). Rules change only the
fields they specify — null fields carry forward from the previous rule.

Key fields:
- mode: controls how the Powerwall sources home power
  - autonomous (Time-Based Control): home draws ALL power from the grid; battery does NOT
    discharge to power the home; solar (if available) charges the battery instead of exporting
  - self_consumption: home draws from solar + battery first; grid is fallback; excess solar exports
  - backup: battery reserved for outages only
- reserve: battery floor % — personal preference, not strategy-driven
- grid_charging (true/false): explicit control to charge battery FROM grid; separate from mode;
  most useful overnight when no solar is available
- grid_export (battery_ok | pv_only): whether battery actively discharges to grid

## Rule intent validation
Some rules include an "Intent:" note written by the homeowner describing what the rule
is supposed to do. When a rule has an intent note:
1. Treat the note as an assertion to verify, not just context.
2. Check whether the rule's actual values (mode, grid_export, reserve, grid_charging,
   conditions, days, months) produce the behavior described in the note.
3. Check whether other rules that fire before or after this rule on the same day type
   (weekday / Saturday / Sunday / holiday) interfere with the stated intent.
4. If the rule does what the note says: confirm it briefly and move on.
5. If the rule does NOT do what the note says: flag the specific discrepancy clearly.
   Explain what the rule actually does vs. what the note claims, and identify which
   specific value or interaction is causing the gap.
6. If the note is ambiguous or partially correct: say so and explain what is and isn't accurate.
Do not treat intent notes as authoritative. They are the homeowner's best understanding,
which may be incomplete or incorrect.

IMPORTANT: grid_export = battery_ok PERMITS battery discharge to grid (up to ~15 kW
combined, 3× Powerwall; at 1% reserve nearly the full 40.5 kWh is available). However,
in TBC (autonomous) mode the Powerwall only actually exports during windows its internal
schedule marks as peak (4–9 PM daily). Outside that window, battery_ok is effectively
a no-op in TBC mode — the battery will not export even though export is permitted.
This is NOT passive solar overflow. If an intent note claims a rule exports to grid,
verify that the rule fires within 4–9 PM; if it fires outside that window in TBC mode,
flag it — the export will likely not occur as intended.

The rules are the homeowner's deliberate automation design. Your job is to understand
what they do by reading the rules array — not to assume what they "should" do. Read
rules in firing order (by hour/minute within each day-type) to understand daily behavior.

INFER the homeowner's strategy from the rules — do not impose a strategy. Common
patterns you may see:
- TBC (autonomous) during super off-peak: home draws ALL power from the grid at the cheapest
  rate; battery does not discharge. During the daytime window (10 AM–2 PM), solar charges the
  battery instead of exporting at the low super off-peak credit rate — a deliberate counter to
  SDG&E pricing that reduces export credit during peak solar hours. Overnight TBC (midnight–6 AM)
  has no solar; a short grid_charging: true window may be added to explicitly fill the battery
  from grid before solar arrives.
- Self-Powered during on-peak: draw from stored battery + solar to minimize grid imports at the
  most expensive rate.
- Active battery export (grid_export: battery_ok) during on-peak evening: discharge stored energy
  to grid for maximum credit. This is NOT passive solar overflow — it actively drains the battery.
- Battery idle during on-peak daytime: may be intentional if solar alone covers home load and
  the battery is being held for evening export or self-consumption.

These are all valid design choices. A battery sitting idle during on-peak hours is
NOT automatically a problem — it may be intentional passive-export mode or reserved
for later self-consumption. Verify the actual flow before suggesting otherwise.

## Prior year data — context for projections
The `Prior Year Monthly Summary` contains ACTUAL monthly performance from the previous
year. Real measured data from the same house, same solar panels, same location.

Prior year behavior reflects a DIFFERENT automation strategy (Tesla's Time-Based Control
algorithm). Current year uses custom rules — the behavior may differ. Compare thoughtfully,
but do not assume current-year patterns will match prior-year patterns for projected
months. Summer and winter behave differently; do not extrapolate from the most recent
month alone.

## Your analysis — cover all four areas:

**1. True-up trajectory**

The `trueup_projection_table` field contains a PRE-RENDERED markdown table.
These numbers were computed server-side with exact arithmetic.

The table is displayed separately in the UI — DO NOT reproduce it in your response.
DO NOT output a markdown table of the projection numbers.
Instead, reference the numbers directly in your analysis (e.g., "June shows -$234 credit").

Analyze:
- Report the full-year projected Net with correct sign interpretation (positive = deficit,
  negative = credit).
- **If Net is between -$500 and -$100 (projected credit in the $100-$500 target range),
  explicitly state "the projection is within target" and do NOT frame this as a problem
  requiring intervention.**
- If Net is outside target (overshoot >$500 credit, undershoot <$100 credit, or deficit),
  identify the main drivers.
- Which months drive the most credit? Which are the biggest costs?
- Flag real risks (data quality issues, unusual month patterns) — not hypothetical ones.

The table MUST appear before any rule change recommendations.

**2. Seasonal transition impact**
Based on the current season and when the next season starts:
- Walk through what happens on a typical day in the upcoming season based on the
  current rules — what mode is the system in at each key time of day? Describe this
  in plain English (e.g., "Around 4:00 AM the battery charges from the grid...").
  Do not quote rule names directly — describe what the system does, not which rule fires.
- How will the season shift affect solar production, electricity rates, and the
  opportunity to sell power back to the grid?
- What rule changes should be made BEFORE the transition?
- Address the battery export window timing given longer summer daylight hours.

**3. Rule review (not "optimization" by default)**
**ONLY discuss rules where you have identified an actual issue — name-vs-value
inconsistency, sequencing gap, or month coverage error. Do not narrate rules that
are functioning correctly. Silence on a rule means it is fine.**

**ONLY suggest rule changes if you can point to a specific inconsistency or gap that
the homeowner likely did not intend.** If the rules appear internally consistent and
the projection is within target, say so — do not invent optimizations for their own sake.

Focus on these checks:
- **Name-vs-value consistency.** The rule's quoted name describes intent; the resolved
  values after "|" show actual behavior. If a name disagrees with its values (e.g., name
  says "export solar only" but action says "active battery export enabled", or name
  mentions a specific time but the resolved time differs), flag it as a likely bug.
- **Rule sequencing.** Trace rule firings in time order per day-type (weekday / Saturday
  / Sunday). Flag cases where (a) a rule enables battery export and no later rule on
  the same day-type switches to solar-only export or changes mode away from active
  discharge before the next day starts, OR (b) two rules fire within 30 minutes and
  the later one contradicts the earlier (the earlier rule has no lasting effect).
- **Month coverage.** Check that each rule's months list actually contains the months
  the name implies. Do not invent month-coverage claims; verify them in the data.
- **Day-type asymmetry (optional observation).** If one day-type has a "stop export"
  rule but another doesn't, mention it as an observation — not a requirement — unless
  it's creating a measurable problem.

**When a Rule-Based Finding flags that battery export starts later than the on-peak
window opens:** Do NOT simply recommend moving it earlier. Instead, reason through
the full energy picture for that window:
- Is solar production still strong between the on-peak start and the current export
  start time? If so, the battery may be charging or at capacity — active export
  during that window would cut into solar charging, not idle capacity.
- After the export window closes, how much battery capacity remains? Would starting
  export earlier leave insufficient reserve for post-on-peak self-consumption,
  potentially causing grid imports at off-peak rates that offset the credit gains?
- Only recommend an earlier export start if you can demonstrate from the data that
  the battery is genuinely full AND idle during that window AND adequate reserve
  would remain for evening self-consumption. If you cannot demonstrate both
  conditions, note the timing as likely intentional and explain the probable rationale
  (e.g., "the later start preserves a full battery for solar charging earlier in the
  afternoon, then exports once solar tapers off").

For any change you suggest, describe the directional impact only (e.g., "captures
more on-peak credit", "reduces morning grid imports"). Do NOT estimate or invent
dollar figures — you do not have the granular hourly data needed to compute them
accurately. Alternative perspectives are welcome as observations, but not required.

**The rule_based_insights findings are already displayed to the user above your
response. Do not restate them.** Your job is to synthesize: do multiple findings
point to a pattern? Does a finding connect to something measurable in the daily
data or projection? A finding is only worth mentioning if you can add context
that the finding itself does not contain — otherwise, leave it out.

Concrete example of what NOT to do: "The Rule-Based Findings correctly identify
that export starts at 7 PM, missing the first three hours of on-peak..." — that
is pure repetition. Instead, connect to data: "On May 17 the battery was at 0%
at 7 AM, which is consistent with the short charging window flagged above."

**4. Daily data observations**
Looking at the daily cost data, comment ONLY if you notice:
- Days with markedly lower credits than expected given the weather/solar context
- Inconsistent patterns between day-types that may indicate a rule gap

Do not push "untapped opportunities" unless the overall projection is undershooting
the target credit range. Exporting more is only valuable if the projection is below
target; overshooting wastes energy at SDG&E's poor surplus payout rate.

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
Use markdown. Use at most ### for headings — never #### or deeper. Use the actual rate
values and cost figures from the data — no generic estimates.
Do not repeat findings already listed in rule_based_insights.

CRITICAL — Write for a homeowner, not an engineer:
- Use plain English terms like "solar production", "grid imports", "battery level",
  "on-peak credits" — NOT technical or code-like identifiers.
- Use 12-hour time: "5:00 PM" — never "hour: 17" or "19:15".
- For rule recommendations: explain WHY the change helps and the expected dollar impact.
  Do NOT walk the user through how to create or edit a rule — they know how.
  Use actual times from the current rules and rate periods; never invent times.
- Never output JSON, arrays, code blocks, underscore_identifiers, or field syntax in recommendations.
- Use dollar amounts to justify every recommendation.

Keep the total response focused — depth over breadth.

After all rule recommendations, end with:

**5. Projected impact (informational)**
Check `optimized_identical` in Data Quality Notes.

- If `true`: the optimized projection is identical to the baseline — all projected
  months already have export rules configured. **Skip this section entirely. Do not
  mention the optimized projection or the "After Changes" table.**
- If `false`: a pre-calculated "After Changes" projection is displayed in the UI
  alongside the baseline. These numbers are computed server-side — DO NOT reproduce
  them as a table. Analyze:
  - Does the optimized projection bring the baseline within the $100-$500 target?
  - If it overshoots into >$500 credit, suggest scaling back (fewer months, higher
    reserve). If it still falls short, note what additional changes might help.
  - State the difference between baseline total and optimized total.
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

    # Home consumption ratio — only use months where both CY and PY have readings data.
    # This prevents ratio explosion when CY is missing future months (e.g. Nov/Dec
    # haven't happened yet) while PY has full-year data.
    # Winter = Nov–May (SDG&E), summer = Jun–Oct.
    winter_months = {1, 2, 3, 4, 5, 11, 12}
    summer_months = {6, 7, 8, 9, 10}

    def _ratio_for_season(months):
        cy_tot = py_tot = 0.0
        for m in months:
            cy_h = cy_power.get(m, {}).get('home_kwh', 0)
            py_h = py_power.get(m, {}).get('home_kwh', 0)
            if cy_h > 0 and py_h > 0:
                cy_tot += cy_h
                py_tot += py_h
        return cy_tot / py_tot if py_tot > 0 else 1.0

    winter_home_ratio = _ratio_for_season(winter_months)
    raw_summer_ratio = _ratio_for_season(summer_months)
    summer_home_ratio = raw_summer_ratio if raw_summer_ratio != 1.0 else min(winter_home_ratio, 1.10)

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

    # Cache TOU periods once — _rule_export_hours is called ~24× per
    # projection build, and the value never changes mid-request.
    _tou_periods_cache = _load_tou_periods() or {}

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
            # Fall back to on-peak end from TOU periods (cached above)
            _on_pk = _tou_periods_cache.get('weekday', {}).get('on_peak', [[16, 21]])
            latest_end = float(max(e for _, e in _on_pk))
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
        'optimized_identical': (baseline_md == optimized_md),
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
                'Time': datetime.fromtimestamp(row[0]).strftime('%Y-%m-%d %H:%M'),
                'Solar (W)': round(row[1] or 0),
                'Home Load (W)': round(row[2] or 0),
                'Battery (W)': round(row[3] or 0),
                'Grid (W)': round(row[4] or 0),
                'Battery Level (%)': round(row[5] or 0, 1),
            })
            last_ts = row[0]

    # Current year monthly summaries
    current_year_monthly = []
    for row in cy_monthly_rows:
        current_year_monthly.append({
            'Month': row[0],
            'Grid Import (kWh)': round(row[1] or 0, 1),
            'Grid Export (kWh)': round(row[2] or 0, 1),
            'Import Cost ($)': round(row[3] or 0, 2),
            'Export Credit ($)': round(row[4] or 0, 2),
            'On-Peak Net (kWh)': round(row[5] or 0, 1),
            'Off-Peak Net (kWh)': round(row[6] or 0, 1),
            'Super Off-Peak Net (kWh)': round(row[7] or 0, 1),
        })

    # Last 7 days of daily costs
    daily_costs_7d = []
    for row in cost_rows:
        daily_costs_7d.append({
            'Date': row[0],
            'Grid Import (kWh)': round(row[1] or 0, 2),
            'Grid Export (kWh)': round(row[2] or 0, 2),
            'Import Cost ($)': round(row[3] or 0, 2),
            'Export Credit ($)': round(row[4] or 0, 2),
            'On-Peak Net (kWh)': round(row[5] or 0, 2),
            'Off-Peak Net (kWh)': round(row[6] or 0, 2),
            'Super Off-Peak Net (kWh)': round(row[7] or 0, 2),
        })

    # Rules as natural-language strings (prevents JSON leakage in recommendations)
    _DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    _MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    _MODE_LABELS = {
        'self_consumption': 'Self-Powered mode',
        'autonomous': 'Time-Based Control mode',
        'backup': 'Backup mode',
    }
    _EXPORT_LABELS = {
        'battery_ok': 'active battery export enabled',
        'pv_only': 'battery export disabled (solar-only export)',
    }
    _COND_LABELS = {
        'battery_pct': 'battery %',
        'net_cost': 'net cost today $',
    }

    def _fmt_days(days):
        if set(days) == {0, 1, 2, 3, 4, 5, 6}:
            return 'Every day'
        if set(days) == {0, 1, 2, 3, 4}:
            return 'Weekdays'
        if set(days) == {5, 6}:
            return 'Weekends'
        if set(days) == {0, 1, 2, 3, 4, 6}:
            return 'Mon-Fri and Sun'
        return ', '.join(_DAY_NAMES[d] for d in sorted(days))

    def _fmt_months(months):
        s = set(months)
        if s == set(range(1, 13)):
            return 'All year'
        if s == {6, 7, 8, 9, 10}:
            return 'June-October (summer)'
        if s == {1, 2, 3, 4, 5, 11, 12}:
            return 'November-May (winter)'
        # Check for contiguous range
        sorted_m = sorted(s)
        if sorted_m == list(range(sorted_m[0], sorted_m[-1] + 1)):
            return f'{_MONTH_NAMES[sorted_m[0]-1]}-{_MONTH_NAMES[sorted_m[-1]-1]}'
        return ', '.join(_MONTH_NAMES[m-1] for m in sorted_m)

    def _fmt_time(h, m):
        period = 'AM' if h < 12 else 'PM'
        display_h = h if h <= 12 else h - 12
        if display_h == 0:
            display_h = 12
        return f'{display_h}:{m:02d} {period}'

    rule_descriptions = []
    for r in rules:
        parts = [f'"{r["name"]}"']
        parts.append(f'{"ENABLED" if r["enabled"] else "DISABLED"}')
        parts.append(f'{_fmt_days(r["days"])}, {_fmt_months(r["months"])} at {_fmt_time(r["hour"], r["minute"])}')
        actions = []
        if r.get('mode'):
            actions.append(_MODE_LABELS.get(r['mode'], r['mode']))
        else:
            actions.append('mode unchanged')
        if r.get('reserve') is not None:
            actions.append(f'battery reserve {r["reserve"]}%')
        if r.get('grid_charging') is True:
            actions.append('grid charging ON')
        elif r.get('grid_charging') is False:
            actions.append('grid charging OFF')
        if r.get('grid_export'):
            actions.append(_EXPORT_LABELS.get(r['grid_export'], r['grid_export']))
        parts.append('→ ' + ', '.join(actions))
        conds = r.get('conditions', [])
        if conds:
            cond_parts = []
            for c in conds:
                label = _COND_LABELS.get(c['type'], c['type'])
                cond_parts.append(f'{c["logic"]} {label} {c["operator"]} {c["value"]}')
            # strip leading 'AND '/'OR ' from first condition
            first = cond_parts[0].split(' ', 1)[1] if cond_parts else ''
            rest = cond_parts[1:]
            parts.append('if ' + first + (' ' + ' '.join(rest) if rest else ''))
        if r.get('notes'):
            parts.append(f'Intent: {r["notes"]}')
        rule_descriptions.append(' | '.join(parts))

    # Prior year monthly summaries
    prior_year_monthly = []
    for row in py_rows:
        prior_year_monthly.append({
            'Month': row[0],
            'Grid Import (kWh)': round(row[1] or 0, 1),
            'Grid Export (kWh)': round(row[2] or 0, 1),
            'Import Cost ($)': round(row[3] or 0, 2),
            'Export Credit ($)': round(row[4] or 0, 2),
            'On-Peak Net (kWh)': round(row[5] or 0, 1),
            'Off-Peak Net (kWh)': round(row[6] or 0, 1),
            'Super Off-Peak Net (kWh)': round(row[7] or 0, 1),
        })

    # Rule-based insights for additional context
    rule_insights = _analyze_rules(rules, rates, SDGE_HOLIDAYS, _load_tou_periods())

    is_summer = now.month in (6, 7, 8, 9, 10)
    jan1_next = date(now.year + 1, 1, 1)
    days_until_trueup = (jan1_next - today).days

    with _lock:
        live_snapshot = dict(_live)

    return json.dumps({
        "Today's Date": today.isoformat(),
        'Current Season': 'summer' if is_summer else 'winter',
        'Next Season Change': 'June 1' if not is_summer else 'November 1',
        'Days Until True-Up': days_until_trueup,
        'Battery Capacity (kWh)': 40.5,
        'Powerwall Count': 3,
        'SDG&E Rates': {k: v for k, v in rates.items()},
        'Upcoming Holidays': holidays,
        'Rules': rule_descriptions,
        'Rule-Based Findings': [{'Title': i['title'], 'Action': i['action']} for i in rule_insights],
        'True-Up Projection Table': baseline_md,
        'Optimized Projection Table': None if projection_meta.get('optimized_identical') else optimized_md,
        'Prior Year Monthly Summary': prior_year_monthly,
        'Prior Year Note': _build_prior_year_note(rules, prior_year, now.year),
        'Current Year Monthly Summary': current_year_monthly,
        'Daily Costs (Last 7 Days)': daily_costs_7d,
        'Power Readings (Last 7 Days, 3-hourly samples)': sampled,
        'Current State': {
            'Battery Level (%)': round(live_snapshot.get('battery_pct', 0), 1),
            'Solar (W)': round(live_snapshot.get('solar_w', 0)),
            'Home Load (W)': round(live_snapshot.get('home_w', 0)),
            'Grid (W)': round(live_snapshot.get('grid_w', 0)),
            'Mode': _MODE_LABELS.get(live_snapshot.get('mode', ''), live_snapshot.get('mode', 'unknown')),
        },
        'Data Quality Notes': projection_meta,
    }, indent=None, default=str), baseline_md, optimized_md


_ai_cache = {'text': None, 'model': None, 'ts': 0, 'table': None}

# Cache never auto-expires. Invalidated only on:
#   - rule create/update/delete (sets ts=0)
#   - explicit refresh request (?refresh=1 query param)
#   - server restart (in-memory only)


class _ProviderError(Exception):
    """Raised by provider call helpers. Carries transient/permanent flag."""
    def __init__(self, message, status=None, transient=False):
        super().__init__(message)
        self.status = status
        self.transient = transient


def _extract_api_error(resp, max_len: int = 200) -> str:
    """Pull a human-readable error message out of a JSON API response.
    Falls back to a truncated body when JSON parsing fails."""
    try:
        return resp.json().get('error', {}).get('message', resp.text[:max_len])
    except Exception:
        return resp.text[:max_len]


def _call_gemini(system_prompt: str, user_msg: str, model: str, api_key: str) -> str:
    """Call Gemini once. Returns response text or raises _ProviderError."""
    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
    payload = {
        'system_instruction': {'parts': [{'text': system_prompt}]},
        'contents': [{'parts': [{'text': user_msg}]}],
        'generationConfig': {
            'temperature': 0.2,
            'maxOutputTokens': 65536,
            'thinkingConfig': {'thinkingBudget': 0},
        },
    }
    try:
        resp = _requests.post(url, json=payload, timeout=300)
    except _requests.exceptions.Timeout:
        raise _ProviderError('Timeout', status=504, transient=True)
    except _requests.exceptions.ConnectionError as exc:
        raise _ProviderError(f'Connection error: {exc}', status=None, transient=True)

    if resp.status_code == 429 or resp.status_code >= 500:
        raise _ProviderError(_extract_api_error(resp), status=resp.status_code, transient=True)
    if resp.status_code >= 400:
        raise _ProviderError(_extract_api_error(resp), status=resp.status_code, transient=False)

    data = resp.json()
    text = ''
    candidates = data.get('candidates', [])
    if candidates:
        parts = candidates[0].get('content', {}).get('parts', [])
        text = '\n'.join(p.get('text', '') for p in parts)
    if not text:
        raise _ProviderError('Empty response', status=200, transient=True)
    return text


def _call_azure_openai(system_prompt: str, user_msg: str,
                       endpoint: str, deployment: str, api_key: str,
                       api_version: str) -> str:
    """Call Azure OpenAI chat/completions once. Returns response text or raises _ProviderError."""
    endpoint = endpoint.rstrip('/')
    url = f'{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}'
    payload = {
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_msg},
        ],
        'temperature': 0.2,
        'max_tokens': 4000,
    }
    headers = {'api-key': api_key, 'Content-Type': 'application/json'}
    try:
        resp = _requests.post(url, json=payload, headers=headers, timeout=300)
    except _requests.exceptions.Timeout:
        raise _ProviderError('Timeout', status=504, transient=True)
    except _requests.exceptions.ConnectionError as exc:
        raise _ProviderError(f'Connection error: {exc}', status=None, transient=True)

    if resp.status_code >= 400:
        transient = resp.status_code == 429 or resp.status_code >= 500
        raise _ProviderError(_extract_api_error(resp), status=resp.status_code, transient=transient)

    data = resp.json()
    choices = data.get('choices', [])
    if not choices:
        raise _ProviderError('Empty response', status=200, transient=True)
    text = choices[0].get('message', {}).get('content', '')
    if not text:
        raise _ProviderError('Empty content', status=200, transient=True)
    return text


def _azure_configured() -> bool:
    return bool(
        get_setting('azure_openai_endpoint', '') and
        get_setting('azure_openai_api_key', '') and
        get_setting('azure_openai_deployment', '')
    )


@app.route('/api/rules/ai-insights', methods=['POST'])
def api_rules_ai_insights():
    # Respect explicit refresh request (bypasses cache)
    force_refresh = request.args.get('refresh') == '1'

    # Return cached response if available and not invalidated (ts > 0)
    if not force_refresh and _ai_cache['text'] and _ai_cache['ts'] > 0:
        return jsonify({'ok': True, 'insights': _ai_cache['text'], 'model': _ai_cache['model'],
                        'projection_table': _ai_cache['table'],
                        'optimized_table': _ai_cache.get('optimized'), 'cached': True,
                        'provider': _ai_cache.get('provider', 'gemini')})

    gemini_key = get_setting('gemini_api_key', '')
    gemini_model = get_setting('gemini_model', 'gemini-2.0-flash')
    if not gemini_key and not _azure_configured():
        return jsonify({'ok': False, 'error': 'No AI provider configured. Add a Gemini or Azure OpenAI key in Settings.'}), 400

    context, table_md, opt_md = _build_ai_context()
    system_prompt = _gemini_system_prompt(_load_tou_periods())
    user_msg = f'Here is the current home energy data:\n\n{context}'

    last_err = None
    last_status = None

    # Phase 1: Gemini with retries (2s, 4s backoff)
    if gemini_key:
        for attempt in range(3):
            if attempt > 0:
                time.sleep(2 ** attempt)
            try:
                text = _call_gemini(system_prompt, user_msg, gemini_model, gemini_key)
                _ai_cache['text'] = text
                _ai_cache['model'] = gemini_model
                _ai_cache['table'] = table_md
                _ai_cache['optimized'] = opt_md
                _ai_cache['provider'] = 'gemini'
                _ai_cache['ts'] = time.time()
                return jsonify({'ok': True, 'insights': text, 'model': gemini_model,
                                'projection_table': table_md, 'optimized_table': opt_md,
                                'provider': 'gemini'})
            except _ProviderError as exc:
                last_err = str(exc)
                last_status = exc.status
                print(f'Gemini attempt {attempt + 1}/3 failed ({exc.status}): {exc}')
                if not exc.transient:
                    break  # don't retry permanent errors like 401, 400
            except Exception as exc:
                last_err = str(exc)
                last_status = 500
                print(f'Gemini attempt {attempt + 1}/3 unexpected error: {exc}')

    gemini_err = f'Gemini: {last_err} (status {last_status})' if last_err else 'Gemini: not configured'

    # Phase 2: Azure OpenAI fallback (single attempt)
    if _azure_configured():
        endpoint = get_setting('azure_openai_endpoint', '')
        deployment = get_setting('azure_openai_deployment', '')
        azure_key = get_setting('azure_openai_api_key', '')
        api_version = get_setting('azure_openai_api_version', '2024-10-21')
        try:
            text = _call_azure_openai(system_prompt, user_msg, endpoint, deployment, azure_key, api_version)
            _ai_cache['text'] = text
            _ai_cache['model'] = f'azure:{deployment}'
            _ai_cache['table'] = table_md
            _ai_cache['optimized'] = opt_md
            _ai_cache['provider'] = 'azure_openai'
            _ai_cache['ts'] = time.time()
            print(f'Gemini failed, Azure OpenAI ({deployment}) succeeded as fallback')
            return jsonify({'ok': True, 'insights': text, 'model': f'azure:{deployment}',
                            'projection_table': table_md, 'optimized_table': opt_md,
                            'provider': 'azure_openai'})
        except _ProviderError as exc:
            azure_err = f'Azure: {exc} (status {exc.status})'
            print(f'Azure OpenAI fallback also failed: {exc}')
            last_err = f'{gemini_err}; {azure_err}'
        except Exception as exc:
            print(f'Azure OpenAI fallback unexpected error: {exc}')
            last_err = f'{gemini_err}; Azure: {exc}'
    else:
        last_err = f'{gemini_err}; Azure not configured'

    # Phase 3: Stale cache fallback
    if _ai_cache['text']:
        age_min = int((time.time() - _ai_cache['ts']) / 60)
        return jsonify({
            'ok': True,
            'insights': _ai_cache['text'],
            'model': _ai_cache['model'],
            'projection_table': _ai_cache['table'],
            'optimized_table': _ai_cache.get('optimized'),
            'cached': True,
            'stale': True,
            'stale_age_min': age_min,
            'provider': _ai_cache.get('provider', 'gemini'),
            'stale_reason': f'All providers unavailable. Showing cached response from {age_min} min ago. Last error: {last_err}',
        })

    # Phase 4: Hard error
    return jsonify({'ok': False, 'error': f'AI providers unavailable. {last_err}'}), 502


@app.route('/api/rules/ai-insights/debug')
def api_rules_ai_insights_debug():
    """Debug endpoint — returns full prompt, context, raw response, and token usage.
    Uses the same Gemini-then-Azure fallback logic as the main endpoint."""
    gemini_key = get_setting('gemini_api_key', '')
    gemini_model = get_setting('gemini_model', 'gemini-2.0-flash')

    context, _, _ = _build_ai_context()
    system_prompt = _gemini_system_prompt(_load_tou_periods())
    user_msg = f'Here is the current home energy data:\n\n{context}'

    result = {
        'system_prompt_chars': len(system_prompt),
        'context_chars': len(context),
        'system_prompt': system_prompt,
    }

    # Try Gemini first (single attempt — debug doesn't retry)
    if gemini_key:
        try:
            url = f'https://generativelanguage.googleapis.com/v1beta/models/{gemini_model}:generateContent?key={gemini_key}'
            payload = {
                'system_instruction': {'parts': [{'text': system_prompt}]},
                'contents': [{'parts': [{'text': user_msg}]}],
                'generationConfig': {
                    'temperature': 0.2,
                    'maxOutputTokens': 65536,
                    'thinkingConfig': {'thinkingBudget': 0},
                },
            }
            resp = _requests.post(url, json=payload, timeout=300)
            raw = resp.json()

            if resp.status_code == 200:
                text = ''
                candidates = raw.get('candidates', [])
                finish_reason = None
                if candidates:
                    parts = candidates[0].get('content', {}).get('parts', [])
                    text = '\n'.join(p.get('text', '') for p in parts)
                    finish_reason = candidates[0].get('finishReason')
                usage = raw.get('usageMetadata', {})
                result.update({
                    'ok': True,
                    'provider': 'gemini',
                    'model': gemini_model,
                    'finish_reason': finish_reason,
                    'usage': {
                        'prompt_tokens': usage.get('promptTokenCount'),
                        'output_tokens': usage.get('candidatesTokenCount'),
                        'thinking_tokens': usage.get('thoughtsTokenCount'),
                        'total_tokens': usage.get('totalTokenCount'),
                    },
                    'response_chars': len(text),
                    'response_text': text,
                })
                return jsonify(result)
            else:
                result['gemini_error'] = f'{resp.status_code}: {raw.get("error", {}).get("message", resp.text[:200])}'
        except Exception as exc:
            result['gemini_error'] = str(exc)

    # Fallback to Azure
    if _azure_configured():
        endpoint = get_setting('azure_openai_endpoint', '')
        deployment = get_setting('azure_openai_deployment', '')
        azure_key = get_setting('azure_openai_api_key', '')
        api_version = get_setting('azure_openai_api_version', '2024-10-21')
        try:
            text = _call_azure_openai(system_prompt, user_msg, endpoint, deployment, azure_key, api_version)
            result.update({
                'ok': True,
                'provider': 'azure_openai',
                'model': f'azure:{deployment}',
                'response_chars': len(text),
                'response_text': text,
            })
            return jsonify(result)
        except Exception as exc:
            result['azure_error'] = str(exc)

    result['ok'] = False
    result['error'] = 'All providers failed. ' + \
        f'Gemini: {result.get("gemini_error", "not configured")}. ' + \
        f'Azure: {result.get("azure_error", "not configured")}.'
    return jsonify(result), 502


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


def _arg_int(name, default):
    """Parse an int query-string param. Returns (value, error_response)."""
    raw = request.args.get(name)
    if raw is None or raw == '':
        return default, None
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, (jsonify({'error': f'{name} must be an integer'}), 400)


@app.route('/api/costs/daily')
def api_costs_daily():
    # Support start/end date filters (default: current year) + pagination
    today = date.today()
    start = request.args.get('start', f'{today.year}-01-01')
    end   = request.args.get('end', today.isoformat())
    limit, err  = _arg_int('limit', 0)   # 0 = no limit
    if err: return err
    offset, err = _arg_int('offset', 0)
    if err: return err
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
    from_str = request.args.get('from')
    from_date = None
    if from_str:
        try:
            from_date = date.fromisoformat(from_str)
        except ValueError:
            return jsonify({'error': 'invalid from date, use YYYY-MM-DD'}), 400
    started = _spawn_rebuild_daily_costs(from_date=from_date)
    return jsonify({'ok': True, 'started': started,
                    'note': None if started else 'rebuild already in progress'})


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
    with _abode_status_lock:
        info = dict(_abode_status)
    info['connected'] = _abode_instance is not None
    return jsonify(info)


@app.route('/api/debug/abode/backfill', methods=['POST'])
def api_debug_abode_backfill():
    """Manually trigger Abode backfill and return result with diagnostics."""
    if _abode_instance is None:
        return jsonify({'error': 'Abode not connected'}), 503
    days, err = _arg_int('days', 30)
    if err: return err

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

    redirect_uri = request.url_root.rstrip('/') + '/nest/callback'

    try:
        tokens = _nest_oauth_exchange({
            'code':         code,
            'grant_type':   'authorization_code',
            'redirect_uri': redirect_uri,
        })
    except Exception as exc:
        return f'<h2>Token exchange failed</h2><pre>{exc}</pre>', 500

    _nest_save_tokens(tokens, save_refresh=True)

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


@app.route('/api/debug/nest/devices')
def api_debug_nest_devices():
    """Dump full device list with traits. Shows what events each device supports."""
    token = _nest_ensure_token()
    if not token:
        return jsonify({'error': 'no valid token'}), 401
    # Force refresh
    _nest_refresh_devices(token)
    summary = []
    for d in _nest_devices_raw:
        traits = d.get('traits', {})
        summary.append({
            'type': d.get('type', ''),
            'name': d.get('name', ''),
            'customName': traits.get('sdm.devices.traits.Info', {}).get('customName', ''),
            'parentRelations': d.get('parentRelations', []),
            'traits': list(traits.keys()),
            'has_clip_preview': 'sdm.devices.traits.CameraClipPreview' in traits,
            'has_event_image': 'sdm.devices.traits.CameraEventImage' in traits,
            'has_motion': 'sdm.devices.traits.CameraMotion' in traits,
            'has_person': 'sdm.devices.traits.CameraPerson' in traits,
        })
    return jsonify({
        'devices': summary,
        'event_counters': _nest_event_counters,
        'poll_stats': _nest_poll_stats,
    })


@app.route('/api/debug/nest/peek')
def api_debug_nest_peek():
    """Pull messages from Pub/Sub WITHOUT acknowledging (so they redeliver).
    Useful for seeing what Google is actually publishing."""
    import base64 as _b64
    subscription = get_setting('nest_pubsub_subscription', '')
    if not subscription:
        return jsonify({'error': 'no subscription configured'}), 400
    token = _nest_ensure_token()
    if not token:
        return jsonify({'error': 'no valid token'}), 401
    try:
        resp = _requests.post(
            f'https://pubsub.googleapis.com/v1/{subscription}:pull',
            headers={'Authorization': f'Bearer {token}'},
            json={'maxMessages': 20, 'returnImmediately': True},
            timeout=30,
        )
        resp.raise_for_status()
        messages = resp.json().get('receivedMessages', [])
        decoded = []
        for m in messages:
            try:
                raw = _b64.b64decode(m['message']['data']).decode('utf-8')
                decoded.append(json.loads(raw))
            except Exception as e:
                decoded.append({'decode_error': str(e)})
        return jsonify({'count': len(messages), 'messages': decoded})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ── Switches drawer endpoints ────────────────────────────────────────────────
@app.route('/api/switches')
def api_switches():
    """Merged list of all switches across providers, with metadata + live state."""
    return jsonify(_get_all_switches())


@app.route('/api/switches/toggle', methods=['POST'])
def api_switches_toggle():
    data = request.get_json(silent=True) or {}
    rid  = data.get('id')
    if rid is None:
        return jsonify({'error': 'id required'}), 400
    res = switch_toggle(int(rid))
    if 'error' in res:
        return jsonify({'error': res['error']}), res.get('code', 500)
    return jsonify(res)


@app.route('/api/switches/set', methods=['POST'])
def api_switches_set():
    data = request.get_json(silent=True) or {}
    rid  = data.get('id')
    on   = data.get('on')
    if rid is None or not isinstance(on, bool):
        return jsonify({'error': 'id and on (bool) required'}), 400
    res = switch_set_state(int(rid), on)
    if 'error' in res:
        return jsonify({'error': res['error']}), res.get('code', 500)
    return jsonify(res)


@app.route('/api/switches/thermostat', methods=['POST'])
def api_switches_thermostat():
    """Update thermostat mode and/or setpoint(s). Payload:
       { id, mode?, setpoint_f?, setpoint_heat_f?, setpoint_cool_f? }"""
    data = request.get_json(silent=True) or {}
    rid  = data.get('id')
    if rid is None:
        return jsonify({'error': 'id required'}), 400
    fields = {}
    if 'mode' in data and data['mode'] is not None:
        fields['mode'] = str(data['mode']).upper()
    for k in ('setpoint_f', 'setpoint_heat_f', 'setpoint_cool_f'):
        if k in data and data[k] is not None:
            try:
                fields[k] = float(data[k])
            except (TypeError, ValueError):
                return jsonify({'error': f'{k} must be numeric'}), 400
    if not fields:
        return jsonify({'error': 'no fields to set'}), 400
    res = switch_set_thermostat(int(rid), **fields)
    if 'error' in res:
        return jsonify({'error': res['error']}), res.get('code', 500)
    return jsonify(res)


@app.route('/api/debug/nest/thermostats')
def api_debug_nest_thermostats():
    """Dump thermostat cache + raw SDM traits."""
    if request.args.get('refresh') == '1':
        token = _nest_ensure_token()
        if token:
            _nest_refresh_devices(token)
    return jsonify({
        'enabled':     get_setting_bool('nest_enabled', False),
        'count':       len(_nest_thermostats),
        'thermostats': _nest_thermostats,
    })


@app.route('/api/switches/brightness', methods=['POST'])
def api_switches_brightness():
    data = request.get_json(silent=True) or {}
    rid = data.get('id')
    b   = data.get('brightness')
    if rid is None or b is None:
        return jsonify({'error': 'id and brightness required'}), 400
    try:
        b = int(b)
    except (TypeError, ValueError):
        return jsonify({'error': 'brightness must be int'}), 400
    res = switch_set_brightness(int(rid), b)
    if 'error' in res:
        return jsonify({'error': res['error']}), res.get('code', 500)
    return jsonify(res)


@app.route('/api/switches/<int:rid>', methods=['PUT'])
def api_switches_update(rid):
    data = request.get_json(silent=True) or {}
    res  = switch_update_meta(rid, data)
    if 'error' in res:
        return jsonify({'error': res['error']}), res.get('code', 500)
    return jsonify(res)


@app.route('/api/switches/alarm/arm-home', methods=['POST'])
def api_switches_alarm_arm_home():
    """Arm Abode to Home mode. Body: {id} — id is the alarm row in switches_meta
    (used to validate the row exists + fetch friendly name for the event log)."""
    data = request.get_json(silent=True) or {}
    rid  = data.get('id')
    if rid is None:
        return jsonify({'error': 'id required'}), 400
    row = _switches_lookup(int(rid))
    if row is None:
        return jsonify({'error': 'not found'}), 404
    _, provider, ext_id, kind, name = row
    if provider != 'abode' or kind != 'alarm':
        return jsonify({'error': 'not an abode alarm row'}), 400
    try:
        new_mode = abode_arm_home()
        _switches_log_event('abode', 'alarm_armed', name,
                            detail=f'mode={new_mode}')
        return jsonify({'ok': True, 'mode': new_mode})
    except Exception as exc:
        _switches_log_event('abode', 'error', f'{name}: arm failed',
                            str(exc), 'failed')
        return jsonify({'error': str(exc)}), 500


@app.route('/api/switches/rediscover', methods=['POST'])
def api_switches_rediscover():
    counts = _switches_rediscover_all()
    return jsonify({'ok': True, 'counts': counts})


@app.route('/api/debug/tuya')
def api_debug_tuya():
    """Dump Tuya cache + indicate if devices.json was found.
    Pass ?probe=1 to also call status() on each device and return raw DPs —
    lets us see multi-outlet strips (DP 1/2/3/... each = one outlet)."""
    have_file = os.path.exists(_TUYA_DEVICEFILE)
    if request.args.get('refresh') == '1':
        count = _tuya_refresh_devices()
    else:
        count = len(_tuya_devices)
    probe = request.args.get('probe') == '1'
    out = {}
    for k, v in _tuya_devices.items():
        entry = {**v, 'local_key': '***'}
        if probe and v.get('ip'):
            try:
                dev = _tuya_make_outlet(k, v)
                status = dev.status()
                entry['probe'] = status.get('dps') if isinstance(status, dict) else status
            except Exception as exc:
                entry['probe_error'] = str(exc)
        out[k] = entry
    return jsonify({
        'enabled':         get_setting_bool('tuya_enabled', False),
        'has_devicefile':  have_file,
        'devicefile_path': _TUYA_DEVICEFILE,
        'count':           count,
        'age_s':           int(time.time() - _tuya_ts) if _tuya_ts else None,
        'devices':         out,
    })


@app.route('/api/debug/kasa')
def api_debug_kasa():
    """Dump current Kasa cache + optionally trigger a fresh discovery."""
    if request.args.get('refresh') == '1':
        n = _kasa_refresh_devices()
    else:
        n = len(_kasa_devices)
    return jsonify({
        'enabled': get_setting_bool('kasa_enabled', False),
        'count':   n,
        'age_s':   int(time.time() - _kasa_ts) if _kasa_ts else None,
        'devices': _kasa_devices,
    })


# ── Network device debug endpoints (Phase A: read-only, no DB) ────────────────
import network_devices as _netdev  # noqa: E402


def _network_router_cfg() -> dict[str, str]:
    return {
        'url':            get_setting('network_router_url', ''),
        'user':           get_setting('network_router_user', ''),
        'pass':           get_setting('network_router_pass', ''),
        'snmp_host':      get_setting('network_router_snmp_host', ''),
        'snmp_community': get_setting('network_router_snmp_community', 'public'),
        'snmp_port':      get_setting('network_router_snmp_port', '161'),
    }


def _network_ap_cfgs() -> list[dict[str, str]]:
    return _netdev.load_ap_configs(get_setting('network_aps', '[]'))


@app.route('/api/debug/network/lrt224')
def api_debug_network_lrt224():
    cfg = _network_router_cfg()
    if not cfg['url']:
        return jsonify({'error': 'network_router_url not set'}), 400
    include_raw = request.args.get('raw') == '1'
    res = _netdev.fetch_lrt224(cfg['url'], cfg['user'], cfg['pass'])
    if not include_raw:
        res = {**res, 'raw': {k: f'<{len(v)} chars; pass ?raw=1 to view>'
                              for k, v in res.get('raw', {}).items()}}
    return jsonify(res)


@app.route('/api/debug/network/config')
def api_debug_network_config():
    """Show parsed config so we can spot password-mangling without exposing
    secrets. Reveals length + character classes + the raw JSON setting so
    we can see if special chars survived round-trip."""
    raw = get_setting('network_aps', '[]')
    aps = _netdev.load_ap_configs(raw)

    def fingerprint(s: str) -> dict:
        if s is None:
            return {'len': 0, 'classes': []}
        classes = set()
        for ch in s:
            if ch.isalpha(): classes.add('letter')
            elif ch.isdigit(): classes.add('digit')
            elif ch == ' ': classes.add('space')
            elif ord(ch) < 32: classes.add(f'ctrl-0x{ord(ch):02x}')
            else: classes.add(f'special-{ch!r}')
        return {'len': len(s), 'classes': sorted(classes)}

    return jsonify({
        'aps_raw_json': raw,
        'aps_parsed_count': len(aps),
        'aps': [
            {
                'name': a.get('name'),
                'url': a.get('url'),
                'user': a.get('user'),
                'pass_fingerprint': fingerprint(a.get('pass', '')),
            }
            for a in aps
        ],
        'router': {
            'url': get_setting('network_router_url', ''),
            'user': get_setting('network_router_user', ''),
            'pass_fingerprint': fingerprint(get_setting('network_router_pass', '')),
        },
    })


@app.route('/api/debug/network/local')
def api_debug_network_local():
    """Ping-sweep the LAN subnet from the dashboard host and dump the
    resulting ARP cache. This is the master device list since the LRT224
    won't share its ARP table."""
    subnet = request.args.get('subnet') or get_setting('network_local_subnet',
                                                       '10.0.0.0/24')
    return jsonify(_netdev.fetch_local_arp(subnet))


@app.route('/api/debug/network/lrt224/snmp')
def api_debug_network_lrt224_snmp():
    cfg = _network_router_cfg()
    host = cfg.get('snmp_host') or (cfg.get('url') or '').replace('http://', '') \
        .replace('https://', '').rstrip('/').split('/')[0].split(':')[0]
    if not host:
        return jsonify({'error': 'set network_router_snmp_host (or network_router_url)'}), 400
    return jsonify(_netdev.fetch_lrt224_snmp(
        host,
        community=cfg.get('snmp_community', 'public'),
        port=int(cfg.get('snmp_port', 161) or 161),
    ))


@app.route('/api/debug/network/lrt224/login_probe')
def api_debug_network_lrt224_probe():
    """Inspect the LRT224 login form so we can write the right login flow."""
    cfg = _network_router_cfg()
    if not cfg['url']:
        return jsonify({'error': 'network_router_url not set'}), 400
    return jsonify(_netdev.lrt224_probe_login(cfg['url']))


@app.route('/api/debug/network/ap/<name>/probe')
def api_debug_network_ap_probe(name):
    """Try every common DD-WRT auth combo to find one that returns 200."""
    aps = _network_ap_cfgs()
    match = next((a for a in aps if a.get('name') == name), None)
    if not match:
        return jsonify({'error': f'AP {name!r} not in network_aps',
                        'available': [a.get('name') for a in aps]}), 404
    return jsonify(_netdev.ddwrt_probe(match['url'], match.get('user', ''),
                                       match.get('pass', '')))


@app.route('/api/debug/network/ap/<name>')
def api_debug_network_ap(name):
    aps = _network_ap_cfgs()
    match = next((a for a in aps if a.get('name') == name), None)
    if not match:
        return jsonify({'error': f'AP {name!r} not in network_aps',
                        'available': [a.get('name') for a in aps]}), 404
    include_raw = request.args.get('raw') == '1'
    res = _netdev.fetch_ddwrt_ap(match['url'], match.get('user', ''),
                                 match.get('pass', ''), match.get('name', ''))
    if not include_raw:
        res = {**res, 'raw': {k: f'<{len(v)} chars; pass ?raw=1 to view>'
                              for k, v in res.get('raw', {}).items()}}
    return jsonify(res)


@app.route('/api/debug/network/all')
def api_debug_network_all():
    res = _netdev.fetch_all(_network_router_cfg(), _network_ap_cfgs(),
                            local_subnet=get_setting('network_local_subnet',
                                                     '10.0.0.0/24'))
    # Strip raw from per-source results unless ?raw=1.
    if request.args.get('raw') != '1':
        if res.get('router'):
            res['router'] = {**res['router'],
                             'raw': {k: f'<{len(v)} chars>'
                                     for k, v in res['router'].get('raw', {}).items()}}
        for ap in res.get('aps', []):
            ap['raw'] = {k: f'<{len(v)} chars>' for k, v in ap.get('raw', {}).items()}
    return jsonify(res)


# ── Network device persistence + polling (Phase B) ────────────────────────────
NETWORK_STATE_PATH = os.path.join(BASE_DIR, 'network_devices.json')

# In-memory cache mirrored to disk. Reads from /api/network/devices serve
# from this; the polling thread updates it then flushes to JSON.
_network_state: dict[str, dict] = _netdev.load_state(NETWORK_STATE_PATH)
_network_state_lock = threading.Lock()
_network_last_poll_ts: float = 0
_network_last_poll_result: dict = {}
# Per-AP quarantine: name → unix-ts when we may retry.
_network_ap_quarantine: dict[str, float] = {}
_NETWORK_QUARANTINE_SECS = 300  # 5 min after consecutive failures


def _network_poll_once() -> dict:
    """Run one network scan, merge into state, write to JSON.
    Honors per-AP quarantine to avoid wedging DD-WRT httpd."""
    global _network_state, _network_last_poll_ts, _network_last_poll_result

    # Filter out quarantined APs.
    now = time.time()
    all_aps = _network_ap_cfgs()
    live_aps = [a for a in all_aps
                if _network_ap_quarantine.get(a.get('name', ''), 0) <= now]

    res = _netdev.fetch_all(
        _network_router_cfg(), live_aps,
        local_subnet=get_setting('network_local_subnet', '10.0.0.0/24'),
    )

    # Update quarantine based on per-AP error count.
    for ap_res in res.get('aps', []):
        name = ap_res.get('ap', '')
        if not name:
            continue
        if ap_res.get('errors'):
            # Any error → quarantine. Severity-blind on purpose: even a
            # single ConnectionReset is a sign we're hitting the lockout.
            _network_ap_quarantine[name] = now + _NETWORK_QUARANTINE_SECS
        else:
            _network_ap_quarantine.pop(name, None)

    with _network_state_lock:
        _netdev.merge_into_state(_network_state, res.get('merged', []),
                                 now_ts=int(now))
        try:
            _netdev.save_state(NETWORK_STATE_PATH, _network_state)
        except Exception as exc:
            print(f'network state save error: {exc}')

    _network_last_poll_ts = now
    _network_last_poll_result = {
        'devices_seen': len(res.get('merged', [])),
        'aps_polled': len(res.get('aps', [])),
        'aps_skipped_quarantined': len(all_aps) - len(live_aps),
        'elapsed_ms': res.get('elapsed_ms', 0),
        'errors': sum(len(a.get('errors', [])) for a in res.get('aps', [])),
    }
    return _network_last_poll_result


def _network_poll_loop():
    """Daemon thread: poll on `network_poll_interval`, gated by `network_enabled`."""
    while True:
        try:
            interval = max(get_setting_int('network_poll_interval', 60), 30)
            if get_setting_bool('network_enabled', False):
                try:
                    _network_poll_once()
                except Exception as exc:
                    print(f'network poll error: {exc}')
        except Exception as exc:
            print(f'network loop error: {exc}')
            interval = 60
        time.sleep(interval)


def _network_state_to_list(state: dict) -> list[dict]:
    """Project the state dict to a sorted list, with `online` derived from
    last_seen vs now. Filtered to the configured LAN subnet — devices whose
    last_ip falls outside the subnet (e.g. WAN-side stragglers, prior
    subnet relics) are excluded. Devices with no IP at all are kept since
    we may know them only via wireless association."""
    import ipaddress
    now = time.time()
    online_window = max(get_setting_int('network_poll_interval', 60) * 2, 120)
    try:
        net = ipaddress.ip_network(get_setting('network_local_subnet',
                                               '10.0.0.0/24'),
                                   strict=False)
    except ValueError:
        net = None
    out = []
    for mac, d in state.items():
        ip = d.get('last_ip')
        if net is not None and ip:
            try:
                if ipaddress.ip_address(ip) not in net:
                    continue
            except ValueError:
                continue
        last_seen = d.get('last_seen') or 0
        out.append({
            'mac': mac,
            'friendly_name': d.get('friendly_name') or '',
            'notes': d.get('notes') or '',
            'hidden': bool(d.get('hidden')),
            'vendor': d.get('vendor'),
            'last_ip': ip,
            'last_hostname': d.get('last_hostname'),
            'nbns_name': d.get('nbns_name'),
            'last_ap': d.get('last_ap'),
            'last_signal': d.get('last_signal'),
            'last_iface': d.get('last_iface'),
            'first_seen': d.get('first_seen'),
            'last_seen': last_seen,
            'online': bool(last_seen and (now - last_seen) <= online_window),
            'seen_on': d.get('seen_on') or [],
        })
    # Numeric IP sort (so 10.0.0.5 comes before 10.0.0.50). Devices without
    # an IP go to the end.
    def _ip_key(d):
        ip = d.get('last_ip') or ''
        try:
            return tuple(int(p) for p in ip.split('.'))
        except (ValueError, AttributeError):
            return (999, 0, 0, 0)
    out.sort(key=_ip_key)
    return out


@app.route('/api/network/devices')
def api_network_devices():
    online_only = request.args.get('online') == '1'
    ap_filter = request.args.get('ap')
    unnamed_only = request.args.get('unnamed') == '1'
    with _network_state_lock:
        items = _network_state_to_list(_network_state)
    if online_only:
        items = [d for d in items if d['online']]
    if ap_filter:
        items = [d for d in items if d['last_ap'] == ap_filter]
    if unnamed_only:
        items = [d for d in items if not d['friendly_name']]
    aps = sorted({d['last_ap'] for d in items if d['last_ap']})
    return jsonify({
        'devices': items,
        'total': len(items),
        'aps': aps,
        'last_poll_ts': _network_last_poll_ts,
        'last_poll': _network_last_poll_result,
        'enabled': get_setting_bool('network_enabled', False),
        'quarantined_aps': [
            {'name': n, 'until': t}
            for n, t in _network_ap_quarantine.items() if t > time.time()
        ],
    })


@app.route('/api/network/devices/<mac>', methods=['PUT'])
def api_network_device_update(mac):
    mac = mac.lower()
    body = request.get_json() or {}
    with _network_state_lock:
        cur = _network_state.get(mac)
        if cur is None:
            # Allow creating a stub for a MAC we haven't observed yet
            # (e.g. user wants to label a device before it appears).
            cur = {'first_seen': int(time.time())}
            _network_state[mac] = cur
        for field in ('friendly_name', 'notes'):
            if field in body:
                cur[field] = str(body[field])[:500]
        if 'hidden' in body:
            cur['hidden'] = bool(body['hidden'])
        try:
            _netdev.save_state(NETWORK_STATE_PATH, _network_state)
        except Exception as exc:
            return jsonify({'error': f'save failed: {exc}'}), 500
    return jsonify({'ok': True, 'mac': mac, 'device': cur})


_NETWORK_REMOVE_MIN_OFFLINE_DAYS = 90


@app.route('/api/network/devices/<mac>', methods=['DELETE'])
def api_network_device_remove(mac):
    """Permanently remove a device entry from network_devices.json.

    Server-enforced guard: the device must have been offline for at least
    90 days (last_seen older than 90*86400 seconds ago). This stops a
    misclicked or buggy client from blowing away a recently-active device.
    Does NOT touch DD-WRT MAC filter lists — bans persist; clean those up
    via the separate filter-audit workflow.
    """
    mac = mac.lower()
    with _network_state_lock:
        cur = _network_state.get(mac)
        if cur is None:
            return jsonify({'error': 'unknown mac'}), 404
        last_seen = cur.get('last_seen') or 0
        age_days = (time.time() - last_seen) / 86400 if last_seen else None
        if last_seen and age_days < _NETWORK_REMOVE_MIN_OFFLINE_DAYS:
            return jsonify({
                'error': 'device too recent to remove',
                'last_seen': last_seen,
                'offline_days': age_days,
                'min_offline_days': _NETWORK_REMOVE_MIN_OFFLINE_DAYS,
            }), 400
        _network_state.pop(mac, None)
        try:
            _netdev.save_state(NETWORK_STATE_PATH, _network_state)
        except Exception as exc:
            return jsonify({'error': f'save failed: {exc}'}), 500
    return jsonify({'ok': True, 'mac': mac})


@app.route('/api/network/rediscover', methods=['POST'])
def api_network_rediscover():
    try:
        result = _network_poll_once()
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    return jsonify({'ok': True, 'result': result})


@app.route('/api/network/ap_filters')
def api_network_ap_filters():
    """Surface current per-AP per-radio MAC filter mode + list. Powers the
    Phase C pin-modal and filter-audit views."""
    out = []
    for ap in _network_ap_cfgs():
        if not ap.get('url'):
            continue
        res = _netdev.fetch_ddwrt_ap(ap['url'], ap.get('user', ''),
                                     ap.get('pass', ''),
                                     ap.get('name', ap['url']))
        out.append({
            'ap': ap.get('name'),
            'filters': res.get('filters', {}),  # {wl0: {mode,enabled,list}, wl1: {...}}
            'errors': res.get('errors', []),
        })
    return jsonify({'aps': out})


def _network_ap_by_name(name: str) -> dict[str, str] | None:
    return next((a for a in _network_ap_cfgs() if a.get('name') == name), None)


@app.route('/api/network/devices/<mac>/filters', methods=['PUT'])
def api_network_device_filters(mac):
    """Apply a per-AP per-radio ban map for one MAC. Body shape:
        {"wl0": {"Master AP": true, "Kid's Room AP": false, ...},
         "wl1": {...}}
    `true` means the MAC should be in that AP×radio's deny list (banned),
    `false` means it should be absent. Server diffs against current state
    and writes only the APs that need changes.
    """
    mac = mac.lower()
    body = request.get_json() or {}
    desired: dict[str, dict[str, bool]] = {
        'wl0': dict(body.get('wl0') or {}),
        'wl1': dict(body.get('wl1') or {}),
    }
    return _apply_filter_ban_map(mac, desired)


def _apply_filter_ban_map(mac: str, desired: dict) -> "tuple":
    aps = _network_ap_cfgs()
    results = []
    for ap in aps:
        ap_name = ap.get('name', '')
        ap_changed = False
        for radio in ('wl0', 'wl1'):
            if ap_name not in desired[radio]:
                continue
            want_banned = bool(desired[radio][ap_name])
            # Read current list.
            try:
                read = _netdev.fetch_ddwrt_ap(ap['url'], ap['user'],
                                              ap['pass'], ap_name)
                current = list(read.get('filters', {})
                                   .get(radio, {}).get('list', []))
            except Exception as e:
                results.append({'ap': ap_name, 'radio': radio,
                                'ok': False, 'error': f'read: {e}'})
                continue
            current_l = [m.lower() for m in current]
            if want_banned and mac not in current_l:
                new_list = current + [mac]
            elif not want_banned and mac in current_l:
                new_list = [m for m in current if m.lower() != mac]
            else:
                results.append({'ap': ap_name, 'radio': radio,
                                'ok': True, 'noop': True})
                continue

            wr = _netdev.ddwrt_set_mac_filter(ap['url'], ap['user'],
                                              ap['pass'], radio, new_list)
            ap_changed = ap_changed or wr.get('ok', False)
            results.append({
                'ap': ap_name, 'radio': radio,
                'ok': wr.get('ok', False),
                'before': len(wr.get('before', [])),
                'after': len(wr.get('after', [])),
                'errors': wr.get('errors', []),
            })
        if ap_changed:
            # Drop our cached read of this AP next poll so the Network page
            # re-renders from fresh data.
            _network_ap_quarantine.pop(ap_name, None)

    return jsonify({'mac': mac, 'results': results,
                    'all_ok': all(r.get('ok', True) for r in results)})


@app.route('/api/network/devices/<mac>/pin', methods=['POST'])
def api_network_device_pin(mac):
    """Pin a wireless device to one AP × band by banning the MAC on every
    other AP × band combination. Body: {ap, radio} where radio is
    'wl0' (2.4), 'wl1' (5), or 'either' (allow on both bands of that AP)."""
    body = request.get_json() or {}
    target_ap = body.get('ap', '')
    target_radio = body.get('radio', 'either')
    if not target_ap:
        return jsonify({'error': 'ap is required'}), 400
    if target_radio not in ('wl0', 'wl1', 'either'):
        return jsonify({'error': 'radio must be wl0/wl1/either'}), 400

    aps = [a.get('name') for a in _network_ap_cfgs() if a.get('name')]
    if target_ap not in aps:
        return jsonify({'error': f'unknown ap {target_ap!r}',
                        'available': aps}), 400

    desired = {'wl0': {}, 'wl1': {}}
    for ap_name in aps:
        for radio in ('wl0', 'wl1'):
            if ap_name == target_ap and (target_radio == 'either'
                                         or target_radio == radio):
                desired[radio][ap_name] = False  # allow here
            else:
                desired[radio][ap_name] = True   # ban everywhere else

    return _apply_filter_ban_map(mac.lower(), desired)


@app.route('/api/network/devices/<mac>/unpin', methods=['POST'])
def api_network_device_unpin(mac):
    """Remove this MAC from every AP × radio filter list (allow everywhere)."""
    aps = [a.get('name') for a in _network_ap_cfgs() if a.get('name')]
    desired = {'wl0': {n: False for n in aps},
               'wl1': {n: False for n in aps}}
    return _apply_filter_ban_map(mac.lower(), desired)


# ── Event Log endpoint ────────────────────────────────────────────────────────
@app.route('/api/events')
def api_events():
    limit, err  = _arg_int('limit', 50)
    if err: return err
    offset, err = _arg_int('offset', 0)
    if err: return err
    limit  = min(limit, 500)
    offset = max(offset, 0)
    system = request.args.get('system', 'all')
    etype  = request.args.get('type')

    # Date range: accept start/end unix timestamps, fall back to days param
    start_ts, err = _arg_int('start', None)
    if err: return err
    end_ts, err = _arg_int('end', None)
    if err: return err
    if start_ts is None:
        days, err = _arg_int('days', 7)
        if err: return err
        start_ts = int(time.time()) - min(days, 365) * 86400

    query  = 'SELECT id,ts,system,event_type,title,detail,result,source,battery_pct FROM event_log WHERE ts >= ?'
    params: list = [start_ts]

    if end_ts:
        query += ' AND ts <= ?'
        params.append(end_ts)
    if system == 'errors':
        query += " AND (result = 'failed' OR event_type = 'error')"
    elif system != 'all':
        systems_list = [s.strip() for s in system.split(',') if s.strip()]
        if len(systems_list) == 1:
            query += ' AND system = ?'
            params.append(systems_list[0])
        elif systems_list:
            placeholders = ','.join('?' * len(systems_list))
            query += f' AND system IN ({placeholders})'
            params.extend(systems_list)
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
            'key': 'kasa',
            'label': 'Kasa Smart Plugs (LAN)',
            'type': 'continuous',
            'enabled_key': 'kasa_enabled',
            'intervals': [
                {'key': 'kasa_poll_interval',     'label': 'State poll', 'unit': 's'},
                {'key': 'kasa_state_poll_enabled', 'label': 'Poll device state',
                 'unit': 'select', 'options': ['0', '1']},
            ],
        },
        {
            'key': 'nest_thermostat',
            'label': 'Nest Thermostat (SDM)',
            'type': 'on-demand',
            'enabled_key': 'nest_thermostat_enabled',
            'intervals': [],
        },
        {
            'key': 'tuya',
            'label': 'Tuya / Smart Life (LAN)',
            'type': 'continuous',
            'enabled_key': 'tuya_enabled',
            'intervals': [
                {'key': 'tuya_poll_interval', 'label': 'State poll', 'unit': 's'},
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
            'label': 'Gemini AI (primary)',
            'type': 'configurable',
            'intervals': [
                {'key': 'gemini_api_key', 'label': 'API Key', 'unit': 'text'},
                {'key': 'gemini_model', 'label': 'Model', 'unit': 'select',
                 'options': ['gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-2.0-flash']},
            ],
        },
        {
            'key': 'azure_openai',
            'label': 'Azure OpenAI (fallback)',
            'type': 'configurable',
            'intervals': [
                {'key': 'azure_openai_endpoint', 'label': 'Endpoint', 'unit': 'url'},
                {'key': 'azure_openai_api_key', 'label': 'API Key', 'unit': 'text'},
                {'key': 'azure_openai_deployment', 'label': 'Deployment', 'unit': 'text'},
                {'key': 'azure_openai_api_version', 'label': 'API Version', 'unit': 'text'},
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
        # Long-form cards — kept at the bottom so the shorter ones tile cleanly up top.
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
            'key': 'network',
            'label': 'Network Devices (LRT224 + DD-WRT APs)',
            'type': 'continuous',
            'enabled_key': 'network_enabled',
            'intervals': [
                {'key': 'network_poll_interval', 'label': 'Poll interval', 'unit': 's'},
                {'key': 'network_router_url',    'label': 'Router URL',    'unit': 'url'},
                {'key': 'network_router_user',   'label': 'Router user',   'unit': 'text'},
                {'key': 'network_router_pass',   'label': 'Router pass',   'unit': 'text'},
                {'key': 'network_local_subnet',  'label': 'LAN subnet (ping-sweep)', 'unit': 'text'},
                {'key': 'network_aps',           'label': 'APs (JSON)',    'unit': 'text'},
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


def _load_pool_gpm_cache() -> None:
    global _pool_normal_gpm, _pool_cleaner_gpm, _pool_edge_gpm
    try:
        _pool_normal_gpm  = float(get_setting('pool_cached_normal_gpm',  0) or 0)
        _pool_cleaner_gpm = float(get_setting('pool_cached_cleaner_gpm', 0) or 0)
        _pool_edge_gpm    = float(get_setting('pool_cached_edge_gpm',    0) or 0)
    except Exception as exc:
        print(f'Pool GPM cache load error: {exc}')


def _start():
    os.chdir(BASE_DIR)
    init_db()
    _load_pool_gpm_cache()
    _backfill_rates_event_url()
    backfill_history()
    # Seed switches_meta with known pool circuits on startup so tiles appear
    # without requiring a manual rediscover. Kasa discovery is driven
    # by its enabled flag in the poller loop.
    if get_setting_bool('pool_enabled', True):
        try:
            _pool_discover_circuits()
        except Exception as exc:
            print(f'Pool circuit seed error: {exc}')
    if get_setting_bool('abode_enabled', True):
        try:
            _abode_seed_alarm_row()
        except Exception as exc:
            print(f'Abode alarm seed error: {exc}')
    # Start the persistent Kasa asyncio loop so Device connections survive.
    if get_setting_bool('kasa_enabled', False):
        try:
            _kasa_start_loop()
        except Exception as exc:
            print(f'Kasa loop start error: {exc}')
    threading.Thread(target=rebuild_daily_costs, daemon=True).start()
    threading.Thread(target=_recalc_pool_target, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=_network_poll_loop, daemon=True).start()
    start_abode_listener()
    print('Dashboard \u2192 http://localhost:5001')
    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)


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
