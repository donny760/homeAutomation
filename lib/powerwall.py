import json
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

import pypowerwall

from lib.state import BASE_DIR, DB_PATH, _live, _lock
from lib.settings import get_setting, get_setting_int, get_setting_bool
from lib.events import _log_system_error, _log_success
from lib.db import write_reading, purge_old
from lib.costs import _spawn_rebuild_daily_costs, _rebuild_today, _is_refresh_due, _read_year_from_json
from lib.fetch_rates import (
    load_or_generate_holidays, HOLIDAYS_PATH, RATES_PATH, fetch_ev_tou2_rates,
)
from lib.rachio import fetch_rachio_events, evaluate_rain_skip
from lib.nest import fetch_nest_events, _nest_ensure_token, _nest_refresh_devices
from lib.pool import fetch_pool, POOL_POLL_INTERVAL
import lib.kasa as kasa
from lib.kasa import _kasa_refresh_devices, _kasa_poll_state
import lib.tuya as tuya
from lib.tuya import _tuya_refresh_devices, _tuya_poll_state

PW_EMAIL      = 'don@nsdsolutions.com'
POLL_INTERVAL = 10   # seconds between pypowerwall polls
DB_WRITE_EVERY = 30  # seconds between DB writes


def backfill_history() -> None:
    """On startup, fill gaps in the last 12 hours using Tesla cloud history.

    The API returns ~15-min interval data.  We use INSERT OR IGNORE so existing
    30-second readings are never overwritten.

    Sign convention from Tesla history API:
      solar_power   – positive = producing
      battery_power – positive = discharging, negative = charging
      grid_power    – positive = importing, negative = exporting
    home_w is derived: home = solar - battery - grid  (energy conservation)
    """
    print('Backfill: fetching last 24 h of history from Tesla cloud…')
    try:
        pw = pypowerwall.Powerwall('', cloudmode=True, email=PW_EMAIL, timeout=30, authpath=BASE_DIR)
        sites = pw.client.getsites()
        if not sites:
            print('Backfill: no sites returned.')
            return
        battery = sites[0]

        now_utc   = datetime.now(timezone.utc)
        start_utc = now_utc - timedelta(hours=24)
        end_str   = now_utc.strftime('%Y-%m-%dT%H:%M:%S.000Z')

        data = battery.get_calendar_history_data(
            kind='power',
            period='day',
            end_date=end_str,
            timezone='America/Los_Angeles',
        )

        series = (data or {}).get('time_series', [])
        if not series:
            print('Backfill: no time_series in response.')
            return

        cutoff = int(start_utc.timestamp())
        inserted = 0
        with sqlite3.connect(DB_PATH) as c:
            for row in series:
                raw_ts = row.get('timestamp', '')
                try:
                    dt = datetime.fromisoformat(raw_ts)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    ts = int(dt.timestamp())
                except ValueError:
                    continue

                if ts < cutoff:
                    continue

                solar_w   = float(row.get('solar_power',   0) or 0)
                batt_w    = float(row.get('battery_power', 0) or 0)
                grid_w    = float(row.get('grid_power',    0) or 0)
                # Tesla history: battery+ = discharging, battery- = charging, grid+ = importing
                home_w      = solar_w + batt_w + grid_w
                batt_stored = -batt_w  # flip to positive=charging, matching live poller

                cur = c.execute(
                    'INSERT OR IGNORE INTO readings VALUES (?,?,?,?,?,?)',
                    (ts, solar_w, home_w, batt_stored, grid_w, None)
                )
                inserted += cur.rowcount

        print(f'Backfill: inserted {inserted} rows ({len(series)} returned by API).')

    except Exception as exc:
        print(f'Backfill error: {exc}')


def poller() -> None:
    pw = None
    last_write = 0
    last_purge = 0
    last_cost_rebuild = 0
    last_today_rebuild = 0
    last_holidays_check = 0
    last_rates_check = 0
    last_rachio_event_poll = 0
    last_rain_skip_check = 0
    last_nest_event_poll = 0
    last_pool_poll = 0
    last_kasa_poll = 0
    last_tuya_poll = 0

    while True:
        poll_interval = get_setting_int('powerwall_poll_interval', POLL_INTERVAL)
        db_write_interval = get_setting_int('powerwall_db_write_interval', DB_WRITE_EVERY)

        if not get_setting_bool('powerwall_enabled', True):
            time.sleep(poll_interval)
            continue

        try:
            if pw is None:
                print('Connecting to Powerwall (cloud mode)…')
                pw = pypowerwall.Powerwall(
                    '', cloudmode=True, email=PW_EMAIL, timeout=30
                )
                print('Connected.')

            power = pw.power() or {}
            level = pw.level() or 0

            solar_w     = float(power.get('solar',   0) or 0)
            battery_w   = -float(power.get('battery', 0) or 0)  # API: positive=discharging; flip to positive=charging
            grid_w      = float(power.get('site',    0) or 0)  # 'site' = grid
            home_w      = float(power.get('load',    0) or 0)
            battery_pct = float(level)

            # Get operating mode via get_mode() only
            # (pw.mode is the connection type, not the operating mode)
            mode = 'self_consumption'
            try:
                val = pw.get_mode()
                if val:
                    mode = val
            except Exception:
                pass

            now = int(time.time())

            with _lock:
                _live.update({
                    'solar_w': solar_w, 'home_w': home_w,
                    'battery_w': battery_w, 'grid_w': grid_w,
                    'battery_pct': battery_pct, 'mode': mode or 'self_consumption',
                    'ts': now,
                })

            if now - last_write >= db_write_interval:
                write_reading(solar_w, home_w, battery_w, grid_w, battery_pct)
                last_write = now

            if now - last_purge >= 86400:
                purge_old()
                last_purge = now

            cost_interval = get_setting_int('cost_rebuild_days', 7) * 86400
            if now - last_cost_rebuild >= cost_interval:
                _spawn_rebuild_daily_costs()
                last_cost_rebuild = now

            if now - last_today_rebuild >= 3600:
                threading.Thread(target=_rebuild_today, daemon=True).start()
                last_today_rebuild = now

            # Holidays + Rates refresh (calendar-driven from shared start date)
            refresh_start = get_setting('refresh_start_date', '')

            # Holidays
            holidays_months = get_setting_int('holidays_poll_months', 1)
            if _is_refresh_due(refresh_start, holidays_months) and now - last_holidays_check >= 86400:
                try:
                    old_year = _read_year_from_json(HOLIDAYS_PATH)
                    load_or_generate_holidays()
                    new_year = _read_year_from_json(HOLIDAYS_PATH)
                    if old_year and new_year and new_year != old_year:
                        _log_success('holidays', 'holidays_updated',
                                     f'Holidays regenerated for {new_year}')
                    print('Holidays refreshed')
                except Exception as exc:
                    print(f'Holidays refresh error: {exc}')
                    _log_system_error('holidays', 'Holiday refresh failed', str(exc))
                last_holidays_check = now

            # Energy rates
            rates_months = get_setting_int('rates_poll_months', 1)
            if _is_refresh_due(refresh_start, rates_months) and now - last_rates_check >= 86400:
                try:
                    old_rates = {}
                    try:
                        with open(RATES_PATH, encoding='utf-8') as f:
                            old_rates = json.load(f)
                    except (FileNotFoundError, json.JSONDecodeError, OSError):
                        pass
                    page_url = get_setting('rates_page_url',
                                           'https://www.sdge.com/total-electric-rates')
                    schedule = get_setting('rate_schedule_name', 'EV-TOU')
                    new_rates = fetch_ev_tou2_rates(page_url=page_url,
                                                    schedule_name=schedule,
                                                    db_path=DB_PATH)
                    rate_keys = ['summer_on_peak', 'summer_off_peak', 'summer_super_off_peak',
                                 'winter_on_peak', 'winter_off_peak', 'winter_super_off_peak',
                                 'base_services_charge_per_day']
                    changes = []
                    for k in rate_keys:
                        old_v = old_rates.get(k)
                        new_v = new_rates.get(k)
                        if old_v is not None and new_v is not None and old_v != new_v:
                            changes.append(f'{k}: {old_v}→{new_v}')
                    if changes:
                        detail_parts = [', '.join(changes)]
                        src_url = new_rates.get('source_url')
                        if src_url:
                            detail_parts.append(src_url)
                        _log_success('rates', 'rates_updated',
                                     f'Rates updated (eff. {new_rates.get("effective_date", "?")})',
                                     detail='  '.join(detail_parts))
                except Exception as exc:
                    print(f'Rate fetch error: {exc}')
                    _log_system_error('rates', 'Energy rate refresh failed', str(exc))
                last_rates_check = now

            # Rachio event logging
            rachio_event_interval = get_setting_int('rachio_event_poll_interval', 1800)
            if now - last_rachio_event_poll >= rachio_event_interval:
                if get_setting_bool('rachio_enabled', True):
                    try:
                        fetch_rachio_events()
                    except Exception as exc:
                        print(f'Rachio event poll error: {exc}')
                        _log_system_error('rachio', 'Event poll error', str(exc))
                last_rachio_event_poll = now

            # Rain-based smart skip
            rain_skip_interval = get_setting_int('rain_skip_check_interval', 3600)
            if now - last_rain_skip_check >= rain_skip_interval:
                try:
                    evaluate_rain_skip()
                except Exception as exc:
                    print(f'Rain skip check error: {exc}')
                    _log_system_error('rachio', 'Rain skip check error', str(exc))
                last_rain_skip_check = now

            # Nest camera/doorbell events (Pub/Sub pull) + thermostat refresh
            nest_event_interval = get_setting_int('nest_poll_interval', 60)
            if now - last_nest_event_poll >= nest_event_interval:
                if get_setting_bool('nest_enabled', False):
                    try:
                        fetch_nest_events()
                    except Exception as exc:
                        print(f'Nest event poll error: {exc}')
                        _log_system_error('nest', 'Event poll error', str(exc))
                    if get_setting_bool('nest_thermostat_enabled', False):
                        try:
                            token = _nest_ensure_token()
                            if token:
                                _nest_refresh_devices(token)
                        except Exception as exc:
                            print(f'Nest thermostat poll error: {exc}')
                last_nest_event_poll = now

            # Pool equipment state polling
            pool_event_interval = get_setting_int('pool_poll_interval', POOL_POLL_INTERVAL)
            if now - last_pool_poll >= pool_event_interval:
                if get_setting_bool('pool_enabled', True):
                    try:
                        fetch_pool()
                    except Exception as exc:
                        print(f'Pool poll error: {exc}')
                        _log_system_error('pool', 'Pool poll error', str(exc))
                last_pool_poll = now

            # Kasa smart plug state polling (LAN, fast)
            kasa_poll_interval = get_setting_int('kasa_poll_interval', 10)
            if now - last_kasa_poll >= kasa_poll_interval:
                if get_setting_bool('kasa_enabled', False):
                    try:
                        # First time after enable: discover; otherwise just poll state
                        if not kasa._kasa_devices:
                            _kasa_refresh_devices()
                        elif get_setting_bool('kasa_state_poll_enabled', True):
                            _kasa_poll_state()
                    except Exception as exc:
                        print(f'Kasa poll error: {exc}')
                        _log_system_error('kasa', 'State poll error', str(exc))
                last_kasa_poll = now

            # Tuya state polling (LAN, sync socket calls)
            tuya_poll_interval = get_setting_int('tuya_poll_interval', 15)
            if now - last_tuya_poll >= tuya_poll_interval:
                if get_setting_bool('tuya_enabled', False):
                    try:
                        if not tuya._tuya_devices:
                            _tuya_refresh_devices()
                        else:
                            _tuya_poll_state()
                    except Exception as exc:
                        print(f'Tuya poll error: {exc}')
                        _log_system_error('tuya', 'State poll error', str(exc))
                last_tuya_poll = now

        except Exception as exc:
            print(f'Poller error: {type(exc).__name__}: {exc}')
            traceback.print_exc()
            _log_system_error('powerwall', 'Poller error',
                              f'{type(exc).__name__}: {exc}')
            pw = None  # force reconnect on next iteration

        time.sleep(poll_interval)
