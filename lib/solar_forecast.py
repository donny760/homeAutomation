import json
import sqlite3
import time
import urllib.request
from datetime import datetime, date, timedelta

import lib.state as state
from lib.db import connect


_sf_cache: dict  = {}
_sf_ts: float    = 0.0
_stf_cache: dict = {}
_stf_ts: float   = 0.0
SF_TTL = 3600
PEAK_RAD_WM2 = 950.0


def _peak_solar_w() -> float:
    cutoff = int((datetime.combine(date.today(), datetime.min.time())
                  - timedelta(days=14)).timestamp())
    with connect() as c:
        row = c.execute('SELECT MAX(solar_w) FROM readings WHERE timestamp >= ?',
                        (cutoff,)).fetchone()
    return float(row[0]) if row and row[0] else 8100.0


def fetch_solar_forecast() -> dict:
    global _sf_cache, _sf_ts
    now = datetime.now()
    today_str = now.date().isoformat()
    current_hour = now.hour

    if _sf_cache.get('date') == today_str and time.time() - _sf_ts < SF_TTL:
        return _sf_cache

    url = (
        'https://api.open-meteo.com/v1/forecast'
        '?latitude=33.09924225276156&longitude=-117.06278850408303'
        '&hourly=shortwave_radiation'
        '&forecast_days=1&timezone=America%2FLos_Angeles'
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())

        hourly = data.get('hourly', {})
        times = hourly.get('time', [])
        rads  = hourly.get('shortwave_radiation', [])

        peak_solar = _peak_solar_w()
        scale = peak_solar / PEAK_RAD_WM2

        new_hours = {}
        for t_str, rad in zip(times, rads):
            h = int(t_str.split('T')[1].split(':')[0])
            new_hours[h] = round(max(0, rad * scale))

        if _sf_cache.get('date') == today_str and 'hours' in _sf_cache:
            merged = dict(_sf_cache['hours'])
            for h, w in new_hours.items():
                if h >= current_hour:
                    merged[h] = w
        else:
            merged = new_hours

        _sf_cache = {'date': today_str, 'hours': merged}
        _sf_ts = time.time()

    except Exception as exc:
        print(f'Solar forecast error: {exc}')
        if not _sf_cache or _sf_cache.get('date') != today_str:
            _sf_cache = {'date': today_str, 'hours': {}}

    return _sf_cache


def fetch_tomorrow_solar_forecast() -> dict:
    global _stf_cache, _stf_ts
    tomorrow_str = (date.today() + timedelta(days=1)).isoformat()

    if _stf_cache.get('date') == tomorrow_str and time.time() - _stf_ts < SF_TTL:
        return _stf_cache

    url = (
        'https://api.open-meteo.com/v1/forecast'
        '?latitude=33.09924225276156&longitude=-117.06278850408303'
        '&hourly=shortwave_radiation'
        '&forecast_days=2&timezone=America%2FLos_Angeles'
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())

        hourly = data.get('hourly', {})
        times  = hourly.get('time', [])
        rads   = hourly.get('shortwave_radiation', [])

        peak_solar = _peak_solar_w()
        scale = peak_solar / PEAK_RAD_WM2

        hours: dict[int, int] = {}
        for t_str, rad in zip(times, rads):
            day_part, hour_part = t_str.split('T')
            if day_part != tomorrow_str:
                continue
            h = int(hour_part.split(':')[0])
            hours[h] = round(max(0, (rad or 0) * scale))

        kwh_by_hour = {h: round(w / 1000, 2) for h, w in hours.items()}
        total_kwh   = round(sum(kwh_by_hour.values()), 2)

        _stf_cache = {
            'date':        tomorrow_str,
            'hours':       hours,
            'kwh_by_hour': kwh_by_hour,
            'total_kwh':   total_kwh,
        }
        _stf_ts = time.time()

        try:
            with connect() as c:
                c.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('tomorrow_solar_kwh', ?)",
                    (str(total_kwh),)
                )
        except Exception:
            pass

    except Exception as exc:
        print(f'Tomorrow solar forecast error: {exc}')
        if _stf_cache.get('date') != tomorrow_str:
            _stf_cache = {'date': tomorrow_str, 'hours': {}, 'kwh_by_hour': {}, 'total_kwh': 0.0}

    return _stf_cache
