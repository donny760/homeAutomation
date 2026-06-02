import asyncio
import sqlite3
import threading
import time

import lib.state as state
from lib.events import _log_system_error, _device_mark_failure, _switches_lock
from lib.db import connect


_kasa_devices: dict     = {}
_kasa_ts: float         = 0.0
_kasa_loop: "asyncio.AbstractEventLoop | None" = None
_kasa_loop_thread: "threading.Thread | None" = None
_kasa_connections: dict = {}
_kasa_failures: dict    = {}
_kasa_quarantine: dict  = {}


def _kasa_start_loop() -> None:
    global _kasa_loop, _kasa_loop_thread
    if _kasa_loop is not None and _kasa_loop.is_running():
        return
    _kasa_loop = asyncio.new_event_loop()
    def _run():
        asyncio.set_event_loop(_kasa_loop)
        try:
            _kasa_loop.run_forever()
        except Exception as exc:
            print(f'Kasa loop crashed: {exc}')
    _kasa_loop_thread = threading.Thread(
        target=_run, daemon=True, name='kasa-asyncio-loop'
    )
    _kasa_loop_thread.start()


def _kasa_submit(coro, timeout: float = 30.0):
    if _kasa_loop is None or not _kasa_loop.is_running():
        _kasa_start_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, _kasa_loop)
    return fut.result(timeout=timeout)


def _log_kasa_reachability(name: str, event: str, detail: str = None) -> None:
    try:
        with connect() as c:
            c.execute(
                'INSERT INTO event_log '
                '(ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), 'kasa', event, f'{name} {event}',
                 detail, 'ok', 'live')
            )
    except Exception as exc:
        print(f'Kasa reachability log error: {exc}')


def _kasa_mark_failure(mac: str, name: str, reason: str) -> None:
    _device_mark_failure(
        key=mac, name=name, reason=reason,
        failures=_kasa_failures, devices=_kasa_devices, quarantine=_kasa_quarantine,
        log_fn=_log_kasa_reachability,
        offline_field='on', offline_value=None,
    )


async def _kasa_close_all_async() -> None:
    for mac, dev in list(_kasa_connections.items()):
        try:
            if hasattr(dev, 'disconnect'):
                await dev.disconnect()
        except Exception as exc:
            print(f'Kasa close failed for {mac}: {exc}')
    _kasa_connections.clear()


def _kasa_read_brightness(dev) -> int:
    try:
        feat = getattr(dev, 'features', {}) or {}
        if 'brightness' in feat:
            val = getattr(feat['brightness'], 'value', None)
            if val is not None:
                return int(val)
    except Exception:
        pass
    try:
        from kasa import Module  # type: ignore
        mods = getattr(dev, 'modules', {}) or {}
        light = mods.get(Module.Light) if hasattr(Module, 'Light') else None
        if light is not None and hasattr(light, 'brightness'):
            return int(light.brightness)
    except Exception:
        pass
    try:
        b = getattr(dev, 'brightness', None)
        if b is not None and getattr(dev, 'is_dimmable', False):
            return int(b)
    except Exception:
        pass
    return None


async def _kasa_set_brightness_on_device(dev, brightness: int) -> None:
    b = max(0, min(100, int(brightness)))
    feat = getattr(dev, 'features', {}) or {}
    if 'brightness' in feat:
        await feat['brightness'].set_value(b)
        return
    try:
        from kasa import Module  # type: ignore
        mods = getattr(dev, 'modules', {}) or {}
        light = mods.get(Module.Light) if hasattr(Module, 'Light') else None
        if light is not None and hasattr(light, 'set_brightness'):
            await light.set_brightness(b)
            return
    except Exception:
        pass
    if hasattr(dev, 'set_brightness'):
        await dev.set_brightness(b)
        return
    raise ValueError('Device is not dimmable')


async def _kasa_discover_async() -> dict:
    from kasa import Discover
    await _kasa_close_all_async()
    _kasa_failures.clear()
    _kasa_quarantine.clear()
    try:
        discovered = await Discover.discover()
    except Exception as exc:
        print(f'Kasa discovery error: {exc}')
        raise
    out = {}
    for ip, dev in discovered.items():
        try:
            await dev.update()
        except Exception as exc:
            print(f'Kasa update failed for {ip}: {exc}')
            continue
        mac = (getattr(dev, 'mac', None) or '').upper()
        if not mac:
            continue
        brightness = _kasa_read_brightness(dev)
        out[mac] = {
            'alias':      getattr(dev, 'alias', None) or f'Kasa {mac[-5:]}',
            'ip':         ip,
            'on':         bool(getattr(dev, 'is_on', False)),
            'model':      getattr(dev, 'model', ''),
            'dimmable':   brightness is not None,
            'brightness': brightness,
        }
        _kasa_connections[mac] = dev
    return out


def _kasa_refresh_devices() -> int:
    global _kasa_devices, _kasa_ts
    try:
        devices = _kasa_submit(_kasa_discover_async(), timeout=60)
    except Exception as exc:
        print(f'Kasa refresh error: {exc}')
        _log_system_error('kasa', 'Discovery failed', str(exc))
        return 0
    now = time.time()
    with _switches_lock:
        _kasa_devices = {mac: {**info, 'last_seen': now} for mac, info in devices.items()}
        _kasa_ts = now
    with connect() as c:
        for mac, info in devices.items():
            kind = 'dimmer' if info.get('dimmable') else 'plug'
            existing = c.execute(
                'SELECT id, kind FROM switches_meta WHERE provider=? AND external_id=?',
                ('kasa', mac)
            ).fetchone()
            if existing is None:
                c.execute(
                    'INSERT INTO switches_meta (provider, external_id, kind, name) '
                    'VALUES (?,?,?,?)',
                    ('kasa', mac, kind, info['alias'])
                )
            elif existing[1] != kind:
                c.execute('UPDATE switches_meta SET kind=? WHERE id=?', (kind, existing[0]))
    return len(devices)


def _log_kasa_external_change(name: str, new_on: bool, detail: str = None) -> None:
    try:
        with connect() as c:
            c.execute(
                'INSERT INTO event_log '
                '(ts, system, event_type, title, detail, result, source) '
                'VALUES (?,?,?,?,?,?,?)',
                (int(time.time()), 'kasa',
                 'plug_turned_on' if new_on else 'plug_turned_off',
                 f'{name} turned {"on" if new_on else "off"}',
                 detail or 'external (switch or schedule)', 'ok', 'live')
            )
    except Exception as exc:
        print(f'Kasa external-change log error: {exc}')


def _kasa_uptime_hint(dev) -> str:
    try:
        sys_info = getattr(dev, 'sys_info', None) or {}
        on_time = sys_info.get('on_time')
        if on_time is None and 'system' in sys_info:
            on_time = (sys_info.get('system', {}).get('get_sysinfo', {})
                       .get('on_time'))
        if on_time is None:
            return ''
        on_time = int(on_time)
        if on_time < 60:
            return f'uptime {on_time}s (likely reboot)'
        if on_time < 3600:
            return f'uptime {on_time // 60}m'
        return f'uptime {on_time // 3600}h{(on_time % 3600) // 60}m'
    except Exception:
        return ''


async def _kasa_ensure_connection(mac: str, info: dict):
    from kasa import Discover
    dev = _kasa_connections.get(mac)
    if dev is not None:
        return dev
    ip = info.get('ip')
    if not ip:
        raise RuntimeError(f'No IP for Kasa {mac}')
    dev = await Discover.discover_single(ip)
    await dev.update()
    _kasa_connections[mac] = dev
    return dev


async def _kasa_update_state_async() -> None:
    now = time.time()
    for mac, info in list(_kasa_devices.items()):
        if not info.get('ip'):
            continue
        q = _kasa_quarantine.get(mac, 0)
        if q and now < q:
            continue
        name = info.get('alias') or f'Kasa {mac[-5:]}'
        try:
            dev = await _kasa_ensure_connection(mac, info)
            await dev.update()
            new_on     = bool(getattr(dev, 'is_on', False))
            last_known = info.get('last_known_on')
            if mac in _kasa_quarantine:
                _kasa_quarantine.pop(mac, None)
                _log_kasa_reachability(name, 'online', 'released from quarantine')
            elif info.get('was_offline'):
                _log_kasa_reachability(name, 'online',
                                       f'state={"on" if new_on else "off"}')
            if last_known is not None and new_on != last_known:
                uptime_hint = _kasa_uptime_hint(dev)
                detail = 'external (switch or schedule)'
                if uptime_hint:
                    detail = f'{detail} · {uptime_hint}'
                _log_kasa_external_change(name, new_on, detail)
            b = _kasa_read_brightness(dev) if info.get('dimmable') else None
            with _switches_lock:
                info['was_offline']   = False
                info['on']            = new_on
                info['last_known_on'] = new_on
                info['last_seen']     = time.time()
                if b is not None:
                    info['brightness'] = b
            _kasa_failures.pop(mac, None)
        except Exception as exc:
            old = _kasa_connections.pop(mac, None)
            if old is not None:
                try:
                    if hasattr(old, 'disconnect'):
                        await old.disconnect()
                except Exception:
                    pass
            _kasa_mark_failure(mac, name, str(exc))


def _kasa_poll_state() -> None:
    if not _kasa_devices:
        return
    try:
        _kasa_submit(_kasa_update_state_async(), timeout=60)
    except Exception as exc:
        print(f'Kasa poll error: {exc}')


async def _kasa_set_async(mac: str, on: bool) -> bool:
    info = _kasa_devices.get(mac)
    if not info:
        raise ValueError(f'Unknown Kasa MAC: {mac}')
    dev = await _kasa_ensure_connection(mac, info)
    if on:
        await dev.turn_on()
    else:
        await dev.turn_off()
    await dev.update()
    new_state = bool(getattr(dev, 'is_on', False))
    b = _kasa_read_brightness(dev) if info.get('dimmable') else None
    with _switches_lock:
        info['on']            = new_state
        info['last_known_on'] = new_state
        info['last_seen']     = time.time()
        if b is not None:
            info['brightness'] = b
    _kasa_failures.pop(mac, None)
    return new_state


async def _kasa_set_brightness_async(mac: str, brightness: int) -> dict:
    info = _kasa_devices.get(mac)
    if not info:
        raise ValueError(f'Unknown Kasa MAC: {mac}')
    if not info.get('dimmable'):
        raise ValueError(f'Kasa {mac} is not dimmable')
    b = max(0, min(100, int(brightness)))
    dev = await _kasa_ensure_connection(mac, info)
    was_on = bool(getattr(dev, 'is_on', False))
    if b > 0:
        if not was_on:
            await dev.turn_on()
            await dev.update()
        await _kasa_set_brightness_on_device(dev, b)
    else:
        if was_on:
            await dev.turn_off()
    await dev.update()
    new_on = bool(getattr(dev, 'is_on', False))
    new_b  = _kasa_read_brightness(dev)
    final_b = new_b if new_b is not None else b
    with _switches_lock:
        info['on']            = new_on
        info['last_known_on'] = new_on
        info['brightness']    = final_b
        info['last_seen']     = time.time()
    _kasa_failures.pop(mac, None)
    return {'on': new_on, 'brightness': final_b}


def kasa_set(mac: str, on: bool) -> bool:
    try:
        return _kasa_submit(_kasa_set_async(mac, on), timeout=20)
    except Exception:
        _kasa_connections.pop(mac, None)
        raise


def kasa_set_brightness(mac: str, brightness: int) -> dict:
    try:
        return _kasa_submit(_kasa_set_brightness_async(mac, brightness), timeout=20)
    except Exception:
        _kasa_connections.pop(mac, None)
        raise
