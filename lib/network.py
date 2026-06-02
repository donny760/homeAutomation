import os
import time
import threading

import lib.network_devices as _netdev
from lib.state import BASE_DIR
from lib.settings import get_setting, get_setting_int, get_setting_bool

NETWORK_STATE_PATH = os.path.join(BASE_DIR, 'network_devices.json')
_NETWORK_QUARANTINE_SECS = 300
_NETWORK_REMOVE_MIN_OFFLINE_DAYS = 90

_network_state: dict = _netdev.load_state(NETWORK_STATE_PATH)
_network_state_lock = threading.Lock()
_network_last_poll_ts: float = 0.0
_network_last_poll_result: dict = {}
_network_ap_quarantine: dict[str, float] = {}


def _network_router_cfg() -> dict[str, str]:
    return {
        'url':            get_setting('network_router_url', ''),
        'user':           get_setting('network_router_user', ''),
        'pass':           get_setting('network_router_pass', ''),
        'snmp_host':      get_setting('network_router_snmp_host', ''),
        'snmp_community': get_setting('network_router_snmp_community', 'public'),
        'snmp_port':      get_setting('network_router_snmp_port', '161'),
    }


def _network_ap_cfgs() -> list[dict[str, str]]:
    return _netdev.load_ap_configs(get_setting('network_aps', '[]'))


def _network_poll_once() -> dict:
    """Run one network scan, merge into state, write to JSON.
    Honors per-AP quarantine to avoid wedging DD-WRT httpd."""
    global _network_last_poll_ts, _network_last_poll_result

    now = time.time()
    all_aps = _network_ap_cfgs()
    with _network_state_lock:
        quarantine_snapshot = dict(_network_ap_quarantine)
    live_aps = [a for a in all_aps
                if quarantine_snapshot.get(a.get('name', ''), 0) <= now]

    res = _netdev.fetch_all(
        _network_router_cfg(), live_aps,
        local_subnet=get_setting('network_local_subnet', '10.0.0.0/24'),
    )

    with _network_state_lock:
        for ap_res in res.get('aps', []):
            name = ap_res.get('ap', '')
            if not name:
                continue
            if ap_res.get('errors'):
                _network_ap_quarantine[name] = now + _NETWORK_QUARANTINE_SECS
            else:
                _network_ap_quarantine.pop(name, None)
        _netdev.merge_into_state(_network_state, res.get('merged', []),
                                 now_ts=int(now))
        try:
            _netdev.save_state(NETWORK_STATE_PATH, _network_state)
        except Exception as exc:
            print(f'network state save error: {exc}')

    _network_last_poll_ts = now
    _network_last_poll_result = {
        'devices_seen': len(res.get('merged', [])),
        'aps_polled': len(res.get('aps', [])),
        'aps_skipped_quarantined': len(all_aps) - len(live_aps),
        'elapsed_ms': res.get('elapsed_ms', 0),
        'errors': sum(len(a.get('errors', [])) for a in res.get('aps', [])),
    }
    return _network_last_poll_result


def _network_poll_loop():
    """Daemon thread: poll on `network_poll_interval`, gated by `network_enabled`."""
    while True:
        try:
            interval = max(get_setting_int('network_poll_interval', 60), 30)
            if get_setting_bool('network_enabled', False):
                try:
                    _network_poll_once()
                except Exception as exc:
                    print(f'network poll error: {exc}')
        except Exception as exc:
            print(f'network loop error: {exc}')
            interval = 60
        time.sleep(interval)


def _network_state_to_list(state: dict) -> list[dict]:
    """Project state dict to a sorted list, with `online` derived from
    last_seen vs now. Filtered to the configured LAN subnet."""
    import ipaddress
    now = time.time()
    online_window = max(get_setting_int('network_poll_interval', 60) * 2, 120)
    try:
        net = ipaddress.ip_network(get_setting('network_local_subnet',
                                               '10.0.0.0/24'),
                                   strict=False)
    except ValueError:
        net = None
    out = []
    for mac, d in state.items():
        ip = d.get('last_ip')
        if net is not None and ip:
            try:
                if ipaddress.ip_address(ip) not in net:
                    continue
            except ValueError:
                continue
        last_seen = d.get('last_seen') or 0
        out.append({
            'mac': mac,
            'friendly_name': d.get('friendly_name') or '',
            'notes': d.get('notes') or '',
            'hidden': bool(d.get('hidden')),
            'vendor': d.get('vendor'),
            'last_ip': ip,
            'last_hostname': d.get('last_hostname'),
            'nbns_name': d.get('nbns_name'),
            'last_ap': d.get('last_ap'),
            'last_signal': d.get('last_signal'),
            'last_iface': d.get('last_iface'),
            'has_bans': bool(d.get('has_bans')),
            'first_seen': d.get('first_seen'),
            'last_seen': last_seen,
            'online': bool(last_seen and (now - last_seen) <= online_window),
            'seen_on': d.get('seen_on') or [],
        })

    def _ip_key(d):
        ip = d.get('last_ip') or ''
        try:
            return tuple(int(p) for p in ip.split('.'))
        except (ValueError, AttributeError):
            return (999, 0, 0, 0)

    out.sort(key=_ip_key)
    return out


def _network_ap_by_name(name: str) -> dict[str, str] | None:
    return next((a for a in _network_ap_cfgs() if a.get('name') == name), None)


def _apply_filter_ban_map(mac: str, desired: dict) -> dict:
    aps = _network_ap_cfgs()
    results = []
    for ap in aps:
        ap_name = ap.get('name', '')
        ap_changed = False
        for radio in ('wl0', 'wl1'):
            if ap_name not in desired[radio]:
                continue
            want_banned = bool(desired[radio][ap_name])
            try:
                read = _netdev.fetch_ddwrt_ap(ap['url'], ap['user'],
                                              ap['pass'], ap_name)
                current = list(read.get('filters', {})
                                   .get(radio, {}).get('list', []))
            except Exception as e:
                results.append({'ap': ap_name, 'radio': radio,
                                'ok': False, 'error': f'read: {e}'})
                continue
            current_l = [m.lower() for m in current]
            if want_banned and mac not in current_l:
                new_list = current + [mac]
            elif not want_banned and mac in current_l:
                new_list = [m for m in current if m.lower() != mac]
            else:
                results.append({'ap': ap_name, 'radio': radio,
                                'ok': True, 'noop': True})
                continue

            wr = _netdev.ddwrt_set_mac_filter(ap['url'], ap['user'],
                                              ap['pass'], radio, new_list)
            ap_changed = ap_changed or wr.get('ok', False)
            results.append({
                'ap': ap_name, 'radio': radio,
                'ok': wr.get('ok', False),
                'before': len(wr.get('before', [])),
                'after': len(wr.get('after', [])),
                'errors': wr.get('errors', []),
            })
        if ap_changed:
            with _network_state_lock:
                _network_ap_quarantine.pop(ap_name, None)

    # Persist ban state so the frontend can show the pin button even when
    # last_ap is null (e.g. device is mid-reconnect after a ban change).
    has_bans = any(bool(desired[r].get(ap)) for r in desired for ap in desired[r])
    with _network_state_lock:
        entry = _network_state.get(mac)
        if entry is not None:
            entry['has_bans'] = has_bans
            _netdev.save_state(NETWORK_STATE_PATH, _network_state)

    return {'mac': mac, 'results': results,
            'all_ok': all(r.get('ok', True) for r in results)}
