"""
SDG&E EV-TOU-2 rate fetching + TOU period classification.
Run on startup (auto-refreshes if stale) or manually: py fetch_rates.py
"""
import io
import json
import os
import re
import requests
import pdfplumber
from datetime import datetime, date, timedelta

BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
RATES_PATH    = os.path.join(BASE_DIR, 'rates.json')
HOLIDAYS_PATH = os.path.join(BASE_DIR, 'holidays.json')


# ── SDG&E holiday calendar ────────────────────────────────────────────────────

def _nth_weekday(year, month, weekday, n):
    """Return the nth occurrence of weekday (0=Mon..6=Sun) in month."""
    d = date(year, month, 1)
    days_ahead = weekday - d.weekday()
    if days_ahead < 0:
        days_ahead += 7
    d += timedelta(days=days_ahead)
    d += timedelta(weeks=n - 1)
    return d


def _last_monday(year, month):
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    d = next_month - timedelta(days=1)
    while d.weekday() != 0:
        d -= timedelta(days=1)
    return d


def generate_sdge_holidays(year):
    return sorted([
        date(year, 1, 1),                       # New Year's Day
        _nth_weekday(year, 2, 0, 3),            # Presidents' Day: 3rd Monday Feb
        _last_monday(year, 5),                   # Memorial Day: last Monday May
        date(year, 7, 4),                        # Independence Day
        _nth_weekday(year, 9, 0, 1),            # Labor Day: 1st Monday Sep
        date(year, 11, 11),                      # Veterans Day
        _nth_weekday(year, 11, 3, 4),           # Thanksgiving: 4th Thursday Nov
        date(year, 12, 25),                      # Christmas Day
    ])


def load_or_generate_holidays():
    """Load holidays.json, regenerating if missing or from a different year."""
    current_year = date.today().year
    if os.path.exists(HOLIDAYS_PATH):
        with open(HOLIDAYS_PATH) as f:
            data = json.load(f)
        if data.get('year') == current_year:
            return {date.fromisoformat(d) for d in data['dates']}
    holidays = generate_sdge_holidays(current_year)
    with open(HOLIDAYS_PATH, 'w') as f:
        json.dump({
            'year':      current_year,
            'dates':     [d.isoformat() for d in holidays],
            'generated': date.today().isoformat(),
        }, f, indent=2)
    return set(holidays)


# Loaded once at module level
SDGE_HOLIDAYS = load_or_generate_holidays()


def is_sdge_holiday(d: date) -> bool:
    return d in SDGE_HOLIDAYS


_HOLIDAY_NAMES = {
    (1, 1): "New Year's Day", (7, 4): 'Independence Day',
    (11, 11): "Veterans Day", (12, 25): 'Christmas Day',
}


def holiday_name(d: date) -> str:
    """Return display name for an SDG&E holiday date."""
    key = (d.month, d.day)
    if key in _HOLIDAY_NAMES:
        return _HOLIDAY_NAMES[key]
    if d.month == 2 and d.weekday() == 0 and 15 <= d.day <= 21:
        return "Presidents' Day"
    if d.month == 5 and d.weekday() == 0 and d.day >= 25:
        return 'Memorial Day'
    if d.month == 9 and d.weekday() == 0 and d.day <= 7:
        return 'Labor Day'
    if d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28:
        return 'Thanksgiving'
    return 'SDG&E Holiday'


# ── TOU period classification ─────────────────────────────────────────────────

_DEFAULT_TOU_PERIODS = {
    'weekday': {
        'on_peak':        [[16, 21]],
        'super_off_peak': [[0, 6]],
        'super_off_peak_winter_mar_apr': [[10, 14]],
    },
    'weekend_holiday': {
        'on_peak':        [[16, 21]],
        'super_off_peak': [[0, 14]],
    },
}


def _hour_in_ranges(h, ranges):
    """Check if hour h falls within any [start, end) range."""
    return any(s <= h < e for s, e in ranges)


def holiday_super_off_peak(hour: int, periods: dict = None) -> bool:
    """True if *hour* falls in the weekend/holiday super off-peak window."""
    p = periods or _DEFAULT_TOU_PERIODS
    ranges = p.get('weekend_holiday', {}).get('super_off_peak', [])
    return _hour_in_ranges(hour, ranges)


def tou_period(dt: datetime, periods: dict = None):
    """Return (season, period) for a local datetime.

    periods: optional TOU period definitions dict (from DB setting).
    Falls back to _DEFAULT_TOU_PERIODS if not provided.
    """
    p = periods or _DEFAULT_TOU_PERIODS
    season     = 'summer' if dt.month in [6, 7, 8, 9, 10] else 'winter'
    h          = dt.hour
    is_weekend = dt.weekday() >= 5
    is_holiday = is_sdge_holiday(dt.date())

    day_type = 'weekend_holiday' if (is_weekend or is_holiday) else 'weekday'
    rules = p.get(day_type, {})

    if _hour_in_ranges(h, rules.get('on_peak', [])):
        return season, 'on_peak'
    if _hour_in_ranges(h, rules.get('super_off_peak', [])):
        return season, 'super_off_peak'
    # Winter March/April extra super off-peak window (weekdays only)
    if (day_type == 'weekday' and season == 'winter' and dt.month in (3, 4)
            and _hour_in_ranges(h, rules.get('super_off_peak_winter_mar_apr', []))):
        return season, 'super_off_peak'
    return season, 'off_peak'


# ── Rate file helpers ─────────────────────────────────────────────────────────

def load_rates() -> dict:
    if os.path.exists(RATES_PATH):
        with open(RATES_PATH) as f:
            return json.load(f)
    return {}


def rates_are_stale(rates: dict, days: int = 30) -> bool:
    updated = rates.get('updated')
    if not updated:
        return True
    try:
        age = (datetime.now() - datetime.fromisoformat(updated)).days
        return age > days
    except Exception:
        return True


def current_rate(rates: dict) -> float:
    season, period = tou_period(datetime.now())
    return rates.get(f'{season}_{period}', 0.0)


# ── Page scraping + PDF fetch + parse ─────────────────────────────────────────

def _discover_current_pdf(page_url: str, schedule_name: str = 'EV-TOU') -> tuple:
    """Scrape SDG&E rates page to find the current PDF link for a schedule.

    Returns (pdf_url, effective_date_str, label) or raises if not found.
    effective_date_str is 'YYYY-MM-DD'.
    """
    resp = requests.get(page_url, timeout=30)
    resp.raise_for_status()
    html = resp.text

    # Find all <a> tags whose text contains the schedule name
    # Pattern: <a ... href="...pdf"...>DATE_RANGE, Schedule EV-TOU...</a>
    pattern = re.compile(
        r'<a[^>]+href="([^"]+\.pdf)"[^>]*>([^<]*' + re.escape(schedule_name) + r'[^<]*)</a>',
        re.IGNORECASE
    )
    matches = pattern.findall(html)
    if not matches:
        raise ValueError(f'No PDF links found for schedule "{schedule_name}" at {page_url}')

    # Find the "Current" entry — its label contains "Current"
    current_url = None
    current_label = None
    for url, label in matches:
        if 'current' in label.lower():
            current_url = url
            current_label = label.strip()
            break

    if not current_url:
        # Fallback: use the first match (typically the most recent)
        current_url = matches[0][0]
        current_label = matches[0][1].strip()

    # Ensure absolute URL
    if current_url.startswith('/'):
        from urllib.parse import urlparse
        parsed = urlparse(page_url)
        current_url = f'{parsed.scheme}://{parsed.netloc}{current_url}'

    # Parse effective date from label like "1/1/26 - Current" or "10/1/25 - 12/31/25"
    date_match = re.match(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{2,4})', current_label)
    if date_match:
        mo, day, yr = date_match.groups()
        yr = int(yr) if len(yr) == 4 else 2000 + int(yr)
        eff_date = f'{yr}-{int(mo):02d}-{int(day):02d}'
    else:
        eff_date = date.today().isoformat()

    return current_url, eff_date, current_label


def _parse_ev_tou2_pdf(pdf_content: bytes) -> dict:
    """Extract EV-TOU-2 rates from PDF content. Returns rate dict."""
    with pdfplumber.open(io.BytesIO(pdf_content)) as pdf:
        text = pdf.pages[0].extract_text()

    lines = text.split('\n')
    ev_tou2_start = next(
        (i for i, l in enumerate(lines) if 'SCHEDULE EV-TOU-2' in l),
        None
    )
    if ev_tou2_start is None:
        raise ValueError('Could not find SCHEDULE EV-TOU-2 section in PDF')

    rates = {}
    season = None
    for line in lines[ev_tou2_start:]:
        if line.strip() in ('Summer', 'Winter'):
            season = line.strip().lower()
            continue
        nums = re.findall(r'\d+\.\d+', line)
        if not nums or not season:
            continue
        last = float(nums[-1])
        if 'On-Peak' in line and 'Super' not in line:
            rates[f'{season}_on_peak'] = last
        elif 'Off-Peak' in line and 'Super' not in line:
            rates[f'{season}_off_peak'] = last
        elif 'Super Off-Peak' in line:
            rates[f'{season}_super_off_peak'] = last
        elif 'Base Services Charge' in line and '$/Day' in line:
            rates['base_services_charge_per_day'] = last
            break
    return rates


def fetch_ev_tou2_rates(page_url: str = None, schedule_name: str = None,
                        db_path: str = None) -> dict:
    """Scrape SDG&E rates page, find current EV-TOU-2 PDF, parse, store.

    Args:
        page_url: URL of the SDG&E rates listing page (default from settings or hardcoded)
        schedule_name: Schedule name to search for (default 'EV-TOU')
        db_path: Path to SQLite database for storing rate_history
    """
    if page_url is None:
        page_url = 'https://www.sdge.com/total-electric-rates'
    if schedule_name is None:
        schedule_name = 'EV-TOU'

    # Step 1: Discover the current PDF URL
    pdf_url, eff_date, label = _discover_current_pdf(page_url, schedule_name)
    print(f'Rates: found "{label}" -> {pdf_url}')

    # Step 2: Download and parse the PDF
    r = requests.get(pdf_url, timeout=30)
    r.raise_for_status()
    rates = _parse_ev_tou2_pdf(r.content)

    if len(rates) < 6:
        raise ValueError(f'Incomplete rates parsed from PDF: {rates}')

    # Step 3: Write rates.json for backward compat (current rate tile, rate card)
    rates['updated'] = datetime.now().isoformat()
    rates['source_url'] = pdf_url
    rates['effective_date'] = eff_date
    with open(RATES_PATH, 'w') as f:
        json.dump(rates, f, indent=2)

    # Step 4: Store in rate_history if db_path provided
    if db_path:
        import sqlite3
        with sqlite3.connect(db_path) as conn:
            conn.execute(
                'INSERT OR REPLACE INTO rate_history '
                '(effective_date, summer_on_peak, summer_off_peak, summer_super_off_peak, '
                ' winter_on_peak, winter_off_peak, winter_super_off_peak, '
                ' base_services_charge_per_day, source_url, fetched_at) '
                'VALUES (?,?,?,?,?,?,?,?,?,?)',
                (eff_date,
                 rates.get('summer_on_peak', 0), rates.get('summer_off_peak', 0),
                 rates.get('summer_super_off_peak', 0),
                 rates.get('winter_on_peak', 0), rates.get('winter_off_peak', 0),
                 rates.get('winter_super_off_peak', 0),
                 rates.get('base_services_charge_per_day', 0),
                 pdf_url, rates['updated'])
            )
            conn.commit()
        print(f'Rates: stored in rate_history (effective {eff_date})')

    print(f'Rates updated from {pdf_url}')
    return rates


if __name__ == '__main__':
    import sys
    db = sys.argv[1] if len(sys.argv) > 1 else None
    print('Fetching SDG&E EV-TOU-2 rates...')
    r = fetch_ev_tou2_rates(db_path=db)
    print(json.dumps(r, indent=2))
