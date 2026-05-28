import os
import sqlite3
import threading
import time

import lib.state as state
from lib.events import _log_system_error
from lib.settings import get_setting_bool, get_setting_int


ABODE_EMAIL    = os.environ.get('ABODE_EMAIL', '')
ABODE_PASSWORD = os.environ.get('ABODE_PASSWORD', '')

_security: dict     = {}
_security_ts: float = 0.0

_abode_instance = None
_abode_status_lock = threading.Lock()
_abode_status = {
    'state': 'idle',
    'last_error': None,
    'last_error_time': None,
    'last_event_time': None,
    'events_received': 0,
    'reconnect_count': 0,
    'last_backfill_time': None,
    'last_backfill_inserted': None,
    'last_backfill_error': None,
}

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

ABODE_MODE_DISPLAY = {'standby': 'Disarmed', 'home': 'Armed Home', 'away': 'Armed Away'}

_MODE_DISPLAY = ABODE_MODE_DISPLAY


def _abode_event_val(event, key):
    if isinstance(event, dict):
        return event.get(key)
    return getattr(event, key, None)


def _abode_write_event(event):
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

        with sqlite3.connect(state.DB_PATH, timeout=10) as c:
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
            if oldest_ts is not None and oldest_ts < cutoff:
                break
            page += 1
        with sqlite3.connect(state.DB_PATH, timeout=30) as c:
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
            if not get_setting_bool('abode_enabled', True):
                if _abode_status['state'] != 'disabled':
                    if _abode_instance is not None:
                        try:
                            _abode_instance.events.stop()
                        except Exception:
                            pass
                        _abode_instance = None
                    with _abode_status_lock:
                        _abode_status['state'] = 'disabled'
                    print('Abode: disabled in settings')
                time.sleep(30)
                continue

            if _abode_instance is not None:
                last_bf = _abode_status.get('last_backfill_time') or 0
                if time.time() - last_bf >= 7200:
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
                retry_delay = 60

                import abodepy.helpers.timeline as tl
                abode.events.add_timeline_callback(tl.ALL, _abode_write_event)
                abode.events.start()
                print('Abode: listener started.')

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
                retry_delay = min(retry_delay * 2 if is_429 else retry_delay, 600)

    t = threading.Thread(target=_run, daemon=True, name='abode-listener')
    t.start()


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


def _abode_seed_alarm_row() -> int:
    with sqlite3.connect(state.DB_PATH) as c:
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
