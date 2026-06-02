import asyncio
import sqlite3
import threading
import time
from datetime import datetime

import lib.state as state
from lib.settings import get_setting, get_setting_bool, get_setting_int
from lib.events import _log_system_error
from lib.db import connect


POOL_POLL_INTERVAL = 30

_pool: dict          = {}
_pool_ts: float      = 0.0
_pool_prev: dict     = {}
_pool_pending: dict  = {}
_pool_gallons_today: float = 0.0
_pool_gallons_date:  str   = ''
_pool_last_accum_ts: float = 0.0
_pool_normal_gpm:    float = 0.0
_pool_cleaner_gpm:   float = 0.0
_pool_edge_gpm:      float = 0.0

_CLEANER_PRESET_RPM = 2950
_MAX_SEGMENT_HOURS  = 18

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

POOL_CIRCUITS = [
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
            for k in keys:
                if not isinstance(d, dict):
                    return None
                d = d.get(k)
            return d

        def _key(d, *candidates):
            for k in candidates:
                if k in d:
                    return d[k]
            return {}

        body    = data.get('body') or data.get(b'body') or {}
        pump    = data.get('pump') or data.get(b'pump') or {}
        circuit = data.get('circuit') or data.get(b'circuit') or {}

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

        hm_idx  = _nested(pool_b, 'heat_mode', 'value')
        hm_opts = _nested(pool_b, 'heat_mode', 'enum_options') or []
        heat_mode = hm_opts[hm_idx] if (hm_idx is not None and isinstance(hm_opts, list) and hm_idx < len(hm_opts)) else None

        pool_pump_on    = bool(_nested(pump1, 'state', 'value'))
        pool_pump_watts = _nested(pump1, 'watts_now', 'value')
        pool_pump_rpm   = _nested(pump1, 'rpm_now', 'value')
        pool_pump_gpm   = _nested(pump1, 'gpm_now', 'value')
        edge_pump_on    = bool(_nested(c506, 'value'))
        edge_pump_watts = _nested(pump0, 'watts_now', 'value')
        edge_pump_rpm   = _nested(pump0, 'rpm_now', 'value')
        edge_pump_gpm   = _nested(pump0, 'gpm_now', 'value')

        pool_circuit_on = bool(_nested(c505, 'value'))
        spa_circuit_on  = bool(_nested(c500, 'value'))
        cleaner_on      = bool(_nested(c508, 'value'))
        pool_light_on   = bool(_nested(c501, 'value'))
        water_light_on  = bool(_nested(c502, 'value'))
        spa_light_on    = bool(_nested(c503, 'value'))
        waterfall_on    = bool(_nested(c504, 'value'))
        spillway_on     = bool(_nested(c507, 'value'))

        feature1_on = None
        for cid, cdata in circuit.items():
            if isinstance(cdata, dict):
                cname = _nested(cdata, 'name') or _nested(cdata, 'name', 'value') or ''
                if isinstance(cname, str) and cname.strip() == 'Feature 1':
                    feature1_on = bool(_nested(cdata, 'value'))
                    break

        scg = data.get('scg') or data.get(b'scg') or {}
        scg_sensor = scg.get('sensor') or scg.get(b'sensor') or {}
        scg_config = scg.get('configuration') or scg.get(b'configuration') or {}
        salt_ppm     = _nested(scg_sensor, 'salt_ppm', 'value')
        scg_state    = _nested(scg_sensor, 'state', 'value')
        scg_pool_pct = _nested(scg_config, 'pool_setpoint', 'value')
        super_chlor  = _nested(scg, 'super_chlorinate', 'value')

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


def _log_pool_changes(new: dict) -> None:
    global _pool_prev, _pool_pending
    if not _pool_prev:
        _pool_prev = {k: new.get(k) for k in _POOL_EVENT_FIELDS}
        _pool_pending = {}
        return
    now = int(time.time())
    try:
        with connect() as c:
            for field, (event_type, label) in _POOL_EVENT_FIELDS.items():
                confirmed_val = _pool_prev.get(field)
                new_val = new.get(field)
                if confirmed_val == new_val:
                    _pool_pending.pop(field, None)
                    continue
                if _pool_pending.get(field) == new_val:
                    st = 'on' if new_val else 'off'
                    title = f'{label} turned {st}'
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
            with connect() as conn:
                conn.execute('PRAGMA busy_timeout=5000')
                conn.executemany(
                    'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                    gpm_updates
                )
                conn.commit()
        except Exception as exc:
            print(f'Pool GPM cache write error: {exc}')


def _recalc_pool_target() -> None:
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
        with connect() as conn:
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
        with connect() as conn:
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
    now = time.time()
    if _pool_ts and int(now) // pool_ttl == int(_pool_ts) // pool_ttl:
        return _pool
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


def _pool_discover_circuits() -> int:
    with connect() as c:
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
    result = asyncio.run(_pool_set_circuit_async(ext_id, on))
    field = POOL_EXT_TO_FIELD.get(ext_id)
    if field:
        _pool[field] = on
        _pool_prev[field] = on
        _pool_pending.pop(field, None)
    return result


def _load_pool_gpm_cache() -> None:
    global _pool_normal_gpm, _pool_cleaner_gpm, _pool_edge_gpm
    try:
        _pool_normal_gpm  = float(get_setting('pool_cached_normal_gpm',  0) or 0)
        _pool_cleaner_gpm = float(get_setting('pool_cached_cleaner_gpm', 0) or 0)
        _pool_edge_gpm    = float(get_setting('pool_cached_edge_gpm',    0) or 0)
    except Exception as exc:
        print(f'Pool GPM cache load error: {exc}')
