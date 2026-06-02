import sqlite3

import lib.state as state
import lib.kasa as kasa
import lib.tuya as tuya
import lib.pool as pool
import lib.abode as abode_mod
import lib.nest as nest_mod
from lib.settings import get_setting_bool
from lib.events import _switches_log_event
from lib.kasa import kasa_set, kasa_set_brightness, _kasa_refresh_devices
from lib.tuya import tuya_set, _tuya_refresh_devices
from lib.abode import ABODE_MODE_DISPLAY, _abode_seed_alarm_row
from lib.nest import nest_set_thermostat, _nest_ensure_token, _nest_refresh_devices, _c_to_f
from lib.pool import POOL_EXT_TO_FIELD, _pool_discover_circuits, pool_set_circuit
from lib.db import connect


def _switches_rediscover_all() -> dict:
    """Run discovery across every enabled provider. Returns per-provider counts."""
    counts = {'kasa': 0, 'pool': 0, 'nest_thermostat': 0, 'tuya': 0}
    if get_setting_bool('kasa_enabled', False):
        counts['kasa'] = _kasa_refresh_devices()
    if get_setting_bool('pool_enabled', True):
        counts['pool'] = _pool_discover_circuits()
    if get_setting_bool('nest_enabled', False):
        token = _nest_ensure_token()
        if token:
            _nest_refresh_devices(token)
            counts['nest_thermostat'] = len(nest_mod._nest_thermostats)
    if get_setting_bool('tuya_enabled', False):
        counts['tuya'] = _tuya_refresh_devices()
    if get_setting_bool('abode_enabled', True):
        counts['abode'] = _abode_seed_alarm_row()
    return counts


def _get_all_switches() -> list:
    """Return merged switch list with DB metadata + live state per provider."""
    out = []
    with connect() as c:
        rows = c.execute(
            'SELECT id, provider, external_id, kind, name, room, sort_order, hidden '
            'FROM switches_meta ORDER BY room, sort_order, name'
        ).fetchall()
    for rid, provider, ext_id, kind, name, room, sort_order, hidden in rows:
        sw_state  = None
        detail    = {}
        reachable = True
        if provider == 'kasa':
            info = kasa._kasa_devices.get(ext_id)
            if info is None:
                reachable = False
            else:
                sw_state = info.get('on')
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
                val = pool._pool.get(field) if field else None
                if val is None:
                    reachable = bool(pool._pool)
                    sw_state = None
                else:
                    sw_state = bool(val)
                detail = {'circuit_id': ext_id}
        elif provider == 'abode':
            if not get_setting_bool('abode_enabled', True):
                reachable = False
            else:
                mode = (abode_mod._security.get('mode') or 'standby').lower()
                sw_state = (mode != 'standby')
                detail = {
                    'mode':         mode,
                    'mode_display': ABODE_MODE_DISPLAY.get(mode, mode),
                    'connected':    abode_mod._security.get('connected', False),
                }
                reachable = bool(abode_mod._abode_instance is not None)
        elif provider == 'tuya':
            if not get_setting_bool('tuya_enabled', False):
                reachable = False
            else:
                dev_id, dp_idx = tuya._parse_tuya_ext_id(ext_id)
                info = tuya._tuya_devices.get(dev_id)
                if info is None:
                    reachable = False
                    sw_state = None
                elif kind == 'sensor':
                    readings  = info.get('sensor_readings') or {}
                    reachable = bool(readings)
                    temp_c    = readings.get('temp_c')
                    temp_f    = round(temp_c * 9 / 5 + 32, 1) if temp_c is not None else None
                    detail = {
                        'temp_f':        temp_f,
                        'humidity':      readings.get('humidity'),
                        'battery_state': readings.get('battery_state'),
                        'online':        info.get('online', True),
                    }
                else:
                    dps = info.get('dp_state') or {}
                    val = dps.get(dp_idx)
                    sw_state = bool(val) if isinstance(val, bool) else None
                    detail = {
                        'ip':       info.get('ip'),
                        'category': info.get('category'),
                        'online':   info.get('online', True),
                        'dp':       dp_idx,
                    }
        elif provider == 'nest':
            if kind == 'thermostat':
                info = nest_mod._nest_thermostats.get(ext_id)
                if info is None:
                    reachable = False
                else:
                    mode = (info.get('mode') or 'OFF').upper()
                    sw_state = mode != 'OFF'
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
            'id':          rid,
            'provider':    provider,
            'external_id': ext_id,
            'kind':        kind,
            'name':        name,
            'room':        room or '',
            'sort_order':  sort_order,
            'hidden':      bool(hidden),
            'state':       sw_state,
            'reachable':   reachable,
            'detail':      detail,
        })
    return out


def _switches_lookup(row_id: int):
    with connect() as c:
        row = c.execute(
            'SELECT id, provider, external_id, kind, name FROM switches_meta WHERE id=?',
            (row_id,)
        ).fetchone()
    return row  # (id, provider, external_id, kind, name) or None


def switch_set_state(row_id: int, on: bool) -> dict:
    """Dispatch set-state to the right provider. Returns result dict."""
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
    if kind == 'routine':
        return switch_set_state(row_id, True)
    if kind == 'alarm':
        return {'error': 'use /api/switches/alarm/arm-home for abode alarm',
                'code': 400}
    current = None
    if provider == 'kasa':
        current = (kasa._kasa_devices.get(ext_id) or {}).get('on')
    elif provider == 'pool':
        field = POOL_EXT_TO_FIELD.get(ext_id)
        current = pool._pool.get(field) if field else None
    elif provider == 'tuya':
        dev_id, dp_idx = tuya._parse_tuya_ext_id(ext_id)
        info = tuya._tuya_devices.get(dev_id) or {}
        val = (info.get('dp_state') or {}).get(dp_idx)
        current = bool(val) if isinstance(val, bool) else None
    if current is None:
        return {'error': 'current state unknown', 'code': 409}
    return switch_set_state(row_id, not current)


def switch_set_thermostat(row_id: int, **fields) -> dict:
    """Dispatch thermostat command to SDM API."""
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
    with connect() as c:
        cur = c.execute(f'UPDATE switches_meta SET {sets} WHERE id=?', vals)
        if cur.rowcount == 0:
            return {'error': 'not found', 'code': 404}
    return {'ok': True}
