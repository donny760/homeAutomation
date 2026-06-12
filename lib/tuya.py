import json
import os
import sqlite3
import time

import lib.state as state
from lib.events import _log_system_error, _device_mark_failure, _switches_lock
from lib.db import connect
from lib.settings import get_setting


_TUYA_DEVICEFILE = os.path.join(state.BASE_DIR, 'devices.json')

_tuya_devices: dict     = {}
_tuya_ts: float         = 0.0
_tuya_connections: dict = {}
_tuya_failures: dict    = {}
_tuya_quarantine: dict  = {}


def _tuya_load_devicefile() -> list:
    if not os.path.exists(_TUYA_DEVICEFILE):
        return []
    try:
        with open(_TUYA_DEVICEFILE, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and 'devices' in data:
            return data['devices']
        return []
    except Exception as exc:
        print(f'Tuya devicefile load error: {exc}')
        return []


def _tuya_make_outlet(dev_id: str, info: dict):
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
    dev = _tuya_connections.get(dev_id)
    if dev is not None:
        return dev
    dev = _tuya_make_outlet(dev_id, info)
    _tuya_connections[dev_id] = dev
    return dev


def _tuya_close_connection(dev_id: str) -> None:
    dev = _tuya_connections.pop(dev_id, None)
    if dev is None:
        return
    try:
        if hasattr(dev, 'close'):
            dev.close()
    except Exception:
        pass


def _tuya_close_all() -> None:
    for dev_id in list(_tuya_connections.keys()):
        _tuya_close_connection(dev_id)


def _log_tuya_reachability(name: str, event: str, detail: str = None) -> None:
    try:
        with connect() as c:
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
    if ':' in ext_id:
        a, b = ext_id.split(':', 1)
        return a, b
    return ext_id, '1'


def _tuya_probe_dps(dev_id: str, info: dict) -> dict:
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
    global _tuya_devices, _tuya_ts
    _tuya_close_all()
    _tuya_failures.clear()
    _tuya_quarantine.clear()
    file_devs = _tuya_load_devicefile()
    if not file_devs:
        print('Tuya: no devices.json found. Run `python -m tinytuya wizard`.')
        _log_system_error('tuya', 'devices.json missing',
                          f'Expected at {_TUYA_DEVICEFILE}. Run tinytuya wizard.')
        return 0

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
            'dp_state':  {},
            'switch_dps': ['1'],
        }

    for dev_id, info in new.items():
        if info.get('category') == 'wsdcg':
            info['switch_dps'] = []  # sensor — no controllable DPs
            continue
        if not info.get('ip'):
            continue
        dps = _tuya_probe_dps(dev_id, info)
        info['dp_state'] = dps
        switch_dps = []
        for dp_idx, val in dps.items():
            if isinstance(val, bool):
                switch_dps.append(dp_idx)
        if switch_dps:
            info['switch_dps'] = sorted(
                switch_dps, key=lambda s: int(s) if s.isdigit() else 999
            )

    with _switches_lock:
        _tuya_devices = new
        _tuya_ts = time.time()

    total_tiles = 0
    with connect() as c:
        for dev_id, info in new.items():
            if info.get('category') == 'wsdcg':
                ext_id = f'{dev_id}:sensor'
                if not c.execute(
                    'SELECT id FROM switches_meta WHERE provider=? AND external_id=?',
                    ('tuya', ext_id)
                ).fetchone():
                    c.execute(
                        'INSERT INTO switches_meta (provider, external_id, kind, name) '
                        'VALUES (?,?,?,?)',
                        ('tuya', ext_id, 'sensor', info['name'])
                    )
                total_tiles += 1
                continue
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
    if not _tuya_devices:
        return
    now = time.time()
    for dev_id, info in list(_tuya_devices.items()):
        if not info.get('ip'):
            continue
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
            if dev_id in _tuya_quarantine:
                _tuya_quarantine.pop(dev_id, None)
                _log_tuya_reachability(name, 'online', 'released from quarantine')
            elif info.get('was_offline'):
                _log_tuya_reachability(name, 'online', 'reconnected')
            info['was_offline'] = False
            info['dp_state']    = dps
            info['last_seen']   = time.time()
            info['online']      = True
            _tuya_failures.pop(dev_id, None)
            if info.get('category') == 'wsdcg':
                temp_raw = dps.get('va_temperature') or dps.get('2')
                hum_raw  = dps.get('va_humidity')    or dps.get('3')
                info['sensor_readings'] = {
                    'temp_c':       (temp_raw / 10.0) if isinstance(temp_raw, (int, float)) else None,
                    'humidity':     hum_raw if isinstance(hum_raw, (int, float)) else None,
                    'battery_state': dps.get('battery_state') or dps.get('14'),
                }
        except Exception as exc:
            _tuya_close_connection(dev_id)
            _tuya_mark_failure(dev_id, name, str(exc))


def _tuya_cloud_poll_sensors() -> None:
    """Fetch latest readings for wsdcg sensors from Tuya cloud."""
    sensor_ids = [
        dev_id for dev_id, info in _tuya_devices.items()
        if info.get('category') == 'wsdcg'
    ]
    if not sensor_ids:
        return

    api_key    = get_setting('tuya_api_key', '')
    api_secret = get_setting('tuya_api_secret', '')
    region     = get_setting('tuya_region', 'us')
    if not api_key or not api_secret:
        print('Tuya cloud poll: no API credentials configured')
        return

    import tinytuya
    cloud = tinytuya.Cloud(apiRegion=region, apiKey=api_key, apiSecret=api_secret)

    for dev_id in sensor_ids:
        info = _tuya_devices.get(dev_id)
        if not info:
            continue
        name = info.get('name') or f'Tuya {dev_id[-5:]}'
        try:
            result = cloud.getstatus(dev_id)
            if not isinstance(result, dict) or not result.get('success'):
                print(f'Tuya cloud poll error for {name}: {result}')
                _log_system_error('tuya', f'Cloud poll error: {name}', str(result))
                continue
            dps = {item['code']: item['value'] for item in result.get('result', [])}
            temp_raw = dps.get('va_temperature')
            hum_raw  = dps.get('va_humidity')
            with _switches_lock:
                info['sensor_readings'] = {
                    'temp_c':        (temp_raw / 10.0) if isinstance(temp_raw, (int, float)) else None,
                    'humidity':      hum_raw if isinstance(hum_raw, (int, float)) else None,
                    'battery_state': dps.get('battery_state'),
                }
                info['online']    = True
                info['last_seen'] = time.time()
        except Exception as exc:
            print(f'Tuya cloud poll error for {name}: {exc}')
            _log_system_error('tuya', f'Cloud poll error: {name}', str(exc))


def tuya_set(ext_id: str, on: bool) -> bool:
    dev_id, dp_idx = _parse_tuya_ext_id(ext_id)
    info = _tuya_devices.get(dev_id)
    if not info:
        raise ValueError(f'Unknown Tuya device: {dev_id}')
    if not info.get('ip'):
        raise RuntimeError(f'Tuya {dev_id} has no LAN IP (may be offline)')
    try:
        dp_as_int = int(dp_idx)
    except ValueError:
        dp_as_int = dp_idx
    try:
        dev = _tuya_get_or_connect(dev_id, info)
        result = dev.set_value(dp_as_int, bool(on))
        if isinstance(result, dict) and result.get('Error'):
            raise RuntimeError(f'Tuya error: {result.get("Error")}')
    except Exception:
        _tuya_close_connection(dev_id)
        raise
    dp_state = info.setdefault('dp_state', {})
    dp_state[dp_idx] = bool(on)
    info['last_seen'] = time.time()
    _tuya_failures.pop(dev_id, None)
    return bool(on)
