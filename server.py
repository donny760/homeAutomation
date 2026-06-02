"""
Powerwall Dashboard — Backend
Polls pypowerwall every 10s, writes to SQLite every 30s, serves JSON via Flask.
Run: py server.py
"""

import os
import sys
import json
import time
import sqlite3
import threading
import urllib.request
import traceback
import requests as _requests
from datetime import datetime, date, timedelta, timezone

import asyncio

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

from flask import Flask, jsonify, send_file, send_from_directory, request, redirect
import pypowerwall
from lib.fetch_rates import (
    load_rates, rates_are_stale, fetch_ev_tou2_rates,
    tou_period, load_or_generate_holidays, SDGE_HOLIDAYS,
    HOLIDAYS_PATH, RATES_PATH,
    holiday_name, is_sdge_holiday,
)
from lib.state import BASE_DIR, DB_PATH, _live, _lock
from lib.db import (
    connect, init_db, write_reading, purge_old,
    _fetch_rows, _fetch_rows_range, today_rows, day_rows, month_rows,
)
from lib.settings import (
    _SETTINGS_DEFAULTS, _seed_settings, load_settings,
    get_setting, get_setting_int, get_setting_bool, _load_tou_periods,
)
from lib.events import (
    _log_system_error, _log_success, _switches_log_event,
    _HOME_CONTROL_TITLE_SUFFIX, _device_mark_failure, _switches_lock,
)
from lib.costs import (
    _cost_rebuild_lock, _spawn_rebuild_daily_costs, _rebuild_today,
    rebuild_daily_costs, _load_rate_history, _rate_for_date,
    _is_refresh_due, _read_year_from_json, _backfill_rates_event_url,
    calc_stats,
)
from lib.solar_forecast import (
    fetch_solar_forecast, fetch_tomorrow_solar_forecast,
)
import lib.pool as pool
from lib.pool import (
    fetch_pool, pool_set_circuit, _pool_discover_circuits,
    POOL_CIRCUITS, POOL_EXT_TO_FIELD, _load_pool_gpm_cache, _recalc_pool_target,
)
import lib.kasa as kasa
from lib.kasa import (
    _kasa_refresh_devices, _kasa_poll_state, kasa_set, kasa_set_brightness,
    _kasa_start_loop,
)
import lib.tuya as tuya
from lib.tuya import (
    _tuya_refresh_devices, _tuya_poll_state, tuya_set,
    _TUYA_DEVICEFILE, _tuya_make_outlet,
)
import lib.abode as abode_mod
from lib.abode import (
    fetch_security, start_abode_listener, abode_backfill,
    abode_arm_home, _abode_seed_alarm_row,
    ABODE_TYPE_MAP, ABODE_MODE_DISPLAY,
    _abode_status, _abode_status_lock,
)
import lib.nest as nest_mod
from lib.nest import (
    fetch_nest_events, nest_set_thermostat, _nest_refresh_devices,
    _nest_ensure_token, _nest_save_tokens, _nest_oauth_exchange,
    NEST_EVENT_TYPE_MAP, NEST_EVENT_TITLE_MAP,
    _nest_poll_stats, _nest_devices, _nest_devices_raw, _nest_devices_ts,
    _nest_event_counters,
)
import lib.rachio as rachio_mod
from lib.rachio import (
    fetch_rachio_schedule, fetch_rachio_events, evaluate_rain_skip,
    _rachio_get, _rachio_put, _rachio_wi_skip_info, _rachio_device_forecast,
    RACHIO_EVENT_TYPE_MAP,
)
from lib.weather import fetch_weather, WMO
import lib.switches as switches_mod
from lib.switches import (
    _switches_rediscover_all, _get_all_switches, _switches_lookup,
    switch_set_state, switch_toggle, switch_set_thermostat,
    switch_set_brightness, switch_update_meta,
)
from lib.powerwall import poller, backfill_history
import lib.ai_insights as ai_insights
from lib.ai_insights import (
    _ai_cache, _azure_configured, _build_ai_context, _gemini_system_prompt,
    _call_gemini, _call_azure_openai, _ProviderError,
)
from lib.rule_helpers import (
    _RULE_REQUIRED_FIELDS, _rule_row_to_dict, _load_all_rules,
    _rule_fires_at, _upcoming_firings, _validate_rule_body,
    _fmt_hour, _analyze_rules,
)

# ── Config ────────────────────────────────────────────────────────────────────
PW_EMAIL          = 'don@nsdsolutions.com'
PW_CAPACITY_KWH   = 40.5          # 3× Powerwall 2 usable capacity (3 × 13.5 kWh)
POLL_INTERVAL     = 10            # seconds between pypowerwall polls
DB_WRITE_EVERY    = 30            # seconds between DB writes
PURGE_DAYS        = 0             # disabled — keep all readings forever
app = Flask(__name__)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0  # no browser caching of static files

@app.errorhandler(Exception)
def handle_exception(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return jsonify(error=e.description), e.code
    app.logger.exception(e)
    return jsonify(error=str(e)), 500








# ── Flask routes ──────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return send_file(os.path.join('static', 'frontend', 'index.html'))


@app.route('/_next/<path:filename>')
def next_static(filename):
    return send_from_directory(os.path.join('static', 'frontend', '_next'), filename)


@app.route('/<path:filename>')
def frontend_static(filename):
    return send_from_directory(os.path.join('static', 'frontend'), filename)


@app.route('/api/live')
def api_live():
    with _lock:
        d = dict(_live)

    solar_w     = d.get('solar_w', 0)
    home_w      = d.get('home_w', 0)
    battery_w   = d.get('battery_w', 0)
    grid_w      = d.get('grid_w', 0)
    battery_pct = d.get('battery_pct', 0)
    mode        = d.get('mode', 'self_consumption')

    # Battery state
    if battery_w > 50:
        batt_status = 'Charging'
        kwh_to_go   = PW_CAPACITY_KWH * (100 - battery_pct) / 100
        hours_rem   = kwh_to_go / (battery_w / 1000) if battery_w > 0 else None
        time_label  = 'to full'
    elif battery_w < -50:
        batt_status = 'Discharging'
        kwh_left    = PW_CAPACITY_KWH * battery_pct / 100
        hours_rem   = kwh_left / (abs(battery_w) / 1000) if battery_w != 0 else None
        time_label  = 'to empty'
    else:
        batt_status = 'Standby'
        hours_rem   = None
        time_label  = None

    t_rows                          = today_rows()
    solar_kwh, s_today, self_suff, grid_kwh = calc_stats(t_rows)
    _, s_month, _, _                = calc_stats(month_rows())

    return jsonify({
        'solar_w':         round(solar_w),
        'home_w':          round(home_w),
        'battery_w':       round(battery_w),
        'grid_w':          round(grid_w),
        'battery_pct':     round(battery_pct, 1),
        'battery_status':  batt_status,
        'battery_rate_w':  round(abs(battery_w)),
        'hours_remaining': round(hours_rem, 2) if hours_rem else None,
        'time_label':      time_label,
        'solar_kwh_today': round(solar_kwh, 2),
        'grid_kwh_today':  round(grid_kwh, 2),
        'savings_today':   round(s_today, 2),
        'savings_month':   round(s_month, 2),
        'self_sufficiency': round(self_suff, 1),
        'mode':            mode,
        'ts':              d.get('ts', 0),
    })


def _filter_chart_rows(raw: list) -> list:
    out = []
    for i, r in enumerate(raw):
        # Drop all-zero glitch readings
        if r[1] == 0 and r[2] == 0 and r[3] == 0 and r[4] == 0:
            continue
        # Drop single-sample outliers: home_w differs >50% from both neighbors
        if 0 < i < len(raw) - 1:
            prev_h, cur_h, next_h = raw[i-1][2], r[2], raw[i+1][2]
            if prev_h > 0 and next_h > 0 and cur_h > 0:
                if abs(cur_h - prev_h) / prev_h > 0.5 and abs(cur_h - next_h) / next_h > 0.5:
                    continue
        out.append({'ts': r[0], 'solar_w': r[1], 'home_w': r[2], 'grid_w': r[4]})
    return out


@app.route('/api/today')
def api_today():
    return jsonify(_filter_chart_rows(today_rows()))


@app.route('/api/day')
def api_day():
    date_str = request.args.get('date', '')
    try:
        target = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'invalid date, use YYYY-MM-DD'}), 400
    return jsonify(_filter_chart_rows(day_rows(target)))


@app.route('/api/weather')
def api_weather():
    return jsonify(fetch_weather())


@app.route('/api/solar-forecast')
def api_solar_forecast():
    fc = fetch_solar_forecast()
    today_str = fc.get('date', date.today().isoformat())
    base_ts = int(datetime.strptime(today_str, '%Y-%m-%d').timestamp())
    points = []
    for h in sorted(fc.get('hours', {}).keys(), key=int):
        w = fc['hours'][h]
        if w > 0:
            points.append({'ts': base_ts + int(h) * 3600, 'solar_w': w})
    return jsonify(points)


@app.route('/api/solar-forecast/tomorrow')
def api_solar_forecast_tomorrow():
    return jsonify(fetch_tomorrow_solar_forecast())


@app.route('/api/pool')
def api_pool():
    return jsonify(fetch_pool())


@app.route('/api/security')
def api_security():
    return jsonify(fetch_security())


@app.route('/api/debug/abode/devices')
def api_debug_abode_devices():
    if abode_mod._abode_instance is None:
        return jsonify({'error': 'Abode not connected'}), 503
    try:
        devices = abode_mod._abode_instance.get_devices()
        return jsonify([
            {'name': getattr(d, 'name', ''), 'type': getattr(d, 'type', ''),
             'status': getattr(d, 'status', ''), 'battery_low': getattr(d, 'battery_low', None)}
            for d in devices
        ])
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


async def _pool_debug_async() -> dict:
    from screenlogicpy import ScreenLogicGateway
    from screenlogicpy.discovery import async_discover
    gateways = await async_discover()
    if not gateways:
        return {'error': 'No ScreenLogic gateway found via UDP discovery'}
    gw = gateways[0]
    gateway = ScreenLogicGateway()
    await gateway.async_connect(ip=gw['ip'], port=gw.get('port', 80))
    try:
        await gateway.async_update()
        return gateway.get_data()
    finally:
        await gateway.async_disconnect()


@app.route('/api/debug/pool')
def api_debug_pool():
    """Dump raw screenlogicpy data — use this to identify correct key paths."""
    try:
        return jsonify(asyncio.run(_pool_debug_async()))
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ── Rachio ───────────────────────────────────────────────────────────────────
@app.route('/api/debug/rachio')
def api_debug_rachio():
    """Return embedded scheduleRules from person response — shows actual field names."""
    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')
        result    = {}
        for device in person.get('devices', []):
            rules = device.get('scheduleRules', [])
            result[device['id']] = rules[:3]  # first 3 rules
        return jsonify(result)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/debug/rachio/events')
def api_debug_rachio_events():
    """Return raw device events from Rachio — shows actual field names."""
    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')
        end_ms    = int(time.time() * 1000)
        start_ms  = end_ms - 7 * 86400 * 1000  # last 7 days
        result    = {}
        for device in person.get('devices', []):
            did = device['id']
            events = _rachio_get(f'/device/{did}/event?startTime={start_ms}&endTime={end_ms}')
            result[did] = events
        return jsonify(result)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/debug/rachio/full')
def api_debug_rachio_full():
    """Dump everything we can pull from Rachio for each device — shows all available fields.
    Tries documented + commonly-undocumented endpoints to find skip prediction data."""
    def _try(path):
        try:
            return _rachio_get(path)
        except Exception as exc:
            return {'__error__': str(exc)}

    try:
        person_id = _rachio_get('/person/info')['id']
        person    = _rachio_get(f'/person/{person_id}')
        result = {
            'person_info':     _try('/person/info'),
            'person':          person,
            'devices_full':    {},
        }
        for device in person.get('devices', []):
            did = device['id']
            zone_ids = [z.get('id') for z in (device.get('zones') or []) if z.get('id')]
            schedule_ids = [r.get('id') for r in (device.get('scheduleRules') or []) if r.get('id')]
            flex_ids = [r.get('id') for r in (device.get('flexScheduleRules') or []) if r.get('id')]

            now_ms = int(time.time() * 1000)
            future_ms = now_ms + 7 * 86400 * 1000

            result['devices_full'][did] = {
                'name':                       device.get('name'),
                'device_keys':                sorted(device.keys()),
                'rainDelayExpirationDate':    device.get('rainDelayExpirationDate'),
                'rainDelayStartDate':         device.get('rainDelayStartDate'),
                # Try various device sub-endpoints
                'current_schedule':           _try(f'/device/{did}/current_schedule'),
                'forecast':                   _try(f'/device/{did}/forecast'),
                'forecast_summary':           _try(f'/device/{did}/forecast_summary'),
                'state':                      _try(f'/device/{did}/state'),
                # Future-window event probes — does the events endpoint expose scheduled future skips?
                'events_future':              _try(f'/device/{did}/event?startTime={now_ms}&endTime={future_ms}'),
                # Possibly-scheduled / upcoming endpoints (undocumented; mostly likely 404)
                'upcoming':                   _try(f'/device/{did}/upcoming'),
                'upcoming_runs':              _try(f'/device/{did}/upcoming_runs'),
                'scheduled_runs':             _try(f'/device/{did}/scheduled_runs'),
                'scheduled_events':           _try(f'/device/{did}/scheduled_events'),
                'calendar':                   _try(f'/device/{did}/calendar'),
                'planned':                    _try(f'/device/{did}/planned'),
                # Per schedule rule (full detail)
                'scheduleRule_detail':        {sid: _try(f'/schedulerule/{sid}') for sid in schedule_ids},
                'scheduleRule_skip':          {sid: _try(f'/schedulerule/{sid}/skip') for sid in schedule_ids},
                'scheduleRule_next':          {sid: _try(f'/schedulerule/{sid}/next_run') for sid in schedule_ids},
                'scheduleRule_skipped':       {sid: _try(f'/schedulerule/{sid}/skipped') for sid in schedule_ids},
                'scheduleRule_upcoming':      {sid: _try(f'/schedulerule/{sid}/upcoming') for sid in schedule_ids},
                'flexScheduleRule_detail':    {fid: _try(f'/flexschedulerule/{fid}') for fid in flex_ids},
                # Per zone
                'zone_detail':                {zid: _try(f'/zone/{zid}') for zid in zone_ids[:3]},  # first 3 only
            }
        return jsonify(result)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500



# ── Rules API endpoints ───────────────────────────────────────────────────────
@app.route('/api/schedule')
def api_schedule():
    with _lock:
        live = dict(_live)
    with connect() as c:
        rules = _load_all_rules(c)
    pw_events     = _upcoming_firings(rules)
    rachio_events = fetch_rachio_schedule()
    all_events    = sorted(pw_events + rachio_events, key=lambda e: e['fire_time'])
    current = {
        'mode':        live.get('mode', 'self_consumption'),
        'battery_pct': live.get('battery_pct', 0),
    }
    return jsonify({'current': current, 'schedule': all_events})


@app.route('/api/rules', methods=['GET'])
def api_rules_get():
    with connect() as c:
        return jsonify(_load_all_rules(c))




@app.route('/api/rules', methods=['POST'])
def api_rules_post():
    body = request.get_json(silent=True)
    err = _validate_rule_body(body)
    if err:
        return jsonify({'error': err}), 400
    days_j   = json.dumps(body['days'])
    months_j = json.dumps(body['months'])
    gc = body.get('grid_charging')
    gc_val = None if gc is None else (1 if gc else 0)
    with connect() as c:
        c.execute('PRAGMA foreign_keys = ON')
        max_order = c.execute('SELECT COALESCE(MAX(sort_order), 0) FROM rules').fetchone()[0]
        cur = c.execute(
            'INSERT INTO rules (name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export,sort_order,notes) '
            'VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (body['name'], 1 if body.get('enabled', True) else 0,
             days_j, months_j, body['hour'], body['minute'],
             body.get('mode'), body.get('reserve'), gc_val, body.get('grid_export'),
             max_order + 1, body.get('notes') or None)
        )
        rid = cur.lastrowid
        for cond in body.get('conditions', []):
            c.execute(
                'INSERT INTO rule_conditions (rule_id,logic,type,operator,value) VALUES (?,?,?,?,?)',
                (rid, cond['logic'], cond['type'], cond['operator'], cond['value'])
            )
        row = c.execute(
            'SELECT id,name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export,notes FROM rules WHERE id=?', (rid,)
        ).fetchone()
        conds = c.execute('SELECT rule_id,logic,type,operator,value FROM rule_conditions WHERE rule_id=?', (rid,)).fetchall()
    cond_list = [{'logic': r[1], 'type': r[2], 'operator': r[3], 'value': r[4]} for r in conds]
    _ai_cache['ts'] = 0  # invalidate AI insights cache
    return jsonify(_rule_row_to_dict(row, cond_list)), 201


@app.route('/api/rules/<int:rid>', methods=['PUT'])
def api_rules_put(rid):
    body = request.get_json(silent=True)
    err = _validate_rule_body(body)
    if err:
        return jsonify({'error': err}), 400
    days_j   = json.dumps(body['days'])
    months_j = json.dumps(body['months'])
    gc = body.get('grid_charging')
    gc_val = None if gc is None else (1 if gc else 0)
    with connect() as c:
        c.execute('PRAGMA foreign_keys = ON')
        if not c.execute('SELECT 1 FROM rules WHERE id=?', (rid,)).fetchone():
            return jsonify({'error': 'not found'}), 404
        c.execute(
            'UPDATE rules SET name=?,enabled=?,days=?,months=?,hour=?,minute=?,mode=?,reserve=?,grid_charging=?,grid_export=?,notes=? WHERE id=?',
            (body['name'], 1 if body.get('enabled', True) else 0,
             days_j, months_j, body['hour'], body['minute'],
             body.get('mode'), body.get('reserve'), gc_val, body.get('grid_export'),
             body.get('notes') or None, rid)
        )
        c.execute('DELETE FROM rule_conditions WHERE rule_id=?', (rid,))
        for cond in body.get('conditions', []):
            c.execute(
                'INSERT INTO rule_conditions (rule_id,logic,type,operator,value) VALUES (?,?,?,?,?)',
                (rid, cond['logic'], cond['type'], cond['operator'], cond['value'])
            )
        row = c.execute(
            'SELECT id,name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export,notes FROM rules WHERE id=?', (rid,)
        ).fetchone()
        conds = c.execute('SELECT rule_id,logic,type,operator,value FROM rule_conditions WHERE rule_id=?', (rid,)).fetchall()
    cond_list = [{'logic': r[1], 'type': r[2], 'operator': r[3], 'value': r[4]} for r in conds]
    _ai_cache['ts'] = 0  # invalidate AI insights cache
    return jsonify(_rule_row_to_dict(row, cond_list))


@app.route('/api/rules/<int:rid>', methods=['DELETE'])
def api_rules_delete(rid):
    with connect() as c:
        c.execute('PRAGMA foreign_keys = ON')
        c.execute('DELETE FROM rules WHERE id=?', (rid,))
    _ai_cache['ts'] = 0
    return '', 204


@app.route('/api/rules/reorder', methods=['POST'])
def api_rules_reorder():
    """Accept an ordered list of rule IDs and update sort_order accordingly."""
    body = request.get_json(silent=True)
    ids = body.get('ids') if body else None
    if not ids or not isinstance(ids, list):
        return jsonify({'error': 'ids list required'}), 400
    with connect() as c:
        for pos, rid in enumerate(ids):
            c.execute('UPDATE rules SET sort_order=? WHERE id=?', (pos, rid))
    _ai_cache['ts'] = 0
    return '', 204


@app.route('/api/rules/<int:rid>/toggle', methods=['PUT'])
def api_rules_toggle(rid):
    with connect() as c:
        if not c.execute('SELECT 1 FROM rules WHERE id=?', (rid,)).fetchone():
            return jsonify({'error': 'not found'}), 404
        c.execute('UPDATE rules SET enabled = 1 - enabled WHERE id=?', (rid,))
        row = c.execute(
            'SELECT id,name,enabled FROM rules WHERE id=?', (rid,)
        ).fetchone()
    return jsonify({'id': rid, 'enabled': bool(row[2])})



@app.route('/api/rules/insights')
def api_rules_insights():
    with connect() as c:
        rules = _load_all_rules(c)
    rates    = load_rates() or {}
    holidays = SDGE_HOLIDAYS
    insights = _analyze_rules(rules, rates, holidays, _load_tou_periods())
    return jsonify(insights)


@app.route('/api/rules/ai-insights', methods=['POST'])
def api_rules_ai_insights():
    # Respect explicit refresh request (bypasses cache)
    force_refresh = request.args.get('refresh') == '1'

    # Return cached response if available and not invalidated (ts > 0)
    if not force_refresh and _ai_cache['text'] and _ai_cache['ts'] > 0:
        return jsonify({'ok': True, 'insights': _ai_cache['text'], 'model': _ai_cache['model'],
                        'projection_table': _ai_cache['table'],
                        'optimized_table': _ai_cache.get('optimized'), 'cached': True,
                        'provider': _ai_cache.get('provider', 'gemini')})

    gemini_key = get_setting('gemini_api_key', '')
    gemini_model = get_setting('gemini_model', 'gemini-2.0-flash')
    if not gemini_key and not _azure_configured():
        return jsonify({'ok': False, 'error': 'No AI provider configured. Add a Gemini or Azure OpenAI key in Settings.'}), 400

    context, table_md, opt_md = _build_ai_context()
    system_prompt = _gemini_system_prompt(_load_tou_periods())
    user_msg = f'Here is the current home energy data:\n\n{context}'

    last_err = None
    last_status = None

    # Phase 1: Gemini with retries (2s, 4s backoff)
    if gemini_key:
        for attempt in range(3):
            if attempt > 0:
                time.sleep(2 ** attempt)
            try:
                text = _call_gemini(system_prompt, user_msg, gemini_model, gemini_key)
                _ai_cache['text'] = text
                _ai_cache['model'] = gemini_model
                _ai_cache['table'] = table_md
                _ai_cache['optimized'] = opt_md if opt_md != table_md else None
                _ai_cache['provider'] = 'gemini'
                _ai_cache['ts'] = time.time()
                return jsonify({'ok': True, 'insights': text, 'model': gemini_model,
                                'projection_table': table_md,
                                'optimized_table': opt_md if opt_md != table_md else None,
                                'provider': 'gemini'})
            except _ProviderError as exc:
                last_err = str(exc)
                last_status = exc.status
                print(f'Gemini attempt {attempt + 1}/3 failed ({exc.status}): {exc}')
                if not exc.transient:
                    break  # don't retry permanent errors like 401, 400
            except Exception as exc:
                last_err = str(exc)
                last_status = 500
                print(f'Gemini attempt {attempt + 1}/3 unexpected error: {exc}')

    gemini_err = f'Gemini: {last_err} (status {last_status})' if last_err else 'Gemini: not configured'

    # Phase 2: Azure OpenAI fallback (single attempt)
    if _azure_configured():
        endpoint = get_setting('azure_openai_endpoint', '')
        deployment = get_setting('azure_openai_deployment', '')
        azure_key = get_setting('azure_openai_api_key', '')
        api_version = get_setting('azure_openai_api_version', '2024-10-21')
        try:
            text = _call_azure_openai(system_prompt, user_msg, endpoint, deployment, azure_key, api_version)
            _ai_cache['text'] = text
            _ai_cache['model'] = f'azure:{deployment}'
            _ai_cache['table'] = table_md
            _ai_cache['optimized'] = opt_md if opt_md != table_md else None
            _ai_cache['provider'] = 'azure_openai'
            _ai_cache['ts'] = time.time()
            print(f'Gemini failed, Azure OpenAI ({deployment}) succeeded as fallback')
            return jsonify({'ok': True, 'insights': text, 'model': f'azure:{deployment}',
                            'projection_table': table_md,
                            'optimized_table': opt_md if opt_md != table_md else None,
                            'provider': 'azure_openai'})
        except _ProviderError as exc:
            azure_err = f'Azure: {exc} (status {exc.status})'
            print(f'Azure OpenAI fallback also failed: {exc}')
            last_err = f'{gemini_err}; {azure_err}'
        except Exception as exc:
            print(f'Azure OpenAI fallback unexpected error: {exc}')
            last_err = f'{gemini_err}; Azure: {exc}'
    else:
        last_err = f'{gemini_err}; Azure not configured'

    # Phase 3: Stale cache fallback
    if _ai_cache['text']:
        age_min = int((time.time() - _ai_cache['ts']) / 60)
        return jsonify({
            'ok': True,
            'insights': _ai_cache['text'],
            'model': _ai_cache['model'],
            'projection_table': _ai_cache['table'],
            'optimized_table': _ai_cache.get('optimized'),
            'cached': True,
            'stale': True,
            'stale_age_min': age_min,
            'provider': _ai_cache.get('provider', 'gemini'),
            'stale_reason': f'All providers unavailable. Showing cached response from {age_min} min ago. Last error: {last_err}',
        })

    # Phase 4: Hard error
    return jsonify({'ok': False, 'error': f'AI providers unavailable. {last_err}'}), 502


@app.route('/api/rules/ai-insights/debug')
def api_rules_ai_insights_debug():
    """Debug endpoint — returns full prompt, context, raw response, and token usage.
    Uses the same Gemini-then-Azure fallback logic as the main endpoint."""
    gemini_key = get_setting('gemini_api_key', '')
    gemini_model = get_setting('gemini_model', 'gemini-2.0-flash')

    context, _, _ = _build_ai_context()
    system_prompt = _gemini_system_prompt(_load_tou_periods())
    user_msg = f'Here is the current home energy data:\n\n{context}'

    result = {
        'system_prompt_chars': len(system_prompt),
        'context_chars': len(context),
        'system_prompt': system_prompt,
    }

    # Try Gemini first (single attempt — debug doesn't retry)
    if gemini_key:
        try:
            text = _call_gemini(system_prompt, user_msg, gemini_model, gemini_key)
            result.update({
                'ok': True,
                'provider': 'gemini',
                'model': gemini_model,
                'response_chars': len(text),
                'response_text': text,
            })
            return jsonify(result)
        except Exception as exc:
            result['gemini_error'] = str(exc)

    # Fallback to Azure
    if _azure_configured():
        endpoint = get_setting('azure_openai_endpoint', '')
        deployment = get_setting('azure_openai_deployment', '')
        azure_key = get_setting('azure_openai_api_key', '')
        api_version = get_setting('azure_openai_api_version', '2024-10-21')
        try:
            text = _call_azure_openai(system_prompt, user_msg, endpoint, deployment, azure_key, api_version)
            result.update({
                'ok': True,
                'provider': 'azure_openai',
                'model': f'azure:{deployment}',
                'response_chars': len(text),
                'response_text': text,
            })
            return jsonify(result)
        except Exception as exc:
            result['azure_error'] = str(exc)

    result['ok'] = False
    result['error'] = 'All providers failed. ' + \
        f'Gemini: {result.get("gemini_error", "not configured")}. ' + \
        f'Azure: {result.get("azure_error", "not configured")}.'
    return jsonify(result), 502


# ── Costs + Rates endpoints ──────────────────────────────────────────────────
@app.route('/api/costs/ytd')
def api_costs_ytd():
    year = date.today().year
    jan1 = f'{year}-01-01'
    today = date.today().isoformat()
    with connect() as c:
        row = c.execute(
            'SELECT SUM(import_kwh), SUM(export_kwh), '
            '       SUM(import_cost), SUM(export_credit) '
            'FROM daily_costs WHERE date >= ? AND date <= ?',
            (jan1, today)
        ).fetchone()
    import_kwh    = round(row[0] or 0, 2)
    export_kwh    = round(row[1] or 0, 2)
    import_cost   = round(row[2] or 0, 2)
    export_credit = round(row[3] or 0, 2)
    return jsonify({
        'import_kwh':    import_kwh,
        'export_kwh':    export_kwh,
        'import_cost':   import_cost,
        'export_credit': export_credit,
        'net_cost':      round(import_cost - export_credit, 2),
        'as_of':         today,
    })


def _arg_int(name, default):
    """Parse an int query-string param. Returns (value, error_response)."""
    raw = request.args.get(name)
    if raw is None or raw == '':
        return default, None
    try:
        return int(raw), None
    except (TypeError, ValueError):
        return None, (jsonify({'error': f'{name} must be an integer'}), 400)


@app.route('/api/costs/daily')
def api_costs_daily():
    # Support start/end date filters (default: current year) + pagination
    today = date.today()
    start = request.args.get('start', f'{today.year}-01-01')
    end   = request.args.get('end', today.isoformat())
    limit, err  = _arg_int('limit', 0)   # 0 = no limit
    if err: return err
    offset, err = _arg_int('offset', 0)
    if err: return err
    with connect() as c:
        # Total count for pagination
        total = c.execute(
            'SELECT COUNT(*) FROM daily_costs WHERE date >= ? AND date <= ?',
            (start, end)
        ).fetchone()[0]
        sql = ('SELECT date, import_kwh, export_kwh, import_cost, export_credit, '
               '       on_peak_kwh, off_peak_kwh, super_off_peak_kwh, '
               '       on_peak_cost, off_peak_cost, super_off_peak_cost '
               'FROM daily_costs WHERE date >= ? AND date <= ? ORDER BY date DESC')
        params: list = [start, end]
        if limit > 0:
            sql += ' LIMIT ? OFFSET ?'
            params += [limit, offset]
        rows = c.execute(sql, params).fetchall()
    rates = load_rates()
    rates_as_of = (rates.get('updated') or '')[:10] if rates else ''
    days = [
        {
            'date':          r[0],
            'import_kwh':    round(r[1], 2),
            'export_kwh':    round(r[2], 2),
            'import_cost':   round(r[3], 2),
            'export_credit': round(r[4], 2),
            'net_cost':      round(r[3] - r[4], 2),
            'on_peak_kwh':        round(r[5] or 0, 2),
            'off_peak_kwh':       round(r[6] or 0, 2),
            'super_off_peak_kwh': round(r[7] or 0, 2),
            'on_peak_cost':        round(r[8] or 0, 2),
            'off_peak_cost':       round(r[9] or 0, 2),
            'super_off_peak_cost': round(r[10] or 0, 2),
        }
        for r in rows
    ]
    return jsonify({'start': start, 'end': end, 'total': total,
                    'rates_as_of': rates_as_of, 'days': days})


@app.route('/api/costs/rebuild', methods=['POST'])
def api_costs_rebuild():
    from_str = request.args.get('from')
    from_date = None
    if from_str:
        try:
            from_date = date.fromisoformat(from_str)
        except ValueError:
            return jsonify({'error': 'invalid from date, use YYYY-MM-DD'}), 400
    started = _spawn_rebuild_daily_costs(from_date=from_date)
    return jsonify({'ok': True, 'started': started,
                    'note': None if started else 'rebuild already in progress'})


@app.route('/api/rates')
def api_rates():
    data = load_rates() or {}
    data['holidays'] = sorted(d.isoformat() for d in SDGE_HOLIDAYS)
    data['tou_periods'] = _load_tou_periods()
    return jsonify(data)


@app.route('/api/rates/refresh', methods=['POST'])
def api_rates_refresh():
    try:
        rates = fetch_ev_tou2_rates()
        return jsonify({'ok': True, 'updated': rates.get('updated')})
    except Exception as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 500


# ── Abode debug endpoint ─────────────────────────────────────────────────────
@app.route('/api/debug/abode/timeline')
def api_debug_abode_timeline():
    """Return first page of raw Abode timeline — use to verify field names."""
    if abode_mod._abode_instance is None:
        return jsonify({'error': 'Abode not connected yet'}), 503
    try:
        resp = abode_mod._abode_instance.send_request(
            'get', 'https://my.goabode.com/api/v1/timeline?size=5'
        )
        return jsonify(resp.json())
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


@app.route('/api/debug/abode/status')
def api_debug_abode_status():
    """Return Abode listener connection state and stats."""
    with _abode_status_lock:
        info = dict(_abode_status)
    info['connected'] = abode_mod._abode_instance is not None
    return jsonify(info)


@app.route('/api/debug/abode/backfill', methods=['POST'])
def api_debug_abode_backfill():
    """Manually trigger Abode backfill and return result with diagnostics."""
    if abode_mod._abode_instance is None:
        return jsonify({'error': 'Abode not connected'}), 503
    days, err = _arg_int('days', 30)
    if err: return err

    # Collect diagnostics: fetch page 1 raw to show what we're getting
    diag = {}
    try:
        resp = abode_mod._abode_instance.send_request(
            'get', f'https://my.goabode.com/api/v1/timeline?size=5')
        raw = resp.json()
        if isinstance(raw, list):
            diag['api_sample'] = [
                {'event_utc': e.get('event_utc'), 'event_name': e.get('event_name'),
                 'device_name': e.get('device_name'), 'date': e.get('date')}
                for e in raw[:3]
            ]
    except Exception:
        pass

    # Check existing row count before
    with connect() as c:
        before = c.execute(
            "SELECT COUNT(*) FROM event_log WHERE system='abode'").fetchone()[0]

    inserted = abode_backfill(abode_mod._abode_instance, days=days)

    with connect() as c:
        after = c.execute(
            "SELECT COUNT(*) FROM event_log WHERE system='abode'").fetchone()[0]

    # Direct DB check: does a known Mar 28 event exist?
    spot_check = {}
    try:
        sample_ts = int(diag.get('api_sample', [{}])[0].get('event_utc', 0))
        sample_title = diag.get('api_sample', [{}])[0].get('event_name', '')
        with connect() as c:
            spot_check['ts'] = sample_ts
            spot_check['title'] = sample_title
            spot_check['exact_match'] = c.execute(
                'SELECT COUNT(*) FROM event_log WHERE ts=? AND system=? AND title=?',
                (sample_ts, 'abode', sample_title)).fetchone()[0]
            spot_check['ts_only'] = c.execute(
                'SELECT COUNT(*) FROM event_log WHERE ts=?',
                (sample_ts,)).fetchone()[0]
            spot_check['db_path'] = DB_PATH
    except Exception as e:
        spot_check['error'] = str(e)

    return jsonify({
        'code_version': 'v8-page1log',
        'ok': True,
        'inserted': inserted,
        'collected': _abode_status.get('last_backfill_collected', 0),
        'days': days,
        'rows_before': before,
        'rows_after': after,
        'backfill_error': _abode_status.get('last_backfill_error'),
        'duplicates_skipped': _abode_status.get('last_backfill_dupes', 0),
        'spot_check': spot_check,
        'collected_dates': _abode_status.get('last_backfill_dates', {}),
        'existing_set_size': _abode_status.get('last_backfill_existing_size', 0),
        'skipped_no_ts': _abode_status.get('last_backfill_skipped', 0),
        'pages_fetched': _abode_status.get('last_backfill_pages', 0),
        'backfill_page1': _abode_status.get('last_backfill_page1'),
        'diagnostics': diag,
    })


@app.route('/api/debug/abode/dedup', methods=['POST'])
def api_debug_abode_dedup():
    """Remove duplicate abode events from event_log."""
    with connect() as c:
        before = c.execute("SELECT COUNT(*) FROM event_log WHERE system='abode'").fetchone()[0]
        c.execute('''DELETE FROM event_log WHERE system='abode' AND id NOT IN (
            SELECT MIN(id) FROM event_log WHERE system='abode'
            GROUP BY ts, system, title)''')
        after = c.execute("SELECT COUNT(*) FROM event_log WHERE system='abode'").fetchone()[0]
    return jsonify({'before': before, 'after': after, 'removed': before - after})


@app.route('/api/debug/abode/test-event', methods=['POST'])
def api_debug_abode_test_event():
    """Insert a synthetic Abode event for UI testing."""
    import random
    samples = [
        ('door_open',    'Front Door Opened'),
        ('door_closed',  'Front Door Closed'),
        ('lock_locked',  'Garage Door Lock Locked'),
        ('lock_unlocked','Garage Door Lock Unlocked'),
        ('arm_away',     'System Armed Away'),
        ('arm_home',     'System Armed Home'),
        ('disarm',       'System Disarmed'),
        ('motion',       'Living Room Motion Detected'),
    ]
    evt, title = random.choice(samples)
    ts = int(time.time())
    with connect() as c:
        c.execute(
            'INSERT INTO event_log '
            '(ts, system, event_type, title, detail, result, source) '
            'VALUES (?,?,?,?,?,?,?)',
            (ts, 'abode', evt, title, 'synthetic test event', 'info', 'test')
        )
    return jsonify({'ok': True, 'ts': ts, 'event_type': evt, 'title': title})


# ── Nest OAuth + debug ───────────────────────────────────────────────────────
@app.route('/nest/auth')
def nest_auth():
    """Redirect user to Google OAuth consent screen for Nest/SDM access."""
    import urllib.parse
    client_id  = get_setting('nest_client_id', '')
    project_id = get_setting('nest_project_id', '')
    if not client_id or not project_id:
        return jsonify({'error': 'Nest client_id or project_id not configured'}), 400

    redirect_uri = request.url_root.rstrip('/') + '/nest/callback'
    params = urllib.parse.urlencode({
        'client_id':     client_id,
        'redirect_uri':  redirect_uri,
        'response_type': 'code',
        'scope':         'https://www.googleapis.com/auth/sdm.service https://www.googleapis.com/auth/pubsub',
        'access_type':   'offline',
        'prompt':        'consent',
    })
    url = f'https://nestservices.google.com/partnerconnections/{project_id}/auth?{params}'
    return redirect(url)


@app.route('/nest/callback')
def nest_callback():
    """Exchange authorization code for tokens, store refresh_token."""
    code = request.args.get('code')
    error = request.args.get('error')
    if error:
        return f'<h2>Nest authorization failed</h2><p>{error}</p>', 400
    if not code:
        return '<h2>Missing authorization code</h2>', 400

    redirect_uri = request.url_root.rstrip('/') + '/nest/callback'

    try:
        tokens = _nest_oauth_exchange({
            'code':         code,
            'grant_type':   'authorization_code',
            'redirect_uri': redirect_uri,
        })
    except Exception as exc:
        return f'<h2>Token exchange failed</h2><pre>{exc}</pre>', 500

    _nest_save_tokens(tokens, save_refresh=True)

    return ('<h2>Nest connected successfully!</h2>'
            '<p>You can close this tab and return to the dashboard.</p>'
            '<p>Enable the Nest connector in Settings to start receiving events.</p>')


@app.route('/api/debug/nest/status')
def api_debug_nest_status():
    token = get_setting('nest_access_token', '')
    expiry = get_setting_int('nest_token_expiry', 0)
    return jsonify({
        'enabled': get_setting_bool('nest_enabled', False),
        'has_refresh_token': bool(get_setting('nest_refresh_token', '')),
        'token_valid': bool(token and time.time() < expiry),
        'token_expiry': expiry,
        'subscription': get_setting('nest_pubsub_subscription', ''),
        'cached_devices': _nest_devices,
        'devices_cache_age': int(time.time() - _nest_devices_ts) if _nest_devices_ts else None,
    })


@app.route('/api/debug/nest/devices')
def api_debug_nest_devices():
    """Dump full device list with traits. Shows what events each device supports."""
    token = _nest_ensure_token()
    if not token:
        return jsonify({'error': 'no valid token'}), 401
    # Force refresh
    _nest_refresh_devices(token)
    summary = []
    for d in _nest_devices_raw:
        traits = d.get('traits', {})
        summary.append({
            'type': d.get('type', ''),
            'name': d.get('name', ''),
            'customName': traits.get('sdm.devices.traits.Info', {}).get('customName', ''),
            'parentRelations': d.get('parentRelations', []),
            'traits': list(traits.keys()),
            'has_clip_preview': 'sdm.devices.traits.CameraClipPreview' in traits,
            'has_event_image': 'sdm.devices.traits.CameraEventImage' in traits,
            'has_motion': 'sdm.devices.traits.CameraMotion' in traits,
            'has_person': 'sdm.devices.traits.CameraPerson' in traits,
        })
    return jsonify({
        'devices': summary,
        'event_counters': _nest_event_counters,
        'poll_stats': _nest_poll_stats,
    })


@app.route('/api/debug/nest/peek')
def api_debug_nest_peek():
    """Pull messages from Pub/Sub WITHOUT acknowledging (so they redeliver).
    Useful for seeing what Google is actually publishing."""
    import base64 as _b64
    subscription = get_setting('nest_pubsub_subscription', '')
    if not subscription:
        return jsonify({'error': 'no subscription configured'}), 400
    token = _nest_ensure_token()
    if not token:
        return jsonify({'error': 'no valid token'}), 401
    try:
        resp = _requests.post(
            f'https://pubsub.googleapis.com/v1/{subscription}:pull',
            headers={'Authorization': f'Bearer {token}'},
            json={'maxMessages': 20, 'returnImmediately': True},
            timeout=30,
        )
        resp.raise_for_status()
        messages = resp.json().get('receivedMessages', [])
        decoded = []
        for m in messages:
            try:
                raw = _b64.b64decode(m['message']['data']).decode('utf-8')
                decoded.append(json.loads(raw))
            except Exception as e:
                decoded.append({'decode_error': str(e)})
        return jsonify({'count': len(messages), 'messages': decoded})
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ── Switches drawer endpoints ────────────────────────────────────────────────
@app.route('/api/switches')
def api_switches():
    """Merged list of all switches across providers, with metadata + live state."""
    return jsonify(_get_all_switches())


@app.route('/api/switches/toggle', methods=['POST'])
def api_switches_toggle():
    data = request.get_json(silent=True) or {}
    rid  = data.get('id')
    if rid is None:
        return jsonify({'error': 'id required'}), 400
    res = switch_toggle(int(rid))
    if 'error' in res:
        return jsonify({'error': res['error']}), res.get('code', 500)
    return jsonify(res)


@app.route('/api/switches/set', methods=['POST'])
def api_switches_set():
    data = request.get_json(silent=True) or {}
    rid  = data.get('id')
    on   = data.get('on')
    if rid is None or not isinstance(on, bool):
        return jsonify({'error': 'id and on (bool) required'}), 400
    res = switch_set_state(int(rid), on)
    if 'error' in res:
        return jsonify({'error': res['error']}), res.get('code', 500)
    return jsonify(res)


@app.route('/api/switches/thermostat', methods=['POST'])
def api_switches_thermostat():
    """Update thermostat mode and/or setpoint(s). Payload:
       { id, mode?, setpoint_f?, setpoint_heat_f?, setpoint_cool_f? }"""
    data = request.get_json(silent=True) or {}
    rid  = data.get('id')
    if rid is None:
        return jsonify({'error': 'id required'}), 400
    fields = {}
    if 'mode' in data and data['mode'] is not None:
        fields['mode'] = str(data['mode']).upper()
    for k in ('setpoint_f', 'setpoint_heat_f', 'setpoint_cool_f'):
        if k in data and data[k] is not None:
            try:
                fields[k] = float(data[k])
            except (TypeError, ValueError):
                return jsonify({'error': f'{k} must be numeric'}), 400
    if not fields:
        return jsonify({'error': 'no fields to set'}), 400
    res = switch_set_thermostat(int(rid), **fields)
    if 'error' in res:
        return jsonify({'error': res['error']}), res.get('code', 500)
    return jsonify(res)


@app.route('/api/debug/nest/thermostats')
def api_debug_nest_thermostats():
    """Dump thermostat cache + raw SDM traits."""
    if request.args.get('refresh') == '1':
        token = _nest_ensure_token()
        if token:
            _nest_refresh_devices(token)
    return jsonify({
        'enabled':     get_setting_bool('nest_enabled', False),
        'count':       len(nest_mod._nest_thermostats),
        'thermostats': nest_mod._nest_thermostats,
    })


@app.route('/api/switches/brightness', methods=['POST'])
def api_switches_brightness():
    data = request.get_json(silent=True) or {}
    rid = data.get('id')
    b   = data.get('brightness')
    if rid is None or b is None:
        return jsonify({'error': 'id and brightness required'}), 400
    try:
        b = int(b)
    except (TypeError, ValueError):
        return jsonify({'error': 'brightness must be int'}), 400
    res = switch_set_brightness(int(rid), b)
    if 'error' in res:
        return jsonify({'error': res['error']}), res.get('code', 500)
    return jsonify(res)


@app.route('/api/switches/<int:rid>', methods=['PUT'])
def api_switches_update(rid):
    data = request.get_json(silent=True) or {}
    res  = switch_update_meta(rid, data)
    if 'error' in res:
        return jsonify({'error': res['error']}), res.get('code', 500)
    return jsonify(res)


@app.route('/api/switches/alarm/arm-home', methods=['POST'])
def api_switches_alarm_arm_home():
    """Arm Abode to Home mode. Body: {id} — id is the alarm row in switches_meta
    (used to validate the row exists + fetch friendly name for the event log)."""
    data = request.get_json(silent=True) or {}
    rid  = data.get('id')
    if rid is None:
        return jsonify({'error': 'id required'}), 400
    row = _switches_lookup(int(rid))
    if row is None:
        return jsonify({'error': 'not found'}), 404
    _, provider, ext_id, kind, name = row
    if provider != 'abode' or kind != 'alarm':
        return jsonify({'error': 'not an abode alarm row'}), 400
    try:
        new_mode = abode_arm_home()
        _switches_log_event('abode', 'alarm_armed', name,
                            detail=f'mode={new_mode}')
        return jsonify({'ok': True, 'mode': new_mode})
    except Exception as exc:
        _switches_log_event('abode', 'error', f'{name}: arm failed',
                            str(exc), 'failed')
        return jsonify({'error': str(exc)}), 500


@app.route('/api/switches/rediscover', methods=['POST'])
def api_switches_rediscover():
    counts = _switches_rediscover_all()
    return jsonify({'ok': True, 'counts': counts})


@app.route('/api/debug/tuya')
def api_debug_tuya():
    """Dump Tuya cache + indicate if devices.json was found.
    Pass ?probe=1 to also call status() on each device and return raw DPs —
    lets us see multi-outlet strips (DP 1/2/3/... each = one outlet)."""
    have_file = os.path.exists(_TUYA_DEVICEFILE)
    if request.args.get('refresh') == '1':
        count = _tuya_refresh_devices()
    else:
        count = len(tuya._tuya_devices)
    probe = request.args.get('probe') == '1'
    out = {}
    for k, v in tuya._tuya_devices.items():
        entry = {**v, 'local_key': '***'}
        if probe and v.get('ip'):
            try:
                dev = _tuya_make_outlet(k, v)
                status = dev.status()
                entry['probe'] = status.get('dps') if isinstance(status, dict) else status
            except Exception as exc:
                entry['probe_error'] = str(exc)
        out[k] = entry
    return jsonify({
        'enabled':         get_setting_bool('tuya_enabled', False),
        'has_devicefile':  have_file,
        'devicefile_path': _TUYA_DEVICEFILE,
        'count':           count,
        'age_s':           int(time.time() - tuya._tuya_ts) if tuya._tuya_ts else None,
        'devices':         out,
    })


@app.route('/api/debug/kasa')
def api_debug_kasa():
    """Dump current Kasa cache + optionally trigger a fresh discovery."""
    if request.args.get('refresh') == '1':
        n = _kasa_refresh_devices()
    else:
        n = len(kasa._kasa_devices)
    return jsonify({
        'enabled': get_setting_bool('kasa_enabled', False),
        'count':   n,
        'age_s':   int(time.time() - kasa._kasa_ts) if kasa._kasa_ts else None,
        'devices': kasa._kasa_devices,
    })


# ── Network device debug endpoints (Phase A: read-only, no DB) ────────────────
import lib.network as network_mod
import lib.network_devices as _netdev
from lib.network import (
    NETWORK_STATE_PATH, _NETWORK_REMOVE_MIN_OFFLINE_DAYS,
    _network_state, _network_state_lock,
    _network_router_cfg, _network_ap_cfgs,
    _network_state_to_list, _network_ap_by_name, _apply_filter_ban_map,
    _network_poll_once, _network_poll_loop,
)




@app.route('/api/debug/network/lrt224')
def api_debug_network_lrt224():
    cfg = _network_router_cfg()
    if not cfg['url']:
        return jsonify({'error': 'network_router_url not set'}), 400
    include_raw = request.args.get('raw') == '1'
    res = _netdev.fetch_lrt224(cfg['url'], cfg['user'], cfg['pass'])
    if not include_raw:
        res = {**res, 'raw': {k: f'<{len(v)} chars; pass ?raw=1 to view>'
                              for k, v in res.get('raw', {}).items()}}
    return jsonify(res)


@app.route('/api/debug/network/config')
def api_debug_network_config():
    """Show parsed config so we can spot password-mangling without exposing
    secrets. Reveals length + character classes + the raw JSON setting so
    we can see if special chars survived round-trip."""
    raw = get_setting('network_aps', '[]')
    aps = _netdev.load_ap_configs(raw)

    def fingerprint(s: str) -> dict:
        if s is None:
            return {'len': 0, 'classes': []}
        classes = set()
        for ch in s:
            if ch.isalpha(): classes.add('letter')
            elif ch.isdigit(): classes.add('digit')
            elif ch == ' ': classes.add('space')
            elif ord(ch) < 32: classes.add(f'ctrl-0x{ord(ch):02x}')
            else: classes.add(f'special-{ch!r}')
        return {'len': len(s), 'classes': sorted(classes)}

    return jsonify({
        'aps_raw_json': raw,
        'aps_parsed_count': len(aps),
        'aps': [
            {
                'name': a.get('name'),
                'url': a.get('url'),
                'user': a.get('user'),
                'pass_fingerprint': fingerprint(a.get('pass', '')),
            }
            for a in aps
        ],
        'router': {
            'url': get_setting('network_router_url', ''),
            'user': get_setting('network_router_user', ''),
            'pass_fingerprint': fingerprint(get_setting('network_router_pass', '')),
        },
    })


@app.route('/api/debug/network/local')
def api_debug_network_local():
    """Ping-sweep the LAN subnet from the dashboard host and dump the
    resulting ARP cache. This is the master device list since the LRT224
    won't share its ARP table."""
    subnet = request.args.get('subnet') or get_setting('network_local_subnet',
                                                       '10.0.0.0/24')
    return jsonify(_netdev.fetch_local_arp(subnet))


@app.route('/api/debug/network/lrt224/snmp')
def api_debug_network_lrt224_snmp():
    cfg = _network_router_cfg()
    host = cfg.get('snmp_host') or (cfg.get('url') or '').replace('http://', '') \
        .replace('https://', '').rstrip('/').split('/')[0].split(':')[0]
    if not host:
        return jsonify({'error': 'set network_router_snmp_host (or network_router_url)'}), 400
    return jsonify(_netdev.fetch_lrt224_snmp(
        host,
        community=cfg.get('snmp_community', 'public'),
        port=int(cfg.get('snmp_port', 161) or 161),
    ))


@app.route('/api/debug/network/lrt224/login_probe')
def api_debug_network_lrt224_probe():
    """Inspect the LRT224 login form so we can write the right login flow."""
    cfg = _network_router_cfg()
    if not cfg['url']:
        return jsonify({'error': 'network_router_url not set'}), 400
    return jsonify(_netdev.lrt224_probe_login(cfg['url']))


@app.route('/api/debug/network/ap/<name>/probe')
def api_debug_network_ap_probe(name):
    """Try every common DD-WRT auth combo to find one that returns 200."""
    aps = _network_ap_cfgs()
    match = next((a for a in aps if a.get('name') == name), None)
    if not match:
        return jsonify({'error': f'AP {name!r} not in network_aps',
                        'available': [a.get('name') for a in aps]}), 404
    return jsonify(_netdev.ddwrt_probe(match['url'], match.get('user', ''),
                                       match.get('pass', '')))


@app.route('/api/debug/network/ap/<name>')
def api_debug_network_ap(name):
    aps = _network_ap_cfgs()
    match = next((a for a in aps if a.get('name') == name), None)
    if not match:
        return jsonify({'error': f'AP {name!r} not in network_aps',
                        'available': [a.get('name') for a in aps]}), 404
    include_raw = request.args.get('raw') == '1'
    res = _netdev.fetch_ddwrt_ap(match['url'], match.get('user', ''),
                                 match.get('pass', ''), match.get('name', ''))
    if not include_raw:
        res = {**res, 'raw': {k: f'<{len(v)} chars; pass ?raw=1 to view>'
                              for k, v in res.get('raw', {}).items()}}
    return jsonify(res)


@app.route('/api/debug/network/all')
def api_debug_network_all():
    res = _netdev.fetch_all(_network_router_cfg(), _network_ap_cfgs(),
                            local_subnet=get_setting('network_local_subnet',
                                                     '10.0.0.0/24'))
    # Strip raw from per-source results unless ?raw=1.
    if request.args.get('raw') != '1':
        if res.get('router'):
            res['router'] = {**res['router'],
                             'raw': {k: f'<{len(v)} chars>'
                                     for k, v in res['router'].get('raw', {}).items()}}
        for ap in res.get('aps', []):
            ap['raw'] = {k: f'<{len(v)} chars>' for k, v in ap.get('raw', {}).items()}
    return jsonify(res)



# ── Network device CRUD routes ────────────────────────────────────────────────
@app.route('/api/network/devices')
def api_network_devices():
    online_only  = request.args.get('online') == '1'
    ap_filter    = request.args.get('ap')
    unnamed_only = request.args.get('unnamed') == '1'
    with _network_state_lock:
        items = _network_state_to_list(_network_state)
    if online_only:
        items = [d for d in items if d['online']]
    if ap_filter:
        items = [d for d in items if d['last_ap'] == ap_filter]
    if unnamed_only:
        items = [d for d in items if not d['friendly_name']]
    aps = sorted({d['last_ap'] for d in items if d['last_ap']})
    return jsonify({
        'devices': items,
        'total': len(items),
        'aps': aps,
        'last_poll_ts': network_mod._network_last_poll_ts,
        'last_poll': network_mod._network_last_poll_result,
        'enabled': get_setting_bool('network_enabled', False),
        'quarantined_aps': [
            {'name': n, 'until': t}
            for n, t in network_mod._network_ap_quarantine.items()
            if t > time.time()
        ],
    })


@app.route('/api/network/devices/<mac>', methods=['PUT'])
def api_network_device_update(mac):
    mac  = mac.lower()
    body = request.get_json() or {}
    with _network_state_lock:
        cur = _network_state.get(mac)
        if cur is None:
            cur = {'first_seen': int(time.time())}
            _network_state[mac] = cur
        for field in ('friendly_name', 'notes'):
            if field in body:
                cur[field] = str(body[field])[:500]
        if 'hidden' in body:
            cur['hidden'] = bool(body['hidden'])
        try:
            _netdev.save_state(NETWORK_STATE_PATH, _network_state)
        except Exception as exc:
            return jsonify({'error': f'save failed: {exc}'}), 500
    return jsonify({'ok': True, 'mac': mac, 'device': cur})


@app.route('/api/network/devices/<mac>', methods=['DELETE'])
def api_network_device_remove(mac):
    mac = mac.lower()
    with _network_state_lock:
        cur = _network_state.get(mac)
        if cur is None:
            return jsonify({'error': 'unknown mac'}), 404
        last_seen = cur.get('last_seen') or 0
        age_days = (time.time() - last_seen) / 86400 if last_seen else None
        if last_seen and age_days < _NETWORK_REMOVE_MIN_OFFLINE_DAYS:
            return jsonify({
                'error': 'device too recent to remove',
                'last_seen': last_seen,
                'offline_days': age_days,
                'min_offline_days': _NETWORK_REMOVE_MIN_OFFLINE_DAYS,
            }), 400
        _network_state.pop(mac, None)
        try:
            _netdev.save_state(NETWORK_STATE_PATH, _network_state)
        except Exception as exc:
            return jsonify({'error': f'save failed: {exc}'}), 500
    return jsonify({'ok': True, 'mac': mac})


@app.route('/api/network/rediscover', methods=['POST'])
def api_network_rediscover():
    try:
        result = _network_poll_once()
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    return jsonify({'ok': True, 'result': result})


@app.route('/api/network/ap_filters')
def api_network_ap_filters():
    out = []
    for ap in _network_ap_cfgs():
        if not ap.get('url'):
            continue
        res = _netdev.fetch_ddwrt_ap(ap['url'], ap.get('user', ''),
                                     ap.get('pass', ''),
                                     ap.get('name', ap['url']))
        out.append({
            'ap': ap.get('name'),
            'filters': res.get('filters', {}),
            'errors': res.get('errors', []),
        })
    return jsonify({'aps': out})


@app.route('/api/network/devices/<mac>/filters', methods=['PUT'])
def api_network_device_filters(mac):
    mac  = mac.lower()
    body = request.get_json() or {}
    desired = {
        'wl0': dict(body.get('wl0') or {}),
        'wl1': dict(body.get('wl1') or {}),
    }
    return jsonify(_apply_filter_ban_map(mac, desired))


@app.route('/api/network/devices/<mac>/pin', methods=['POST'])
def api_network_device_pin(mac):
    body         = request.get_json() or {}
    target_ap    = body.get('ap', '')
    target_radio = body.get('radio', 'either')
    if not target_ap:
        return jsonify({'error': 'ap is required'}), 400
    if target_radio not in ('wl0', 'wl1', 'either'):
        return jsonify({'error': 'radio must be wl0/wl1/either'}), 400
    aps = [a.get('name') for a in _network_ap_cfgs() if a.get('name')]
    if target_ap not in aps:
        return jsonify({'error': f'unknown ap {target_ap!r}', 'available': aps}), 400
    desired = {'wl0': {}, 'wl1': {}}
    for ap_name in aps:
        for radio in ('wl0', 'wl1'):
            if ap_name == target_ap and (target_radio == 'either'
                                         or target_radio == radio):
                desired[radio][ap_name] = False
            else:
                desired[radio][ap_name] = True
    return jsonify(_apply_filter_ban_map(mac.lower(), desired))


@app.route('/api/network/devices/<mac>/unpin', methods=['POST'])
def api_network_device_unpin(mac):
    aps     = [a.get('name') for a in _network_ap_cfgs() if a.get('name')]
    desired = {'wl0': {n: False for n in aps},
               'wl1': {n: False for n in aps}}
    return jsonify(_apply_filter_ban_map(mac.lower(), desired))


# ── Event Log endpoint ────────────────────────────────────────────────────────
@app.route('/api/events')
def api_events():
    limit, err  = _arg_int('limit', 50)
    if err: return err
    offset, err = _arg_int('offset', 0)
    if err: return err
    limit  = min(limit, 500)
    offset = max(offset, 0)
    system = request.args.get('system', 'all')
    etype  = request.args.get('type')

    # Date range: accept start/end unix timestamps, fall back to days param
    start_ts, err = _arg_int('start', None)
    if err: return err
    end_ts, err = _arg_int('end', None)
    if err: return err
    if start_ts is None:
        days, err = _arg_int('days', 7)
        if err: return err
        start_ts = int(time.time()) - min(days, 365) * 86400

    query  = 'SELECT id,ts,system,event_type,title,detail,result,source,battery_pct FROM event_log WHERE ts >= ?'
    params: list = [start_ts]

    if end_ts:
        query += ' AND ts <= ?'
        params.append(end_ts)
    if system == 'errors':
        query += " AND (result = 'failed' OR event_type = 'error')"
    elif system != 'all':
        systems_list = [s.strip() for s in system.split(',') if s.strip()]
        if len(systems_list) == 1:
            query += ' AND system = ?'
            params.append(systems_list[0])
        elif systems_list:
            placeholders = ','.join('?' * len(systems_list))
            query += f' AND system IN ({placeholders})'
            params.extend(systems_list)
    if etype:
        query += ' AND event_type = ?'
        params.append(etype)

    query += ' ORDER BY ts DESC LIMIT ? OFFSET ?'
    params.append(limit + 1)   # fetch one extra to detect has_more
    params.append(offset)

    with connect() as c:
        rows = c.execute(query, params).fetchall()

    has_more = len(rows) > limit
    rows = rows[:limit]

    results = []
    for row in rows:
        rid, ts, sys_, evt, title, detail, result, source, batt = row
        d = datetime.fromtimestamp(ts)
        ts_display = (
            d.strftime('%b %#d  %#I:%M %p') if os.name == 'nt'
            else d.strftime('%b %-d  %-I:%M %p')
        )
        results.append({
            'id':          rid,
            'ts':          ts,
            'ts_display':  ts_display,
            'system':      sys_,
            'event_type':  evt,
            'title':       title,
            'detail':      detail,
            'result':      result,
            'source':      source,
            'battery_pct': batt,
        })
    return jsonify({'events': results, 'has_more': has_more})


# ── Settings endpoints ────────────────────────────────────────────────────────
@app.route('/api/settings')
def api_settings():
    settings = load_settings()
    # Add runtime info for each connector
    connectors = [
        {
            'key': 'powerwall',
            'label': 'Powerwall',
            'type': 'continuous',
            'enabled_key': 'powerwall_enabled',
            'intervals': [
                {'key': 'powerwall_poll_interval', 'label': 'Poll interval', 'unit': 's'},
                {'key': 'powerwall_db_write_interval', 'label': 'DB write interval', 'unit': 's'},
            ],
        },
        {
            'key': 'pool',
            'label': 'Pool (ScreenLogic)',
            'type': 'on-demand',
            'enabled_key': 'pool_enabled',
            'intervals': [
                {'key': 'pool_poll_interval', 'label': 'Poll interval', 'unit': 's'},
            ],
        },
        {
            'key': 'rachio',
            'label': 'Rachio / Sprinklers',
            'type': 'on-demand',
            'enabled_key': 'rachio_enabled',
            'intervals': [
                {'key': 'rachio_poll_interval',      'label': 'Schedule poll',  'unit': 's'},
                {'key': 'rachio_event_poll_interval', 'label': 'Event log poll', 'unit': 's'},
            ],
        },
        {
            'key': 'rain_skip',
            'label': 'Smart Rain Skip',
            'type': 'on-demand',
            'enabled_key': 'rain_skip_enabled',
            'intervals': [
                {'key': 'rain_skip_check_interval', 'label': 'Check interval',  'unit': 's'},
                {'key': 'rain_lookback_days',       'label': 'Rain lookback',   'unit': 'days'},
                {'key': 'rain_mm_per_skip_day',     'label': 'mm per skip day', 'unit': 'text'},
                {'key': 'rain_skip_max_days',       'label': 'Max skip days',   'unit': 'days'},
            ],
        },
        {
            'key': 'abode',
            'label': 'Abode',
            'type': 'websocket',
            'enabled_key': 'abode_enabled',
            'intervals': [],
        },
        {
            'key': 'kasa',
            'label': 'Kasa Smart Plugs (LAN)',
            'type': 'continuous',
            'enabled_key': 'kasa_enabled',
            'intervals': [
                {'key': 'kasa_poll_interval',     'label': 'State poll', 'unit': 's'},
                {'key': 'kasa_state_poll_enabled', 'label': 'Poll device state',
                 'unit': 'select', 'options': ['0', '1']},
            ],
        },
        {
            'key': 'nest_thermostat',
            'label': 'Nest Thermostat (SDM)',
            'type': 'on-demand',
            'enabled_key': 'nest_thermostat_enabled',
            'intervals': [],
        },
        {
            'key': 'tuya',
            'label': 'Tuya / Smart Life (LAN)',
            'type': 'continuous',
            'enabled_key': 'tuya_enabled',
            'intervals': [
                {'key': 'tuya_poll_interval', 'label': 'State poll', 'unit': 's'},
            ],
        },
        {
            'key': 'maintenance',
            'label': 'Maintenance',
            'type': 'scheduled',
            'intervals': [
                {'key': 'cost_rebuild_days', 'label': 'Cost rebuild', 'unit': 'days'},
                {'key': 'refresh_start_date', 'label': 'Refresh start date', 'unit': 'date'},
                {'key': 'holidays_poll_months', 'label': 'Holiday refresh', 'unit': 'months'},
                {'key': 'rates_poll_months', 'label': 'Energy Rate refresh', 'unit': 'months'},
            ],
        },
        {
            'key': 'sdge',
            'label': 'SDG\u0026E Rates',
            'type': 'configurable',
            'intervals': [
                {'key': 'rates_page_url', 'label': 'Rates page URL', 'unit': 'url'},
                {'key': 'rate_schedule_name', 'label': 'Schedule name', 'unit': 'text'},
            ],
        },
        {
            'key': 'gemini',
            'label': 'Gemini AI (primary)',
            'type': 'configurable',
            'intervals': [
                {'key': 'gemini_api_key', 'label': 'API Key', 'unit': 'text'},
                {'key': 'gemini_model', 'label': 'Model', 'unit': 'select',
                 'options': ['gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-2.0-flash']},
            ],
        },
        {
            'key': 'azure_openai',
            'label': 'Azure OpenAI (fallback)',
            'type': 'configurable',
            'intervals': [
                {'key': 'azure_openai_endpoint', 'label': 'Endpoint', 'unit': 'url'},
                {'key': 'azure_openai_api_key', 'label': 'API Key', 'unit': 'text'},
                {'key': 'azure_openai_deployment', 'label': 'Deployment', 'unit': 'text'},
                {'key': 'azure_openai_api_version', 'label': 'API Version', 'unit': 'text'},
            ],
        },
        {
            'key': 'frontend',
            'label': 'Dashboard Refresh',
            'type': 'frontend',
            'intervals': [
                {'key': 'fe_poll_interval', 'label': 'Live power', 'unit': 'ms'},
                {'key': 'fe_chart_interval', 'label': 'Chart', 'unit': 'ms'},
                {'key': 'fe_weather_interval', 'label': 'Weather', 'unit': 'ms'},
                {'key': 'fe_automations_interval', 'label': 'Automations', 'unit': 'ms'},
                {'key': 'fe_pool_interval', 'label': 'Pool tile', 'unit': 'ms'},
                {'key': 'fe_costs_interval', 'label': 'Costs tile', 'unit': 'ms'},
                {'key': 'fe_rates_interval', 'label': 'Rates', 'unit': 'ms'},
                {'key': 'fe_events_interval', 'label': 'Event log', 'unit': 'ms'},
            ],
        },
        # Long-form cards — kept at the bottom so the shorter ones tile cleanly up top.
        {
            'key': 'nest',
            'label': 'Nest (Camera/Doorbell)',
            'type': 'on-demand',
            'enabled_key': 'nest_enabled',
            'intervals': [
                {'key': 'nest_poll_interval',       'label': 'Poll interval',        'unit': 's'},
                {'key': 'nest_pubsub_subscription', 'label': 'Pub/Sub subscription', 'unit': 'text'},
                {'key': 'nest_client_id',           'label': 'OAuth Client ID',      'unit': 'text'},
                {'key': 'nest_client_secret',       'label': 'OAuth Client Secret',  'unit': 'text'},
                {'key': 'nest_project_id',          'label': 'Device Access Project', 'unit': 'text'},
            ],
        },
        {
            'key': 'network',
            'label': 'Network Devices (LRT224 + DD-WRT APs)',
            'type': 'continuous',
            'enabled_key': 'network_enabled',
            'intervals': [
                {'key': 'network_poll_interval', 'label': 'Poll interval', 'unit': 's'},
                {'key': 'network_router_url',    'label': 'Router URL',    'unit': 'url'},
                {'key': 'network_router_user',   'label': 'Router user',   'unit': 'text'},
                {'key': 'network_router_pass',   'label': 'Router pass',   'unit': 'text'},
                {'key': 'network_local_subnet',  'label': 'LAN subnet (ping-sweep)', 'unit': 'text'},
                {'key': 'network_aps',           'label': 'APs (JSON)',    'unit': 'text'},
            ],
        },
    ]
    return jsonify({'settings': settings, 'connectors': connectors})


@app.route('/api/settings', methods=['PUT'])
def api_settings_update():
    data = request.get_json() or {}
    valid_keys = set(_SETTINGS_DEFAULTS.keys())
    with connect() as c:
        for key, value in data.items():
            if key in valid_keys:
                c.execute(
                    'INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)',
                    (key, str(value))
                )
        c.commit()
    return jsonify({'ok': True})


# ── Windows Service (optional) ────────────────────────────────────────────────
try:
    import win32event, win32service, win32serviceutil, servicemanager

    class PowerwallDashboardService(win32serviceutil.ServiceFramework):
        _svc_name_         = 'PowerwallDashboard'
        _svc_display_name_ = 'Powerwall Dashboard'
        _svc_description_  = 'Powerwall monitoring dashboard (Flask + pypowerwall)'

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop = win32event.CreateEvent(None, 0, 0, None)

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            win32event.SetEvent(self._stop)

        def SvcDoRun(self):
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                                  servicemanager.PYS_SERVICE_STARTED,
                                  (self._svc_name_, ''))
            _start()

    HAS_WIN32 = True

except ImportError:
    HAS_WIN32 = False



def _start():
    os.chdir(BASE_DIR)
    init_db()
    _load_pool_gpm_cache()
    _backfill_rates_event_url()
    backfill_history()
    # Seed switches_meta with known pool circuits on startup so tiles appear
    # without requiring a manual rediscover. Kasa discovery is driven
    # by its enabled flag in the poller loop.
    if get_setting_bool('pool_enabled', True):
        try:
            _pool_discover_circuits()
        except Exception as exc:
            print(f'Pool circuit seed error: {exc}')
    if get_setting_bool('abode_enabled', True):
        try:
            _abode_seed_alarm_row()
        except Exception as exc:
            print(f'Abode alarm seed error: {exc}')
    # Start the persistent Kasa asyncio loop so Device connections survive.
    if get_setting_bool('kasa_enabled', False):
        try:
            _kasa_start_loop()
        except Exception as exc:
            print(f'Kasa loop start error: {exc}')
    threading.Thread(target=rebuild_daily_costs, daemon=True).start()
    threading.Thread(target=_recalc_pool_target, daemon=True).start()
    threading.Thread(target=poller, daemon=True).start()
    threading.Thread(target=_network_poll_loop, daemon=True).start()
    start_abode_listener()
    print('Dashboard \u2192 http://localhost:5001')
    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    if len(sys.argv) > 1:
        if HAS_WIN32:
            win32serviceutil.HandleCommandLine(PowerwallDashboardService)
        else:
            print('pywin32 not installed.  Run: pip install pywin32')
            sys.exit(1)
    else:
        _start()
