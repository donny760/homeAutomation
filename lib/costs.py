import json
import os
import sqlite3
import threading
from datetime import datetime, date

import lib.state as state
from lib.fetch_rates import load_rates, tou_period, RATES_PATH
from lib.settings import _load_tou_periods
from lib.db import connect


_cost_rebuild_lock = threading.Lock()


def _load_rate_history() -> list:
    with connect() as c:
        return c.execute(
            'SELECT effective_date, end_date, '
            '       summer_on_peak, summer_off_peak, summer_super_off_peak, '
            '       winter_on_peak, winter_off_peak, winter_super_off_peak, '
            '       COALESCE(base_services_charge_per_day, 0), '
            '       tou_periods_json '
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
            return result
    return None


def _is_refresh_due(start_date_str: str, interval_months: int) -> bool:
    """Check if a recurring task anchored to start_date is due today."""
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
    tomorrow = midnight + 86400

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
            rate = day_rate.get(f'{season}_{period}', 0.0)
            if kwh > 0:
                v['import_kwh'] += kwh
                v['import_cost'] += kwh * rate
            elif kwh < 0:
                v['export_kwh'] += abs(kwh)
                v['export_credit'] += abs(kwh) * rate
            v[f'{period}_kwh'] += kwh
            v[f'{period}_cost'] += kwh * rate

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
    rate_periods = _load_rate_history()
    fallback_rates = load_rates() if not rate_periods else None
    if not rate_periods and not fallback_rates:
        print('rebuild_daily_costs: no rate data available, skipping')
        return

    target_year = year or date.today().year
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
            rate = rate_info.get(f'{season}_{period}', 0.0)
            if d not in day_data:
                day_data[d] = {
                    'import_kwh': 0.0, 'export_kwh': 0.0,
                    'import_cost': 0.0, 'export_credit': 0.0,
                    'on_peak_kwh': 0.0, 'off_peak_kwh': 0.0, 'super_off_peak_kwh': 0.0,
                    'on_peak_cost': 0.0, 'off_peak_cost': 0.0, 'super_off_peak_cost': 0.0,
                }
            if kwh > 0:
                day_data[d]['import_kwh']  += kwh
                day_data[d]['import_cost'] += kwh * rate
            elif kwh < 0:
                day_data[d]['export_kwh']    += abs(kwh)
                day_data[d]['export_credit'] += abs(kwh) * rate
            day_data[d][f'{period}_kwh']  += kwh
            day_data[d][f'{period}_cost'] += kwh * rate

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


def _spawn_rebuild_daily_costs(from_date=None) -> bool:
    if not _cost_rebuild_lock.acquire(blocking=False):
        return False

    def _run():
        try:
            rebuild_daily_costs(from_date=from_date)
        finally:
            _cost_rebuild_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


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
