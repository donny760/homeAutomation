import json
import time
import urllib.request

from lib.settings import get_setting_int
from lib.rachio import get_device_coords
from lib.events import _log_system_error


_wx_cache: dict = {}
_wx_ts: float   = 0.0
WX_TTL = 600

WMO = {
    0: 'Clear', 1: 'Mainly Clear', 2: 'Partly Cloudy', 3: 'Overcast',
    45: 'Foggy', 48: 'Icy Fog',
    51: 'Light Drizzle', 53: 'Drizzle', 55: 'Heavy Drizzle',
    61: 'Light Rain', 63: 'Rain', 65: 'Heavy Rain',
    71: 'Light Snow', 73: 'Snow', 75: 'Heavy Snow',
    80: 'Rain Showers', 81: 'Showers', 82: 'Heavy Showers',
    95: 'Thunderstorm', 96: 'Thunderstorm', 99: 'Thunderstorm',
}


def fetch_weather() -> dict:
    global _wx_cache, _wx_ts
    if time.time() - _wx_ts < WX_TTL:
        return _wx_cache

    lookback = get_setting_int('rain_lookback_days', 5)
    lat, lng = get_device_coords()
    url = (
        'https://api.open-meteo.com/v1/forecast'
        f'?latitude={lat}&longitude={lng}'
        '&current=temperature_2m,relative_humidity_2m,weathercode,windspeed_10m,winddirection_10m,is_day'
        '&daily=precipitation_sum,cloudcover_mean'
        f'&past_days={lookback}'
        '&forecast_days=3&timezone=America%2FLos_Angeles'
    )
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            data = json.loads(r.read())
        cw    = data.get('current', {})
        daily = data.get('daily', {})
        dates    = daily.get('time', [])
        precip   = daily.get('precipitation_sum', [])
        clouds   = daily.get('cloudcover_mean', [])

        n = len(dates)
        today_idx    = lookback if n > lookback else None
        tomorrow_idx = lookback + 1 if n > lookback + 1 else None

        clouds_tm = clouds[tomorrow_idx] if tomorrow_idx is not None else None
        rain_tm   = precip[tomorrow_idx] if tomorrow_idx is not None else None

        rain_history = []
        for i, (d, mm) in enumerate(zip(dates, precip)):
            if today_idx is not None and i <= today_idx:
                rain_history.append({'date': d, 'mm': mm or 0})

        rain_forecast = {}
        if today_idx is not None:
            for i in range(today_idx, n):
                rain_forecast[dates[i]] = precip[i] or 0

        _wx_cache = {
            'temp_f':          round(cw.get('temperature_2m', 0) * 9 / 5 + 32, 1),
            'humidity':        cw.get('relative_humidity_2m'),
            'desc':            WMO.get(cw.get('weathercode', 0), ''),
            'weathercode':     cw.get('weathercode', 0),
            'tomorrow_cloud':  clouds_tm,
            'tomorrow_rain':   rain_tm,
            'bad_forecast':    (clouds_tm or 0) > 60 or (rain_tm or 0) > 1,
            'rain_history':    rain_history,
            'rain_forecast':   rain_forecast,
        }

        try:
            aqi_url = (
                'https://air-quality-api.open-meteo.com/v1/air-quality'
                f'?latitude={lat}&longitude={lng}'
                '&current=us_aqi&timezone=America%2FLos_Angeles'
            )
            with urllib.request.urlopen(aqi_url, timeout=10) as aq:
                aqi_data = json.loads(aq.read())
            _wx_cache['aqi'] = aqi_data.get('current', {}).get('us_aqi')
        except Exception:
            _wx_cache['aqi'] = None

        _wx_ts = time.time()
    except Exception as exc:
        print(f'Weather error: {exc}')
        _log_system_error('weather', 'Weather fetch failed', str(exc))
        if not _wx_cache:
            _wx_cache = {}

    return _wx_cache
