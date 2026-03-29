"""
abode_import.py — One-time Abode activity CSV importer.

Run: py abode_import.py abode_activity.csv

Abode's website activity log can be exported as CSV from the Abode web app.
The CSV column names are undocumented — this script prints them on first run
so you can verify/adjust the field name mappings before committing any data.

To do a dry run without writing: py abode_import.py abode_activity.csv --dry-run
"""

import csv
import os
import sqlite3
import sys
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'powerwall.db')

ABODE_TYPE_MAP = {
    'Closed':       'door_closed',
    'Open':         'door_open',
    'LockClosed':   'lock_locked',
    'LockOpen':     'lock_unlocked',
    'Motion':       'motion',
    'Alarm':        'alarm',
    'Disarmed':     'disarm',
    'Armed Away':   'arm_away',
    'Armed Home':   'arm_home',
    'Home':         'arm_home',
    'Away':         'arm_away',
    'Standby':      'disarm',
    # Add more as seen in actual export
}


def import_abode_csv(path, dry_run=False):
    inserted = skipped = 0

    with open(path, newline='', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        print('Columns found:', reader.fieldnames)
        print()

        rows = list(reader)

    if not rows:
        print('No data rows found.')
        return

    # Print first 3 rows so you can confirm field names before committing
    print('First rows (preview):')
    for r in rows[:3]:
        print(' ', dict(r))
    print()

    if dry_run:
        print('DRY RUN — no data written. Remove --dry-run to import.')
        return

    with sqlite3.connect(DB_PATH) as conn:
        for row in rows:
            try:
                # Common Abode CSV field names — adjust to match actual export
                date_str  = row.get('Date') or row.get('date', '')
                time_str  = row.get('Time') or row.get('time', '')
                event_str = row.get('Event') or row.get('event', '')
                device    = row.get('Device') or row.get('device', '')

                if not date_str or not time_str:
                    raise ValueError(f'Missing date/time fields — check column names')

                # Try common date/time formats
                for fmt in ('%m/%d/%Y %I:%M %p', '%m/%d/%Y %H:%M', '%Y-%m-%d %H:%M:%S'):
                    try:
                        dt = datetime.strptime(f'{date_str} {time_str}', fmt)
                        break
                    except ValueError:
                        continue
                else:
                    raise ValueError(f'Unrecognized date/time format: {date_str!r} {time_str!r}')

                ts    = int(dt.timestamp())
                evt   = ABODE_TYPE_MAP.get(event_str, 'unknown')
                title = f'{device}: {event_str}' if device else event_str
                detail = 'imported from Abode activity export'

                # INSERT OR IGNORE prevents duplicates on re-run
                conn.execute(
                    'INSERT OR IGNORE INTO event_log '
                    '(ts, system, event_type, title, detail, result, source) '
                    'VALUES (?,?,?,?,?,?,?)',
                    (ts, 'abode', evt, title, detail, None, 'import')
                )
                inserted += 1

            except Exception as e:
                print(f'Skip row: {e} — {dict(row)}')
                skipped += 1

        conn.commit()

    print(f'Import complete: {inserted} inserted, {skipped} skipped')


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: py abode_import.py <path/to/abode_activity.csv> [--dry-run]')
        sys.exit(1)

    csv_path = sys.argv[1]
    dry = '--dry-run' in sys.argv

    import_abode_csv(csv_path, dry_run=dry)
