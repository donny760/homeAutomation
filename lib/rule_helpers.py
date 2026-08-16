import json
import sqlite3
from datetime import datetime, date, timedelta

from lib.fetch_rates import is_sdge_holiday, holiday_name
from lib.settings import get_setting, _load_tou_periods

_RULE_REQUIRED_FIELDS = ('name', 'days', 'months', 'hour', 'minute')


def _fmt_hour(h):
    """Format hour int as '2 PM', '12 AM', etc."""
    if h == 0:
        return 'midnight'
    ampm = 'AM' if h < 12 else 'PM'
    h12 = h % 12 or 12
    return f'{h12} {ampm}'


def _rule_row_to_dict(row, conditions):
    rid, name, enabled, days_j, months_j, hour, minute, mode, reserve, gc, ge, notes = row
    return {
        'id':           rid,
        'name':         name,
        'enabled':      bool(enabled),
        'days':         json.loads(days_j),
        'months':       json.loads(months_j),
        'hour':         hour,
        'minute':       minute,
        'mode':         mode,
        'reserve':      reserve,
        'grid_charging': None if gc is None else bool(gc),
        'grid_export':  ge,
        'notes':        notes,
        'conditions':   conditions,
    }


def _load_all_rules(c):
    rows = c.execute(
        'SELECT id,name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export,notes FROM rules ORDER BY sort_order, id'
    ).fetchall()
    cond_rows = c.execute('SELECT rule_id,logic,type,operator,value FROM rule_conditions').fetchall()
    cond_map = {}
    for rule_id, logic, ctype, op, val in cond_rows:
        cond_map.setdefault(rule_id, []).append(
            {'logic': logic, 'type': ctype, 'operator': op, 'value': val}
        )
    return [_rule_row_to_dict(r, cond_map.get(r[0], [])) for r in rows]


def _rule_fires_at(rule, d):
    weekday     = d.weekday()
    days_set    = set(rule['days'])
    is_holiday  = is_sdge_holiday(d)
    has_weekend = bool(days_set & {5, 6})

    if is_holiday:
        if not has_weekend:
            return None
    else:
        if weekday not in days_set:
            return None

    if d.month not in set(rule['months']):
        return None
    try:
        return datetime(d.year, d.month, d.day, rule['hour'], rule['minute'])
    except (ValueError, TypeError):
        # An out-of-range or non-numeric hour/minute already in the DB. Skip the
        # rule rather than letting it take down every caller of _upcoming_firings
        # (/api/schedule renders all rules from one pass). _validate_rule_body
        # blocks new ones; this covers rows written before that existed.
        return None


def _upcoming_firings(rules, hours=48):
    now = datetime.now()
    cutoff = now + timedelta(hours=hours)
    events = []
    paused_shown: set = set()
    tou = _load_tou_periods()
    # Paused rules are scanned further out (up to 8 days) so a recurring rule whose next
    # occurrence falls outside the 48h window — e.g. a weekday rule paused on a Friday, next
    # firing Monday — still produces one pinned row the user can resume from.
    for delta_days in range(0, 8):
        d = now.date() + timedelta(days=delta_days)
        within_window = delta_days <= 2
        if within_window and is_sdge_holiday(d):
            fire_dt = datetime(d.year, d.month, d.day, 0, 0)
            if fire_dt <= cutoff:
                events.append({
                    'fire_time':     fire_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                    'source':        'powerwall',
                    'name':          f'Holiday: {holiday_name(d)} — weekend rules apply',
                    'holiday_info':  True,
                    'mode':          None,
                    'reserve':       None,
                    'grid_charging': None,
                    'grid_export':   None,
                    'conditions':    [],
                })
        for rule in rules:
            paused = not rule['enabled']
            if paused and rule['id'] in paused_shown:
                continue
            if not paused and not within_window:
                continue  # enabled rules keep the original 48h horizon
            fire_dt = _rule_fires_at(rule, d)
            if not fire_dt or fire_dt <= now:
                continue
            # Paused rules ignore the cutoff so they always yield one pinned row.
            if not paused and fire_dt > cutoff:
                continue
            events.append({
                'fire_time':     fire_dt.strftime('%Y-%m-%dT%H:%M:%S'),
                'source':        'powerwall',
                'rule_id':       rule['id'],
                'enabled':       bool(rule['enabled']),
                'pinned':        paused,
                'name':          rule['name'],
                'mode':          rule['mode'],
                'reserve':       rule['reserve'],
                'grid_charging': rule['grid_charging'],
                'grid_export':   rule['grid_export'],
                'conditions':    rule['conditions'],
            })
            if paused:
                paused_shown.add(rule['id'])
    events.sort(key=lambda e: e['fire_time'])
    return events


def _validate_rule_body(body):
    if not isinstance(body, dict):
        return 'JSON object required'
    missing = [k for k in _RULE_REQUIRED_FIELDS if k not in body]
    if missing:
        return f'missing fields: {", ".join(missing)}'
    # hour/minute go straight into datetime() when the schedule is built, so an
    # out-of-range value written here surfaces as a 500 on /api/schedule later.
    for field, hi in (('hour', 23), ('minute', 59)):
        val = body[field]
        if isinstance(val, bool) or not isinstance(val, int):
            return f'{field} must be an integer'
        if not 0 <= val <= hi:
            return f'{field} must be between 0 and {hi}'
    return None


def _analyze_rules(rules, rates, holidays, tou_periods=None):
    """Deterministic analysis of Powerwall rules against EV-TOU-2 rate schedule."""
    insights = []
    now = datetime.now()
    today = now.date()

    enabled = [r for r in rules if r.get('enabled')]
    sop_winter = rates.get('winter_super_off_peak', 0.25)
    sop_summer = rates.get('summer_super_off_peak', 0.26)
    on_summer  = rates.get('summer_on_peak', 0.78)
    on_winter  = rates.get('winter_on_peak', 0.51)

    _tp = tou_periods or {}
    _wd = _tp.get('weekday', {})
    _wh = _tp.get('weekend_holiday', {})

    wd_sop_ranges    = _wd.get('super_off_peak', [[0, 6], [10, 14]])
    wd_sop_overnight = [r for r in wd_sop_ranges if r[0] < 8] or [[0, 6]]
    wd_sop_daytime   = [r for r in wd_sop_ranges if r[0] >= 8]
    wd_sop_start     = _fmt_hour(min(s for s, _ in wd_sop_overnight))
    wd_sop_end       = _fmt_hour(max(e for _, e in wd_sop_overnight))
    day_sop_start_h  = min(s for s, _ in wd_sop_daytime) if wd_sop_daytime else 10
    day_sop_end_h    = max(e for _, e in wd_sop_daytime) if wd_sop_daytime else 14
    day_sop_start    = _fmt_hour(day_sop_start_h)
    day_sop_end      = _fmt_hour(day_sop_end_h)

    on_peak_ranges = _wd.get('on_peak', [[16, 21]])
    on_start_h = min(s for s, _ in on_peak_ranges)
    on_end_h   = max(e for _, e in on_peak_ranges)
    on_start   = _fmt_hour(on_start_h)
    on_end     = _fmt_hour(on_end_h)

    # ── 1. Grid charging window duration ─────────────────────────────────────
    charge_on  = [r for r in enabled if r.get('grid_charging') is True]
    charge_off = [r for r in enabled if r.get('grid_charging') is False]

    if charge_on:
        for on_r in charge_on:
            on_min  = on_r['hour'] * 60 + on_r['minute']
            on_days = set(on_r['days'])
            best_off = None
            for off_r in charge_off:
                off_min = off_r['hour'] * 60 + off_r['minute']
                if off_min > on_min and on_days & set(off_r['days']):
                    if best_off is None or off_min < best_off['hour'] * 60 + best_off['minute']:
                        best_off = off_r
            if best_off:
                window = (best_off['hour'] * 60 + best_off['minute']) - on_min
                if window < 180:
                    kwh = round(window * 5 / 60, 1)
                    insights.append({
                        'severity': 'warning',
                        'title':  f'Grid charging window is only {window} minutes',
                        'detail': (
                            f'"{on_r["name"]}" charges from {on_r["hour"]}:{on_r["minute"]:02d} '
                            f'until "{best_off["name"]}" stops it at {best_off["hour"]}:{best_off["minute"]:02d}. '
                            f'At ~5 kW that adds only ~{kwh} kWh to a 40.5 kWh battery bank (3× Powerwall 2). '
                            f'Super off-peak runs {wd_sop_start}–{wd_sop_end} at ${sop_winter:.3f}/kWh.'
                        ),
                        'action': 'Start grid charging earlier to fully charge at super off-peak rates.',
                        'rule_id': on_r['id'],
                    })
    else:
        insights.append({
            'severity': 'suggestion',
            'title':  'No grid charging rules configured',
            'detail': (
                f'Charging from grid during super off-peak (${sop_winter:.3f}/kWh) offsets '
                f'on-peak usage (${on_summer:.3f}/kWh) — a {on_summer / sop_winter:.1f}x saving.'
            ),
            'action': f'Add a rule to enable grid charging during {wd_sop_start}–{wd_sop_end} (super off-peak).',
        })

    # ── 2. Sunday grid charging gap ──────────────────────────────────────────
    if charge_on:
        charge_days = set()
        for r in charge_on:
            charge_days.update(r['days'])
        if 6 not in charge_days:
            insights.append({
                'severity': 'suggestion',
                'title':  'Sunday excluded from grid charging',
                'detail': (
                    'Grid charging rules cover Mon–Sat but skip Sunday. '
                    'The Powerwall may not be topped off for Sunday’s on-peak hours.'
                ),
                'action': 'Add Sunday to an existing grid charging rule or create a Sunday-specific rule.',
            })

    # ── 3. Weekday daytime super off-peak window ─────────────────────────────
    if wd_sop_daytime:
        day_tbc = [r for r in enabled
                   if r.get('mode') == 'autonomous'
                   and {0, 1, 2, 3, 4} & set(r['days'])
                   and day_sop_start_h <= r['hour'] < day_sop_end_h]
        if not day_tbc:
            insights.append({
                'severity': 'suggestion',
                'title':  f'Weekday daytime super off-peak window ({day_sop_start}–{day_sop_end}) not utilized',
                'detail': (
                    f'EV-TOU-2 has a daytime super off-peak window {day_sop_start}–{day_sop_end} on weekdays year-round '
                    f'(${sop_winter:.3f}/kWh winter, ${sop_summer:.3f}/kWh summer). '
                    f'Switching to Time-Based Control means the home draws from the grid at the cheapest rate '
                    f'while solar (if available) charges the battery instead of exporting at the low super off-peak credit rate.'
                ),
                'action': f'Create rules: Time-Based Control at {day_sop_start} and Self-Powered at {day_sop_end}, weekdays.',
            })

    # ── 4. No rule at on-peak boundary ──────────────────────────────────────
    at_on_start = [r for r in enabled if r['hour'] == on_start_h and r['minute'] <= 5]
    if not at_on_start:
        insights.append({
            'severity': 'suggestion',
            'title':  f'No rule at {on_start} on-peak boundary',
            'detail': (
                f'On-peak starts at {on_start} (${on_summer:.3f}/kWh summer, ${on_winter:.3f}/kWh winter). '
                f'No rule adjusts Powerwall settings at this critical transition.'
            ),
            'action': f'Consider a {on_start} rule to set Self-Powered mode and verify reserve covers the {on_start}–{on_end} peak.',
        })

    # ── 5. Battery export starts late (season-aware) ────────────────────────
    summer = {6, 7, 8, 9, 10}
    for season_label, season_months, late_hour, rate_val in [
        ('summer', summer, 19, on_summer),
        ('winter', {1, 2, 3, 4, 5, 11, 12}, 18, on_winter),
    ]:
        season_export = [r for r in enabled
                         if r.get('grid_export') == 'battery_ok'
                         and season_months & set(r['months'])
                         and {0, 1, 2, 3, 4} & set(r['days'])]
        for r in season_export:
            if r['hour'] >= late_hour:
                missed = r['hour'] - on_start_h
                insights.append({
                    'severity': 'suggestion',
                    'title':  f'Battery export starts at {r["hour"]}:{r["minute"]:02d} — on-peak begins {on_start}',
                    'detail': (
                        f'"{r["name"]}" enables battery export {missed}+ hours after on-peak starts. '
                        f'On-peak runs {on_start}–{on_end} at ${rate_val:.3f}/kWh ({season_label}).'
                    ),
                    'action': f'Consider starting export earlier to capture more {season_label} on-peak value.',
                    'rule_id': r['id'],
                })

    # ── 6. November in summer export rules ───────────────────────────────────
    nov_export = [r for r in enabled
                  if r.get('grid_export') == 'battery_ok'
                  and 11 in set(r['months'])
                  and summer & set(r['months'])]
    if nov_export:
        insights.append({
            'severity': 'info',
            'title':  'November grouped with summer in export rules',
            'detail': (
                f'SDG&E classifies November as winter (on-peak ${on_winter:.3f} vs summer ${on_summer:.3f}/kWh). '
                f'Export is still profitable but sunset is earlier — less solar by 7 PM.'
            ),
            'action': 'Consider separate November export rules with earlier timing for shorter daylight.',
        })

    # ── 7. Upcoming weekday holidays ─────────────────────────────────────────
    upcoming = sorted(d for d in holidays if today <= d <= today + timedelta(days=90))
    weekday_holidays = [d for d in upcoming if d.weekday() < 5]

    _p = tou_periods or {}
    _hol_sop = _p.get('weekend_holiday', {}).get('super_off_peak', [[0, 14]])
    _hol_sop_end = _fmt_hour(max(e for _, e in _hol_sop))
    _hol_on  = _p.get('weekend_holiday', {}).get('on_peak', [[16, 21]])
    _hol_on_start = _fmt_hour(min(s for s, _ in _hol_on))
    _hol_on_end   = _fmt_hour(max(e for _, e in _hol_on))

    for hd in weekday_holidays:
        name = holiday_name(hd)
        day_name = hd.strftime('%A')
        insights.append({
            'severity':     'info',
            'title':        f'{name} ({hd.strftime("%b %d")}) — weekend rules apply',
            'detail': (
                f'{name} falls on a {day_name} and uses the holiday TOU schedule: '
                f'super off-peak midnight–{_hol_sop_end}, on-peak {_hol_on_start}–{_hol_on_end}. '
                f'Weekend rules will fire automatically on this day. '
                f'Use a holiday condition to create holiday-specific rules (e.g. battery hold).'
            ),
            'action': 'Review your weekend rules to ensure they cover holiday behavior.',
            'holiday_date': hd.isoformat(),
        })

    # ── 8. Holiday calendar health ───────────────────────────────────────────
    if not holidays:
        insights.append({
            'severity': 'warning',
            'title':  'No holiday dates configured',
            'detail': (
                f'SDG&E holidays use a different TOU schedule (super off-peak midnight–{_hol_sop_end}). '
                'Without holiday dates, weekend rules cannot activate on holidays and '
                'holiday conditions will not work.'
            ),
            'action': 'Refresh holiday dates via Settings.',
        })
    elif all(d < today for d in holidays):
        insights.append({
            'severity': 'warning',
            'title':  'All holiday dates have passed',
            'detail': (
                f'The last holiday was {max(holidays).isoformat()}. '
                f'Holiday dates need refreshing for upcoming holidays.'
            ),
            'action': 'Refresh holiday dates via Settings or wait for automatic refresh.',
        })

    last_verified = get_setting('tou_periods_last_verified', '')
    try:
        stale = not last_verified or (date.today() - date.fromisoformat(last_verified)).days > 180
    except ValueError:
        stale = True
    if stale:
        insights.append({
            'severity': 'info',
            'title':  'TOU schedule not verified in 6+ months',
            'detail': (
                'The on-peak, off-peak, and super off-peak time windows are configured '
                'based on SDG&E EV-TOU-2 tariff Sheet 3. SDG&E occasionally adjusts these '
                'hours. Last verified: ' + (last_verified or 'never') + '.'
            ),
            'action': 'Check SDG&E EV-TOU-2 tariff schedule and update tou_periods_last_verified in Settings.',
        })

    return insights
