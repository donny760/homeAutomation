import json
import os
import time

import lib.state as state
from lib.db import connect


_SETTINGS_DEFAULTS = {
    'powerwall_enabled':           '1',
    'powerwall_poll_interval':     '10',
    'powerwall_db_write_interval': '30',
    'pool_enabled':                '1',
    'pool_poll_interval':          '30',
    'rachio_enabled':              '1',
    'rachio_poll_interval':        '300',
    'rachio_event_poll_interval':  '1800',
    'rain_skip_enabled':           '0',
    'rain_lookback_days':          '5',
    'rain_mm_per_skip_day':        '1',
    'rain_skip_max_days':          '7',
    'rain_skip_check_interval':    '3600',
    'abode_enabled':               '1',
    'cost_rebuild_days':           '7',   # lookback window for the daily catch-up cost rebuild
    'holidays_poll_months':        '1',
    'rates_poll_months':           '1',
    'refresh_start_date':          '',
    'rates_page_url':              'https://www.sdge.com/total-electric-rates',
    'rate_schedule_name':          'EV-TOU',
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
    'tou_periods_last_verified':   '',
    # Internal state (not user config — deliberately absent from the Settings page)
    'cost_rebuild_pending_from':   '',   # daily_costs stale from this date forward
    'rates_last_success':          '',   # last successful rate refresh (ISO date)
    'holidays_last_success':       '',   # last successful holiday refresh (ISO date)
    'fe_poll_interval':            '10000',
    'fe_chart_interval':           '60000',
    'fe_weather_interval':         '600000',
    'fe_automations_interval':     '60000',
    'fe_pool_interval':            '60000',
    'fe_costs_interval':           '300000',
    'fe_rates_interval':           '600000',
    'fe_events_interval':          '60000',
    'fe_security_interval':        '60000',
    'fe_forecast_interval':        '3600000',
    'security_poll_interval':      '30',
    'gemini_api_key':              '',
    'gemini_model':                'gemini-2.0-flash',
    'azure_openai_endpoint':       '',
    'azure_openai_api_key':        '',
    'azure_openai_deployment':     '',
    'azure_openai_api_version':    '2024-10-21',
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
    'kasa_enabled':                '0',
    'kasa_poll_interval':          '10',
    'kasa_state_poll_enabled':     '1',
    'pool_control_enabled':        '0',
    'tuya_enabled':                '0',
    'tuya_poll_interval':          '15',
    'tuya_api_key':                '',
    'tuya_api_secret':             '',
    'tuya_region':                 'us',
    'tuya_cloud_poll_interval':    '300',
    'network_enabled':             '0',
    'network_poll_interval':       '60',
    'network_router_url':          '',
    'network_router_user':         '',
    'network_router_pass':         '',
    'network_router_snmp_host':      '',
    'network_router_snmp_community': 'public',
    'network_router_snmp_port':      '161',
    'network_local_subnet':        '10.0.0.0/24',
    'network_aps':                 '[]',
    # Gates /api/debug/* — off by default. These routes mutate state, run
    # expensive scans, and echo router credentials. Turn on only while debugging.
    'debug_enabled':               '0',
}


def _seed_settings(conn):
    for key, default in _SETTINGS_DEFAULTS.items():
        conn.execute(
            'INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)',
            (key, default)
        )
    conn.commit()


def load_settings() -> dict:
    """Fresh, uncached read of all settings (used to render the Settings page)."""
    with connect() as c:
        rows = c.execute('SELECT key, value FROM settings').fetchall()
    return {k: v for k, v in rows}


# In-process settings cache. The poller reads ~15 settings every 10s; without a
# cache that's ~90 open/query/close cycles per minute for config that rarely
# changes. A short TTL bounds staleness for the *other* process (rules.py);
# the writing process (server.py) invalidates explicitly on PUT /api/settings.
_SETTINGS_TTL = 30.0  # seconds
_settings_cache: dict = {}
_settings_cache_ts: float = 0.0


def _settings_snapshot() -> dict:
    global _settings_cache, _settings_cache_ts
    now = time.time()
    if now - _settings_cache_ts >= _SETTINGS_TTL:
        # Atomic rebind — concurrent readers see either the old or new dict, never partial.
        _settings_cache = load_settings()
        _settings_cache_ts = now
    return _settings_cache


def invalidate_settings_cache() -> None:
    global _settings_cache_ts
    _settings_cache_ts = 0.0


def set_setting(key: str, value) -> None:
    """Write one setting and drop the read cache, so the next get_setting() in
    this process sees it immediately (the TTL is 30s; the pending-rebuild marker
    is written and read back within seconds)."""
    with connect() as c:
        c.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                  (key, str(value)))
    invalidate_settings_cache()


def get_setting(key: str, default=None):
    val = _settings_snapshot().get(key)
    return val if val is not None else default


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
    return None
