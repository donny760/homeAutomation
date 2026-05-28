import json
import logging
import sqlite3
import time
from datetime import datetime, date, timedelta

import lib.state as state
from lib.fetch_rates import load_rates


def _db_version(conn) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def _add_col(conn, table, col, definition):
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
    except Exception:
        pass  # column already exists — idempotent


def _migrate_v1(conn):
    """Bootstrap migration: consolidates all prior scattered ALTER TABLE blocks."""
    conn.execute("DROP INDEX IF EXISTS idx_event_log_unique")
    for col in ("on_peak_kwh", "off_peak_kwh", "super_off_peak_kwh",
                "on_peak_cost", "off_peak_cost", "super_off_peak_cost"):
        _add_col(conn, "daily_costs", col, "REAL DEFAULT 0")
    _add_col(conn, "rate_history", "base_services_charge_per_day", "REAL DEFAULT 0")
    _add_col(conn, "rules", "sort_order", "INTEGER DEFAULT 0")
    conn.execute("UPDATE rules SET sort_order = id WHERE sort_order = 0")
    _add_col(conn, "rules", "notes", "TEXT")
    conn.execute("PRAGMA user_version = 1")


_MIGRATIONS = [(1, _migrate_v1)]


def _migrate(conn) -> None:
    current = _db_version(conn)
    for version, fn in _MIGRATIONS:
        if version <= current:
            continue
        logging.info("db: applying migration v%d (%s)", version, fn.__name__)
        with conn:
            fn(conn)
    logging.info("db: schema v%d", _db_version(conn))


def _seed_rate_history(conn):
    """If rate_history is empty, seed from rates.json so existing data isn't lost."""
    count = conn.execute('SELECT COUNT(*) FROM rate_history').fetchone()[0]
    if count > 0:
        return
    rates = load_rates()
    if not rates or 'summer_on_peak' not in rates:
        return
    eff_date = '2026-01-01'
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


def init_db() -> None:
    # Deferred imports to avoid circular deps at module load time
    from rules import seed_default_rules as _seed_rules
    from lib.settings import _seed_settings

    with sqlite3.connect(state.DB_PATH) as c:
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
        c.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
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
        _seed_rules(c)
        _seed_settings(c)
        _seed_rate_history(c)
        _migrate(c)
        # Force tou_periods to year-round weekday super off-peak (effective 2026-05-01)
        correct_tou = json.dumps({
            'weekday':         {'on_peak': [[16, 21]], 'super_off_peak': [[0, 6], [10, 14]]},
            'weekend_holiday': {'on_peak': [[16, 21]], 'super_off_peak': [[0, 14]]},
        })
        c.execute("INSERT INTO settings (key,value) VALUES ('tou_periods',?) ON CONFLICT(key) DO UPDATE SET value=?",
                  (correct_tou, correct_tou))


def write_reading(solar_w, home_w, battery_w, grid_w, battery_pct) -> None:
    with sqlite3.connect(state.DB_PATH) as c:
        c.execute(
            'INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?)',
            (int(time.time()), solar_w, home_w, battery_w, grid_w, battery_pct)
        )


def purge_old() -> None:
    """Disabled — keep all readings forever."""
    pass


def _fetch_rows(since_ts: int) -> list:
    with sqlite3.connect(state.DB_PATH) as c:
        return c.execute(
            'SELECT timestamp, solar_w, home_w, battery_w, grid_w '
            'FROM readings WHERE timestamp >= ? ORDER BY timestamp',
            (since_ts,)
        ).fetchall()


def _fetch_rows_range(start_ts: int, end_ts: int) -> list:
    with sqlite3.connect(state.DB_PATH) as c:
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
