"""
One-time historical backfill from Tesla cloud API.
Populates powerwall.db readings back to 2026-01-01.
Run: py backfill.py
"""
import os
import sqlite3
from datetime import datetime, timezone

import pypowerwall

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'powerwall.db')
START    = datetime(2026, 1, 1, tzinfo=timezone.utc)
PW_EMAIL = 'don@nsdsolutions.com'


def backfill():
    print('Connecting to Powerwall cloud...')
    pw = pypowerwall.Powerwall('', cloudmode=True,
                               email=PW_EMAIL,
                               timeout=60, authpath=BASE_DIR)
    print('Connected. Fetching history month by month...')

    sites = pw.client.getsites()
    if not sites:
        print('No sites returned — cannot backfill.')
        return 0
    battery = sites[0]

    inserted = 0
    skipped  = 0
    cutoff   = int(START.timestamp())

    conn = sqlite3.connect(DB_PATH)
    try:
        now     = datetime.now(timezone.utc)
        current = START

        while current <= now:
            # Use first day of next month as end_date so period='month' covers the full month
            year_next  = current.year + (current.month // 12)
            month_next = (current.month % 12) + 1
            end_dt     = datetime(year_next, month_next, 1, tzinfo=timezone.utc)
            end_str    = end_dt.strftime('%Y-%m-%dT%H:%M:%S.000Z')
            month_lbl  = current.strftime('%Y-%m')
            try:
                data   = battery.get_calendar_history_data(
                    kind='power',
                    period='month',
                    end_date=end_str,
                    timezone='America/Los_Angeles',
                )
                series = (data or {}).get('time_series', [])
            except Exception as e:
                print(f'  {month_lbl}: error fetching — {e}')
                series = []

            month_inserted = 0
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

                solar_w     = float(row.get('solar_power',   0) or 0)
                batt_w      = float(row.get('battery_power', 0) or 0)
                grid_w      = float(row.get('grid_power',    0) or 0)
                home_w      = solar_w + batt_w + grid_w
                batt_stored = -batt_w      # flip: positive = charging

                cur = conn.execute(
                    'INSERT OR IGNORE INTO readings '
                    '(timestamp, solar_w, home_w, battery_w, grid_w, battery_pct) '
                    'VALUES (?,?,?,?,?,?)',
                    (ts, solar_w, home_w, batt_stored, grid_w,
                     row.get('percentage', None))
                )
                if cur.rowcount:
                    inserted += 1
                    month_inserted += 1
                else:
                    skipped += 1

            print(f'  {month_lbl}: {len(series)} rows returned, {month_inserted} inserted')

            current = end_dt  # already computed as first day of next month

        conn.commit()
    finally:
        conn.close()

    print(f'\nBackfill complete: {inserted} inserted, {skipped} already existed')

    # Trigger cost rebuild after backfill
    try:
        from server import rebuild_daily_costs
        print('Rebuilding daily costs...')
        rebuild_daily_costs()
        print('Daily costs rebuilt.')
    except Exception as e:
        print(f'Cost rebuild skipped (run server.py first): {e}')

    return inserted


if __name__ == '__main__':
    backfill()
