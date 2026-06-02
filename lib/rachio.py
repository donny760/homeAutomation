import json
import os
import re
import sqlite3
import time
import urllib.request
from datetime import datetime, date, timedelta, timezone

import lib.state as state
from lib.events import _log_system_error
from lib.settings import get_setting_bool, get_setting_int
from lib.db import connect


RACHIO_API_KEY = os.environ.get('RACHIO_API_KEY', '')
RACHIO_BASE    = 'https://api.rach.io/1/public'
RACHIO_TTL     = 300

_rachio_schedule: list = []
_rachio_ts: float      = 0.0
_rachio_event_ts: float = 0.0
_rain_skip_ts: float   = 0.0
_device_coords: tuple | None = None

RAIN_MIN_MM = 1.0

RACHIO_EVENT_TYPE_MAP = {
    'ZONE_STARTED':         'zone_started',
    'ZONE_COMPLETED':       'zone_completed',
    'ZONE_STOPPED':         'zone_stopped',
    'SCHEDULE_STARTED':     'schedule_started',
    'SCHEDULE_COMPLETED':   'schedule_completed',
    'SCHEDULE_STOPPED':     'schedule_stopped',
    'RAIN_DELAY_ON':        'rain_delay',
    'RAIN_DELAY_OFF':       'rain_delay_off',
    'RAIN_SENSOR_TRIPPED':  'rain_sensor',
    'WEATHER_INTELLIGENCE': 'weather_skip',
    'SKIP':                 'skip',
    'DEVICE_OFFLINE':       'device_offline',
    'DEVICE_ONLINE':        'device_online',
}


def _rachio_get(path: str) -> dict:
    req = urllib.request.Request(
        RACHIO_BASE + path,
        headers={'Authorization': f'Bearer {RACHIO_API_KEY}', 'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _rachio_put(path: str, body: dict) -> dict | None:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        RACHIO_BASE + path, data=data, method='PUT',
        headers={'Authorization': f'Bearer {RACHIO_API_KEY}', 'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def _get_device_coords() -> tuple[float, float]:
    global _device_coords
    if _device_coords is not None:
        return _device_coords
    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')
        for device in person.get('devices', []):
            lat, lng = device.get('latitude'), device.get('longitude')
            if lat and lng:
                _device_coords = (float(lat), float(lng))
                print(f'Weather: using device coords ({lat}, {lng})')
                return _device_coords
    except Exception as exc:
        print(f'Weather coord lookup error: {exc}')
    _device_coords = (33.09924225276156, -117.06278850408303)
    return _device_coords


# Public alias for weather.py
get_device_coords = _get_device_coords


def _rachio_next_run(start_h: int, start_m: int, rachio_days: set):
    from datetime import time as dt_time
    run_t = dt_time(hour=int(start_h), minute=int(start_m))
    now   = datetime.now()
    for delta in range(8):
        cdate      = (now + timedelta(days=delta)).date()
        rachio_dow = (cdate.weekday() + 1) % 7
        if rachio_dow in rachio_days:
            cdt = datetime.combine(cdate, run_t)
            if cdt > now:
                return cdt
    return None


def _rachio_runs_in_window(start_h: int, start_m: int, rachio_days: set,
                            hours: int = 48, past_hours: int = 0):
    from datetime import time as dt_time
    run_t  = dt_time(hour=int(start_h), minute=int(start_m))
    now    = datetime.now()
    start  = now - timedelta(hours=past_hours)
    cutoff = now + timedelta(hours=hours)
    runs   = []
    total_days = (hours + past_hours) // 24 + 2
    for delta in range(-((past_hours // 24) + 1), total_days):
        cdate      = (now + timedelta(days=delta)).date()
        rachio_dow = (cdate.weekday() + 1) % 7
        if rachio_dow in rachio_days:
            cdt = datetime.combine(cdate, run_t)
            if start <= cdt <= cutoff:
                runs.append(cdt)
    return runs


def _rachio_days_from_job_types(job_types: list) -> set:
    days = set()
    for jt in job_types:
        m = re.match(r'DAY_OF_WEEK_(\d+)', jt)
        if m:
            days.add(int(m.group(1)))
    if not days and any('INTERVAL' in jt for jt in job_types):
        days = set(range(7))
    return days


def fetch_rachio_events() -> int:
    global _rachio_event_ts
    if not get_setting_bool('rachio_enabled', True):
        return 0
    inserted = 0
    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')

        end_ms   = int(time.time() * 1000)
        start_ms = end_ms - 48 * 3600 * 1000
        rows = []
        for device in person.get('devices', []):
            did   = device['id']
            dname = device.get('name', '?')
            raw_events = _rachio_get(f'/device/{did}/event?startTime={start_ms}&endTime={end_ms}')
            if not isinstance(raw_events, list):
                raw_events = raw_events.get('events', []) if isinstance(raw_events, dict) else []
            for ev in raw_events:
                try:
                    raw_type   = ev.get('type') or ev.get('subType') or 'UNKNOWN'
                    event_type = RACHIO_EVENT_TYPE_MAP.get(raw_type, raw_type.lower())
                    title      = ev.get('summary') or ev.get('eventType', raw_type)
                    ts_raw = ev.get('eventDate') or ev.get('createDate')
                    ts = int(ts_raw / 1000) if ts_raw else int(time.time())
                    zone   = ev.get('zoneName', '')
                    sched  = ev.get('scheduleName', '')
                    dur    = ev.get('durationInMinutes', '')
                    detail = f'device: {dname}  zone: {zone}  schedule: {sched}  duration: {dur}min'.strip()
                    rows.append((ts, 'rachio', event_type, title, detail, 'info', 'live'))
                except Exception:
                    continue

        if rows:
            with connect() as c:
                existing = set(
                    c.execute(
                        'SELECT ts, title FROM event_log WHERE system = ?', ('rachio',)
                    ).fetchall()
                )
                for row in rows:
                    ts, sys_, evt, title, detail, result, source = row
                    if (ts, title) not in existing:
                        c.execute(
                            'INSERT INTO event_log '
                            '(ts, system, event_type, title, detail, result, source) '
                            'VALUES (?,?,?,?,?,?,?)', row)
                        existing.add((ts, title))
                        inserted += 1
            if inserted:
                print(f'Rachio events: logged {inserted} new events')

        _rachio_event_ts = time.time()
    except Exception as exc:
        print(f'Rachio event poll error: {exc}')
        _log_system_error('rachio', 'Event poll error', str(exc))
    return inserted


def evaluate_rain_skip() -> None:
    global _rain_skip_ts, _rachio_ts
    if not get_setting_bool('rain_skip_enabled', False):
        return
    if not get_setting_bool('rachio_enabled', True):
        return

    import math
    mm_per_day    = get_setting_int('rain_mm_per_skip_day', 1)
    max_days      = get_setting_int('rain_skip_max_days', 7)
    lookback_days = get_setting_int('rain_lookback_days', 5)

    # Deferred import breaks rachio↔weather cycle
    from lib.weather import fetch_weather
    wx = fetch_weather()
    rain_history = wx.get('rain_history', [])
    if not rain_history:
        return

    rainy = [(e['date'], e['mm']) for e in rain_history if (e['mm'] or 0) >= RAIN_MIN_MM]
    if not rainy:
        _rain_skip_ts = time.time()
        return

    last_rain_date_str = max(rainy, key=lambda r: r[0])[0]
    last_rain_date     = datetime.strptime(last_rain_date_str, '%Y-%m-%d').date()

    accumulated = sum((e['mm'] or 0) for e in rain_history)
    skip_days   = min(int(math.floor(accumulated / mm_per_day)), max_days) if mm_per_day > 0 else 0
    if skip_days <= 0:
        _rain_skip_ts = time.time()
        return

    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')

        for device in person.get('devices', []):
            did   = device['id']
            dname = device.get('name', '?')

            wi_skips, threshold_in = _rachio_wi_skip_info(did, lookback_hours=lookback_days * 24)
            last_skip_date = None
            for _sched_id, run_dt in wi_skips:
                d = run_dt.date()
                if last_skip_date is None or d > last_skip_date:
                    last_skip_date = d
            anchor_date = max(last_rain_date, last_skip_date) if last_skip_date else last_rain_date

            end_dt     = datetime.combine(anchor_date, datetime.min.time()) + timedelta(days=skip_days)
            our_end_ts = end_dt.timestamp()
            now_ts     = time.time()
            if our_end_ts <= now_ts:
                continue

            existing_end_ts = 0
            rd_exp = device.get('rainDelayExpirationDate')
            if rd_exp and isinstance(rd_exp, (int, float)) and rd_exp > 0:
                existing_end_ts = rd_exp / 1000
            if existing_end_ts >= our_end_ts:
                existing_dt = datetime.fromtimestamp(existing_end_ts).strftime('%Y-%m-%d %H:%M')
                print(f'Rain skip: {dname} — existing delay until {existing_dt} is longer, skipping')
                continue

            try:
                forecast_by_date = _rachio_device_forecast(did)
                today_d     = datetime.now().date()
                tomorrow_d  = today_d + timedelta(days=1)
                today_in    = forecast_by_date.get(today_d.isoformat(), 0)
                tomorrow_in = forecast_by_date.get(tomorrow_d.isoformat(), 0)
                if today_in >= threshold_in or tomorrow_in >= threshold_in:
                    print(f'Rain skip: {dname} — Rachio forecast covers next 48h '
                          f'(today={today_in:.2f}", tomorrow={tomorrow_in:.2f}", '
                          f'threshold={threshold_in:.2f}"), deferring')
                    continue
            except Exception as exc:
                print(f'Rain skip forecast check error: {exc}')
                continue

            duration_secs = int(our_end_ts - now_ts)
            _rachio_put('/device/rain_delay', {'id': did, 'duration': duration_secs})

            _rachio_ts = 0.0

            existing_info = ''
            if existing_end_ts > now_ts:
                existing_dt = datetime.fromtimestamp(existing_end_ts).strftime('%Y-%m-%d %H:%M')
                existing_info = f'  existing_delay_until: {existing_dt}'
            anchor_info = (f'rachio_last_skip: {last_skip_date}'
                           if last_skip_date else f'last_rain: {last_rain_date}')
            detail = (f'device: {dname}  accumulated: {accumulated:.1f}mm  '
                      f'lookback: {len(rain_history)} days  skip: {skip_days} days  '
                      f'anchor: {anchor_date} ({anchor_info})  '
                      f'delay_until: {end_dt.strftime("%Y-%m-%d %H:%M")}{existing_info}')

            today_ts = int(datetime.now().replace(hour=0, minute=0, second=0).timestamp())
            title    = f'Rain skip: {skip_days} days ({dname})'
            with connect() as c:
                exists = c.execute(
                    'SELECT 1 FROM event_log WHERE system=? AND ts=? AND title=?',
                    ('rachio', today_ts, title)
                ).fetchone()
                if not exists:
                    c.execute(
                        'INSERT INTO event_log '
                        '(ts, system, event_type, title, detail, result, source) '
                        'VALUES (?,?,?,?,?,?,?)',
                        (today_ts, 'rachio', 'rain_skip_extended', title, detail, 'ok', 'live'))
            print(f'Rain skip applied: {dname} — {skip_days} days '
                  f'(anchor {anchor_date}, end {end_dt.strftime("%Y-%m-%d %H:%M")})')

        _rain_skip_ts = time.time()
    except Exception as exc:
        import traceback
        err = f'{type(exc).__name__}: {exc}\n' + traceback.format_exc()[-400:]
        print(f'Rain skip error: {err}')
        _log_system_error('rachio', 'Rain skip evaluation error', err)


def _rachio_device_forecast(device_id: str) -> dict:
    forecast_by_date = {}
    try:
        data = _rachio_get(f'/device/{device_id}/forecast')
        if not isinstance(data, dict):
            return forecast_by_date
        entries = []
        cur = data.get('current')
        if isinstance(cur, dict):
            entries.append(cur)
        fc = data.get('forecast')
        if isinstance(fc, list):
            entries.extend(fc)
        for entry in entries:
            ts_ms = entry.get('localizedTimeStamp') or (entry.get('time') and entry.get('time') * 1000)
            if not ts_ms:
                continue
            d = datetime.fromtimestamp(ts_ms / 1000).date().isoformat()
            precip_in = entry.get('calculatedPrecip')
            if precip_in is None:
                precip_in = entry.get('precipIntensity', 0)
            forecast_by_date[d] = float(precip_in or 0)
    except Exception as exc:
        print(f'Rachio device forecast error: {exc}')
    return forecast_by_date


def _rachio_wi_skip_info(device_id: str, lookback_hours: int = 48):
    skip_set = set()
    threshold_in = 0.06
    try:
        end_ms   = int(time.time() * 1000)
        start_ms = end_ms - lookback_hours * 3600 * 1000
        raw = _rachio_get(f'/device/{device_id}/event?startTime={start_ms}&endTime={end_ms}')
        if not isinstance(raw, list):
            return skip_set, threshold_in
        for ev in raw:
            if ev.get('subType') != 'WEATHER_INTELLIGENCE_SKIP':
                continue
            sched_id = ev.get('scheduleId') or ''
            summary  = ev.get('summary') or ''

            m = re.search(r'scheduled for (\d+)/(\d+) at (\d+):(\d+)\s*([AP])M', summary)
            run_dt = None
            if m:
                mo, dy, hh, mm, ap = m.groups()
                hh = int(hh) % 12
                if ap == 'P':
                    hh += 12
                year = datetime.now().year
                try:
                    candidate = datetime(year, int(mo), int(dy), hh, int(mm))
                    if candidate > datetime.now() + timedelta(days=1):
                        candidate = candidate.replace(year=year - 1)
                    run_dt = candidate
                except Exception:
                    run_dt = None
            if run_dt is None:
                ed = ev.get('eventDate')
                if isinstance(ed, (int, float)):
                    run_dt = datetime.fromtimestamp(ed / 1000)

            if run_dt is not None:
                skip_set.add((sched_id, run_dt))

            tm = re.search(r'threshold of (\d+\.\d+)\s*in', summary)
            if tm:
                try:
                    threshold_in = float(tm.group(1))
                except Exception:
                    pass
    except Exception as exc:
        print(f'Rachio WI skip fetch error: {exc}')
    return skip_set, threshold_in


def fetch_rachio_schedule() -> list:
    global _rachio_schedule, _rachio_ts
    if not get_setting_bool('rachio_enabled', True):
        return _rachio_schedule or []
    rachio_ttl = get_setting_int('rachio_poll_interval', RACHIO_TTL)
    if time.time() - _rachio_ts < rachio_ttl:
        return _rachio_schedule
    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')
        events    = []
        now_local = datetime.now()

        for device in person.get('devices', []):
            rules = device.get('scheduleRules', [])
            print(f'Rachio device {device.get("name","?")}: {len(rules)} schedule rules')

            rd_until = None
            rd_exp = device.get('rainDelayExpirationDate')
            if rd_exp and isinstance(rd_exp, (int, float)) and rd_exp > 0:
                rd_dt = datetime.fromtimestamp(rd_exp / 1000, tz=timezone.utc).astimezone().replace(tzinfo=None)
                if rd_dt > now_local:
                    rd_until = rd_dt

            _wi_skip_set, threshold_in = _rachio_wi_skip_info(device['id'], lookback_hours=48)
            forecast_by_date = _rachio_device_forecast(device['id'])

            for rule in rules:
                if not rule.get('enabled', True):
                    continue
                try:
                    h    = rule.get('startHour', 0)
                    m    = rule.get('startMinute', 0)
                    days = _rachio_days_from_job_types(rule.get('scheduleJobTypes', []))
                    duration_min = round(rule.get('totalDuration', rule.get('duration', 0)) / 60)
                    name         = rule.get('name', rule.get('externalName', 'Irrigation'))

                    for run_dt in _rachio_runs_in_window(h, m, days, hours=48):
                        evt = {
                            'fire_time':    run_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                            'name':         name,
                            'duration_min': duration_min,
                            'source':       'rachio',
                        }

                        skip_reason = None

                        if rd_until is not None and run_dt <= rd_until:
                            skip_reason = 'Skipped due to Rain'

                        if skip_reason is None:
                            forecast_in = forecast_by_date.get(run_dt.date().isoformat(), 0)
                            if forecast_in >= threshold_in:
                                skip_reason = f'Likely skipped — {forecast_in:.2f}" forecast'

                        if skip_reason:
                            evt['skip']        = True
                            evt['skip_reason'] = skip_reason
                        events.append(evt)
                except Exception as exc:
                    print(f'Rachio rule skip: {exc}')
                    continue

        events.sort(key=lambda e: e['fire_time'])
        _rachio_schedule = events
        _rachio_ts = time.time()
        skipped_count = sum(1 for e in events if e.get('skip'))
        print(f'Rachio: fetched {len(events)} upcoming events ({skipped_count} skipped)')
    except Exception as exc:
        print(f'Rachio error: {exc}')
    return _rachio_schedule
