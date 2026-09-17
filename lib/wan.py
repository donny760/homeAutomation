"""Dual-WAN failover detection.

The LRT224 fails over from Cox (WAN1) to T-Mobile (WAN2) silently. SNMP is
disabled on the router, and a ping proves nothing — failover is transparent at
L3, so a ping to 8.8.8.8 succeeds over either WAN. What *does* change is who
owns our egress IP, so that is the signal: fetch the external IP, classify its
owner by PTR/ASN, and log one event_log row per confirmed transition.

Classification is by owner, never by IP equality — a Cox DHCP lease renewal
hands us a new address that still classifies as `primary` and logs nothing.
"""
import ipaddress
import socket
import time

import requests

from lib.settings import get_setting, get_setting_int, set_setting
from lib.events import _log_system_error, _log_success, format_duration

# Three independent ASNs. v4-pinned: a v6 answer would flip-flop against the
# cached v4 address and log churn on every poll.
_IP_PROVIDERS = (
    'https://api4.ipify.org',
    'https://ipv4.icanhazip.com',
    'https://ifconfig.me/ip',
)
# DNS-free reachability check — literals only, so a dead resolver cannot fake
# an outage.
_EGRESS_PROBES = (('1.1.1.1', 443), ('8.8.8.8', 53))

_WAN_CONFIRM_SECS = 90     # a transition must hold this long before it commits
_CLASS_RETRY_SECS = 600    # re-attempt ownership lookup while class is unknown
_STALE_GAP_SECS   = 900    # older heartbeat than this = we were not watching
_HEARTBEAT_SECS   = 300
_ERROR_LOG_SECS   = 300

_confirmed_state: str   = ''      # '', 'primary', 'backup', 'down'
_confirmed_since: float = 0.0
_since_approx: bool     = False   # since came from a boot seed, not a transition
_pending: str | None    = None
_pending_since: float   = 0.0
_loaded: bool           = False

_cached_ip: str         = ''
_cached_class: str      = ''
_cached_detail: str     = ''
_class_retry_at: float  = 0.0
_class_match_snapshot: str = ''

_probe_rotor: int       = 0
_last_obs: str          = ''
_last_obs_ts: float     = 0.0
_last_heartbeat: float  = 0.0
_last_error_log: float  = 0.0


def _match_tokens() -> list[str]:
    raw = get_setting('wan_primary_match', 'cox') or 'cox'
    return [t.strip().lower() for t in raw.split(',') if t.strip()]


def _matches_primary(text: str) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(tok in low for tok in _match_tokens())


def probe_external_ip() -> tuple[str | None, list[dict]]:
    """Return (ip, per-provider results). Rotates which provider is tried
    first so load spreads across the three."""
    global _probe_rotor
    results: list[dict] = []
    ip: str | None = None
    order = _IP_PROVIDERS[_probe_rotor:] + _IP_PROVIDERS[:_probe_rotor]
    _probe_rotor = (_probe_rotor + 1) % len(_IP_PROVIDERS)
    for url in order:
        if ip is not None:
            break
        try:
            # Bare get, no Session — a pooled socket that died with the WAN
            # must never be reused.
            r = requests.get(url, timeout=(3, 5))
            text = (r.text or '').strip()
            addr = ipaddress.ip_address(text)
            if addr.version != 4 or not addr.is_global:
                raise ValueError(f'not a global v4 address: {text}')
            ip = str(addr)
            results.append({'url': url, 'ok': True, 'ip': ip})
        except Exception as exc:
            results.append({'url': url, 'ok': False, 'error': str(exc)[:120]})
    return ip, results


def _has_egress() -> bool:
    for host, port in _EGRESS_PROBES:
        try:
            with socket.create_connection((host, port), timeout=3):
                return True
        except Exception:
            continue
    return False


def classify_ip(ip: str) -> tuple[str, str]:
    """Return (class, detail) for an egress IP. 'unknown' when neither the
    PTR nor ipinfo can tell us — we freeze rather than guess."""
    ptr = ''
    try:
        ptr = socket.gethostbyaddr(ip)[0] or ''
    except (socket.herror, socket.gaierror, OSError):
        ptr = ''
    if _matches_primary(ptr):
        return 'primary', f'ptr={ptr}'

    org = ''
    try:
        r = requests.get(f'https://ipinfo.io/{ip}/json', timeout=(3, 5))
        if r.status_code == 200:
            org = (r.json().get('org') or '').strip()
    except Exception:
        org = ''

    if org:
        if _matches_primary(org):
            return 'primary', f'org={org}'
        return 'backup', f'org={org}' + (f'; ptr={ptr}' if ptr else '')
    if ptr:
        # A present PTR that clearly is not ours is decisive on its own.
        return 'backup', f'ptr={ptr}'
    return 'unknown', 'no PTR, ipinfo unavailable'


def _class_for(ip: str) -> tuple[str, str]:
    """Cached classification — the ownership lookup runs only when the IP
    changes, when the cached class is unknown, or when the match string is
    edited (without which a Settings change would have no effect until the
    IP happened to change)."""
    global _cached_ip, _cached_class, _cached_detail
    global _class_retry_at, _class_match_snapshot
    now = time.time()
    match_now = get_setting('wan_primary_match', 'cox') or 'cox'
    stale = (ip != _cached_ip
             or match_now != _class_match_snapshot
             or (_cached_class == 'unknown' and now >= _class_retry_at))
    if stale:
        _cached_class, _cached_detail = classify_ip(ip)
        _cached_ip = ip
        _class_match_snapshot = match_now
        _class_retry_at = now + _CLASS_RETRY_SECS
    return _cached_class, _cached_detail


def _observe() -> tuple[str, str]:
    """One observation: 'primary' | 'backup' | 'down' | 'unknown'."""
    ip, _results = probe_external_ip()
    if ip is None:
        if _has_egress():
            # We can reach the internet — the lookups failed provider-side or
            # the resolver is down. No information, so this is not an outage.
            return 'unknown', 'IP providers unreachable but egress is up'
        return 'down', 'all IP providers failed and no TCP egress'
    cls, detail = _class_for(ip)
    return cls, f'IP {ip} ({detail})'


def _load_state() -> None:
    global _confirmed_state, _confirmed_since, _since_approx, _loaded
    _loaded = True
    last_poll = get_setting_int('wan_last_poll_ts', 0)
    saved = get_setting('wan_state', '') or ''
    since = get_setting_int('wan_state_since', 0)
    # Resume only if the heartbeat proves we were watching recently. Otherwise
    # a service that was off for days would resurrect a stale 'backup' and
    # claim "Cox restored after 3 days".
    if saved and since and last_poll and (time.time() - last_poll) <= _STALE_GAP_SECS:
        _confirmed_state, _confirmed_since, _since_approx = saved, float(since), False
    else:
        _confirmed_state, _confirmed_since, _since_approx = '', 0.0, False


def _commit(new_state: str, detail: str, at: float) -> None:
    global _confirmed_state, _confirmed_since, _since_approx, _pending
    prev, prev_since, approx = _confirmed_state, _confirmed_since, _since_approx
    # wan_state_since is written first: set_setting is one transaction per call,
    # so a crash between the two under-reports a duration rather than inventing
    # a transition.
    set_setting('wan_state_since', int(at))
    set_setting('wan_state', new_state)
    _confirmed_state, _confirmed_since, _since_approx = new_state, at, False
    _pending = None

    if not prev:
        # First determination since boot. Healthy is not an event.
        if new_state == 'backup':
            _log_system_error('wan', 'Started on backup WAN - Cox already down', detail)
        elif new_state == 'down':
            _log_system_error('wan', 'Started with no internet egress', detail)
        if new_state != 'primary':
            _since_approx = True
        return

    dur = format_duration(at - prev_since)
    if approx:
        dur = '~' + dur

    if new_state == 'primary':
        if prev == 'backup':
            _log_success('wan', 'wan_restored', f'Cox restored after {dur}',
                         f'{detail}; backup WAN carried traffic {dur}')
        else:
            _log_success('wan', 'wan_restored', f'Internet restored after {dur} (Cox)',
                         f'{detail}; no egress for {dur}')
    elif new_state == 'backup':
        if prev == 'primary':
            _log_system_error('wan', 'Cox down - failed over to backup WAN',
                              f'{detail}; Cox was up {dur}')
        else:
            _log_system_error('wan', f'Internet restored after {dur} - on backup WAN',
                              f'{detail}; Cox still down')
    else:  # down
        if prev == 'primary':
            _log_system_error('wan', 'Internet down - no egress from server-04',
                              f'{detail}; Cox was up {dur}')
        else:
            _log_system_error('wan', 'Internet down - backup WAN failed too',
                              f'{detail}; backup carried traffic {dur}')


def _wan_poll_once() -> dict:
    global _pending, _pending_since, _last_obs, _last_obs_ts, _last_heartbeat
    if not _loaded:
        _load_state()
    now = time.time()
    obs, detail = _observe()
    _last_obs, _last_obs_ts = obs, now

    if now - _last_heartbeat >= _HEARTBEAT_SECS:
        set_setting('wan_last_poll_ts', int(now))
        _last_heartbeat = now

    if obs == 'unknown':
        pass                                   # no information — freeze
    elif obs == _confirmed_state:
        _pending = None
    elif obs != _pending:
        _pending, _pending_since = obs, now
    else:
        interval = max(get_setting_int('wan_poll_interval', 30), 1)
        # Floor the confirm window at one interval so a commit always needs at
        # least two independent sightings.
        if now - _pending_since >= max(_WAN_CONFIRM_SECS, interval):
            _commit(obs, detail, _pending_since)

    return {'observed': obs, 'detail': detail, 'state': _confirmed_state,
            'pending': _pending}


def _wan_poll_loop():
    """Daemon thread: check egress ownership on `wan_poll_interval`.
    0 disables monitoring entirely."""
    global _last_error_log
    while True:
        try:
            interval = get_setting_int('wan_poll_interval', 30)
            if interval <= 0:
                time.sleep(60)
                continue
            interval = max(interval, 10)
            try:
                _wan_poll_once()
            except Exception as exc:
                print(f'wan poll error: {exc}')
                if time.time() - _last_error_log > _ERROR_LOG_SECS:
                    _log_system_error('wan', 'WAN monitor error', str(exc)[:500])
                    _last_error_log = time.time()
        except Exception as exc:
            print(f'wan loop error: {exc}')
            interval = 60
        time.sleep(interval)


def wan_debug_snapshot(probe: bool = False) -> dict:
    """Read-only view for /api/debug/wan. Touches no state-machine variable."""
    out = {
        'enabled': get_setting_int('wan_poll_interval', 30) > 0,
        'poll_interval': get_setting_int('wan_poll_interval', 30),
        'primary_match': get_setting('wan_primary_match', 'cox'),
        'confirm_secs': _WAN_CONFIRM_SECS,
        'state': _confirmed_state or None,
        'state_since': int(_confirmed_since) or None,
        'state_for': (format_duration(time.time() - _confirmed_since)
                      if _confirmed_since else None),
        'since_approx': _since_approx,
        'pending': _pending,
        'pending_since': int(_pending_since) or None,
        'last_observed': _last_obs or None,
        'last_observed_ts': int(_last_obs_ts) or None,
        'cached_ip': _cached_ip or None,
        'cached_class': _cached_class or None,
        'cached_detail': _cached_detail or None,
        'persisted': {
            'wan_state': get_setting('wan_state', ''),
            'wan_state_since': get_setting('wan_state_since', ''),
            'wan_last_poll_ts': get_setting('wan_last_poll_ts', ''),
        },
    }
    if probe:
        ip, results = probe_external_ip()
        out['probe'] = {'ip': ip, 'providers': results, 'egress': _has_egress()}
        if ip:
            cls, detail = classify_ip(ip)
            out['probe']['classified'] = {'class': cls, 'detail': detail}
    return out
