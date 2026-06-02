"""Network device discovery via web-UI scraping.

Pulls device tables from:
- Linksys LRT224 (gateway / DHCP server) — HTTPS web UI, basic auth
- DD-WRT access points — HTTP web UI, basic auth

Phase A: read-only. Returns parsed records plus the raw response so the debug
endpoints can show exactly what the device sent back when parsing misses.
"""
from __future__ import annotations

import json
import re
import ssl
import time
import urllib3
from typing import Any
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from requests.auth import HTTPBasicAuth
from urllib3.util.ssl_ import create_urllib3_context

# LRT224 ships a self-signed cert; suppress the warning once at import.
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DEFAULT_TIMEOUT = 6


class _LegacySSLAdapter(HTTPAdapter):
    """Speak TLS to ancient appliance firmware (LRT224, older DD-WRT HTTPS).

    Modern OpenSSL refuses these handshakes by default: SECLEVEL=2 forbids
    the weak ciphers/keys these devices use, and OP_LEGACY_SERVER_CONNECT is
    needed for servers that don't support secure renegotiation. Lowering
    SECLEVEL to 0 and forcing the broadest cipher set lets the handshake
    complete. Only used for devices on the LAN; we already pass verify=False.
    """

    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context(ciphers='ALL:@SECLEVEL=0')
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        # 0x4 = OP_LEGACY_SERVER_CONNECT (allow unsafe legacy renegotiation).
        ctx.options |= 0x4
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)


def _legacy_session(force_close: bool = False) -> requests.Session:
    """Build a session that talks to old appliances. `force_close` adds
    `Connection: close` — needed for LRT224, but actually harmful for
    DD-WRT's tiny httpd which prefers keep-alive."""
    s = requests.Session()
    s.mount('https://', _LegacySSLAdapter())
    s.mount('http://', HTTPAdapter())
    headers = {
        'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36'),
        'Accept': ('text/html,application/xhtml+xml,application/xml;q=0.9,'
                   'image/avif,image/webp,*/*;q=0.8'),
        'Accept-Language': 'en-US,en;q=0.9',
    }
    if force_close:
        headers['Connection'] = 'close'
    s.headers.update(headers)
    return s


_VALID_MAC_RE = re.compile(r'^[0-9a-f]{2}(:[0-9a-f]{2}){5}$')


def _norm_mac(mac: str) -> str:
    """Lowercase, colon-separated. Returns '' if input isn't a real MAC —
    callers should treat empty as "skip this row" to avoid IP-shaped strings
    or wildcards leaking into state as fake MAC keys."""
    if not mac:
        return ''
    m = re.sub(r'[^0-9a-fA-F]', '', mac)
    if len(m) != 12:
        return ''
    norm = ':'.join(m[i:i+2] for i in range(0, 12, 2)).lower()
    if not _VALID_MAC_RE.match(norm):
        return ''
    # Drop broadcast / null sentinels that show up in some ARP tables.
    if norm in ('ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00'):
        return ''
    return norm


# ── DD-WRT ────────────────────────────────────────────────────────────────────
# DD-WRT exposes Status_Wireless.live.asp and Status_Lan.live.asp as
# semicolon-terminated `var key='value';` blobs. The values of interest are
# the colon-separated arrays below. Output format is stable across builds
# from ~2014 onward.

_DDWRT_VAR_RE = re.compile(r"\{(\w+)::([^}]*)\}")


def _ddwrt_parse_live(text: str) -> dict[str, str]:
    """Parse a DD-WRT *.live.asp response into {key: value}."""
    return {m.group(1): m.group(2) for m in _DDWRT_VAR_RE.finditer(text)}


def _split_ddwrt_array(s: str) -> list[str]:
    """DD-WRT array values are quoted comma-separated: 'a','b','c'."""
    if not s:
        return []
    return [x.strip().strip("'").strip('"') for x in s.split(',')]


_MAX_FILTER_SLOTS = 256


def ddwrt_set_mac_filter(url: str, user: str, pw: str, radio: str,
                         macs: list[str]) -> dict[str, Any]:
    """Replace the entire MAC filter list for one radio (wl0 or wl1) on a
    DD-WRT AP. Returns {ok: bool, before: [...], after: [...], errors: [...]}.

    DD-WRT stores the list as 256 individual `wl{N}_mac{i}` form fields;
    unused slots are empty strings. We POST all 256 every time so DD-WRT
    rewrites the entire list deterministically. The POST goes to
    /apply.cgi with submit_button=WL_FilterTable-wl{N}, matching the form
    structure on the actual filter-list page.
    """
    if radio not in ('wl0', 'wl1'):
        return {'ok': False, 'errors': [f'invalid radio: {radio!r}']}
    base = url.rstrip('/') + '/'
    auth = HTTPBasicAuth(user, pw)
    out: dict[str, Any] = {'ok': False, 'before': [], 'after': [],
                           'errors': []}
    sess = _legacy_session(force_close=False)
    sess.headers['Referer'] = urljoin(base, 'Wireless_MAC.asp')

    # 1) Snapshot current list (returned as `before`).
    try:
        r = sess.get(urljoin(base, f'WL_FilterTable-{radio}.asp'),
                     auth=auth, verify=False, timeout=DEFAULT_TIMEOUT)
        before = re.findall(
            rf'name=["\']{radio}_mac\d+["\'][^>]*value=["\']'
            rf'((?:[0-9A-Fa-f]{{2}}[:-]){{5}}[0-9A-Fa-f]{{2}})["\']',
            r.text or '', re.IGNORECASE)
        out['before'] = [_norm_mac(m) for m in before]
    except Exception as e:
        out['errors'].append(f'read before: {e}')
        return out

    # 2) Normalize requested MAC list, dedupe, drop blanks.
    new_macs: list[str] = []
    for m in macs:
        norm = _norm_mac(m)
        if norm and norm not in new_macs:
            new_macs.append(norm)
    if len(new_macs) > _MAX_FILTER_SLOTS:
        out['errors'].append(
            f'too many MACs ({len(new_macs)} > {_MAX_FILTER_SLOTS})')
        return out

    # 3) Build form payload covering all 256 slots.
    payload: list[tuple[str, str]] = [
        ('submit_button', f'WL_FilterTable-{radio}'),
        # ApplyTake = the "Apply Settings" button. action=Apply alone only
        # saves to nvram (Save button), without restarting the wireless
        # service so the new filter engages immediately. ApplyTake commits
        # AND restarts services. Confirmed by inspecting the JS on
        # Wireless_MAC.asp: to_apply(F)→applytake(F)→action=ApplyTake.
        ('action', 'ApplyTake'),
        ('change_action', ''),
        ('submit_type', ''),
        ('ifname', radio),
        (f'{radio}_mac_list', ''),
    ]
    for i in range(_MAX_FILTER_SLOTS):
        # DD-WRT stores MACs in uppercase with `:` separators. Match the
        # firmware's own format to avoid spurious diffs in the page.
        val = new_macs[i].upper() if i < len(new_macs) else ''
        payload.append((f'{radio}_mac{i}', val))

    try:
        post = sess.post(urljoin(base, 'apply.cgi'), data=payload,
                         auth=auth, verify=False, timeout=DEFAULT_TIMEOUT * 2)
        if post.status_code not in (200, 302):
            out['errors'].append(
                f'apply.cgi returned {post.status_code}')
            return out
    except Exception as e:
        out['errors'].append(f'apply.cgi POST: {e}')
        return out

    # 4) Brief settle, then re-read to confirm.
    time.sleep(2.0)
    try:
        r2 = sess.get(urljoin(base, f'WL_FilterTable-{radio}.asp'),
                      auth=auth, verify=False, timeout=DEFAULT_TIMEOUT)
        after = re.findall(
            rf'name=["\']{radio}_mac\d+["\'][^>]*value=["\']'
            rf'((?:[0-9A-Fa-f]{{2}}[:-]){{5}}[0-9A-Fa-f]{{2}})["\']',
            r2.text or '', re.IGNORECASE)
        out['after'] = [_norm_mac(m) for m in after]
    except Exception as e:
        out['errors'].append(f'read after: {e}')
        return out

    out['ok'] = sorted(out['after']) == sorted(new_macs)

    # Sub-page ApplyTake commits the filter list to nvram but doesn't
    # restart the radio service, so already-associated banned stations
    # stay connected. Manually re-applying the parent Wireless_MAC.asp
    # form via ApplyTake triggers the wifi restart that actually kicks
    # them. We replay every hidden+visible input from the parent form
    # so we don't accidentally clear unrelated settings.
    if out['ok']:
        try:
            r3 = sess.get(urljoin(base, 'Wireless_MAC.asp'),
                          auth=auth, verify=False, timeout=DEFAULT_TIMEOUT)
            parent_html = r3.text or ''
            parent_payload: list[tuple[str, str]] = []
            seen_names: set[str] = set()
            # Pull all <input> name/value pairs from inside the wireless
            # form. Order matters less than completeness — DD-WRT only
            # cares that every nvram-bound field is present.
            for m in re.finditer(
                    r'<input[^>]*\bname=["\']([^"\']+)["\'][^>]*\bvalue=["\']([^"\']*)["\'][^>]*>'
                    r'|<input[^>]*\bvalue=["\']([^"\']*)["\'][^>]*\bname=["\']([^"\']+)["\'][^>]*>',
                    parent_html, re.IGNORECASE):
                name = m.group(1) or m.group(4)
                value = m.group(2) if m.group(1) else m.group(3)
                if not name:
                    continue
                # Don't double-include radio buttons — only checked ones.
                tag_match = re.search(
                    rf'<input[^>]*name=["\']{re.escape(name)}["\'][^>]*value=["\']{re.escape(value)}["\'][^>]*>'
                    rf'|<input[^>]*value=["\']{re.escape(value)}["\'][^>]*name=["\']{re.escape(name)}["\'][^>]*>',
                    parent_html, re.IGNORECASE)
                tag = tag_match.group(0) if tag_match else ''
                is_radio = 'type="radio"' in tag.lower() or "type='radio'" in tag.lower()
                if is_radio and 'checked' not in tag.lower():
                    continue
                key = (name, value) if is_radio else name
                if key in seen_names:
                    continue
                seen_names.add(key if is_radio else name)
                parent_payload.append((name, value))
            # Force ApplyTake (Apply Settings button), even if the form's
            # default action attribute was Apply.
            parent_payload = [(k, v) for k, v in parent_payload if k != 'action']
            parent_payload.insert(0, ('action', 'ApplyTake'))
            sess.post(urljoin(base, 'apply.cgi'), data=parent_payload,
                      auth=auth, verify=False, timeout=DEFAULT_TIMEOUT * 2)
        except Exception as e:
            out['errors'].append(f'parent ApplyTake (deauth): {e}')

    return out


def ddwrt_probe(url: str, user: str, pw: str) -> dict[str, Any]:
    """Inspect what auth scheme the AP advertises and try a few common
    user/auth-mode combos to find one that returns 200."""
    base = url.rstrip('/') + '/'
    sess = _legacy_session()
    out: dict[str, Any] = {'url': url, 'unauth': {}, 'attempts': []}

    # 1) No-auth GET — should return 401 with WWW-Authenticate header.
    try:
        r = sess.get(urljoin(base, 'Status_Wireless.live.asp'), verify=False,
                     timeout=DEFAULT_TIMEOUT)
        out['unauth'] = {
            'status': r.status_code,
            'www_authenticate': r.headers.get('WWW-Authenticate'),
            'server': r.headers.get('Server'),
            'len': len(r.text),
        }
    except Exception as e:
        out['unauth'] = {'error': str(e)}

    # 2) Try several auth combos.
    from requests.auth import HTTPDigestAuth
    combos: list[tuple[str, Any]] = [
        (f'basic / {user}',  HTTPBasicAuth(user, pw)),
        ('basic / root',     HTTPBasicAuth('root', pw)),
        ('basic / admin',    HTTPBasicAuth('admin', pw)),
        (f'digest / {user}', HTTPDigestAuth(user, pw)),
        ('digest / root',    HTTPDigestAuth('root', pw)),
        ('digest / admin',   HTTPDigestAuth('admin', pw)),
    ]
    for label, a in combos:
        s2 = _legacy_session()
        try:
            r = s2.get(urljoin(base, 'Status_Wireless.live.asp'), auth=a,
                       verify=False, timeout=DEFAULT_TIMEOUT)
            out['attempts'].append({
                'auth': label,
                'status': r.status_code,
                'len': len(r.text),
                'has_assoclist': 'active_wireless' in (r.text or ''),
            })
        except Exception as e:
            out['attempts'].append({'auth': label, 'error': str(e)})
    return out


def fetch_ddwrt_ap(url: str, user: str, pw: str, ap_name: str) -> dict[str, Any]:
    """Fetch wireless + LAN status from one DD-WRT AP.

    Returns {ap, devices: [...], filter_mode, filter_list, raw: {...}, errors: [...]}.
    """
    base = url.rstrip('/') + '/'
    auth = HTTPBasicAuth(user, pw)
    sess = _legacy_session(force_close=False)
    out: dict[str, Any] = {
        'ap': ap_name,
        'url': url,
        'devices': [],
        'filter_mode': None,   # 'disable' | 'allow' | 'deny'
        'filter_list': [],
        'raw': {},
        'errors': [],
    }

    def _get(path: str) -> str | None:
        try:
            r = sess.get(urljoin(base, path), auth=auth, verify=False,
                         timeout=DEFAULT_TIMEOUT)
            if r.status_code == 401:
                out['errors'].append(
                    f'{path}: 401 — WWW-Authenticate: '
                    f'{r.headers.get("WWW-Authenticate", "<none>")}'
                )
                return None
            r.raise_for_status()
            return r.text
        except Exception as e:
            out['errors'].append(f'{path}: {e}')
            return None

    # 1) Wireless associations: MAC, signal/noise, uptime, TX/RX rates.
    wl_text = _get('Status_Wireless.live.asp')
    if wl_text:
        out['raw']['wireless'] = wl_text[:8000]
        wl = _ddwrt_parse_live(wl_text)
        # active_wireless format varies between DD-WRT builds — older builds
        # use 11 cols (mac, _, iface, uptime, tx, rx, info, sig, noise, snr,
        # quality), newer ones add 4 trailing packet-stat zeros (15 cols).
        # Mixed-firmware fleets like Don's hit BOTH on different APs, so
        # don't try to fix a column count: each row starts with a MAC, so
        # find MAC indices and slice between them.
        arr = _split_ddwrt_array(wl.get('active_wireless', ''))
        mac_idxs = [i for i, c in enumerate(arr)
                    if re.match(r'^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$', c or '')]
        for i, start in enumerate(mac_idxs):
            end = mac_idxs[i + 1] if i + 1 < len(mac_idxs) else len(arr)
            row = arr[start:end]
            mac = _norm_mac(row[0])
            if not mac:
                continue
            try:
                signal = int(row[7]) if len(row) > 7 else None
            except ValueError:
                signal = None
            iface = row[2] if len(row) > 2 else None
            out['devices'].append({
                'mac': mac,
                'ip': None,
                'hostname': None,
                'ap': ap_name,
                'iface': iface,         # wl0 / wl1 — useful for 2.4 vs 5 GHz
                'signal': signal,
                'source': 'ddwrt-wireless',
            })

    # 2) LAN status: ARP table + DHCP leases. Same column-drift problem
    # as active_wireless above — anchor on MAC positions instead of
    # assuming a fixed column count.
    #   arp_table layout:    hostname, ip, mac, conn_count    (4 cols, MAC@2)
    #   dhcp_leases layout:  hostname, ip, mac, expires, idx  (5 cols, MAC@2)
    lan_text = _get('Status_Lan.live.asp')

    def _emit_macanchored(arr: list[str], source: str) -> None:
        mac_idxs = [i for i, c in enumerate(arr)
                    if re.match(r'^[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}$', c or '')]
        for j, mi in enumerate(mac_idxs):
            mac = _norm_mac(arr[mi])
            if not mac:
                continue
            # MAC is at offset +2 from row start in both layouts.
            row_start = max(mi - 2, 0)
            ip = arr[row_start + 1] if row_start + 1 < len(arr) else ''
            host = arr[row_start] if row_start < len(arr) else ''
            out['devices'].append({
                'mac': mac,
                'ip': ip if ip and ip != '*' else None,
                'hostname': host if host and host != '*' else None,
                'ap': ap_name,
                'signal': None,
                'source': source,
            })

    if lan_text:
        out['raw']['lan'] = lan_text[:8000]
        lan = _ddwrt_parse_live(lan_text)
        _emit_macanchored(_split_ddwrt_array(lan.get('arp_table', '')),
                          'ddwrt-arp')
        _emit_macanchored(_split_ddwrt_array(lan.get('dhcp_leases', '')),
                          'ddwrt-dhcp')

    # 3) MAC filter — Wireless_MAC.asp shows per-radio enable/mode toggles
    # but NOT the actual MAC list. The list lives behind the "Edit MAC
    # Filter List" button on a per-radio sub-page (WL_FilterTable-wl0.asp /
    # WL_FilterTable-wl1.asp), where each banned MAC sits in an input named
    # `wl{N}_mac{i}`.
    mac_text = _get('Wireless_MAC.asp')
    if mac_text:
        out['raw']['mac_filter'] = mac_text[:8000]
        out['filters'] = {}
        for unit in (0, 1):
            radio = f'wl{unit}'
            # The HTML uses two pairs of radio buttons per radio:
            #   name="wlN_macmode1" value="other"     → filter enabled
            #   name="wlN_macmode1" value="disabled"  → filter disabled
            #   name="wlN_macmode"  value="deny"      → ban list = block these
            #   name="wlN_macmode"  value="allow"     → ban list = allow only these
            # Attribute order varies, so match the input tag generally and
            # check for `checked` and `value=...` independently.
            tag_re = re.compile(
                rf'<input[^>]*name=["\']{radio}_macmode(1?)["\'][^>]*>',
                re.IGNORECASE)
            mode = None
            enabled = False
            for m in tag_re.finditer(mac_text):
                tag = m.group(0)
                is_enable_radio = m.group(1) == '1'
                if 'checked' not in tag.lower():
                    continue
                vm = re.search(r'value=["\']([^"\']+)["\']', tag, re.I)
                if not vm:
                    continue
                v = vm.group(1).lower()
                if is_enable_radio:
                    enabled = (v == 'other')
                else:
                    if v in ('deny', 'allow', 'disable'):
                        mode = v
            list_text = _get(f'WL_FilterTable-{radio}.asp')
            macs: list[str] = []
            if list_text:
                # Inputs are named wl{N}_mac{i}, value=<MAC>. Empty values
                # are unused slots — skip them.
                input_re = re.compile(
                    rf'name=["\']{radio}_mac\d+["\'][^>]*value=["\']'
                    rf'((?:[0-9A-Fa-f]{{2}}[:-]){{5}}[0-9A-Fa-f]{{2}})["\']',
                    re.IGNORECASE)
                for m in input_re.finditer(list_text):
                    norm = _norm_mac(m.group(1))
                    if norm and norm not in macs:
                        macs.append(norm)
            out['filters'][radio] = {
                'mode': mode,
                'enabled': enabled,
                'list': macs,
            }
        # Backwards-compat aliases (old debug endpoints still reference the
        # flat fields). Surface wl0 by default.
        out['filter_mode'] = out['filters']['wl0']['mode']
        out['filter_list'] = out['filters']['wl0']['list']

    return out


# ── LRT224 ────────────────────────────────────────────────────────────────────
# The LRT224 web UI is HTML with embedded tables. The two pages of interest
# are System Status → DHCP Status (DHCP leases) and Setup → ARP Binding (ARP
# table). Exact paths vary by firmware build; we try a few known ones and
# return whichever returns 200. Phase A verification will confirm the right
# path for Don's specific firmware.

# Candidate paths, ordered by likelihood for current LRT224 firmware (2.0.x).
_LRT224_DHCP_PATHS = [
    'dhcp_status_router.htm',
    'sysinfo_DHCPStatus.htm',
    'StatusDhcp.htm',
    'system_dhcp.htm',
]
_LRT224_ARP_PATHS = [
    'arp_table.htm',
    'sysinfo_arp.htm',
    'system_arp.htm',
    'ARPTable.htm',
]

# A row is any <tr> containing both a MAC-shaped string and an IP-shaped one.
_MAC_RE = re.compile(r'\b([0-9a-fA-F]{2}(?:[:\-][0-9a-fA-F]{2}){5})\b')
_IP_RE = re.compile(r'\b((?:\d{1,3}\.){3}\d{1,3})\b')
_TR_RE = re.compile(r'<tr\b[^>]*>(.*?)</tr>', re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r'<[^>]+>')


def _strip_html(s: str) -> str:
    return re.sub(r'\s+', ' ', _TAG_RE.sub(' ', s)).strip()


def _scrape_table(html: str, source: str) -> list[dict[str, Any]]:
    """Pull (mac, ip, hostname) triples from any HTML table rows."""
    rows: list[dict[str, Any]] = []
    for tr in _TR_RE.findall(html or ''):
        macs = _MAC_RE.findall(tr)
        ips = _IP_RE.findall(tr)
        if not macs or not ips:
            continue
        text = _strip_html(tr)
        # Hostname heuristic: any token that's not the MAC, IP, or pure number.
        tokens = [t for t in text.split()
                  if not _MAC_RE.fullmatch(t)
                  and not _IP_RE.fullmatch(t)
                  and not re.fullmatch(r'[\d:.\-/]+', t)
                  and t not in ('--', '-', 'N/A', '*')]
        hostname = tokens[0] if tokens else None
        rows.append({
            'mac': _norm_mac(macs[0]),
            'ip': ips[0],
            'hostname': hostname,
            'ap': None,
            'signal': None,
            'source': source,
        })
    return rows


_META_REFRESH_RE = re.compile(
    r'<meta[^>]+http-equiv=["\']?refresh["\']?[^>]+url=([^"\'>\s]+)',
    re.IGNORECASE)


def _fetch_with_meta_refresh(sess: requests.Session, url: str,
                             max_hops: int = 4) -> tuple[requests.Response, list[str]]:
    """Follow meta-refresh redirects (LRT224 redirects via HTML, not HTTP)."""
    hops: list[str] = []
    cur = url
    for _ in range(max_hops):
        hops.append(cur)
        r = sess.get(cur, verify=False, timeout=DEFAULT_TIMEOUT,
                     allow_redirects=True)
        m = _META_REFRESH_RE.search(r.text or '')
        if not m or len(r.text) > 400:  # real page, not a redirect stub
            return r, hops
        next_url = m.group(1).strip()
        if next_url.startswith('/'):
            from urllib.parse import urlparse
            p = urlparse(cur)
            next_url = f'{p.scheme}://{p.netloc}{next_url}'
        elif not next_url.startswith('http'):
            next_url = urljoin(cur, next_url)
        cur = next_url
    return r, hops


def _nbns_query(ip: str, timeout: float = 0.6) -> str | None:
    """One-shot NetBIOS Name Service Node Status query (UDP 137).

    Picks up Windows hostnames that don't have reverse-DNS PTR records.
    Returns the first non-group NetBIOS name registered, or None.
    """
    import socket
    # Wildcard-name query: 0x20 + 32 bytes of "CK" repeated + 0x00 terminator.
    encoded = b'\x20' + (b'CK' * 16) + b'\x00'
    pkt = (
        b'\xa3\x44'           # transaction id (arbitrary)
        b'\x00\x00'           # flags
        b'\x00\x01'           # questions = 1
        b'\x00\x00\x00\x00\x00\x00'
        + encoded
        + b'\x00\x21'         # type = NBSTAT
        b'\x00\x01'           # class = IN
    )
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(pkt, (ip, 137))
        data, _ = s.recvfrom(2048)
        s.close()
    except Exception:
        return None

    # Skip the response header (12 bytes), the echoed question (encoded
    # name + 4 bytes of type+class), and 10 bytes of RR header. Then we
    # land on the name count (1 byte) followed by name entries: 15 bytes
    # padded name + 0x00 suffix byte + 2 bytes of flags.
    try:
        idx = 12 + len(encoded) + 4 + 10
        count = data[idx]
        idx += 1
        for i in range(count):
            name = data[idx:idx + 15].rstrip(b' \x00').decode('ascii', 'ignore')
            suffix = data[idx + 15]
            flags = int.from_bytes(data[idx + 16:idx + 18], 'big')
            idx += 18
            # Suffix 0x00 = workstation, 0x20 = file server. Skip group
            # entries (flags bit 0x8000 = group).
            if suffix in (0x00, 0x20) and not (flags & 0x8000) and name:
                return name
    except Exception:
        pass
    return None


def _ssdp_unicast(ip: str, timeout: float = 1.0) -> dict[str, str] | None:
    """Send a unicast SSDP M-SEARCH directly to one host on UDP 1900.

    More reliable than multicast on Windows when virtual NICs (Hyper-V,
    WSL) confuse interface selection. Returns parsed headers or None.
    """
    import socket
    msg = (
        b'M-SEARCH * HTTP/1.1\r\n'
        b'HOST: 239.255.255.250:1900\r\n'
        b'MAN: "ssdp:discover"\r\n'
        b'MX: 1\r\n'
        b'ST: ssdp:all\r\n\r\n'
    )
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        s.sendto(msg, (ip, 1900))
        data, _ = s.recvfrom(4096)
        s.close()
    except Exception:
        return None
    headers: dict[str, str] = {}
    for line in data.decode('utf-8', 'ignore').splitlines()[1:]:
        if ':' in line:
            k, v = line.split(':', 1)
            headers[k.strip().upper()] = v.strip()
    return headers or None


def fetch_local_arp(subnet: str = '10.0.0.0/24',
                    ping_timeout_ms: int = 400,
                    ping_concurrency: int = 64) -> dict[str, Any]:
    """Build a device map from the dashboard host's own LAN visibility.

    Strategy: parallel ping-sweep the subnet to populate the local ARP
    cache, then parse `arp -a` output. Captures every device that responds
    to ICMP plus anything we've recently talked to. This bypasses the
    LRT224's broken SNMP and the LRT224 web UI's JS shell entirely.
    """
    import ipaddress
    import subprocess
    from concurrent.futures import ThreadPoolExecutor, as_completed

    out: dict[str, Any] = {
        'subnet': subnet,
        'devices': [],
        'pinged': 0,
        'pingable': 0,
        'errors': [],
    }

    try:
        net = ipaddress.ip_network(subnet, strict=False)
    except ValueError as e:
        out['errors'].append(f'bad subnet: {e}')
        return out

    # Cap to /24-equivalent so we don't ping huge subnets.
    if net.num_addresses > 512:
        out['errors'].append(f'subnet too large ({net.num_addresses} addrs); '
                             f'use /24 or smaller')
        return out

    is_windows = (re.match(r'^win', __import__('sys').platform) is not None)
    if is_windows:
        ping_args = ['ping', '-n', '1', '-w', str(ping_timeout_ms)]
    else:
        ping_args = ['ping', '-c', '1', '-W', str(max(1, ping_timeout_ms // 1000))]

    def _ping(ip: str) -> bool:
        try:
            r = subprocess.run(ping_args + [ip], capture_output=True,
                               timeout=(ping_timeout_ms / 1000) + 2)
            return r.returncode == 0
        except Exception:
            return False

    hosts = [str(h) for h in net.hosts()]
    out['pinged'] = len(hosts)
    with ThreadPoolExecutor(max_workers=ping_concurrency) as ex:
        for fut in as_completed([ex.submit(_ping, h) for h in hosts]):
            if fut.result():
                out['pingable'] += 1

    # Dump local ARP cache.
    try:
        if is_windows:
            r = subprocess.run(['arp', '-a'], capture_output=True, text=True,
                               timeout=10)
            arp_text = r.stdout
        else:
            r = subprocess.run(['arp', '-an'], capture_output=True, text=True,
                               timeout=10)
            arp_text = r.stdout
    except Exception as e:
        out['errors'].append(f'arp dump: {e}')
        return out

    seen: set[tuple[str, str]] = set()
    for line in arp_text.splitlines():
        ip_m = _IP_RE.search(line)
        mac_m = _MAC_RE.search(line)
        if not ip_m or not mac_m:
            continue
        ip = ip_m.group(1)
        try:
            if ipaddress.ip_address(ip) not in net:
                continue
        except ValueError:
            continue
        mac = _norm_mac(mac_m.group(1))
        if mac in {'ff:ff:ff:ff:ff:ff', '00:00:00:00:00:00'}:
            continue
        if (ip, mac) in seen:
            continue
        seen.add((ip, mac))
        out['devices'].append({
            'mac': mac,
            'ip': ip,
            'hostname': None,
            'vendor': None,
            'ap': None,
            'signal': None,
            'source': 'local-arp',
        })

    # Enrich (parallel where useful):
    #   1. reverse-DNS hostnames (LAN DNS, ~1s for /24)
    #   2. NBNS Node Status (Windows hosts, ~1s parallelized)
    #   3. SSDP M-SEARCH multicast (smart TVs, NAS, media servers, ~2s)
    #   4. MAC vendor OUI lookup (offline DB, instant)
    import socket

    def _rdns(ip: str) -> str | None:
        try:
            return socket.gethostbyaddr(ip)[0]
        except Exception:
            return None

    with ThreadPoolExecutor(max_workers=64) as ex:
        rdns_futs = {ex.submit(_rdns, d['ip']): d for d in out['devices']}
        nbns_futs = {ex.submit(_nbns_query, d['ip']): d for d in out['devices']}
        for fut in as_completed(rdns_futs):
            host = fut.result()
            if host:
                rdns_futs[fut]['hostname'] = host
        for fut in as_completed(nbns_futs):
            nb = fut.result()
            if nb and not nbns_futs[fut].get('hostname'):
                nbns_futs[fut]['hostname'] = nb
            if nb:
                nbns_futs[fut]['nbns_name'] = nb

    with ThreadPoolExecutor(max_workers=32) as ex:
        ssdp_futs = {ex.submit(_ssdp_unicast, d['ip']): d for d in out['devices']}
        for fut in as_completed(ssdp_futs):
            info = fut.result()
            if info:
                ssdp_futs[fut]['ssdp_server'] = info.get('SERVER')
                ssdp_futs[fut]['ssdp_location'] = info.get('LOCATION')
                # If no hostname yet, try to use the SSDP friendly name
                # (often more useful than vendor on its own).
                if info.get('SERVER') and not ssdp_futs[fut].get('hostname'):
                    ssdp_futs[fut]['ssdp_friendly'] = info['SERVER']

    try:
        from mac_vendor_lookup import MacLookup
        ml = MacLookup()
        for d in out['devices']:
            try:
                d['vendor'] = ml.lookup(d['mac'])
            except Exception:
                pass
    except Exception as e:
        out['errors'].append(f'vendor lookup unavailable: {e}')

    return out


def fetch_lrt224_snmp(host: str, community: str = 'public',
                      port: int = 161, timeout: int = 4) -> dict[str, Any]:
    """Pull the ARP table (ipNetToMediaTable) from the LRT224 over SNMPv2c.

    Returns {'devices': [...], 'sys': {...}, 'errors': [...]}.

    Each device row: mac, ip, source='lrt224-snmp-arp'. The router's ARP
    table covers every device that's communicated on the LAN, so this is
    effectively the master device list for the network.
    """
    out: dict[str, Any] = {'host': host, 'devices': [], 'sys': {}, 'errors': []}
    try:
        import asyncio
        from pysnmp.hlapi.v3arch.asyncio import (
            SnmpEngine, CommunityData, UdpTransportTarget, ContextData,
            ObjectType, ObjectIdentity, get_cmd, walk_cmd)
    except Exception as e:
        out['errors'].append(f'pysnmp import failed: {e}')
        return out
    out.setdefault('raw_oids', [])

    sys_oids = {
        'sysDescr':  '1.3.6.1.2.1.1.1.0',
        'sysName':   '1.3.6.1.2.1.1.5.0',
        'sysUpTime': '1.3.6.1.2.1.1.3.0',
    }
    arp_oid = '1.3.6.1.2.1.4.22.1'
    arp_mac_col = '1.3.6.1.2.1.4.22.1.2.'

    by_key: dict[tuple[int, str], dict[str, Any]] = {}

    async def _run() -> None:
        engine = SnmpEngine()
        auth = CommunityData(community, mpModel=1)  # mpModel=1 → SNMPv2c
        target = await UdpTransportTarget.create((host, port), timeout=timeout,
                                                 retries=1)
        ctx = ContextData()

        # 1) System info — confirms SNMP handshake works.
        for label, oid in sys_oids.items():
            try:
                err_ind, err_status, _, var_binds = await get_cmd(
                    engine, auth, target, ctx,
                    ObjectType(ObjectIdentity(oid)))
                if err_ind:
                    out['errors'].append(f'sysinfo {label}: {err_ind}')
                    continue
                if err_status:
                    out['errors'].append(f'sysinfo {label}: {err_status.prettyPrint()}')
                    continue
                for vb in var_binds:
                    out['sys'][label] = vb[1].prettyPrint()
            except Exception as e:
                out['errors'].append(f'sysinfo {label}: {e}')

        # 2) ipNetToMediaTable walk. We only care about column 2 (MAC), but
        # we capture all rows with raw OIDs for debugging.
        try:
            async for err_ind, err_status, _, var_binds in walk_cmd(
                    engine, auth, target, ctx,
                    ObjectType(ObjectIdentity(arp_oid)),
                    lexicographicMode=False):
                if err_ind:
                    out['errors'].append(f'arp walk: {err_ind}')
                    break
                if err_status:
                    out['errors'].append(f'arp walk: {err_status.prettyPrint()}')
                    break
                for vb in var_binds:
                    oid_str = str(vb[0])
                    val = vb[1]
                    # Capture first ~80 raw OIDs for diagnostics.
                    if len(out['raw_oids']) < 80:
                        try:
                            raw_repr = val.prettyPrint()
                        except Exception:
                            raw_repr = repr(val)
                        out['raw_oids'].append(f'{oid_str} = {raw_repr}')

                    if not oid_str.startswith(arp_mac_col):
                        continue
                    tail = oid_str[len(arp_mac_col):].split('.')
                    if len(tail) < 5:
                        continue
                    try:
                        ifindex = int(tail[0])
                        ip = '.'.join(tail[1:5])
                    except ValueError:
                        continue
                    try:
                        raw = val.asOctets()
                    except Exception:
                        raw = b''
                    if len(raw) == 6:
                        mac = ':'.join(f'{b:02x}' for b in raw)
                    else:
                        mac = _norm_mac(raw.decode('latin1', 'ignore'))
                    by_key[(ifindex, ip)] = {'ip': ip, 'mac': mac,
                                             'iface_index': ifindex}
        except Exception as e:
            out['errors'].append(f'arp walk: {e}')

        engine.close_dispatcher()

    try:
        asyncio.run(_run())
    except Exception as e:
        out['errors'].append(f'asyncio: {e}')

    for entry in by_key.values():
        if not entry.get('mac'):
            continue
        if entry.get('ip') in (None, '', '0.0.0.0'):
            continue  # SNMP stub rows on the LRT224 — never real ARP entries.
        out['devices'].append({
            'mac': entry['mac'],
            'ip':  entry['ip'],
            'hostname': None,
            'ap': None,
            'signal': None,
            'iface_index': entry['iface_index'],
            'source': 'lrt224-snmp-arp',
        })
    return out


def lrt224_login(url: str, user: str, pw: str) -> requests.Session | None:
    """Authenticate against the LRT224 web UI and return a session ready to
    fetch protected pages, or None on failure.

    The router serves the login form HTML from welcome.cgi only when the
    request includes a same-origin Referer/Origin (without those it returns
    a 104-byte meta-refresh stub). The form embeds a per-page-load
    `auth_key`, and the password is sent as `md5(plaintext_password +
    auth_key)`. Login POSTs to userLogin.cgi; success returns a session
    cookie (PHPSESSID-style)."""
    import hashlib

    base = url.rstrip('/') + '/'
    sess = _legacy_session(force_close=True)
    sess.headers['Referer'] = base
    sess.headers['Origin']  = base.rstrip('/')

    try:
        r = sess.get(urljoin(base, 'cgi-bin/welcome.cgi'),
                     verify=False, timeout=DEFAULT_TIMEOUT)
    except Exception as e:
        print(f'lrt224 GET welcome.cgi failed: {e}')
        return None
    if len(r.text) < 1000:
        # Got the redirect stub, not the login page. Likely the legacy SSL
        # adapter or headers are off — bail rather than spinning.
        return None

    m = re.search(r'name="auth_key"\s+value=["\'](\d+)["\']', r.text)
    if not m:
        print('lrt224: auth_key not found in welcome.cgi response')
        return None
    auth_key = m.group(1)
    hashed = hashlib.md5((pw + auth_key).encode('utf-8')).hexdigest()

    sess.headers['Referer'] = urljoin(base, 'cgi-bin/welcome.cgi')
    payload = {
        'login': 'true',
        'portalname': 'CommonPortal',
        'ModelName': 'LRT224',
        'password_expired': '0',
        'username': user,
        'password': hashed,
        'auth_key': auth_key,
        'md5_old_pass': '',
    }
    try:
        r2 = sess.post(urljoin(base, 'cgi-bin/userLogin.cgi'),
                       data=payload, verify=False,
                       timeout=DEFAULT_TIMEOUT, allow_redirects=False)
    except Exception as e:
        print(f'lrt224 POST userLogin.cgi failed: {e}')
        return None

    # Either we now have a session cookie OR the response sets one. Quick
    # validation: hit a protected page and see if it returns real content.
    test = sess.get(urljoin(base, 'dhcp_status.htm'), verify=False,
                    timeout=DEFAULT_TIMEOUT)
    if len(test.text) < 500:
        return None
    return sess


# LRT224 DHCP page format isn't a real HTML table — the data is embedded
# in a hidden form input as a flat string with `;`-separated entries, each
# entry shaped `<hostname> &&<ip>&<mac>& <lease>`. Hostname allows only
# safe chars so the first entry doesn't absorb leading form HTML.
_DHCP_ROW_RE = re.compile(
    r'([A-Za-z0-9_.:\-]+)\s*&&\s*'
    r'((?:\d{1,3}\.){3}\d{1,3})\s*&\s*'
    r'((?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2})'
    r'(?:\s*&\s*([^;]*))?',
    re.IGNORECASE)


def _filter_hostname(name: str | None) -> str | None:
    r"""Drop LRT224 placeholder hostnames (`new-host\d+`) per user feedback,
    plus any leftover HTML from the surrounding form (the dhcp_status.htm
    body embeds the data string mid-form, so the first entry's hostname
    capture can absorb form HTML if we don't filter it)."""
    if not name:
        return None
    name = name.strip()
    if not name:
        return None
    if re.match(r'^new-host\d+$', name, re.IGNORECASE):
        return None
    # Anything with HTML or newlines isn't a real hostname.
    if any(c in name for c in '<>\n\r"='):
        return None
    return name


def fetch_lrt224_dhcp(sess: requests.Session, url: str) -> dict[str, Any]:
    """Pull DHCP status + static reservation tables. Returns
    {'devices': [...], 'errors': [...]}."""
    base = url.rstrip('/') + '/'
    out: dict[str, Any] = {'devices': [], 'errors': []}

    for path, source in [('dhcp_status.htm', 'lrt224-dhcp'),
                         ('dhcp_static.htm', 'lrt224-static')]:
        try:
            r = sess.get(urljoin(base, path), verify=False,
                         timeout=DEFAULT_TIMEOUT)
        except Exception as e:
            out['errors'].append(f'{path}: {e}')
            continue
        if len(r.text) < 500:
            out['errors'].append(f'{path}: stub response (session expired?)')
            continue
        for m in _DHCP_ROW_RE.finditer(r.text):
            host = _filter_hostname(m.group(1))
            ip   = m.group(2)
            mac  = _norm_mac(m.group(3))
            if not mac:
                continue
            out['devices'].append({
                'mac': mac,
                'ip': ip,
                'hostname': host,
                'ap': None,
                'signal': None,
                'source': source,
            })
    return out


def lrt224_probe_login(url: str) -> dict[str, Any]:
    """Fetch the login page so we can see the form structure (field names,
    action URL). LRT224 uses CGI session cookies, not basic auth."""
    base = url.rstrip('/') + '/'
    out: dict[str, Any] = {'url': url, 'pages': {}}
    for p in ['', 'cgi-bin/welcome.cgi', 'cgi-bin/login.cgi',
              'login.htm', 'home.htm', 'index.htm']:
        # Fresh session per page — some firmwares choke on session reuse.
        sess = _legacy_session()
        try:
            r, hops = _fetch_with_meta_refresh(sess, urljoin(base, p))
            out['pages'][p or '/'] = {
                'hops': hops,
                'final_url': r.url,
                'status': r.status_code,
                'len': len(r.text),
                'cookies': dict(sess.cookies),
                'forms': re.findall(r'<form[^>]*>', r.text, re.IGNORECASE),
                'inputs': re.findall(r'<input[^>]*>', r.text, re.IGNORECASE)[:30],
                'body_head': r.text[:6000],
            }
        except Exception as e:
            out['pages'][p or '/'] = {'error': str(e)}
    return out


def fetch_lrt224(url: str, user: str, pw: str) -> dict[str, Any]:
    """Fetch DHCP + ARP tables from the LRT224. Tries multiple known paths
    and returns whichever produces table rows."""
    base = url.rstrip('/') + '/'
    auth = HTTPBasicAuth(user, pw)
    # LRT224 closes the connection after each response — force_close avoids
    # ConnectionReset on the next request when keep-alive is assumed.
    sess = _legacy_session(force_close=True)
    out: dict[str, Any] = {
        'url': url,
        'devices': [],
        'raw': {},
        'paths_tried': {'dhcp': [], 'arp': []},
        'errors': [],
    }

    def _try(paths: list[str], kind: str, source: str) -> list[dict[str, Any]]:
        for p in paths:
            full = urljoin(base, p)
            try:
                r = sess.get(full, auth=auth, verify=False,
                             timeout=DEFAULT_TIMEOUT)
                out['paths_tried'][kind].append({'path': p, 'status': r.status_code,
                                                 'len': len(r.text)})
                if r.status_code != 200:
                    continue
                rows = _scrape_table(r.text, source)
                if rows:
                    out['raw'][kind] = r.text[:12000]
                    return rows
                # Save first 200 OK even if empty, for debugging.
                if kind not in out['raw']:
                    out['raw'][kind] = r.text[:12000]
            except Exception as e:
                out['errors'].append(f'{p}: {e}')
        return []

    out['devices'].extend(_try(_LRT224_DHCP_PATHS, 'dhcp', 'lrt224-dhcp'))
    out['devices'].extend(_try(_LRT224_ARP_PATHS, 'arp', 'lrt224-arp'))
    return out


# ── Merge ─────────────────────────────────────────────────────────────────────
def merge_by_mac(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse per-source records into one entry per MAC.

    Later records override IP/hostname when the earlier value was empty.
    `seen_on` is the union of source labels and AP names.
    """
    merged: dict[str, dict[str, Any]] = {}
    for r in records:
        mac = r.get('mac')
        if not mac:
            continue
        cur = merged.setdefault(mac, {
            'mac': mac, 'ip': None, 'hostname': None, 'vendor': None,
            'ap': None, 'signal': None, 'seen_on': [],
        })
        # Take first non-null for fields we accumulate, except ap/signal
        # which always take the latest (so wireless data overrides).
        for k in ('ip', 'hostname', 'vendor', 'nbns_name',
                  'ssdp_server', 'ssdp_location', 'iface'):
            if r.get(k) and not cur.get(k):
                cur[k] = r[k]
        # Only take ap from wireless client list entries (they have iface set).
        # ARP table records set ap=ap_name but have no iface — ignore those.
        if r.get('ap') and r.get('iface'):
            cur['ap'] = r['ap']
        if r.get('signal') is not None:
            cur['signal'] = r['signal']
        label = r.get('source', '')
        if r.get('ap'):
            label = f"{label}@{r['ap']}"
        if label and label not in cur['seen_on']:
            cur['seen_on'].append(label)
    return sorted(merged.values(), key=lambda d: (d['ip'] or '999', d['mac']))


# Cached LRT224 session — single login persists across polls until a
# protected page comes back as a stub (= session expired upstream), at
# which point we re-login.
_lrt224_session: requests.Session | None = None
_lrt224_session_url: str = ''


def _lrt224_poll(url: str, user: str, pw: str) -> list[dict[str, Any]]:
    """Login (or reuse cached session), fetch DHCP tables, return records.
    On stub responses (session expired), re-login once and retry."""
    global _lrt224_session, _lrt224_session_url

    if _lrt224_session is None or _lrt224_session_url != url:
        _lrt224_session = lrt224_login(url, user, pw)
        _lrt224_session_url = url
    if _lrt224_session is None:
        return []

    res = fetch_lrt224_dhcp(_lrt224_session, url)
    # If we got a "stub response" error, the cached session expired — try
    # one fresh login + refetch.
    if any('stub response' in e for e in res.get('errors', [])):
        _lrt224_session = lrt224_login(url, user, pw)
        if _lrt224_session is None:
            return []
        res = fetch_lrt224_dhcp(_lrt224_session, url)

    return res.get('devices', [])


def fetch_all(router_cfg: dict[str, str] | None,
              ap_cfgs: list[dict[str, str]],
              local_subnet: str | None = None) -> dict[str, Any]:
    """Top-level: hit local ARP + every AP (router optional), return merged."""
    started = time.time()
    router_result: dict[str, Any] | None = None
    local_result: dict[str, Any] | None = None
    ap_results: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []

    if local_subnet:
        local_result = fetch_local_arp(local_subnet)
        all_records.extend(local_result.get('devices', []))

    # LRT224 web UI scrape — the auth dance (md5(pw+auth_key) → POST
    # userLogin.cgi) is now cracked. SNMP path stays disabled (no ARP/DHCP
    # in MIB). We reuse a cached session across polls to avoid the per-IP
    # session cap, only re-logging in if the protected page returns the
    # 104-byte meta-refresh stub (= session expired).
    if router_cfg and router_cfg.get('url') and router_cfg.get('user') \
            and router_cfg.get('pass'):
        records = _lrt224_poll(router_cfg['url'], router_cfg['user'],
                               router_cfg['pass'])
        router_result = {'url': router_cfg['url'],
                         'devices': records,
                         'count': len(records)}
        all_records.extend(records)

    for ap in ap_cfgs:
        if not ap.get('url'):
            continue
        res = fetch_ddwrt_ap(ap['url'], ap.get('user', ''), ap.get('pass', ''),
                             ap.get('name', ap['url']))
        ap_results.append(res)
        all_records.extend(res.get('devices', []))

    return {
        'local': local_result,
        'router': router_result,
        'aps': ap_results,
        'merged': merge_by_mac(all_records),
        'elapsed_ms': int((time.time() - started) * 1000),
    }


def load_ap_configs(network_aps_setting: str) -> list[dict[str, str]]:
    """Parse the network_aps JSON setting; tolerate empty/garbage."""
    try:
        v = json.loads(network_aps_setting or '[]')
        return v if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


# ── Persistent state (Phase B) ────────────────────────────────────────────────
# Single JSON file at repo root. Each top-level key is a lowercase MAC; value
# is a dict with both user-editable fields (friendly_name, notes, hidden) and
# polling-derived fields (last_*, vendor, seen_on, first_seen, last_seen).

_USER_FIELDS = ('friendly_name', 'notes', 'hidden')


def load_state(path: str) -> dict[str, dict[str, Any]]:
    """Read network_devices.json. Returns empty dict if missing/garbage —
    polling will repopulate on next tick."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(path: str, state: dict[str, dict[str, Any]]) -> None:
    """Atomic write: serialize to <path>.tmp, then rename. Avoids leaving a
    half-written file if the process dies mid-flush."""
    import os
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def merge_into_state(state: dict[str, dict[str, Any]],
                     fresh_records: list[dict[str, Any]],
                     now_ts: int | None = None) -> dict[str, dict[str, Any]]:
    """Fold a poll's worth of merged-by-MAC records into persistent state.

    Preserves user-editable fields (friendly_name, notes, hidden) and
    overwrites only the last_* / vendor / seen_on / last_seen fields.
    `first_seen` is set on insert and never touched again.
    """
    if now_ts is None:
        now_ts = int(time.time())

    # `fresh_records` may already be merged-by-mac OR a flat per-source list;
    # detect by looking for `seen_on` on ANY record (merged form always sets
    # it, even when empty — flat form never does). Checking only [0] was
    # fragile because a merged record with no labels could lack the key.
    if not fresh_records:
        merged = []
    elif any('seen_on' in r for r in fresh_records):
        merged = fresh_records
    else:
        merged = merge_by_mac(fresh_records)

    for r in merged:
        mac = (r.get('mac') or '').lower()
        if not mac:
            continue
        cur = state.get(mac)
        if cur is None:
            cur = {f: ('' if f != 'hidden' else False) for f in _USER_FIELDS}
            cur['first_seen'] = now_ts
            state[mac] = cur

        cur['last_seen'] = now_ts
        cur['last_ip'] = r.get('ip')
        cur['last_hostname'] = r.get('hostname')
        cur['last_ap'] = r.get('ap')
        cur['last_signal'] = r.get('signal')
        cur['last_iface'] = r.get('iface')
        cur['vendor'] = r.get('vendor') or cur.get('vendor')
        cur['nbns_name'] = r.get('nbns_name') or cur.get('nbns_name')
        cur['ssdp_server'] = r.get('ssdp_server') or cur.get('ssdp_server')
        cur['seen_on'] = r.get('seen_on') or []

    return state
