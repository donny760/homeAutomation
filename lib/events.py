import sqlite3
import threading
import time

import lib.state as state
from lib.db import connect


_switches_lock = threading.Lock()

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


def _log_system_error(system: str, title: str, detail: str = None) -> None:
    try:
        with connect() as c:
            c.execute(
                'INSERT INTO event_log '
                '(ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), system, 'error', title, detail, 'failed', 'live')
            )
    except Exception:
        pass


def _log_success(system: str, event_type: str, title: str, detail: str = None) -> None:
    try:
        with connect() as c:
            c.execute(
                'INSERT INTO event_log '
                '(ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), system, event_type, title, detail, 'ok', 'live')
            )
    except Exception:
        pass


def _switches_log_event(provider: str, event_type: str, title: str, detail: str = None,
                        result: str = 'ok') -> None:
    provider_label = (provider or '').strip()
    prefixed_detail = detail
    if provider_label:
        tag = f'[{provider_label}]'
        prefixed_detail = tag if not detail else f'{tag} {detail}'
    suffix = _HOME_CONTROL_TITLE_SUFFIX.get(event_type)
    composed_title = f'{title} {suffix}' if suffix else title
    try:
        with connect() as c:
            c.execute(
                'INSERT INTO event_log (ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), 'home_control', event_type, composed_title,
                 prefixed_detail, result, 'ui')
            )
    except Exception as exc:
        print(f'Switch event log error: {exc}')


def _device_mark_failure(*, key: str, name: str, reason: str,
                         failures: dict, devices: dict, quarantine: dict,
                         log_fn, offline_field: str, offline_value) -> None:
    """Shared offline/quarantine tracking for Kasa & Tuya plugs."""
    count = failures.get(key, 0) + 1
    failures[key] = count
    info = devices.get(key) or {}
    if not info.get('was_offline'):
        log_fn(name, 'offline', reason[:200])
        info['was_offline'] = True
    info[offline_field] = offline_value
    if count >= 3 and key not in quarantine:
        quarantine[key] = time.time() + 300
        log_fn(name, 'quarantined',
               f'{count} consecutive failures; backing off 5 min')
