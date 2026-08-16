import asyncio
import json
import sqlite3
import threading
import time
from datetime import datetime

import lib.state as state
from lib.settings import get_setting, get_setting_bool, get_setting_int, set_setting
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

_pool_circuit_names: dict = {}   # ext_id -> panel name              (from gateway)
_pool_presets:       dict = {}   # ext_id -> {'pump': idx, 'rpm': n} (from gateway)
_pool_gpm_samples:   dict = {}   # str(pump_idx) -> {str(preset_rpm): gpm}

_MAX_SEGMENT_HOURS = 18

# Fallbacks used only until live samples/presets exist. Measured at the gateway
# on 2026-08-15; superseded per-bucket by the first live sample at that speed.
# Flow is affine in RPM with a large negative intercept on this system (38 gpm
# @ 2150, 66 @ 3000), so a single constant or a proportional ratio is always
# wrong — _pool_gpm_for interpolates between observed speeds instead.
POOL_GPM_FALLBACK      = {1: {2150: 38.0, 3000: 66.0}, 0: {1380: 33.0}}
POOL_BASE_RPM_FALLBACK = {1: 1800, 0: 1380}
POOL_PRESET_RPM_TOL    = 60      # snap window for rpm_now -> preset rpm

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
    # Speed-override circuits, added 2026-08. Fields and event types are keyed by
    # circuit id, not by function: the panel name is user-mutable (510 was called
    # 'Feature 1' until 2026-08-15) and the preset RPM can be re-programmed, but
    # the circuit number is a hardware slot. The legacy ten keep their semantic
    # names — those event_types are persisted history that _recalc_pool_target
    # matches on and must never change.
    'circuit_510_on':  ('circuit_510_changed',   'Edge Prime'),
    'circuit_511_on':  ('circuit_511_changed',   'Pool 2150'),
    'circuit_512_on':  ('circuit_512_changed',   'Pool 2700'),
}

POOL_CIRCUITS = [
    ('500', 500, 'Spa',         'spa_circuit_on'),
    ('501', 501, 'Pool Light',  'pool_light_on'),
    ('502', 502, 'Water Light', 'water_light_on'),
    ('503', 503, 'Spa Light',   'spa_light_on'),
    ('504', 504, 'Waterfall',   'waterfall_on'),
    ('505', 505, 'Pool',        'pool_circuit_on'),
    ('506', 506, 'Edge Pump',   'edge_pump_on'),
    ('507', 507, 'Spillway',    'spillway_on'),
    ('508', 508, 'Cleaner',     'cleaner_on'),
    ('510', 510, 'Edge Prime',  'circuit_510_on'),
    ('511', 511, 'Pool 2150',   'circuit_511_on'),
    ('512', 512, 'Pool 2700',   'circuit_512_on'),
]
POOL_EXT_TO_FIELD = {ext: field for ext, _, _, field in POOL_CIRCUITS}

# Circuits 513-517 ('Feature 4'-'Feature 8') and 519 ('AuxEx') exist on the panel
# but have no pump preset assigned, so they are deliberately not tracked — they
# would be tiles that toggle nothing.

# ── Gallon reconstruction: event_type maps. APPEND-ONLY, FOREVER. ──────────────
# These are read against event_log rows going back months; changing a key orphans
# history.
POOL_PUMP_EVENT_TYPES = {'pump_changed': 1, 'edge_pump_changed': 0}

# event_type -> ext_id whose pump preset that circuit selects
POOL_SPEED_EVENT_TYPES = {
    'spa_circuit_changed':  '500',
    'waterfall_changed':    '504',
    'pool_circuit_changed': '505',
    'spillway_changed':     '507',
    'cleaner_changed':      '508',
    'circuit_510_changed':  '510',
    'circuit_511_changed':  '511',
    'circuit_512_changed':  '512',
    # Read-only alias: circuit 510 was named 'Feature 1' until 2026-08-15 15:52,
    # so its 75 historical rows are genuinely 510 (12-min runs matching 510's
    # 720s default_runtime). Keeps the rolling 30-day window accurate; delete
    # this line after 2026-09-15, when those rows age out.
    'feature1_changed':     '510',
}


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

        temp_f  = _nested(pool_b, 'last_temperature', 'value')
        spa_f   = _nested(spa_b,  'last_temperature', 'value')

        hm_idx  = _nested(pool_b, 'heat_mode', 'value')
        hm_opts = _nested(pool_b, 'heat_mode', 'enum_options') or []
        heat_mode = hm_opts[hm_idx] if (hm_idx is not None and isinstance(hm_opts, list) and hm_idx < len(hm_opts)) else None

        # Walk the circuit table once: harvest panel names, the device_id ->
        # circuit_id map the pump preset tables are keyed by, and each tracked
        # circuit's on/off state.
        dev_to_ext:      dict = {}
        names:           dict = {}
        circuit_states:  dict = {}
        for cid_key, cd in circuit.items():
            if not isinstance(cd, dict):
                continue
            try:
                cid = int(cd.get('circuit_id') or cid_key)
            except (TypeError, ValueError):
                continue
            dev = cd.get('device_id')
            if dev is None:
                dev = cid - 499       # fallback; holds on every panel seen so far
            try:
                dev_to_ext[int(dev)] = str(cid)
            except (TypeError, ValueError):
                pass
            nm = cd.get('name')
            if isinstance(nm, bytes):
                nm = nm.decode('utf-8', errors='ignore')
            if isinstance(nm, str) and nm.strip():
                names[str(cid)] = nm.strip()

        for ext_id, cid, _factory, field in POOL_CIRCUITS:
            cd = _key(circuit, cid, str(cid))
            # None (not False) when the circuit is absent from this payload — a
            # missing circuit must read as 'unknown', never as a confident 'off'.
            circuit_states[field] = bool(_nested(cd, 'value')) if cd else None

        # Pump preset tables: which circuit drives which RPM. Unused slots come
        # back as {device_id: 0, setpoint: 30, is_rpm: 0} — setpoint is a GPM
        # target there, not an RPM, so both guards are required.
        presets: dict = {}
        for p_idx, p in ((0, pump0), (1, pump1)):
            for _slot, pr in (p.get('preset') or {}).items():
                if not isinstance(pr, dict):
                    continue
                dev, sp = pr.get('device_id'), pr.get('setpoint')
                if not dev or not sp or not pr.get('is_rpm'):
                    continue
                ext = dev_to_ext.get(int(dev))
                if ext and ext not in presets:      # first pump wins if shared
                    presets[ext] = {'pump': p_idx, 'rpm': int(sp)}

        pool_pump_on    = bool(_nested(pump1, 'state', 'value'))
        pool_pump_watts = _nested(pump1, 'watts_now', 'value')
        pool_pump_rpm   = _nested(pump1, 'rpm_now', 'value')
        pool_pump_gpm   = _nested(pump1, 'gpm_now', 'value')
        edge_pump_watts = _nested(pump0, 'watts_now', 'value')
        edge_pump_rpm   = _nested(pump0, 'rpm_now', 'value')
        edge_pump_gpm   = _nested(pump0, 'gpm_now', 'value')

        # edge_pump_on stays keyed to circuit 506, NOT to pump0's own state field:
        # that field has been observed reading 0 while 506 is on and the pump is
        # drawing 174 W at 1380 rpm, so it cannot be trusted as 'running'.
        edge_pump_on = bool(circuit_states.get('edge_pump_on'))

        scg = data.get('scg') or data.get(b'scg') or {}
        scg_sensor = scg.get('sensor') or scg.get(b'sensor') or {}
        scg_config = scg.get('configuration') or scg.get(b'configuration') or {}
        salt_ppm     = _nested(scg_sensor, 'salt_ppm', 'value')
        scg_state    = _nested(scg_sensor, 'state', 'value')
        scg_pool_pct = _nested(scg_config, 'pool_setpoint', 'value')
        super_chlor  = _nested(scg, 'super_chlorinate', 'value')

        state_out = {
            'temp_f':          round(float(temp_f), 1) if temp_f is not None else None,
            'pump_on':         pool_pump_on,
            'pump_watts':      int(pool_pump_watts) if pool_pump_watts is not None else None,
            'pump_rpm':        int(pool_pump_rpm)   if pool_pump_rpm   is not None else None,
            'pump_gpm':        int(pool_pump_gpm)   if pool_pump_gpm   is not None else None,
            'edge_pump_watts': int(edge_pump_watts) if edge_pump_watts is not None else None,
            'edge_pump_rpm':   int(edge_pump_rpm)   if edge_pump_rpm   is not None else None,
            'edge_pump_gpm':   int(edge_pump_gpm)   if edge_pump_gpm   is not None else None,
            'salt_ppm':        int(salt_ppm) if salt_ppm is not None else None,
            'scg_active':      bool(scg_state) if scg_state is not None else None,
            'scg_pool_pct':    int(scg_pool_pct) if scg_pool_pct is not None else None,
            'super_chlor':     bool(super_chlor) if super_chlor is not None else None,
        }
        # Per-circuit on/off (spa_circuit_on, cleaner_on, circuit_511_on, ...);
        # overwrites edge_pump_on with the same c506 value it was derived from.
        state_out.update(circuit_states)
        state_out['edge_pump_on'] = edge_pump_on
        return state_out, {'circuit_names': names, 'presets': presets}
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
                if new_val is None:
                    # Circuit absent from this payload (panel reconfigured, or a
                    # partial read). Not a state change — logging it as 'off' is
                    # what produced the bogus 'Feature 1 turned off' on
                    # 2026-08-15 when circuit 510 was renamed out from under the
                    # old name-scan.
                    _pool_pending.pop(field, None)
                    continue
                if confirmed_val is None:
                    _pool_prev[field] = new_val      # adopt first known value silently
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


def _pool_snap_preset_rpm(pump_idx: int, rpm: int):
    """Nearest configured preset RPM for this pump, or None when rpm matches no
    preset (mid-ramp, or a speed set by hand at the keypad)."""
    cands = [p['rpm'] for p in _pool_presets.values() if p.get('pump') == pump_idx]
    if not cands:
        cands = list(POOL_GPM_FALLBACK.get(pump_idx, {}))
    if not cands:
        return None
    best = min(cands, key=lambda r: abs(r - rpm))
    return best if abs(best - rpm) <= POOL_PRESET_RPM_TOL else None


def _record_gpm_sample(pump_idx: int, rpm, gpm) -> None:
    """Learn GPM per preset speed. Attributing by *speed* is the fix: the old code
    bucketed by 'cleaner on / cleaner off', so a Pool-2150 run (2150 rpm, 38 gpm,
    cleaner off) was recorded as the 1800 rpm filtration baseline and inflated
    every gallon target derived from it."""
    global _pool_gpm_samples
    if not rpm or not gpm:
        return
    try:
        gpm_f = float(gpm)
    except (TypeError, ValueError):
        return
    if not (5.0 <= gpm_f <= 150.0):
        return
    snapped = _pool_snap_preset_rpm(pump_idx, int(rpm))
    if snapped is None:
        return
    bucket = _pool_gpm_samples.setdefault(str(pump_idx), {})
    prev   = bucket.get(str(snapped))
    if prev is not None and abs(float(prev) - gpm_f) < 0.5:
        return                                    # no meaningful drift, no write
    bucket[str(snapped)] = gpm_f
    try:
        set_setting('pool_cached_gpm_samples', json.dumps(_pool_gpm_samples))
    except Exception as exc:
        _log_system_error('pool', 'Pool GPM sample write error', str(exc))


def _pool_gpm_for(pump_idx: int, rpm: int) -> float:
    """Observed GPM at this speed. Learned samples win over the fallback table;
    unseen speeds interpolate between the two nearest known speeds, because flow
    is affine in RPM with a large negative intercept here (38 gpm @ 2150 vs 66 @
    3000 — proportional scaling under-reads by ~25%)."""
    merged = dict(POOL_GPM_FALLBACK.get(pump_idx, {}))
    for r, g in (_pool_gpm_samples.get(str(pump_idx)) or {}).items():
        try:
            merged[int(r)] = float(g)
        except (TypeError, ValueError):
            continue
    if not merged:
        return 0.0
    if rpm in merged:
        return merged[rpm]
    lo = max((r for r in merged if r < rpm), default=None)
    hi = min((r for r in merged if r > rpm), default=None)
    if lo is not None and hi is not None:
        f = (rpm - lo) / float(hi - lo)
        return merged[lo] + f * (merged[hi] - merged[lo])
    # Outside the observed range: extrapolate along the line through the two
    # nearest samples. Proportional scaling would be badly wrong below the range
    # (38 gpm @ 2150 scaled to 1800 predicts 31.8; the affine fit through
    # 2150/3000 gives 26.5), and flow is affine in RPM on this system.
    known = sorted(merged)
    if len(known) >= 2:
        a, b = (known[0], known[1]) if rpm < known[0] else (known[-2], known[-1])
        slope = (merged[b] - merged[a]) / float(b - a)
        return max(0.0, merged[a] + slope * (rpm - a))
    anchor = known[0]
    return merged[anchor] * (rpm / float(anchor))   # single sample: proportional


def _accumulate_pool_gallons(pool: dict) -> None:
    global _pool_gallons_today, _pool_gallons_date, _pool_last_accum_ts
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
    pool_gpm = pool.get('pump_gpm')
    edge_gpm = pool.get('edge_pump_gpm')
    if pool.get('pump_on') and pool_gpm:
        _pool_gallons_today += pool_gpm * elapsed_min
        _record_gpm_sample(1, pool.get('pump_rpm'), pool_gpm)
    # 'Edge Prime' (circuit 510) runs the edge pump at 1850 rpm with circuit 506
    # off, so gating on edge_pump_on alone would silently drop those gallons.
    if (pool.get('edge_pump_on') or pool.get('circuit_510_on')) and edge_gpm:
        _pool_gallons_today += edge_gpm * elapsed_min
        _record_gpm_sample(0, pool.get('edge_pump_rpm'), edge_gpm)


def _build_segments(events, etype, day_start, horizon_ts):
    """On/off spans for one event_type. Matched on the 'turned on'/'turned off'
    title text written by _log_pool_changes — persisted format, do not change."""
    MAX_SEG_SECS = _MAX_SEGMENT_HOURS * 3600
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
        # Still on at the horizon (end of that day, or 'now' for today).
        segs.append((start, start + min(max(horizon_ts - start, 0), MAX_SEG_SECS)))
    return segs


def _merge_segments(segs):
    """Union of possibly-overlapping (start, end) spans, so pump runtime built
    from more than one event source is never counted twice."""
    out: list = []
    for s, e in sorted(segs):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


def _pool_day_gallons(events, day_start, horizon_ts) -> float:
    """Gallons for one day, integrating each pump's *effective* speed.

    On EasyTouch the pump runs the highest preset among the circuits that are on
    (verified: with only 'Pool 2150' on, pump 1 reports 2150 rpm), so pump-on time
    is sliced at every override boundary and each slice priced at max(active
    preset rpm), defaulting to that pump's lowest preset — its filtration
    baseline — when nothing we track is on.

    This replaces the old cleaner-∩-pump intersection: with seven override
    circuits on pump 1 that approach would need pairwise-and-worse intersections,
    whereas the sweep is one pass with a max().
    """
    segs = {et: _build_segments(events, et, day_start, horizon_ts)
            for et in list(POOL_PUMP_EVENT_TYPES) + list(POOL_SPEED_EVENT_TYPES)}
    total = 0.0
    for pump_et, pump_idx in POOL_PUMP_EVENT_TYPES.items():
        pump_segs = segs[pump_et]
        rpm_by_ext = {ext: p['rpm'] for ext, p in _pool_presets.items()
                      if p.get('pump') == pump_idx}
        overrides = [(rpm_by_ext[ext], segs[et])
                     for et, ext in POOL_SPEED_EVENT_TYPES.items()
                     if ext in rpm_by_ext and segs[et]]
        if pump_idx == 0 and '510' in rpm_by_ext:
            # 'Edge Prime' (510) runs the edge pump with circuit 506 off, so its
            # spans are pump-0 runtime in their own right, not just an override.
            pump_segs = pump_segs + segs.get('circuit_510_changed', []) \
                                  + segs.get('feature1_changed', [])
        pump_segs = _merge_segments(pump_segs)
        if not pump_segs:
            continue
        base_rpm = (min(rpm_by_ext.values()) if rpm_by_ext
                    else POOL_BASE_RPM_FALLBACK.get(pump_idx, 1800))
        for s, e in pump_segs:
            bounds = sorted({s, e} | {t for _r, ss in overrides
                                        for seg in ss for t in seg
                                        if s < t < e})
            for t0, t1 in zip(bounds, bounds[1:]):
                mid    = (t0 + t1) / 2.0
                active = [r for r, ss in overrides
                          if any(a <= mid < b for a, b in ss)]
                rpm    = max(active) if active else base_rpm
                total += (t1 - t0) / 60.0 * _pool_gpm_for(pump_idx, rpm)
    return total


def _recalc_pool_target() -> None:
    cutoff_ts   = int(time.time()) - 30 * 86400
    event_types = tuple(POOL_PUMP_EVENT_TYPES) + tuple(POOL_SPEED_EVENT_TYPES)
    placeholders = ','.join('?' * len(event_types))

    try:
        with connect() as conn:
            conn.execute('PRAGMA busy_timeout=5000')
            rows = conn.execute(
                f'''SELECT ts, event_type, title FROM event_log
                    WHERE  system = 'pool'
                      AND  event_type IN ({placeholders})
                      AND  ts >= ?
                    ORDER BY ts''',
                event_types + (cutoff_ts,)
            ).fetchall()
    except Exception as exc:
        _log_system_error('pool', 'Pool target recalc DB error', str(exc))
        return

    from collections import defaultdict
    by_date: dict = defaultdict(list)
    for ts, event_type, title in rows:
        by_date[time.strftime('%Y-%m-%d', time.localtime(ts))].append((ts, event_type, title))

    today  = time.strftime('%Y-%m-%d', time.localtime(time.time()))
    now_ts = time.time()

    weekday_vals: list = []
    weekend_vals: list = []

    for day, events in sorted(by_date.items()):
        if day == today:
            continue
        day_start = int(time.mktime(time.strptime(day, '%Y-%m-%d')))
        gallons   = _pool_day_gallons(events, day_start, day_start + 86400)
        if gallons <= 0:
            continue
        dow = time.localtime(day_start).tm_wday
        (weekday_vals if dow < 5 else weekend_vals).append(gallons)

    today_events = by_date.get(today, [])
    if today_events:
        day_start = int(time.mktime(time.strptime(today, '%Y-%m-%d')))
        global _pool_gallons_today, _pool_gallons_date, _pool_last_accum_ts
        _pool_gallons_today = _pool_day_gallons(today_events, day_start, now_ts)
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
        _pool, meta = asyncio.run(_pool_fetch_async())
        _pool_ts    = time.time()
        _pool_sync_panel_meta(meta)
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


def _pool_panel_names() -> dict:
    """Panel names: live poll cache -> persisted blob -> {} (callers fall back to
    the factory name in POOL_CIRCUITS). Never opens a gateway connection — the
    per-poll connect/disconnect churn is bad enough with one."""
    global _pool_circuit_names
    if _pool_circuit_names:
        return _pool_circuit_names
    try:
        blob = get_setting('pool_cached_circuit_names', '') or ''
        if blob:
            data = json.loads(blob)
            if isinstance(data, dict):
                _pool_circuit_names = {str(k): v for k, v in data.items() if v}
    except Exception as exc:
        _log_system_error('pool', 'Pool name cache load error', str(exc))
    return _pool_circuit_names


def _pool_sync_panel_meta(meta: dict) -> None:
    """Persist panel names/presets and push keypad renames into switches_meta —
    only when something actually changed, so the steady state costs two dict
    comparisons per poll."""
    global _pool_circuit_names, _pool_presets
    names   = {k: v for k, v in (meta.get('circuit_names') or {}).items() if v}
    presets = meta.get('presets') or {}
    try:
        if presets and presets != _pool_presets:
            _pool_presets = presets
            set_setting('pool_cached_presets', json.dumps(presets))
        if names and names != _pool_circuit_names:
            _pool_circuit_names = names
            set_setting('pool_cached_circuit_names', json.dumps(names))
            _pool_discover_circuits()
    except Exception as exc:
        _log_system_error('pool', 'Pool panel meta sync error', str(exc))


def _pool_discover_circuits() -> int:
    """Seed switches_meta rows for every tracked circuit, and follow keypad
    renames without clobbering names the user edited in the drawer.

        name == source_name  -> still panel-synced; update both
        name != source_name  -> user renamed it here; update source_name only
        source_name IS NULL  -> provenance unknown; adopt panel name, keep `name`
    """
    names = _pool_panel_names()
    with connect() as c:
        row = c.execute(
            "SELECT room FROM switches_meta WHERE provider='pool' AND room <> '' "
            "GROUP BY room ORDER BY COUNT(*) DESC LIMIT 1"
        ).fetchone()
        default_room = row[0] if row else ''   # new tiles land beside the others
        for ext_id, _cid, factory_name, _field in POOL_CIRCUITS:
            panel    = names.get(ext_id)
            existing = c.execute(
                'SELECT id, name, source_name FROM switches_meta '
                'WHERE provider=? AND external_id=?', ('pool', ext_id)
            ).fetchone()
            if existing is None:
                nm = panel or factory_name
                c.execute(
                    'INSERT INTO switches_meta '
                    '(provider, external_id, kind, name, room, source_name) '
                    'VALUES (?,?,?,?,?,?)',
                    ('pool', ext_id, 'circuit', nm, default_room, nm)
                )
                continue
            if not panel:
                continue                       # no panel data: touch nothing
            rid, name, source_name = existing
            if source_name is None:
                c.execute('UPDATE switches_meta SET source_name=? WHERE id=?',
                          (panel, rid))
            elif name == source_name:
                if panel != name:
                    c.execute(
                        'UPDATE switches_meta SET name=?, source_name=? WHERE id=?',
                        (panel, panel, rid)
                    )
            elif panel != source_name:
                c.execute('UPDATE switches_meta SET source_name=? WHERE id=?',
                          (panel, rid))
    return len(POOL_CIRCUITS)


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


def _load_pool_caches() -> None:
    """Load the persisted panel names, pump presets and GPM samples. Must run
    before _pool_discover_circuits() and before the startup _recalc_pool_target
    thread, both of which need this data and may run before the first poll."""
    global _pool_gpm_samples, _pool_presets
    try:
        blob = get_setting('pool_cached_gpm_samples', '') or ''
        if blob:
            data = json.loads(blob)
            if isinstance(data, dict):
                _pool_gpm_samples = data
    except Exception as exc:
        _log_system_error('pool', 'Pool GPM sample cache load error', str(exc))
    try:
        blob = get_setting('pool_cached_presets', '') or ''
        if blob:
            data = json.loads(blob)
            if isinstance(data, dict):
                _pool_presets = {str(k): v for k, v in data.items()}
    except Exception as exc:
        _log_system_error('pool', 'Pool preset cache load error', str(exc))
    _pool_panel_names()
