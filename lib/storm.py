"""Tesla Storm Watch: surface it on the dashboard and record it in event_log.

Tesla publishes Storm Watch through the Fleet API with a type and a window —
unlike VPP, which exposes nothing (investigated 2026-10-06, no grid-services
signal of any kind for this site). Reading it costs nothing extra: the alert
rides along in the `live_status` response the poller's `pw.power()` already
fetches every 10s, so this reads pypowerwall's cache and never calls `poll()`.

Reporting only. Nothing here writes to Tesla or changes rule behavior — on
2026-10-09 Tesla's own app banner read "Powerwall is charging to provide backup
protection" while the battery discharged at 2.6 kW, so judging whether the
Powerwall is doing the right thing is a separate problem from saying a watch
is on.
"""
import logging
import time
from datetime import datetime

from lib.events import _log_success, format_duration
from lib.settings import get_setting, set_setting

_log = logging.getLogger(__name__)

CHECK_EVERY   = 60     # seconds between checks (the poller loops every 10s)
CACHE_MAX_AGE = 60     # use cached live_status only if the poller fetched it recently

_last_check = 0.0


def cached_storm(pw) -> dict | None:
    """Storm Watch state from the live_status response already in pypowerwall's
    cache. Never makes a Tesla request; returns None when there is no recent
    cached copy (unknown is not the same as 'no storm')."""
    try:
        fleet = pw.client.fleet
        key = f'api/1/energy_sites/{fleet.site_id}/live_status'
        if time.time() - fleet.pwcachetime.get(key, 0) > CACHE_MAX_AGE:
            return None
        live = (fleet.pwcache.get(key) or {}).get('response') or {}
        if not live:
            return None
        active = bool(live.get('storm_mode_active'))
        # storm_mode_states carries the type and window; it can be absent or
        # empty while storm_mode_active is true, so treat it as optional detail.
        states = live.get('storm_mode_states') or []
        s = states[0] if isinstance(states, list) and states else {}
        return {
            'active':   active,
            'type':     s.get('storm_type') or ('Storm Watch' if active else None),
            'start':    s.get('start_time'),
            'end':      s.get('end_time'),
            'event_id': s.get('watch_event_id'),
        }
    except Exception:
        return None


def check(storm: dict | None, now: float | None = None) -> None:
    """Log one row when a watch starts and one when it ends. Called every poller
    loop; does real work at most once per CHECK_EVERY."""
    global _last_check
    now = time.time() if now is None else now
    if now - _last_check < CHECK_EVERY:
        return
    _last_check = now
    if storm is None:
        return                      # cache cold/stale — unknown, not 'ended'
    try:
        _transition(storm, now)
    except Exception as exc:
        _log.warning('storm check failed: %r', exc)


def _clock(iso: str | None) -> str | None:
    """'2026-10-09T16:00:00-07:00' -> '4:00 PM'."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso).strftime('%I:%M %p').lstrip('0')
    except ValueError:
        return None


def describe(storm: dict | None) -> str | None:
    """Banner text, e.g. 'Tropical Storm Watch until 4:00 PM'."""
    if not storm or not storm.get('active'):
        return None
    label = storm.get('type') or 'Storm Watch'
    end = _clock(storm.get('end'))
    return f'{label} until {end}' if end else label


def _transition(storm: dict, now: float) -> None:
    # Identify the event so a restart mid-watch doesn't re-log the start. Tesla
    # supplies watch_event_id; fall back to the start time, then to a constant.
    logged = get_setting('storm_event_id', '') or ''
    current = (storm.get('event_id') or storm.get('start') or 'active') if storm['active'] else ''

    if storm['active']:
        if logged == current:
            return
        set_setting('storm_event_id', current)
        set_setting('storm_since', str(int(now)))
        window = ' to '.join(x for x in (_clock(storm.get('start')), _clock(storm.get('end'))) if x)
        said = describe(storm)
        # Avoid 'Storm Watch: Storm Watch' when Tesla gives no specific type.
        title = said if said.startswith('Storm Watch') else f'Storm Watch: {said}'
        _log_success('powerwall', 'storm_watch_start', title,
                     f'{storm.get("type") or "Storm Watch"}'
                     + (f' — {window}' if window else '')
                     + '. Tesla may charge the Powerwall for backup; it does not always do so.')
        return

    if not logged:
        return                      # no watch was running
    since = get_setting('storm_since', '') or ''
    try:
        lasted = format_duration(now - float(since)) if since else None
    except ValueError:
        lasted = None
    set_setting('storm_event_id', '')
    set_setting('storm_since', '')
    _log_success('powerwall', 'storm_watch_end',
                 'Storm Watch ended' + (f' after {lasted}' if lasted else ''))
