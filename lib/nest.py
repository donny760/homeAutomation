import json
import sqlite3
import time
from datetime import datetime

import requests as _requests

import lib.state as state
from lib.events import _log_system_error
from lib.settings import get_setting, get_setting_bool, get_setting_int
from lib.db import connect


_nest_event_ts: float   = 0.0
_nest_devices: dict     = {}
_nest_devices_raw: list = []
_nest_devices_ts: float = 0.0
_NEST_DEVICE_CACHE_TTL  = 3600
_nest_event_counters: dict = {}
_nest_poll_stats: dict = {
    'calls': 0, 'last_call_ts': None, 'last_pull_count': None,
    'last_error': None, 'pull_count_total': 0,
}
_nest_thermostats: dict = {}

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


def _extract_api_error(resp, max_len: int = 200) -> str:
    try:
        return resp.json().get('error', {}).get('message', resp.text[:max_len])
    except Exception:
        return resp.text[:max_len]


def _nest_oauth_exchange(extra: dict) -> dict:
    client_id     = get_setting('nest_client_id', '')
    client_secret = get_setting('nest_client_secret', '')
    data = {'client_id': client_id, 'client_secret': client_secret, **extra}
    resp = _requests.post(NEST_OAUTH_TOKEN_URL, data=data, timeout=15)
    resp.raise_for_status()
    return resp.json()


def _nest_save_tokens(tokens: dict, *, save_refresh: bool) -> None:
    expires_in = tokens.get('expires_in', 3600)
    rows = [
        ('nest_access_token', tokens.get('access_token', '')),
        ('nest_token_expiry', str(int(time.time()) + expires_in - 60)),
    ]
    if save_refresh:
        rows.append(('nest_refresh_token', tokens.get('refresh_token', '')))
    with connect() as c:
        c.executemany(
            'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', rows
        )
        c.commit()


def _nest_ensure_token() -> str | None:
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
    if c is None:
        return None
    try:
        return round(float(c) * 9.0 / 5.0 + 32.0, 1)
    except (TypeError, ValueError):
        return None


def _f_to_c(f):
    if f is None:
        return None
    try:
        return round((float(f) - 32.0) * 5.0 / 9.0, 2)
    except (TypeError, ValueError):
        return None


def _nest_refresh_devices(token):
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
            room_name = ''
            for pr in d.get('parentRelations', []) or []:
                dn = pr.get('displayName')
                if isinstance(dn, str) and dn.strip():
                    room_name = dn.strip()
                    break
            display = custom or room_name or dev_type or 'Unknown'
            _nest_devices[name] = {'name': display}
            if dev_type_full == 'sdm.devices.types.THERMOSTAT':
                tmode    = traits.get('sdm.devices.traits.ThermostatMode', {}) or {}
                setpt    = traits.get('sdm.devices.traits.ThermostatTemperatureSetpoint', {}) or {}
                hvac     = traits.get('sdm.devices.traits.ThermostatHvac', {}) or {}
                ambient  = traits.get('sdm.devices.traits.Temperature', {}) or {}
                humidity = traits.get('sdm.devices.traits.Humidity', {}) or {}
                eco      = traits.get('sdm.devices.traits.ThermostatEco', {}) or {}
                thermostats[name] = {
                    'display_name':    display,
                    'mode':            tmode.get('mode'),
                    'available_modes': tmode.get('availableModes', []),
                    'setpoint_heat_c': setpt.get('heatCelsius'),
                    'setpoint_cool_c': setpt.get('coolCelsius'),
                    'ambient_c':       ambient.get('ambientTemperatureCelsius'),
                    'humidity':        humidity.get('ambientHumidityPercent'),
                    'hvac_status':     hvac.get('status'),
                    'eco_mode':        eco.get('mode'),
                }
        _nest_thermostats = thermostats
        _nest_devices_ts = time.time()
        if thermostats:
            with connect() as c:
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
        _log_system_error('nest', 'Device list error', str(exc))


def _nest_thermostat_command(device_path: str, command: str, params: dict) -> dict:
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
    info = _nest_thermostats.get(device_path)
    if info is None:
        raise ValueError(f'Unknown Nest thermostat: {device_path}')
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
        info['mode'] = mode
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
    try:
        token = _nest_ensure_token()
        if token:
            _nest_refresh_devices(token)
    except Exception as exc:
        print(f'Nest post-command refresh failed: {exc}')
        _log_system_error('nest', 'Post-command refresh failed', str(exc))
    return dict(_nest_thermostats.get(device_path, info))


def _nest_get_device_name(device_path: str, token: str) -> str:
    if not _nest_devices or time.time() - _nest_devices_ts > _NEST_DEVICE_CACHE_TTL:
        _nest_refresh_devices(token)
    if device_path in _nest_devices:
        return _nest_devices[device_path].get('name', 'Unknown')
    return device_path.rsplit('/', 1)[-1][:6] if device_path else 'Unknown'


def fetch_nest_events() -> int:
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

        if not _nest_devices or time.time() - _nest_devices_ts > _NEST_DEVICE_CACHE_TTL:
            _nest_refresh_devices(token)

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

        if rows:
            with connect() as c:
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
