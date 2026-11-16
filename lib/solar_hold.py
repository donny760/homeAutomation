"""Log episodes where solar surplus is exported while the battery sits idle below full.

Tesla's "Optimized Charging" (battery-longevity hold, seen 2026-11-15) parks the
Powerwall near 80% and sends surplus solar to the grid. Fleet gives no flag for
it that we know of, so this records the *behavior*: battery idle, not full, solar
exporting. It says nothing about the cause — Time-Based Control can do the same.

Reads values the poller already has; never calls Tesla, never changes rules.
One start row after the condition has held OPEN_AFTER, one end row after it has
been false CLOSE_AFTER. State persists to settings so a restart resumes the
episode instead of duplicating it.
"""
import logging
import time

from lib.events import _log_success, format_duration
from lib.settings import get_setting, set_setting

_log = logging.getLogger(__name__)

CHECK_EVERY   = 60      # seconds between evaluations (poller loops every 10s)
OPEN_AFTER    = 600     # condition must hold this long before an episode opens
CLOSE_AFTER   = 300     # and be false this long before it closes
STALE_GAP     = 900     # a persisted episode older than this on startup is closed, not resumed
MAX_STEP      = 300     # cap one integration step so a stalled poller can't inflate kWh

SOC_MIN, SOC_MAX = 5, 97
BATTERY_IDLE_W   = 250
EXPORT_MIN_W     = 500
SOLAR_MIN_W      = 1000

_last_check = 0.0
_loaded = False
_held_since: float | None = None    # condition first seen true (pre-open)
_pre_kwh = 0.0                      # export accumulated before the episode opens
_clear_since: float | None = None   # condition first seen false while open
_ep: dict | None = None             # open episode
_last_sample: dict | None = None


def _held(v: dict) -> bool:
    soc, bat, grid, solar = (v.get('battery_pct'), v.get('battery_w'),
                             v.get('grid_w'), v.get('solar_w'))
    if None in (soc, bat, grid, solar):
        return False
    return (SOC_MIN < soc < SOC_MAX and abs(bat) < BATTERY_IDLE_W
            and grid < -EXPORT_MIN_W and solar > SOLAR_MIN_W)


def _mode_label(mode: str | None) -> str:
    return {'autonomous': 'Time-Based Control',
            'self_consumption': 'Self-Powered',
            'backup': 'Backup'}.get(mode or '', mode or 'unknown mode')


def _persist() -> None:
    if _ep:
        set_setting('solar_hold_since', str(int(_ep['since'])))
        set_setting('solar_hold_kwh', f"{_ep['kwh']:.3f}")
        set_setting('solar_hold_soc', f"{_ep['soc_min']:.1f},{_ep['soc_max']:.1f}")
        set_setting('solar_hold_last_ts', str(int(_ep['last_ts'])))
    else:
        for k in ('solar_hold_since', 'solar_hold_kwh', 'solar_hold_soc', 'solar_hold_last_ts'):
            set_setting(k, '')


def _load(now: float) -> None:
    """Resume an episode persisted by a previous run, or close it if stale."""
    global _ep, _loaded
    _loaded = True
    since = get_setting('solar_hold_since', '') or ''
    if not since:
        return
    try:
        soc_min, soc_max = (float(x) for x in (get_setting('solar_hold_soc', '') or '0,0').split(','))
        ep = {'since': float(since),
              'kwh': float(get_setting('solar_hold_kwh', '') or 0),
              'soc_min': soc_min, 'soc_max': soc_max,
              'last_ts': float(get_setting('solar_hold_last_ts', '') or since),
              'mode': None}
    except ValueError:
        _ep = None
        _persist()
        return
    if now - ep['last_ts'] <= STALE_GAP:
        _ep = ep
        return
    _ep = ep
    _close('monitoring interrupted (service was down)', end_ts=ep['last_ts'])


def _close(reason: str, end_ts: float) -> None:
    global _ep
    ep = _ep
    _ep = None
    _persist()
    if not ep:
        return
    lasted = format_duration(end_ts - ep['since'])
    soc = (f"{ep['soc_min']:.0f}%" if round(ep['soc_min']) == round(ep['soc_max'])
           else f"{ep['soc_min']:.0f}–{ep['soc_max']:.0f}%")
    _log_success('powerwall', 'solar_hold_end',
                 f'Solar hold ended after {lasted} — {reason}',
                 f"{ep['kwh']:.1f} kWh exported while battery held at {soc}"
                 + (f" · {_mode_label(ep['mode'])}" if ep.get('mode') else ''))


def _end_reason(v: dict) -> str:
    soc, bat, grid = v.get('battery_pct'), v.get('battery_w'), v.get('grid_w')
    if soc is not None and soc >= SOC_MAX:
        return 'battery reached full'
    if bat is not None and bat <= -BATTERY_IDLE_W:
        return 'battery started charging'
    if bat is not None and bat >= BATTERY_IDLE_W:
        return 'battery started discharging'
    if grid is not None and grid >= -EXPORT_MIN_W:
        return 'solar no longer in surplus'
    return 'condition cleared'


def check(v: dict, now: float | None = None) -> None:
    """Called every poller loop with the live values; real work once per CHECK_EVERY."""
    global _last_check
    now = time.time() if now is None else now
    if now - _last_check < CHECK_EVERY:
        return
    _last_check = now
    try:
        _step(v, now)
    except Exception as exc:
        _log.warning('solar_hold check failed: %r', exc)


def _step(v: dict, now: float) -> None:
    global _held_since, _clear_since, _ep, _last_sample, _pre_kwh
    if not _loaded:
        _load(now)

    held = _held(v)
    prev, _last_sample = _last_sample, {'ts': now, 'grid_w': v.get('grid_w')}
    step_kwh = 0.0
    if held and prev and prev.get('grid_w') is not None:
        step_kwh = max(0.0, -v['grid_w']) * min(now - prev['ts'], MAX_STEP) / 3_600_000

    if _ep is None:
        if not held:
            _held_since = None
            return
        if _held_since is None:
            _held_since = now
            _pre_kwh = 0.0
        else:
            _pre_kwh += step_kwh    # export during the OPEN_AFTER wait counts too
        if now - _held_since < OPEN_AFTER:
            return
        soc = v['battery_pct']
        _ep = {'since': _held_since, 'kwh': _pre_kwh, 'soc_min': soc, 'soc_max': soc,
               'last_ts': now, 'mode': v.get('mode')}
        _clear_since = None
        _persist()
        _log_success('powerwall', 'solar_hold_start',
                     f"Solar not charging battery at {soc:.0f}% — exporting {-v['grid_w'] / 1000:.1f} kW",
                     f"{_mode_label(v.get('mode'))} · solar {v['solar_w'] / 1000:.1f} kW, "
                     f"home {(v.get('home_w') or 0) / 1000:.1f} kW. Tesla does not report why "
                     f"(e.g. Optimized Charging).")
        return

    # Episode open: integrate export since the previous sample (one sample per
    # minute is plenty for a record), track SoC and mode.
    if held:
        _clear_since = None
        _ep['kwh'] += step_kwh
        soc = v['battery_pct']
        _ep['soc_min'] = min(_ep['soc_min'], soc)
        _ep['soc_max'] = max(_ep['soc_max'], soc)
        _ep['mode'] = v.get('mode') or _ep.get('mode')
        _ep['last_ts'] = now
        _persist()
        return

    # Reason is taken from the first sample after the hold broke — that's the
    # moment that says what actually ended it.
    if _clear_since is None:
        _clear_since = now
        _ep['pending_reason'] = _end_reason(v)
    if now - _clear_since >= CLOSE_AFTER:
        _close(_ep['pending_reason'], end_ts=_clear_since)
        _clear_since = None
        _held_since = None
