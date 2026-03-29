"""
One-time historical backfill from Tesla cloud API.
Populates powerwall.db readings back to 2026-01-01.
Run: py backfill.py
"""
import os
import sqlite3
from datetime import datetime, timezone, timedelta

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
    print('Connected. Fetching history day by day...')

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
        today   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        current = START

        while current <= today:
            next_day = current + timedelta(days=1)
            end_str  = next_day.strftime('%Y-%m-%dT%H:%M:%S.000Z')
            day_lbl  = current.strftime('%Y-%m-%d')

            try:
                data   = battery.get_calendar_history_data(
                    kind='power',
                    period='day',
                    end_date=end_str,
                    timezone='America/Los_Angeles',
                )
                series = (data or {}).get('time_series', [])
            except Exception as e:
                print(f'  {day_lbl}: error fetching — {e}')
                series = []

            day_inserted = 0
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
                    day_inserted += 1
                else:
                    skipped += 1

            print(f'  {day_lbl}: {len(series)} rows returned, {day_inserted} inserted')
            current = next_day

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
