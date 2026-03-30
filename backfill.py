"""
Historical backfill from Tesla cloud API.
Imports power + SOE (battery %) data, rate history from SDG&E PDFs,
and rebuilds daily_costs.

Run: py backfill.py              # backfill from 2025-01-01
     py backfill.py 2024-06-01   # custom start date
"""
import os
import sys
import sqlite3
import json
import re
from datetime import datetime, timezone, timedelta, date

import pypowerwall
import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH  = os.path.join(BASE_DIR, 'powerwall.db')
DEFAULT_START = datetime(2025, 1, 1, tzinfo=timezone.utc)
PW_EMAIL = 'don@nsdsolutions.com'


def backfill_readings(start=None):
    """Import power + SOE data from Tesla cloud API."""
    start = start or DEFAULT_START
    print(f'Connecting to Powerwall cloud...')
    pw = pypowerwall.Powerwall('', cloudmode=True,
                               email=PW_EMAIL,
                               timeout=60, authpath=BASE_DIR)
    print('Connected.')

    sites = pw.client.getsites()
    if not sites:
        print('No sites returned — cannot backfill.')
        return 0
    battery = sites[0]

    inserted = 0
    skipped  = 0
    cutoff   = int(start.timestamp())

    conn = sqlite3.connect(DB_PATH)
    try:
        today   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
        current = start

        while current <= today:
            day_lbl  = current.strftime('%Y-%m-%d')
            # end_date at midnight UTC of next day to get full 24h of data
            end_str  = f'{(current + timedelta(days=1)).strftime("%Y-%m-%d")}T06:59:59.000Z'

            # ── Power data ──
            try:
                data   = battery.get_calendar_history_data(
                    kind='power', period='day',
                    end_date=end_str, timezone='America/Los_Angeles',
                )
                series = (data or {}).get('time_series', [])
            except Exception as e:
                print(f'  {day_lbl}: power error — {e}')
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
                batt_stored = -batt_w  # flip: positive = charging

                cur = conn.execute(
                    'INSERT OR IGNORE INTO readings '
                    '(timestamp, solar_w, home_w, battery_w, grid_w, battery_pct) '
                    'VALUES (?,?,?,?,?,?)',
                    (ts, solar_w, home_w, batt_stored, grid_w, None)
                )
                if cur.rowcount:
                    inserted += 1
                    day_inserted += 1
                else:
                    skipped += 1

            # ── SOE (battery %) data ──
            try:
                soe_data = battery.get_calendar_history_data(
                    kind='soe', period='day',
                    end_date=end_str, timezone='America/Los_Angeles',
                )
                soe_series = (soe_data or {}).get('time_series', [])
            except Exception as e:
                print(f'  {day_lbl}: soe error — {e}')
                soe_series = []

            for row in soe_series:
                raw_ts = row.get('timestamp', '')
                try:
                    dt = datetime.fromisoformat(raw_ts)
                    ts = int(dt.timestamp())
                except ValueError:
                    continue
                soe = row.get('soe')
                if soe is not None:
                    conn.execute(
                        'INSERT OR IGNORE INTO readings '
                        '(timestamp, solar_w, home_w, battery_w, grid_w, battery_pct) '
                        'VALUES (?, NULL, NULL, NULL, NULL, ?)', (ts, soe))
                    conn.execute(
                        'UPDATE readings SET battery_pct = ? '
                        'WHERE timestamp = ? AND battery_pct IS NULL', (soe, ts))

            pwr_count = len(series)
            soe_count = len(soe_series)
            print(f'  {day_lbl}: power={pwr_count} soe={soe_count} inserted={day_inserted}')

            # Commit every month
            if current.day == 1:
                conn.commit()

            current += timedelta(days=1)

        conn.commit()
    finally:
        conn.close()

    print(f'\nReadings backfill complete: {inserted} inserted, {skipped} already existed')
    return inserted


def backfill_rate_history():
    """Scrape SDG&E rates page and import all historical EV-TOU-2 rate PDFs."""
    sys.path.insert(0, BASE_DIR)
    from fetch_rates import _parse_ev_tou2_pdf

    print('\nFetching rate history from SDG&E...')
    resp = requests.get('https://www.sdge.com/total-electric-rates', timeout=30)
    resp.raise_for_status()
    html = resp.text

    # Find all EV-TOU PDF links (exclude GF, TOU-5, CARE, MB, P variants)
    pattern = re.compile(
        r'<a[^>]+href="([^"]+\.pdf)"[^>]*>'
        r'([^<]*Schedule EV-TOU (?:and|&amp;|\&) EV-TOU-2[^<]*)</a>',
        re.IGNORECASE
    )
    matches = pattern.findall(html)
    print(f'Found {len(matches)} EV-TOU-2 rate PDFs')

    conn = sqlite3.connect(DB_PATH)
    imported = 0

    for url, label in matches:
        # Parse effective date from label
        date_match = re.match(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})', label.strip())
        if not date_match:
            continue
        mo, day, yr = date_match.groups()
        yr = int(yr) if len(yr) == 4 else 2000 + int(yr)
        eff_date = f'{yr}-{int(mo):02d}-{int(day):02d}'

        # Only import 2024+ (covers all of 2025 and 2026)
        if eff_date < '2024-01-01':
            continue

        # Parse end date from label (e.g., "10/1/25 - 12/31/25")
        end_match = re.search(r'[\-–]\s*(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})', label)
        end_date = None
        if end_match and 'current' not in label.lower():
            emo, eday, eyr = end_match.groups()
            eyr = int(eyr) if len(eyr) == 4 else 2000 + int(eyr)
            end_date = f'{eyr}-{int(emo):02d}-{int(eday):02d}'

        # Ensure absolute URL
        if url.startswith('/'):
            url = f'https://www.sdge.com{url}'

        try:
            r = requests.get(url, timeout=30)
            r.raise_for_status()
            rates = _parse_ev_tou2_pdf(r.content)
            if len(rates) < 6:
                print(f'  {eff_date}: incomplete rates, skipping')
                continue

            conn.execute(
                'INSERT OR REPLACE INTO rate_history '
                '(effective_date, end_date, summer_on_peak, summer_off_peak, '
                'summer_super_off_peak, winter_on_peak, winter_off_peak, '
                'winter_super_off_peak, source_url, fetched_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,datetime("now"))',
                (eff_date, end_date,
                 rates.get('summer_on_peak'), rates.get('summer_off_peak'),
                 rates.get('summer_super_off_peak'),
                 rates.get('winter_on_peak'), rates.get('winter_off_peak'),
                 rates.get('winter_super_off_peak'), url)
            )
            imported += 1
            print(f'  {eff_date} to {end_date or "current"}: OK')
        except Exception as e:
            print(f'  {eff_date}: error — {e}')

    conn.commit()
    conn.close()
    print(f'Rate history: {imported} periods imported')
    return imported


def rebuild_costs():
    """Rebuild daily_costs for all years with data."""
    sys.path.insert(0, BASE_DIR)
    try:
        from server import rebuild_daily_costs
        conn = sqlite3.connect(DB_PATH)
        years = conn.execute(
            'SELECT DISTINCT CAST(strftime("%Y", datetime(timestamp, "unixepoch")) AS INTEGER) '
            'FROM readings ORDER BY 1'
        ).fetchall()
        conn.close()

        for (year,) in years:
            print(f'Rebuilding daily costs for {year}...')
            rebuild_daily_costs(year=year)
        print('Daily costs rebuilt.')
    except Exception as e:
        print(f'Cost rebuild failed: {e}')
        print('You may need to restart server.py first, then run: py -c "from server import rebuild_daily_costs; rebuild_daily_costs(2025); rebuild_daily_costs(2026)"')


def main():
    start = DEFAULT_START
    if len(sys.argv) > 1:
        try:
            start = datetime.fromisoformat(sys.argv[1]).replace(tzinfo=timezone.utc)
        except ValueError:
            print(f'Invalid date: {sys.argv[1]}. Use YYYY-MM-DD format.')
            sys.exit(1)

    print(f'=== Backfill starting from {start.strftime("%Y-%m-%d")} ===\n')

    backfill_readings(start)
    backfill_rate_history()
    rebuild_costs()

    print('\n=== Backfill complete ===')


if __name__ == '__main__':
    main()
