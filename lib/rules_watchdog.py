"""Rules-engine watchdog: raise one event_log row when rules.py stops checking in,
or when a rule change it made never shows up on the Powerwall.

Runs inside the poller (server.py) and costs nothing extra against Tesla: the
Powerwall's actual settings come from the site_info response the poller's
get_mode() call already fetched, read straight out of pypowerwall's cache.

rules.py publishes two internal settings (never shown on the Settings page):
  rules_heartbeat_ts  epoch, rewritten every 5 min while its eval loop runs
  rules_target        JSON {"ts": epoch, "fields": {...}} written when its
                      target changes, holding only the fields that changed

Deliberately quiet: nothing is written on a normal day, and a mismatch is only
judged right after a rule change, so a manual change in the Tesla app later
never raises an alert. Each problem gets one error row plus one recovery row.
"""
import json
import logging
import time
from datetime import datetime

from lib.settings import get_setting
from lib.events import _log_system_error, _log_success, format_duration

_log = logging.getLogger(__name__)

CHECK_EVERY   = 60     # seconds between checks (the poller loops every 10s)
SILENT_AFTER  = 660    # heartbeat age before alerting: two 5-min beats + slack
APPLY_GRACE   = 600    # time a rule change has to reach the Powerwall
FRESH_WINDOW  = 1800   # on server start, ignore rule changes older than this
CACHE_MAX_AGE = 60     # use cached site_info only if the poller fetched it recently

_MODE = {'autonomous': 'Time-Based Control', 'self_consumption': 'Self-Powered',
         'backup': 'Backup'}
_EXPORT = {'battery_ok': 'battery export', 'pv_only': 'solar-only export',
           'never': 'no export'}

_read = get_setting        # tests swap this out

_last_check = 0.0
_silent_alerted = False
_watch = None              # the rule change currently being verified


def cached_settings(pw) -> dict | None:
    """Mode, reserve, grid charging and export from the site_info response
    already sitting in pypowerwall's cache. Never makes a Tesla request: returns
    None when there is no recent cached copy."""
    try:
        fleet = pw.client.fleet
        key = f'api/1/energy_sites/{fleet.site_id}/site_info'
        if time.time() - fleet.pwcachetime.get(key, 0) > CACHE_MAX_AGE:
            return None
        resp = (fleet.pwcache.get(key) or {}).get('response') or {}
        if not resp:
            return None
        comp = resp.get('components') or {}
        # Same interpretation as FleetAPI.get_grid_export / get_grid_charging.
        if comp.get('non_export_configured'):
            export = 'never'
        else:
            export = comp.get('customer_preferred_export_rule') or 'battery_ok'
        reserve = resp.get('backup_reserve_percent')
        return {
            'mode':          resp.get('default_real_mode'),
            'reserve':       None if reserve is None else int(reserve),
            'grid_charging': not bool(comp.get('disallow_charge_from_grid_with_solar_installed')),
            'grid_export':   export,
        }
    except Exception:
        return None


def check(actual: dict | None, now: float | None = None) -> None:
    """Called every poller loop; does real work at most once per CHECK_EVERY."""
    global _last_check
    now = time.time() if now is None else now
    if now - _last_check < CHECK_EVERY:
        return
    _last_check = now
    try:
        _check_heartbeat(now)
        _check_rule_change(actual, now)
    except Exception as exc:
        _log.warning('rules watchdog check failed: %r', exc)


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime('%I:%M %p').lstrip('0')


def _describe(field: str, value) -> str:
    if field == 'mode':
        return _MODE.get(value, str(value))
    if field == 'reserve':
        return f'{value}% reserve'
    if field == 'grid_charging':
        return 'grid charging ON' if value else 'grid charging OFF'
    if field == 'grid_export':
        return _EXPORT.get(value, f'export {value}')
    return f'{field}={value}'


def _check_heartbeat(now: float) -> None:
    global _silent_alerted
    hb = _read('rules_heartbeat_ts')
    if not hb:
        return                      # rules.py build without the hooks: nothing to judge
    last = float(hb)
    age = now - last
    if age >= SILENT_AFTER:
        if not _silent_alerted:
            _silent_alerted = True
            _log_system_error(
                'powerwall',
                f"Rules engine hasn't checked in for {format_duration(age)} — "
                f"automations may not be running",
                f'Last check-in {_clock(last)}. Restart the PowerwallRules service.')
    elif _silent_alerted:
        _silent_alerted = False
        _log_success('powerwall', 'rules_engine_ok', 'Rules engine is checking in again')


def _check_rule_change(actual: dict | None, now: float) -> None:
    global _watch
    raw = _read('rules_target')
    if not raw:
        return
    target = json.loads(raw)
    ts, fields = float(target['ts']), target.get('fields') or {}

    if _watch is None or _watch['ts'] != ts:
        # On server start, a rule change from long ago was either applied or
        # already dealt with — don't re-judge it against later manual changes.
        stale = _watch is None and now - ts > FRESH_WINDOW
        _watch = {'ts': ts, 'fields': fields, 'alerted': False, 'done': stale}

    w = _watch
    if w['done'] or actual is None:
        return                      # unknown is not a mismatch (poller logs Tesla outages)

    diff = {k: v for k, v in w['fields'].items()
            if v is not None and actual.get(k) != v}
    if not diff:
        if w['alerted']:
            _log_success('powerwall', 'rules_applied_late',
                         f'Powerwall now matches the {_clock(ts)} rule change')
        w['done'] = True            # stop watching; later manual changes are Don's call
        return

    if not w['alerted'] and now - ts >= APPLY_GRACE:
        w['alerted'] = True
        expected = ', '.join(_describe(k, v) for k, v in diff.items())
        got = ', '.join(_describe(k, actual.get(k)) for k in diff)
        _log_system_error(
            'powerwall',
            f"Powerwall didn't follow the {_clock(ts)} rule change — "
            f"expected {expected}; actual {got}",
            f'Not applied after {format_duration(now - ts)}. Check rules.log; '
            f'restarting the PowerwallRules service usually recovers it.')
