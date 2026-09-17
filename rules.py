"""
Powerwall Rules Engine — v2
Loads rules from SQLite (powerwall.db), re-reads each eval cycle.

Usage:
  py rules.py              # run in foreground
  py rules.py install      # install Windows service  (requires: pip install pywin32)
  py rules.py start
  py rules.py stop
  py rules.py remove
"""

import os, sys, time, logging, logging.handlers, json
from datetime import datetime, date, timedelta

import pypowerwall
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

from lib.fetch_rates import is_sdge_holiday, holiday_name
from lib.db import init_db, connect

# ── Config ────────────────────────────────────────────────────────────────────
PW_EMAIL      = 'don@nsdsolutions.com'
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DB_PATH       = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'powerwall.db'))
LOG_PATH      = os.environ.get('LOG_PATH', os.path.join(BASE_DIR, 'rules.log'))
EVAL_INTERVAL = 60    # seconds between evaluations
LOOP_SLEEP    = 30    # main loop cadence in seconds
# Always-enforce reconciliation means a field that never converges would retry
# forever; these bound both the log noise and the traffic to Tesla.
ERROR_LOG_INTERVAL  = 300   # min gap between repeat error rows for the same field
CONVERGE_FAIL_LIMIT = 10    # consecutive failures before a field backs off
CONVERGE_BACKOFF    = 300   # retry gap for a field that will not converge
UNREADABLE_ALERT_AFTER = 300  # unreadable this long → red event_log row on the dashboard
RECONNECT_AFTER_FAILS  = 3    # consecutive unreadable cycles before a full reconnect
RECONNECT_MIN_GAP      = 300  # min gap between those reconnects

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=3
        ),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('rules')


class _LastErrorHandler(logging.Handler):
    """Remembers pypowerwall's most recent ERROR message.

    pypowerwall reports the real cause of a failure (HTTP status, token refresh
    401) only through its logger and hands callers a bare None, so this is the
    only way to put the reason into an event_log row's detail.
    """
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.last = None

    def emit(self, record):
        try:
            self.last = record.getMessage()[:500]
        except Exception:
            pass


_pw_errors = _LastErrorHandler()
logging.getLogger('pypowerwall').addHandler(_pw_errors)


def log_event(conn, system, event_type, title, detail=None,
              result=None, source='live', battery_pct=None):
    conn.execute(
        'INSERT INTO event_log '
        '(ts, system, event_type, title, detail, result, source, battery_pct) '
        'VALUES (?,?,?,?,?,?,?,?)',
        (int(time.time()), system, event_type, title,
         detail, result, source, battery_pct)
    )
    conn.commit()


def load_rules_from_db(conn) -> list:
    """Return list of enabled rule dicts with parsed days/months and conditions list."""
    rows = conn.execute(
        'SELECT id,name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export '
        'FROM rules WHERE enabled=1'
    ).fetchall()

    cond_rows = conn.execute(
        '''SELECT rc.rule_id, rc.logic, rc.type, rc.operator, rc.value
           FROM rule_conditions rc
           JOIN rules r ON r.id = rc.rule_id
           WHERE r.enabled = 1'''
    ).fetchall()
    cond_map = {}
    for rule_id, logic, ctype, op, val in cond_rows:
        cond_map.setdefault(rule_id, []).append(
            {'logic': logic, 'type': ctype, 'operator': op, 'value': val}
        )

    rules = []
    for row in rows:
        rid, name, enabled, days_j, months_j, hour, minute, mode, reserve, gc, ge = row
        grid_charging = None if gc is None else bool(gc)
        rules.append({
            'id': rid,
            'name': name,
            'days': frozenset(json.loads(days_j)),
            'months': frozenset(json.loads(months_j)),
            'hour': hour, 'minute': minute,
            'mode': mode, 'reserve': reserve,
            'grid_charging': grid_charging,
            'grid_export': ge,
            'conditions': cond_map.get(rid, []),
        })
    return rules


# ── Condition evaluation ──────────────────────────────────────────────────────
def _eval_single(cond: dict, live: dict) -> bool:
    ctype = cond['type']
    if ctype in ('battery_pct', 'net_cost', 'net_cost_ytd', 'tomorrow_solar_kwh'):
        actual = live.get(ctype)
        if actual is None:
            return False  # data unavailable — condition fails safe
        op = cond['operator']
        v  = cond['value']
        if op == '>':  return actual >  v
        if op == '<':  return actual <  v
        if op == '>=': return actual >= v
        if op == '<=': return actual <= v
    return False


def evaluate_conditions(conditions: list, live: dict) -> bool:
    """
    AND conditions: all must pass.
    OR  conditions: at least one must pass (or none exist).
    Mixed: AND conditions are checked first; if any AND fails → False.
    Then OR block: passes if no OR conditions exist OR any passes.
    """
    if not conditions:
        return True
    and_conds = [c for c in conditions if c['logic'] == 'AND']
    or_conds  = [c for c in conditions if c['logic'] == 'OR']
    if and_conds and not all(_eval_single(c, live) for c in and_conds):
        return False
    if or_conds and not any(_eval_single(c, live) for c in or_conds):
        return False
    return True


# ── State reconstruction ──────────────────────────────────────────────────────
def _rule_fires_at(rule: dict, d: date) -> datetime | None:
    weekday    = d.weekday()
    is_holiday = is_sdge_holiday(d)
    has_weekend = bool(rule['days'] & {5, 6})

    if is_holiday:
        # Treat holiday like a weekend: only weekend rules fire
        if not has_weekend:
            return None
    else:
        if weekday not in rule['days']:
            return None

    if d.month not in rule['months']:
        return None
    return datetime(d.year, d.month, d.day, rule['hour'], rule['minute'])


def current_target_state(dt: datetime, rules: list, live: dict, cond_cache: dict | None = None) -> dict:
    state = {
        'mode':          'autonomous',
        'reserve':       20,
        'grid_charging': False,
        'grid_export':   'pv_only',
    }
    fired_events = []
    for delta_days in (2, 1, 0):
        d = dt.date() - timedelta(days=delta_days)
        for rule in rules:
            fire_dt = _rule_fires_at(rule, d)
            if fire_dt and fire_dt <= dt:
                fired_events.append((fire_dt, rule))

    for fire_dt, rule in sorted(fired_events, key=lambda x: x[0]):
        if rule['conditions']:
            cache_key = (rule['id'], fire_dt.isoformat())
            if cond_cache is not None and cache_key not in cond_cache:
                cond_cache[cache_key] = evaluate_conditions(rule['conditions'], live)
            passed = cond_cache.get(cache_key, True) if cond_cache is not None else evaluate_conditions(rule['conditions'], live)
            if not passed:
                continue
        for key in ('mode', 'reserve', 'grid_charging', 'grid_export'):
            if rule[key] is not None:
                state[key] = rule[key]

    if is_sdge_holiday(dt.date()):
        state['_holiday'] = holiday_name(dt.date())

    return state


def next_rule_fire(dt: datetime, rules: list) -> datetime | None:
    soonest = None
    for delta_days in (0, 1, 2):
        d = dt.date() + timedelta(days=delta_days)
        for rule in rules:
            fire_dt = _rule_fires_at(rule, d)
            if fire_dt and fire_dt > dt:
                if soonest is None or fire_dt < soonest:
                    soonest = fire_dt
    return soonest


def get_live_state(conn) -> dict:
    state = {}
    # battery_pct: skip NULL rows (backfill from Fleet API history has no SoC) and
    # zero rows (written during cloud outages) — both would make < N conditions
    # spuriously True.  None causes _eval_single to return False (fail safe).
    try:
        row = conn.execute(
            'SELECT battery_pct FROM readings WHERE battery_pct > 0 ORDER BY timestamp DESC LIMIT 1'
        ).fetchone()
        state['battery_pct'] = float(row[0]) if row else None
    except Exception:
        state['battery_pct'] = None
    try:
        today = date.today().isoformat()
        row = conn.execute(
            'SELECT import_cost - export_credit FROM daily_costs WHERE date = ?', (today,)
        ).fetchone()
        state['net_cost'] = float(row[0]) if row and row[0] is not None else None
    except Exception:
        state['net_cost'] = None
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = 'tomorrow_solar_kwh'"
        ).fetchone()
        state['tomorrow_solar_kwh'] = float(row[0]) if row and row[0] is not None else None
    except Exception:
        state['tomorrow_solar_kwh'] = None
    try:
        year_start = f"{date.today().year}-01-01"
        row = conn.execute(
            'SELECT SUM(import_cost) - SUM(export_credit) FROM daily_costs WHERE date >= ?',
            (year_start,)
        ).fetchone()
        state['net_cost_ytd'] = float(row[0]) if row and row[0] is not None else None
    except Exception:
        state['net_cost_ytd'] = None
    return state


# ── Mode label for display ────────────────────────────────────────────────────
_MODE_LABEL = {
    'self_consumption': 'Self-Powered',
    'autonomous':       'Time-Based Control',
    'backup':           'Backup',
}


# ── Apply Settings ────────────────────────────────────────────────────────────
_CONN_ERROR_NAMES = {'ConnectionError', 'Timeout', 'ConnectTimeout', 'ReadTimeout',
                     'SSLError', 'ProxyError', 'ChunkedEncodingError', 'OSError'}


def _is_connection_error(exc: Exception) -> bool:
    """True only for transport-level faults, where rebuilding the client may help."""
    names = {cls.__name__ for cls in type(exc).__mro__}
    return bool(names & _CONN_ERROR_NAMES)


def new_apply_state() -> dict:
    """Per-field retry bookkeeping for apply_settings (not a cache of applied values)."""
    return {'fails': {}, 'next_try': {}, 'last_err': {}}


def new_read_state() -> dict:
    """Bookkeeping for reading Powerwall state: token-file tracking + outage alerts."""
    return {'mtime': None, 'fails': 0, 'down_since': 0.0, 'alerted': False,
            'last_reconnect': 0.0, 'last_warn': 0.0}


def _token_mtime(fleet) -> float | None:
    try:
        return os.path.getmtime(fleet.configfile)
    except Exception:
        return None


def _reload_tokens(fleet, state: dict, reason: str) -> None:
    """Re-read access/refresh tokens from the shared token file.

    rules.py and server.py's poller share one Fleet API token file, and Tesla
    rotates the refresh token on every use — so whenever the poller refreshes, the
    refresh token rules.py holds in memory is dead.  Without a reload, rules.py
    401s on its next refresh forever while the file on disk is perfectly valid
    (13 hours on 2026-09-16).
    """
    mtime = _token_mtime(fleet)   # taken before loading: a write mid-load re-triggers
    try:
        fleet.load_config()
        state['mtime'] = mtime
        log.info('Reloaded Fleet API tokens (%s)', reason)
    except Exception as exc:
        # A read racing the poller's write can hit a half-written file; retry next cycle.
        log.warning('Token reload failed (%s): %r', reason, exc)


def read_actual_state(pw, state: dict | None = None) -> dict | None:
    """Read the Powerwall's *actual* current settings back from Tesla.

    One forced site_info refresh; the four getters below are then served from that
    same warm cache, so this costs a single HTTP round trip.  Returns None when
    Tesla is unreadable — the caller skips the cycle rather than enforcing blind.

    With `state`, first picks up tokens rotated by the other process (token file
    changed on disk), and reloads again after a failed read.
    """
    try:
        fleet = pw.client.fleet
    except Exception as exc:
        log.error('read_actual_state: no fleet client: %r', exc)
        return None

    if state is not None:
        mtime = _token_mtime(fleet)
        if state['mtime'] is None:
            state['mtime'] = mtime
        elif mtime is not None and mtime != state['mtime']:
            _reload_tokens(fleet, state, 'token file changed')

    try:
        if fleet.get_site_info(force=True):
            reserve = fleet.get_battery_reserve()
            return {
                'reserve':       None if reserve is None else int(reserve),
                'mode':          fleet.get_operating_mode(),
                'grid_charging': bool(fleet.get_grid_charging()),
                'grid_export':   fleet.get_grid_export(),
            }
    except Exception as exc:
        log.error('read_actual_state failed: %r', exc)
        _pw_errors.last = repr(exc)[:500]

    if state is not None:
        _reload_tokens(fleet, state, 'read failed')
    return None


def _emit_event(conn, *args, **kwargs) -> None:
    """log_event on `conn`, or on a short-lived connection when there is none."""
    try:
        if conn is not None:
            log_event(conn, *args, **kwargs)
            return
        c = connect()
        try:
            log_event(c, *args, **kwargs)
        finally:
            c.close()
    except Exception as exc:
        log.error('event_log write failed: %r', exc)


def note_unreadable(conn, state: dict, now: float) -> bool:
    """Record a cycle where Powerwall state could not be read.

    Writes one red event_log row once the outage passes UNREADABLE_ALERT_AFTER —
    previously this was a rules.log-only warning and a 13-hour outage never
    reached the dashboard.  Returns True when the caller should force a reconnect.
    """
    state['fails'] += 1
    if not state['down_since']:
        state['down_since'] = now
    down = now - state['down_since']

    if now - state['last_warn'] >= ERROR_LOG_INTERVAL:
        state['last_warn'] = now
        log.warning('Could not read Powerwall state for %ds — skipping apply', int(down))

    if not state['alerted'] and down >= UNREADABLE_ALERT_AFTER:
        state['alerted'] = True
        _emit_event(conn, 'powerwall', 'error',
                    "Rules engine can't read Powerwall — automations paused",
                    detail=_pw_errors.last or 'no error recorded', result='failed')

    if (state['fails'] >= RECONNECT_AFTER_FAILS
            and now - state['last_reconnect'] >= RECONNECT_MIN_GAP):
        state['last_reconnect'] = now
        return True
    return False


def note_readable(conn, state: dict, now: float) -> None:
    """Record a successful read; logs recovery if an outage had been alerted."""
    if state['alerted']:
        mins = max(1, int((now - state['down_since']) / 60))
        log.info('Powerwall readable again after %d min', mins)
        _emit_event(conn, 'powerwall', 'rules_engine_recovered',
                    f'Rules engine reconnected after {mins} min — automations resumed',
                    result='ok')
    state['fails'] = 0
    state['down_since'] = 0.0
    state['alerted'] = False
    _pw_errors.last = None


def _set_grid_import_export(fleet, allow_charge: bool, export_rule: str):
    """Write both grid_import_export fields in a single POST.

    Tesla exposes grid charging and the export rule on one endpoint, but
    pypowerwall's set_grid_charging/set_grid_export each send only their own key.
    Writing one key at a time risks the omitted field being reset to default —
    and under always-enforce reconciliation two setters that clobber each other
    would ping-pong once a cycle forever.  Always send both.
    """
    data = {
        'disallow_charge_from_grid_with_solar_installed': not allow_charge,
        'customer_preferred_export_rule': export_rule,
    }
    payload = fleet.poll(f'api/1/energy_sites/{fleet.site_id}/grid_import_export',
                         'POST', data)
    fleet.pwcachetime.pop(f'api/1/energy_sites/{fleet.site_id}/site_info', None)
    return payload


def apply_settings(pw, target: dict, actual: dict, state: dict,
                   conn=None, battery_pct=None) -> bool:
    """Reconcile the Powerwall toward target. Logs one combined event row per call.

    Compares against `actual` — read back from Tesla this cycle — never against a
    cache of what we believe we wrote.  A write that Tesla accepts but never
    applies is therefore retried next cycle instead of being remembered as done.

    Calls the fleet setters directly rather than pw.set_reserve/pw.set_mode: the
    pypowerwall wrapper (set_operation) back-fills a missing reserve via
    get_reserve(), which returns None while Tesla is erroring, then raises
    TypeError on `level > 0` — aborting every setting after it.
    """
    fleet   = pw.client.fleet
    changes = []
    errors  = []
    now     = time.time()

    def _settled(key):
        state['fails'].pop(key, None)
        state['next_try'].pop(key, None)

    def _attempt(key, call, labels):
        """One write plus its retry bookkeeping. labels = [(text, event_type), ...]."""
        if now < state['next_try'].get(key, 0.0):
            return
        # Isolated per key: one write failing must not skip the others.
        try:
            result = call()
            reason = f'returned {result!r}'
        except Exception as exc:
            result = None
            reason = repr(exc)

        # Falsy, not just None — the fleet setters return False for a value they
        # reject, which the old `is not None` test logged as success.
        if result:
            log.info('set_%s → OK (%s)', key, ', '.join(t for t, _ in labels))
            changes.extend(labels)
            _settled(key)
            return

        fails = state['fails'].get(key, 0) + 1
        state['fails'][key] = fails
        log.error('set_%s failed (%s, attempt %d)', key, reason, fails)
        if fails == CONVERGE_FAIL_LIMIT:
            log.warning('%s has not converged after %d attempts — backing off to '
                        'one retry per %ds', key, fails, CONVERGE_BACKOFF)
        if fails >= CONVERGE_FAIL_LIMIT:
            state['next_try'][key] = now + CONVERGE_BACKOFF
        if now - state['last_err'].get(key, 0.0) >= ERROR_LOG_INTERVAL:
            state['last_err'][key] = now
            errors.append((f'set_{key} failed', reason))

    try:
        # reserve and mode each own their endpoint (/backup, /operation).
        for field, setter, label, etype in (
            ('reserve', fleet.set_battery_reserve,
             lambda v: f"Reserve → {v}%",                   'reserve_changed'),
            ('mode',    fleet.set_operating_mode,
             lambda v: f"Mode → {_MODE_LABEL.get(v, v)}",   'mode_changed'),
        ):
            want = target.get(field)
            if want is None or want == actual.get(field):
                _settled(field)
                continue
            _attempt(field, lambda s=setter, w=want: s(w), [(label(want), etype)])

        # grid charging + export rule share one endpoint — write them together,
        # carrying over the current value for whichever field no rule sets.
        cur_charge, cur_export = actual.get('grid_charging'), actual.get('grid_export')
        want_charge, want_export = target.get('grid_charging'), target.get('grid_export')
        eff_charge = cur_charge if want_charge is None else want_charge
        eff_export = cur_export if want_export is None else want_export

        labels = []
        if eff_charge != cur_charge:
            labels.append((f"Grid charging → {'ON' if eff_charge else 'OFF'}",
                           'grid_charging_changed'))
        if eff_export != cur_export:
            labels.append((f"Grid export → {eff_export}", 'grid_export_changed'))

        if labels and eff_charge is not None and eff_export is not None:
            _attempt('grid_import_export',
                     lambda: _set_grid_import_export(fleet, eff_charge, eff_export),
                     labels)
        elif not labels:
            _settled('grid_import_export')
    finally:
        # In `finally` so a throw above can never discard already-collected errors
        # — that is how a 24-minute crash loop stayed invisible on the dashboard.
        if conn:
            if changes:
                title = '  ·  '.join(lbl for lbl, _ in changes)
                ctype = changes[0][1] if len(changes) == 1 else 'automation_fired'
                log_event(conn, 'powerwall', ctype, title,
                          result='ok', battery_pct=battery_pct)
            if errors:
                log_event(conn, 'powerwall', 'error',
                          '  ·  '.join(t for t, _ in errors),
                          detail='\n'.join(f'{t}: {d}' for t, d in errors),
                          result='failed', battery_pct=battery_pct)

    return bool(changes or errors)


# ── Main Loop ─────────────────────────────────────────────────────────────────
def main_loop(stop_fn=None):
    os.chdir(BASE_DIR)
    log.info('Powerwall Rules Engine v2 starting.')

    init_db()  # lib/db handles schema + seeding via its own connection

    pw               = None
    last_eval        = 0.0
    apply_state      = new_apply_state()
    pw_retry_after   = 0.0   # epoch — don't call apply_settings until this time
    read_state       = new_read_state()
    last_holiday_logged = None
    last_nxt         = None
    last_state_sig   = None
    cond_cache: dict = {}  # {(rule_id, fire_dt_iso): bool} — conditions evaluated once at fire time

    while True:
        if stop_fn and stop_fn():
            log.info('Stop signal — exiting.')
            break

        now = time.time()

        if pw is None:
            try:
                log.info('Connecting to Powerwall (Fleet API mode)…')
                # Shares server.py's token file by design. A separate authpath
                # needs its own OAuth: pypowerwall requires the config file to
                # already exist in fleetapi mode (os.access on a missing file is
                # False) and never bootstraps one, and a mere *copy* of this file
                # is worse than sharing — it dies permanently when Tesla rotates
                # the refresh token, whereas re-reading the shared file recovers.
                pw = pypowerwall.Powerwall('', fleetapi=True,
                                           email=PW_EMAIL, timeout=30,
                                           authpath=BASE_DIR)
                # pypowerwall does not raise when site discovery fails — it logs
                # "No sites found" and returns a client with no site_id, against
                # which every later call fails.  Treat that as a bad connect.
                if not pw.siteid:
                    pw = None
                    log.error('Connected but no site_id (site discovery failed) '
                              '— retry in %ds', LOOP_SLEEP)
                    note_unreadable(None, read_state, now)
                    time.sleep(LOOP_SLEEP)
                    continue
                read_state['mtime'] = _token_mtime(pw.client.fleet)
                log.info('Connected.')
            except Exception as exc:
                log.error('Connection failed: %s — retry in %ds', exc, LOOP_SLEEP)
                _pw_errors.last = _pw_errors.last or repr(exc)[:500]
                note_unreadable(None, read_state, now)
                time.sleep(LOOP_SLEEP)
                continue

        if now - last_eval >= EVAL_INTERVAL:
            target = None
            live   = {}
            # Fresh connection per eval cycle — the 60s cadence makes open/close
            # cheap, and not holding a long-lived handle avoids pinning the WAL.
            conn = connect()
            try:
                try:
                    rules = load_rules_from_db(conn)
                    live  = get_live_state(conn)
                    dt    = datetime.now()

                    # Prune cond_cache entries older than 3 days to prevent unbounded growth
                    cutoff = (dt - timedelta(days=3)).isoformat()
                    cond_cache = {k: v for k, v in cond_cache.items() if k[1] >= cutoff}

                    target = current_target_state(dt, rules, live, cond_cache)
                    nxt    = next_rule_fire(dt, rules)

                    # Log holiday once per day
                    hol = target.pop('_holiday', None)
                    if hol and last_holiday_logged != dt.date():
                        log.info('Holiday active: %s — weekend rules apply', hol)
                        log_event(conn, 'powerwall', 'holiday_active',
                                  f'Holiday: {hol} — weekend rules apply',
                                  result='ok', battery_pct=live.get('battery_pct'))
                        last_holiday_logged = dt.date()

                    state_sig = (target['mode'], target['reserve'], target['grid_charging'], target['grid_export'])
                    if state_sig != last_state_sig:
                        log.info(
                            'STATE  mode=%-16s reserve=%s  grid_charge=%-5s  grid_export=%s%s',
                            target['mode'],
                            f"{target['reserve']}%" if target['reserve'] is not None else 'none',
                            target['grid_charging'], target['grid_export'],
                            '  [HOLIDAY]' if hol else '',
                        )
                        last_state_sig = state_sig

                    if nxt != last_nxt:
                        if nxt:
                            log.info('Next rule fires at %s', nxt.strftime('%Y-%m-%d %H:%M'))
                        last_nxt = nxt

                    last_eval = now

                except Exception as exc:
                    log.exception('Evaluation error: %s: %s', type(exc).__name__, exc)

                if target is not None and now >= pw_retry_after:
                    actual = read_actual_state(pw, read_state)
                    if actual is None:
                        # Tesla unreadable — do not enforce against a state we
                        # cannot see.  The next cycle re-reads and reconciles.
                        if note_unreadable(conn, read_state, now):
                            log.warning('Powerwall unreadable for %d cycles — reconnecting',
                                        read_state['fails'])
                            pw = None
                    else:
                        note_readable(conn, read_state, now)
                        try:
                            apply_settings(pw, target, actual, apply_state,
                                           conn=conn, battery_pct=live.get('battery_pct'))
                        except Exception as exc:
                            # Only transport faults justify a reconnect.  Treating
                            # every error as one previously produced a reconnect
                            # loop that re-crashed in the same place each cycle.
                            log.exception('Powerwall apply error: %r', exc)
                            if _is_connection_error(exc):
                                pw = None
                                pw_retry_after = now + 60
            finally:
                conn.close()

        time.sleep(LOOP_SLEEP)


# ── Windows Service (optional) ────────────────────────────────────────────────
try:
    import win32event, win32service, win32serviceutil, servicemanager

    class PowerwallRulesService(win32serviceutil.ServiceFramework):
        _svc_name_         = 'PowerwallRules'
        _svc_display_name_ = 'Powerwall Rules Engine'
        _svc_description_  = 'SDG&E TOU-based Powerwall automation'

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
            main_loop(stop_fn=lambda: (
                win32event.WaitForSingleObject(self._stop, 0) == win32event.WAIT_OBJECT_0
            ))

    HAS_WIN32 = True

except ImportError:
    HAS_WIN32 = False


# ── Entry Point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    if len(sys.argv) > 1:
        if HAS_WIN32:
            win32serviceutil.HandleCommandLine(PowerwallRulesService)
        else:
            print('pywin32 not installed.  Run: pip install pywin32')
            sys.exit(1)
    else:
        main_loop()
