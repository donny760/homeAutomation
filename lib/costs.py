import json
import os
import sqlite3
import threading
from datetime import datetime, date, timedelta

import lib.state as state
from lib.fetch_rates import load_rates, tou_period, RATES_PATH
from lib.settings import _load_tou_periods, get_setting, set_setting
from lib.db import connect, _fetch_rows_range
from lib.events import _log_system_error


_cost_rebuild_lock = threading.Lock()

# Cached month-to-date savings for every day *before* today. Those days can no
# longer change, so the full scan they need runs once a day instead of on every
# /api/live poll. See month_savings_prior_days().
_month_cache: dict = {'key': None, 'value': 0.0}


def _load_rate_history() -> list:
    with connect() as c:
        return c.execute(
            'SELECT effective_date, end_date, '
            '       summer_on_peak, summer_off_peak, summer_super_off_peak, '
            '       winter_on_peak, winter_off_peak, winter_super_off_peak, '
            '       COALESCE(base_services_charge_per_day, 0), '
            '       tou_periods_json, '
            '       summer_on_peak_export, summer_off_peak_export, '
            '       summer_super_off_peak_export, winter_on_peak_export, '
            '       winter_off_peak_export, winter_super_off_peak_export '
            'FROM rate_history ORDER BY effective_date'
        ).fetchall()


def _rate_for_date(rate_periods, d_iso: str) -> dict | None:
    for row in reversed(rate_periods):
        eff = row[0]
        if d_iso >= eff:
            result = {
                'summer_on_peak': row[2], 'summer_off_peak': row[3],
                'summer_super_off_peak': row[4],
                'winter_on_peak': row[5], 'winter_off_peak': row[6],
                'winter_super_off_peak': row[7],
                'base_services_charge_per_day': row[8] if len(row) > 8 else 0,
            }
            tou_json = row[9] if len(row) > 9 else None
            if tou_json:
                try:
                    result['_tou_periods'] = json.loads(tou_json)
                except (json.JSONDecodeError, TypeError):
                    pass
            # Length-guarded: lib/ai_insights.py builds its own 9-column tuple
            # and passes it here, so anything past index 8 must stay optional.
            if len(row) > 15:
                result.update({
                    'summer_on_peak_export': row[10],
                    'summer_off_peak_export': row[11],
                    'summer_super_off_peak_export': row[12],
                    'winter_on_peak_export': row[13],
                    'winter_off_peak_export': row[14],
                    'winter_super_off_peak_export': row[15],
                })
            return result
    return None


def _period_rates(rate_info: dict, season: str, period: str) -> tuple:
    """(import_rate, export_rate) for a TOU period.

    Export falls back to the retail import rate when no export rate is stored —
    NEM 2.0 credits exports at full retail. Populate the *_export columns on
    rate_history when the account moves to NBT.
    """
    imp = rate_info.get(f'{season}_{period}', 0.0)
    exp = rate_info.get(f'{season}_{period}_export')
    return imp, (imp if exp is None else exp)


def _is_refresh_due(start_date_str: str, interval_months: int,
                    last_run: str = '') -> bool:
    """Check if a recurring task anchored to start_date is due today.

    `last_run` is the ISO date of the last successful run; with it the interval
    is actually enforced (without it, `today >= last_due` is permanently true
    once the first boundary passes). Defaulted so callers that don't track a
    last-run date keep the previous behaviour.
    """
    if not start_date_str:
        return True
    try:
        start = date.fromisoformat(start_date_str)
    except ValueError:
        return True
    today = date.today()
    if today < start:
        return False
    if interval_months <= 0:
        return True
    months_elapsed = (today.year - start.year) * 12 + (today.month - start.month)
    intervals_passed = months_elapsed // interval_months
    total_months = (start.month - 1) + intervals_passed * interval_months
    due_year = start.year + total_months // 12
    due_month = total_months % 12 + 1
    due_day = min(start.day, 28)
    last_due = date(due_year, due_month, due_day)
    if last_run:
        try:
            return date.fromisoformat(last_run[:10]) < last_due
        except ValueError:
            return True
    return today >= last_due


def _read_year_from_json(path):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f).get('year')
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _backfill_rates_event_url() -> None:
    try:
        if not os.path.exists(RATES_PATH):
            return
        with open(RATES_PATH) as f:
            src_url = json.load(f).get('source_url')
        if not src_url:
            return
        with connect() as c:
            c.execute(
                "UPDATE event_log SET detail = detail || '  ' || ? "
                "WHERE system='rates' AND event_type='rates_updated' "
                "AND (detail IS NULL OR detail NOT LIKE '%http%')",
                (src_url,)
            )
    except Exception:
        pass


def _rebuild_today() -> None:
    # Serialize with full rebuilds via _cost_rebuild_lock — both write today's
    # daily_costs row and contend on the WAL write lock. If a full rebuild is
    # already running it covers today too, so skip rather than block.
    if not _cost_rebuild_lock.acquire(blocking=False):
        return
    try:
        _rebuild_today_locked()
    finally:
        _cost_rebuild_lock.release()


def _rebuild_today_locked() -> None:
    today_dt = date.today()
    today_str = today_dt.isoformat()
    midnight = int(datetime(today_dt.year, today_dt.month, today_dt.day).timestamp())
    # Next local midnight, not midnight+24h: on the two DST transition days the
    # local day is 23 or 25 hours, so a fixed 86400 window drops an hour of
    # today's readings or absorbs an hour of tomorrow's. Mirrors db.day_rows().
    tomorrow = int(datetime.combine(today_dt + timedelta(days=1),
                                    datetime.min.time()).timestamp())

    rate_periods = _load_rate_history()
    fallback_rates = load_rates() if not rate_periods else None
    if not rate_periods and not fallback_rates:
        return

    fallback_tou = _load_tou_periods()
    day_rate = (_rate_for_date(rate_periods, today_str) if rate_periods else None) or fallback_rates or {}
    tou_cfg = day_rate.get('_tou_periods') or fallback_tou

    with connect() as c:
        rows = c.execute(
            'SELECT timestamp, grid_w FROM readings '
            'WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp',
            (midnight, tomorrow)
        ).fetchall()

        v = {
            'import_kwh': 0.0, 'export_kwh': 0.0,
            'import_cost': 0.0, 'export_credit': 0.0,
            'on_peak_kwh': 0.0, 'off_peak_kwh': 0.0, 'super_off_peak_kwh': 0.0,
            'on_peak_cost': 0.0, 'off_peak_cost': 0.0, 'super_off_peak_cost': 0.0,
        }
        for i in range(1, len(rows)):
            ts0, g0 = rows[i - 1]
            ts1, g1 = rows[i]
            dt_h = (ts1 - ts0) / 3600
            if dt_h > 1:
                continue
            dt = datetime.fromtimestamp(ts1)
            avg_grid = ((g0 or 0) + (g1 or 0)) / 2
            kwh = avg_grid * dt_h / 1000
            season, period = tou_period(dt, tou_cfg)
            imp, exp = _period_rates(day_rate, season, period)
            if kwh > 0:
                v['import_kwh'] += kwh
                v['import_cost'] += kwh * imp
                v[f'{period}_cost'] += kwh * imp
            elif kwh < 0:
                v['export_kwh'] += abs(kwh)
                v['export_credit'] += abs(kwh) * exp
                v[f'{period}_cost'] += kwh * exp   # kwh negative -> credit
            v[f'{period}_kwh'] += kwh

        c.execute(
            'INSERT OR REPLACE INTO daily_costs '
            '(date, import_kwh, export_kwh, import_cost, export_credit, '
            ' on_peak_kwh, off_peak_kwh, super_off_peak_kwh, '
            ' on_peak_cost, off_peak_cost, super_off_peak_cost) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
            (today_str,
             round(v['import_kwh'], 4), round(v['export_kwh'], 4),
             round(v['import_cost'], 4), round(v['export_credit'], 4),
             round(v['on_peak_kwh'], 4), round(v['off_peak_kwh'], 4), round(v['super_off_peak_kwh'], 4),
             round(v['on_peak_cost'], 4), round(v['off_peak_cost'], 4), round(v['super_off_peak_cost'], 4))
        )


def rebuild_daily_costs(year: int = None, from_date=None) -> None:
    """Rebuild daily_costs from readings.

    With `year`, rebuilds that year only (lib/backfill.py). Otherwise spans
    from_date's year through the current year — a rate change effective in a
    prior year has to be able to correct that year.
    """
    if year is not None:
        _rebuild_year(year, from_date)
        return
    end_year = date.today().year
    start_year = from_date.year if from_date else end_year
    for y in range(start_year, end_year + 1):
        _rebuild_year(y, from_date)


def _rebuild_year(target_year: int, from_date=None) -> None:
    rate_periods = _load_rate_history()
    fallback_rates = load_rates() if not rate_periods else None
    if not rate_periods and not fallback_rates:
        print('rebuild_daily_costs: no rate data available, skipping')
        return

    jan1 = int(datetime(target_year, 1, 1).timestamp())
    dec31_end = int(datetime(target_year + 1, 1, 1).timestamp())
    start_ts = max(jan1, int(datetime(from_date.year, from_date.month, from_date.day).timestamp())) \
               if from_date else jan1

    fallback_tou = _load_tou_periods()

    with connect() as c:
        rows = c.execute(
            'SELECT timestamp, grid_w FROM readings '
            'WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp',
            (start_ts, dec31_end)
        ).fetchall()

        day_data: dict = {}
        _rate_cache: dict = {}
        for i in range(1, len(rows)):
            ts0, g0 = rows[i - 1]
            ts1, g1 = rows[i]
            dt_h = (ts1 - ts0) / 3600
            if dt_h > 1:
                continue
            dt   = datetime.fromtimestamp(ts1)
            d    = dt.date().isoformat()
            avg_grid = ((g0 or 0) + (g1 or 0)) / 2
            kwh  = avg_grid * dt_h / 1000
            if d not in _rate_cache:
                if rate_periods:
                    _rate_cache[d] = _rate_for_date(rate_periods, d) or fallback_rates or {}
                else:
                    _rate_cache[d] = fallback_rates or {}
            rate_info = _rate_cache[d]
            tou_cfg = rate_info.get('_tou_periods') or fallback_tou
            season, period = tou_period(dt, tou_cfg)
            imp, exp = _period_rates(rate_info, season, period)
            if d not in day_data:
                day_data[d] = {
                    'import_kwh': 0.0, 'export_kwh': 0.0,
                    'import_cost': 0.0, 'export_credit': 0.0,
                    'on_peak_kwh': 0.0, 'off_peak_kwh': 0.0, 'super_off_peak_kwh': 0.0,
                    'on_peak_cost': 0.0, 'off_peak_cost': 0.0, 'super_off_peak_cost': 0.0,
                }
            if kwh > 0:
                day_data[d]['import_kwh']  += kwh
                day_data[d]['import_cost'] += kwh * imp
                day_data[d][f'{period}_cost'] += kwh * imp
            elif kwh < 0:
                day_data[d]['export_kwh']    += abs(kwh)
                day_data[d]['export_credit'] += abs(kwh) * exp
                day_data[d][f'{period}_cost'] += kwh * exp   # kwh negative -> credit
            day_data[d][f'{period}_kwh']  += kwh

        for d, v in day_data.items():
            c.execute(
                'INSERT OR REPLACE INTO daily_costs '
                '(date, import_kwh, export_kwh, import_cost, export_credit, '
                ' on_peak_kwh, off_peak_kwh, super_off_peak_kwh, '
                ' on_peak_cost, off_peak_cost, super_off_peak_cost) '
                'VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (d, round(v['import_kwh'], 4), round(v['export_kwh'], 4),
                 round(v['import_cost'], 4), round(v['export_credit'], 4),
                 round(v['on_peak_kwh'], 4), round(v['off_peak_kwh'], 4),
                 round(v['super_off_peak_kwh'], 4),
                 round(v['on_peak_cost'], 4), round(v['off_peak_cost'], 4),
                 round(v['super_off_peak_cost'], 4))
            )

    print(f'rebuild_daily_costs: {len(day_data)} days written for {target_year}')


def _spawn_rebuild_daily_costs(from_date=None, clear_pending: str = None) -> bool:
    if not _cost_rebuild_lock.acquire(blocking=False):
        return False   # any pending marker survives; the poller retries

    def _run():
        try:
            rebuild_daily_costs(from_date=from_date)
            # Compare-and-clear: if an earlier correction landed while this ran,
            # the marker now holds that earlier date and must NOT be cleared.
            if clear_pending and get_setting('cost_rebuild_pending_from', '') == clear_pending:
                set_setting('cost_rebuild_pending_from', '')
        except Exception as exc:
            _log_system_error('costs', 'Cost rebuild failed', str(exc))
        finally:
            _cost_rebuild_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


def mark_costs_stale(from_date_iso: str) -> None:
    """Record that daily_costs from *from_date_iso* forward are stale, then try
    to rebuild now. The marker keeps the earliest pending date, so a rebuild
    skipped by the non-blocking lock is retried by the poller instead of lost."""
    if not from_date_iso:
        return
    pending = get_setting('cost_rebuild_pending_from', '') or ''
    if not pending or from_date_iso < pending:
        pending = from_date_iso
        set_setting('cost_rebuild_pending_from', pending)
    try:
        start = date.fromisoformat(pending)
    except ValueError:
        set_setting('cost_rebuild_pending_from', '')
        return
    _spawn_rebuild_daily_costs(from_date=start, clear_pending=pending)


def _first_reading_ts(since_ts: int):
    """Timestamp of the first reading at or after since_ts, or None."""
    with connect() as c:
        row = c.execute(
            'SELECT timestamp FROM readings WHERE timestamp >= ? '
            'ORDER BY timestamp LIMIT 1',
            (since_ts,)
        ).fetchone()
    return row[0] if row else None


def month_savings_prior_days() -> float:
    """Savings for the 1st through end of yesterday, cached for the day.

    /api/live used to recompute the whole month on every 10s poll, which grew to
    a ~89k-row Python loop by month-end and starved the poller thread. Prior days
    are immutable, so they are scanned once and cached; the caller adds today's
    figure, which it already computes from today_rows().

    calc_stats() sums *intervals between consecutive rows*, so splitting the
    month in two would drop the pair that straddles midnight. The first reading
    of today is therefore appended to the prior-days rows, which puts that pair
    on this side of the split and leaves the total identical to a single
    calc_stats(month_rows()) call. That row's timestamp is part of the cache key
    so the value is recomputed once, early on, when it first appears.
    """
    today = date.today()
    midnight = int(datetime.combine(today, datetime.min.time()).timestamp())
    boundary_ts = _first_reading_ts(midnight)

    key = (today, boundary_ts)
    if _month_cache['key'] == key:
        return _month_cache['value']

    start = int(datetime(today.year, today.month, 1).timestamp())
    rows = _fetch_rows_range(start, midnight)
    if boundary_ts is not None:
        rows += _fetch_rows_range(boundary_ts, boundary_ts + 1)

    _, savings, _, _ = calc_stats(rows)
    _month_cache.update(key=key, value=savings)
    return savings


def calc_stats(rows: list) -> tuple:
    rate_periods = _load_rate_history()
    fallback_rates = load_rates() if not rate_periods else None
    fallback_tou = _load_tou_periods()
    _rc: dict = {}

    solar_kwh = home_kwh = grid_import_kwh = savings = 0.0
    for i in range(1, len(rows)):
        dt_h = (rows[i][0] - rows[i-1][0]) / 3600
        solar_w = max(0.0, rows[i][1] or 0)
        home_w  = max(0.0, rows[i][2] or 0)
        grid_w  = rows[i][4] or 0
        dt      = datetime.fromtimestamp(rows[i][0])
        d       = dt.date().isoformat()
        if d not in _rc:
            _rc[d] = (_rate_for_date(rate_periods, d) or fallback_rates or {}) if rate_periods else (fallback_rates or {})
        tou_cfg = _rc[d].get('_tou_periods') or fallback_tou
        season, period = tou_period(dt, tou_cfg)
        rate = _rc[d].get(f'{season}_{period}', 0.0)

        solar_kwh      += solar_w * dt_h / 1000
        home_kwh       += home_w  * dt_h / 1000
        gi              = max(0.0, grid_w) * dt_h / 1000
        grid_import_kwh += gi
        savings         += max(0.0, home_w * dt_h / 1000 - gi) * rate

    self_suff = 0.0
    if home_kwh > 0:
        self_suff = min(100.0, max(0.0, (home_kwh - grid_import_kwh) / home_kwh * 100))

    return solar_kwh, savings, self_suff, grid_import_kwh
