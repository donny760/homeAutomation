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


# ── TOU period classification ─────────────────────────────────────────────────

def tou_period(dt: datetime):
    """Return (season, period) for a local datetime.

    Weekends/holidays: super off-peak all day EXCEPT 4–9 pm which stays on-peak.
    """
    season      = 'summer' if dt.month in [6, 7, 8, 9, 10] else 'winter'
    is_weekend  = dt.weekday() >= 5
    is_holiday  = is_sdge_holiday(dt.date())

    if is_weekend or is_holiday:
        if 16 <= dt.hour < 21:
            return season, 'on_peak'
        return season, 'super_off_peak'

    if 16 <= dt.hour < 21:
        return season, 'on_peak'
    if dt.hour < 6:
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


# ── PDF fetch + parse ─────────────────────────────────────────────────────────

def fetch_ev_tou2_rates() -> dict:
    """Download the SDG&E EV-TOU-2 rate table PDF and write rates.json."""
    year = datetime.now().year
    def _url(y):
        return (
            f"https://www.sdge.com/sites/default/files/regulatory/"
            f"1-1-{str(y)[2:]}%20Schedule%20EV-TOU%20%26%20EV-TOU-2"
            f"%20Total%20Rates%20Tables.pdf"
        )

    url = _url(year)
    r = requests.get(url, timeout=15)
    if r.status_code != 200:
        year -= 1
        url = _url(year)
        r = requests.get(url, timeout=15)
    r.raise_for_status()

    with pdfplumber.open(io.BytesIO(r.content)) as pdf:
        text = pdf.pages[0].extract_text()

    lines = text.split('\n')
    ev_tou2_start = next(i for i, l in enumerate(lines) if 'SCHEDULE EV-TOU-2' in l)

    rates  = {}
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

    rates['updated']    = datetime.now().isoformat()
    rates['source_url'] = url
    with open(RATES_PATH, 'w') as f:
        json.dump(rates, f, indent=2)
    print(f'Rates updated from {url}')
    return rates


if __name__ == '__main__':
    print('Fetching SDG&E EV-TOU-2 rates...')
    r = fetch_ev_tou2_rates()
    print(json.dumps(r, indent=2))
