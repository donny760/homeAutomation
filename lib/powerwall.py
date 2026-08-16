import threading
import time
import traceback
from datetime import datetime, date, timedelta, timezone

import pypowerwall

from lib.state import BASE_DIR, DB_PATH, _live, _lock
from lib.settings import get_setting, get_setting_int, get_setting_bool, set_setting
from lib.events import _log_system_error, _log_success
from lib.db import write_reading, purge_old, connect
from lib.costs import (
    _spawn_rebuild_daily_costs, _rebuild_today, _is_refresh_due,
    _read_year_from_json, mark_costs_stale,
)
from lib.fetch_rates import (
    load_or_generate_holidays, HOLIDAYS_PATH, fetch_ev_tou2_rates,
)
from lib.rachio import fetch_rachio_events, evaluate_rain_skip
from lib.nest import fetch_nest_events, _nest_ensure_token, _nest_refresh_devices
from lib.pool import fetch_pool, POOL_POLL_INTERVAL
import lib.kasa as kasa
from lib.kasa import _kasa_refresh_devices, _kasa_poll_state
import lib.tuya as tuya
from lib.tuya import _tuya_refresh_devices, _tuya_poll_state, _tuya_cloud_poll_sensors

PW_EMAIL      = 'don@nsdsolutions.com'
POLL_INTERVAL = 10   # seconds between pypowerwall polls
DB_WRITE_EVERY = 30  # seconds between DB writes
_cloud_unreachable: bool  = False  # True once outage threshold crossed
_cloud_zero_since:  float = 0.0   # timestamp when all-zero streak started
_last_reconnect_ts: float = 0.0   # last forced reconnect during an outage (throttle)
_backfill_running         = threading.Event()  # prevents overlapping backfill threads
_last_poller_error_log: float = 0.0  # throttles the poller's catch-all error row
POLLER_ERROR_LOG_INTERVAL = 300      # seconds between logged poller errors
POLLER_ERROR_DETAIL_MAX   = 500      # chars of traceback kept per row


BACKFILL_TZ = 'America/Los_Angeles'


def backfill_history(lookback_days: int = 3) -> None:
    """Fill gaps in readings using Tesla Fleet API calendar history (5-min data).

    Iterates one local calendar day at a time over the lookback window. For each
    day it pulls the power time-series and upserts: new timestamps are inserted,
    and existing all-zero rows (poller placeholders written during a cloud outage)
    are overwritten. Genuine non-zero readings are never touched.

    The end of each day's query is capped at "now". Tesla returns NO data for a
    window that ends in the future, so the previous current-day query (which set
    end to the next midnight) silently fetched nothing for today — that was the
    bug that left same-day outage gaps permanently empty.

    Sign convention from Tesla history API:
      solar_power   – positive = producing
      battery_power – positive = discharging, negative = charging
      grid_power    – positive = importing, negative = exporting
    home_w is derived: home = solar + battery + grid  (energy conservation)
    """
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(BACKFILL_TZ)
        pw = pypowerwall.Powerwall('', fleetapi=True, email=PW_EMAIL, timeout=30, authpath=BASE_DIR)

        now_local = datetime.now(tz)
        today     = now_local.date()

        inserted   = 0
        total_rows = 0
        for back in range(lookback_days, -1, -1):
            day   = today - timedelta(days=back)
            start = datetime(day.year, day.month, day.day, 0, 0, 0, tzinfo=tz)
            end   = datetime(day.year, day.month, day.day, 23, 59, 59, tzinfo=tz)
            if end > now_local:           # never request a future-ending window
                end = now_local
            try:
                # start/end as tz-aware datetimes (DST-correct) + time_zone yields
                # 5-min resolution; the API serialises them to RFC3339 itself.
                data = pw.client.fleet.get_calendar_history(
                    kind='power',
                    time_zone=BACKFILL_TZ,
                    start=start,
                    end=end,
                )
                series      = (data or {}).get('time_series', [])
                total_rows += len(series)

                with connect() as c:
                    for row in series:
                        raw_ts = row.get('timestamp', '')
                        try:
                            dt = datetime.fromisoformat(raw_ts)
                            if dt.tzinfo is None:
                                dt = dt.replace(tzinfo=timezone.utc)
                            ts = int(dt.timestamp())
                        except ValueError:
                            _log_system_error('powerwall', 'Backfill parse error',
                                              f'Bad timestamp {raw_ts!r} on {day}')
                            continue

                        solar_w     = float(row.get('solar_power',   0) or 0)
                        batt_w      = float(row.get('battery_power', 0) or 0)
                        grid_w      = float(row.get('grid_power',    0) or 0)
                        home_w      = solar_w + batt_w + grid_w
                        batt_stored = -batt_w  # flip: positive=charging

                        # Insert new rows; overwrite existing rows only if all-zero
                        # (poller writes zeros during cloud outages — replace with real data)
                        c.execute(
                            '''INSERT INTO readings VALUES (?,?,?,?,?,?)
                               ON CONFLICT(timestamp) DO UPDATE SET
                                 solar_w=excluded.solar_w, home_w=excluded.home_w,
                                 battery_w=excluded.battery_w, grid_w=excluded.grid_w
                               WHERE readings.solar_w=0 AND readings.home_w=0
                                 AND readings.battery_w=0 AND readings.grid_w=0''',
                            (ts, solar_w, home_w, batt_stored, grid_w, None)
                        )
                        inserted += c.execute('SELECT changes()').fetchone()[0]

            except Exception as exc:
                _log_system_error('powerwall', 'Backfill day failed',
                                  f'{day}: {exc}')

            time.sleep(0.5)  # be gentle on the Fleet API rate limit

        _log_success('powerwall', 'backfill_complete',
                     f'Backfill: updated {inserted} readings ({total_rows} returned '
                     f'by API) over {lookback_days + 1} days')

    except Exception as exc:
        _log_system_error('powerwall', 'Backfill failed', str(exc))
    finally:
        _backfill_running.clear()


def trigger_backfill(lookback_days: int = 3) -> bool:
    """Manually kick off backfill_history in a background thread.

    Reuses the _backfill_running guard so it never overlaps the auto-recovery
    backfill or a previous manual run. Returns True if a run was started,
    False if one was already in progress.
    """
    if _backfill_running.is_set():
        return False
    _backfill_running.set()
    threading.Thread(target=backfill_history, args=(lookback_days,), daemon=True).start()
    return True


def poller() -> None:
    pw = None
    last_write = 0
    last_purge = 0
    last_cost_rebuild = 0
    last_today_rebuild = 0
    last_pending_check = 0
    last_holidays_check = 0
    last_rates_check = 0
    last_rachio_event_poll = 0
    last_rain_skip_check = 0
    last_nest_event_poll = 0
    last_pool_poll = 0
    last_kasa_poll = 0
    last_tuya_poll       = 0
    last_tuya_cloud_poll = 0

    while True:
        poll_interval = get_setting_int('powerwall_poll_interval', POLL_INTERVAL)
        db_write_interval = get_setting_int('powerwall_db_write_interval', DB_WRITE_EVERY)

        if not get_setting_bool('powerwall_enabled', True):
            time.sleep(poll_interval)
            continue

        try:
            global _cloud_unreachable, _cloud_zero_since, _last_reconnect_ts
            if pw is None:
                pw = pypowerwall.Powerwall(
                    '', fleetapi=True, email=PW_EMAIL, timeout=30, authpath=BASE_DIR
                )
                # Suppress the success log during outage-retry reconnects — recovery is
                # reported by the cloud_recovered message once data actually returns.
                if not _cloud_unreachable:
                    _log_success('powerwall', 'connect', 'Connected to Powerwall (Fleet API mode)')

            power = pw.power() or {}
            level = pw.level() or 0
            now_ts = time.time()
            if not any(power.values()):
                if _cloud_zero_since == 0.0:
                    _cloud_zero_since = now_ts
                elapsed = now_ts - _cloud_zero_since
                if elapsed >= 120:
                    if not _cloud_unreachable:
                        _log_system_error('powerwall', 'Tesla cloud unreachable',
                                          f'No data for {int(elapsed / 60)}+ min — forcing reconnect')
                        _cloud_unreachable = True
                    # Keep forcing reconnects (throttled) for the whole outage, not just
                    # once — each rebuild re-reads the token file and retries Tesla.
                    if now_ts - _last_reconnect_ts >= 120:
                        _last_reconnect_ts = now_ts
                        pw = None  # force reconnect on next iteration
            else:
                if _cloud_unreachable:
                    down_min = max(1, int((now_ts - _cloud_zero_since) / 60))
                    _log_success('powerwall', 'cloud_recovered',
                                 f'Tesla cloud connection restored after {down_min} min')
                    if not _backfill_running.is_set():
                        _backfill_running.set()
                        threading.Thread(target=backfill_history, daemon=True).start()
                _cloud_zero_since = 0.0
                _cloud_unreachable = False
                _last_reconnect_ts = 0.0

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

            # Daily catch-up rebuild, bounded to the last `cost_rebuild_days` days
            # so it never rescans the full year. Full-year rebuilds are reserved
            # for the explicit /api/costs/rebuild trigger.
            if now - last_cost_rebuild >= 86400:
                lookback = get_setting_int('cost_rebuild_days', 7)
                _spawn_rebuild_daily_costs(from_date=date.today() - timedelta(days=lookback))
                last_cost_rebuild = now

            if now - last_today_rebuild >= 3600:
                threading.Thread(target=_rebuild_today, daemon=True).start()
                last_today_rebuild = now

            # Retroactive rate corrections: a rate row that landed while a
            # rebuild was already running leaves a pending marker. Retry until
            # it clears, so the correction is never silently dropped.
            if now - last_pending_check >= 300:
                pending = get_setting('cost_rebuild_pending_from', '')
                if pending:
                    try:
                        _spawn_rebuild_daily_costs(from_date=date.fromisoformat(pending),
                                                   clear_pending=pending)
                    except ValueError:
                        set_setting('cost_rebuild_pending_from', '')
                last_pending_check = now

            # Holidays + Rates refresh (calendar-driven from shared start date)
            refresh_start = get_setting('refresh_start_date', '')

            # Holidays
            holidays_months = get_setting_int('holidays_poll_months', 1)
            if _is_refresh_due(refresh_start, holidays_months,
                               get_setting('holidays_last_success', '')) \
                    and now - last_holidays_check >= 86400:
                try:
                    old_year = _read_year_from_json(HOLIDAYS_PATH)
                    load_or_generate_holidays()
                    new_year = _read_year_from_json(HOLIDAYS_PATH)
                    if old_year and new_year and new_year != old_year:
                        _log_success('holidays', 'holidays_updated',
                                     f'Holidays regenerated for {new_year}')
                    set_setting('holidays_last_success', date.today().isoformat())
                except Exception as exc:
                    _log_system_error('holidays', 'Holiday refresh failed', str(exc))
                last_holidays_check = now

            # Energy rates
            rates_months = get_setting_int('rates_poll_months', 1)
            if _is_refresh_due(refresh_start, rates_months,
                               get_setting('rates_last_success', '')) \
                    and now - last_rates_check >= 86400:
                try:
                    page_url = get_setting('rates_page_url',
                                           'https://www.sdge.com/total-electric-rates')
                    schedule = get_setting('rate_schedule_name', 'EV-TOU')
                    new_rates = fetch_ev_tou2_rates(page_url=page_url,
                                                    schedule_name=schedule,
                                                    db_path=DB_PATH)
                    # Changes are detected against rate_history inside the fetch,
                    # so a failed/clobbered rates.json can't re-report the same
                    # change on every poll (it did, 5x on 2026-06-01).
                    changes = new_rates.get('_changes') or []
                    eff = new_rates.get('effective_date')
                    if changes:
                        detail_parts = [', '.join(changes)]
                        src_url = new_rates.get('source_url')
                        if src_url:
                            detail_parts.append(src_url)
                        _log_success('rates', 'rates_updated',
                                     f'Rates updated (eff. {eff or "?"})',
                                     detail='  '.join(detail_parts))
                        # Re-derive costs from the new rate's effective date so a
                        # rate change retroactively corrects the days it covers.
                        mark_costs_stale(eff)
                    # Only stamped on success, so a transient failure retries
                    # tomorrow rather than waiting for the next interval boundary.
                    set_setting('rates_last_success', date.today().isoformat())
                except Exception as exc:
                    _log_system_error('rates', 'Energy rate refresh failed', str(exc))
                last_rates_check = now

            # Rachio event logging
            rachio_event_interval = get_setting_int('rachio_event_poll_interval', 1800)
            if now - last_rachio_event_poll >= rachio_event_interval:
                if get_setting_bool('rachio_enabled', True):
                    try:
                        fetch_rachio_events()
                    except Exception as exc:
                        _log_system_error('rachio', 'Event poll error', str(exc))
                last_rachio_event_poll = now

            # Rain-based smart skip
            rain_skip_interval = get_setting_int('rain_skip_check_interval', 3600)
            if now - last_rain_skip_check >= rain_skip_interval:
                try:
                    evaluate_rain_skip()
                except Exception as exc:
                    _log_system_error('rachio', 'Rain skip check error', str(exc))
                last_rain_skip_check = now

            # Nest camera/doorbell events (Pub/Sub pull) + thermostat refresh
            nest_event_interval = get_setting_int('nest_poll_interval', 60)
            if now - last_nest_event_poll >= nest_event_interval:
                if get_setting_bool('nest_enabled', False):
                    try:
                        fetch_nest_events()
                    except Exception as exc:
                        _log_system_error('nest', 'Event poll error', str(exc))
                    if get_setting_bool('nest_thermostat_enabled', False):
                        try:
                            token = _nest_ensure_token()
                            if token:
                                _nest_refresh_devices(token)
                        except Exception as exc:
                            _log_system_error('nest', 'Thermostat poll error', str(exc))
                last_nest_event_poll = now

            # Pool equipment state polling
            pool_event_interval = get_setting_int('pool_poll_interval', POOL_POLL_INTERVAL)
            if now - last_pool_poll >= pool_event_interval:
                if get_setting_bool('pool_enabled', True):
                    try:
                        fetch_pool()
                    except Exception as exc:
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
                        _log_system_error('tuya', 'State poll error', str(exc))
                last_tuya_poll = now

            # Tuya cloud polling for battery sensors (wsdcg)
            tuya_cloud_interval = get_setting_int('tuya_cloud_poll_interval', 300)
            if now - last_tuya_cloud_poll >= tuya_cloud_interval:
                if get_setting_bool('tuya_enabled', False):
                    try:
                        _tuya_cloud_poll_sensors()
                    except Exception as exc:
                        _log_system_error('tuya', 'Cloud poll error', str(exc))
                    last_tuya_cloud_poll = now

        except Exception as exc:
            # Throttled: this fires every poll_interval (10s) while a failure
            # persists — a dead Fleet token would otherwise write ~8,640 rows and
            # >10 MB of tracebacks a day into event_log, which is never purged.
            global _last_poller_error_log
            if time.time() - _last_poller_error_log > POLLER_ERROR_LOG_INTERVAL:
                detail = f'{type(exc).__name__}: {exc}\n{traceback.format_exc()}'
                _log_system_error('powerwall', 'Poller error',
                                  detail[:POLLER_ERROR_DETAIL_MAX])
                _last_poller_error_log = time.time()
            pw = None  # force reconnect on next iteration

        time.sleep(poll_interval)
