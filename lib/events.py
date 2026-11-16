import logging
import sqlite3
import threading
import time

import lib.state as state
from lib.db import connect


_switches_lock = threading.Lock()
_log = logging.getLogger(__name__)

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
    except Exception as exc:
        # Swallowing this silently hides the error reporter's own failures —
        # 'database is locked' here means errors stop being recorded at all.
        _log.warning('event_log write failed (%s/%s): %s', system, title, exc)


def _log_success(system: str, event_type: str, title: str, detail: str = None) -> None:
    try:
        with connect() as c:
            c.execute(
                'INSERT INTO event_log '
                '(ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), system, event_type, title, detail, 'ok', 'live')
            )
    except Exception as exc:
        _log.warning('event_log write failed (%s/%s): %s', system, title, exc)


def format_duration(seconds: float) -> str:
    """Human-readable elapsed time for event_log titles.

    Floors every unit so nothing ever reads '0 min', and clamps negatives —
    a Windows clock sync can move time.time() backwards mid-outage."""
    s = int(seconds)
    if s < 0:
        s = 0
    if s < 60:
        return f'{s} sec'
    if s < 3600:
        return f'{s // 60} min'
    if s < 86400:
        hrs, mins = s // 3600, (s % 3600) // 60
        return f'{hrs} hr {mins} min' if mins else f'{hrs} hr'
    days, hrs = s // 86400, (s % 86400) // 3600
    unit = 'day' if days == 1 else 'days'
    return f'{days} {unit} {hrs} hr' if hrs else f'{days} {unit}'


class FetchIncident:
    """Failure backoff + one event_log row per outage for a polled HTTP fetch.

    Without this, a fetch that only advances its TTL on success retries on
    every caller (several frontend polls per minute) and logs every attempt."""

    def __init__(self, system: str, label: str, retry_secs: int = 120):
        self.system = system
        self.label = label
        self.retry_secs = retry_secs
        self._lock = threading.Lock()
        self._since = None      # first failure of the current incident
        self._last_fail = 0.0
        self._attempts = 0
        self._last_error = ''

    def should_skip(self) -> bool:
        with self._lock:
            return self._since is not None and time.time() - self._last_fail < self.retry_secs

    def failed(self, exc: Exception, detail: str = None) -> None:
        detail = detail or str(exc)
        now = time.time()
        with self._lock:
            first = self._since is None
            if first:
                self._since = now
            self._last_fail = now
            self._attempts += 1
            self._last_error = detail
        if first:
            _log_system_error(self.system, f'{self.label} fetch failed', detail)

    def ok(self) -> None:
        with self._lock:
            if self._since is None:
                return
            elapsed = time.time() - self._since
            attempts, last_error = self._attempts, self._last_error
            self._since, self._last_fail, self._attempts, self._last_error = None, 0.0, 0, ''
        event_type = self.label.lower().replace(' ', '_') + '_recovered'
        _log_success(self.system, event_type, f'{self.label} fetch recovered',
                     f'after {format_duration(elapsed)}, {attempts} failed attempts; '
                     f'last error: {last_error}')


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
