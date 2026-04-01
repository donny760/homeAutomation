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

import os, sys, time, logging, sqlite3, json
from datetime import datetime, date, timedelta

import pypowerwall
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

from fetch_rates import is_sdge_holiday, holiday_name, holiday_super_off_peak

# ── Config ────────────────────────────────────────────────────────────────────
PW_EMAIL      = 'don@nsdsolutions.com'
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
DB_PATH       = os.environ.get('DB_PATH', os.path.join(BASE_DIR, 'powerwall.db'))
LOG_PATH      = os.environ.get('LOG_PATH', os.path.join(BASE_DIR, 'rules.log'))
EVAL_INTERVAL = 60    # seconds between evaluations
LOOP_SLEEP    = 30    # main loop cadence in seconds

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-8s  %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.FileHandler(LOG_PATH),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger('rules')

# ── Default rules (v1 seed data) ─────────────────────────────────────────────
# days: JSON array of weekday ints (0=Mon … 6=Sun)
# months: JSON array of month ints (1–12)
DEFAULT_RULES = [
    {
        'name': 'Midnight – Time-Based Control',
        'days': [0,1,2,3,4,5,6], 'months': [1,2,3,4,5,6,7,8,9,10,11,12],
        'hour': 0, 'minute': 0,
        'mode': 'autonomous', 'reserve': None,
        'grid_charging': None, 'grid_export': None,
    },
    {
        'name': 'Weekday 6am – Self-Powered reserve 10%',
        'days': [0,1,2,3,4], 'months': [1,2,3,4,5,6,7,8,9,10,11,12],
        'hour': 6, 'minute': 0,
        'mode': 'self_consumption', 'reserve': 10,
        'grid_charging': None, 'grid_export': None,
    },
    {
        'name': 'Weekend 2pm – Self-Powered reserve 10%',
        'days': [5,6], 'months': [1,2,3,4,5,6,7,8,9,10,11,12],
        'hour': 14, 'minute': 0,
        'mode': 'self_consumption', 'reserve': 10,
        'grid_charging': None, 'grid_export': None,
    },
    {
        'name': 'Mar/Apr 10am – Time-Based Control (super off-peak starts)',
        'days': [0,1,2,3,4], 'months': [3,4],
        'hour': 10, 'minute': 0,
        'mode': 'autonomous', 'reserve': None,
        'grid_charging': None, 'grid_export': None,
    },
    {
        'name': 'Mar/Apr 2pm – Self-Powered (super off-peak ends)',
        'days': [0,1,2,3,4], 'months': [3,4],
        'hour': 14, 'minute': 0,
        'mode': 'self_consumption', 'reserve': None,
        'grid_charging': None, 'grid_export': None,
    },
    {
        'name': 'Non-Mar/Apr Mon–Sat 4am – Backup + grid charge ON',
        'days': [0,1,2,3,4,5], 'months': [1,2,5,6,7,8,9,10,11,12],
        'hour': 4, 'minute': 0,
        'mode': 'backup', 'reserve': None,
        'grid_charging': True, 'grid_export': None,
    },
    {
        'name': 'Non-Mar/Apr 5am – Time-Based Control, reserve 20%, grid charge OFF',
        'days': [0,1,2,3,4,5,6], 'months': [1,2,5,6,7,8,9,10,11,12],
        'hour': 5, 'minute': 0,
        'mode': 'autonomous', 'reserve': 20,
        'grid_charging': False, 'grid_export': None,
    },
    {
        'name': 'Summer Mon–Fri+Sun 7:15pm – reserve 1%, export solar+battery',
        'days': [0,1,2,3,4,6], 'months': [6,7,8,9,10,11],
        'hour': 19, 'minute': 15,
        'mode': None, 'reserve': 1,
        'grid_charging': None, 'grid_export': 'battery_ok',
    },
    {
        'name': 'Summer Mon–Fri+Sun 7:55pm – export solar only',
        'days': [0,1,2,3,4,6], 'months': [6,7,8,9,10,11],
        'hour': 19, 'minute': 55,
        'mode': None, 'reserve': None,
        'grid_charging': None, 'grid_export': 'pv_only',
    },
    {
        'name': 'Summer Sat 7pm – reserve 1%, export solar+battery',
        'days': [5], 'months': [6,7,8,9,10,11],
        'hour': 19, 'minute': 0,
        'mode': None, 'reserve': 1,
        'grid_charging': None, 'grid_export': 'battery_ok',
    },
    {
        'name': 'Summer Sat 9pm – export solar only',
        'days': [5], 'months': [6,7,8,9,10,11],
        'hour': 21, 'minute': 0,
        'mode': None, 'reserve': None,
        'grid_charging': None, 'grid_export': 'pv_only',
    },
]


# ── Database helpers ──────────────────────────────────────────────────────────
def init_db(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS readings (
            timestamp   INTEGER PRIMARY KEY,
            solar_w     REAL,
            home_w      REAL,
            battery_w   REAL,
            grid_w      REAL,
            battery_pct REAL
        );
        CREATE INDEX IF NOT EXISTS idx_ts ON readings(timestamp);

        CREATE TABLE IF NOT EXISTS rules (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            name          TEXT NOT NULL,
            enabled       INTEGER NOT NULL DEFAULT 1,
            days          TEXT NOT NULL,
            months        TEXT NOT NULL,
            hour          INTEGER NOT NULL,
            minute        INTEGER NOT NULL,
            mode          TEXT,
            reserve       INTEGER,
            grid_charging INTEGER,
            grid_export   TEXT
        );

        CREATE TABLE IF NOT EXISTS rule_conditions (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id   INTEGER NOT NULL REFERENCES rules(id) ON DELETE CASCADE,
            logic     TEXT NOT NULL DEFAULT 'AND',
            type      TEXT NOT NULL,
            operator  TEXT NOT NULL,
            value     REAL NOT NULL
        );

        CREATE TABLE IF NOT EXISTS event_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          INTEGER NOT NULL,
            system      TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            title       TEXT NOT NULL,
            detail      TEXT,
            result      TEXT,
            source      TEXT DEFAULT 'live',
            battery_pct REAL
        );
        CREATE INDEX IF NOT EXISTS idx_event_log_ts     ON event_log(ts);
        CREATE INDEX IF NOT EXISTS idx_event_log_system ON event_log(system);
    ''')
    conn.commit()


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


def seed_default_rules(conn):
    """Insert v1 rules if rules table is empty."""
    count = conn.execute('SELECT COUNT(*) FROM rules').fetchone()[0]
    if count > 0:
        return
    log.info('Seeding default rules into DB…')
    for r in DEFAULT_RULES:
        conn.execute(
            'INSERT INTO rules (name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export) '
            'VALUES (?,1,?,?,?,?,?,?,?,?)',
            (
                r['name'],
                json.dumps(r['days']),
                json.dumps(r['months']),
                r['hour'], r['minute'],
                r['mode'], r['reserve'],
                None if r['grid_charging'] is None else (1 if r['grid_charging'] else 0),
                r['grid_export'],
            )
        )
    conn.commit()
    log.info('Seeded %d default rules.', len(DEFAULT_RULES))


def load_rules_from_db(conn) -> list:
    """Return list of enabled rule dicts with parsed days/months and conditions list."""
    rows = conn.execute(
        'SELECT id,name,enabled,days,months,hour,minute,mode,reserve,grid_charging,grid_export '
        'FROM rules WHERE enabled=1'
    ).fetchall()

    cond_rows = conn.execute(
        'SELECT rule_id,logic,type,operator,value FROM rule_conditions'
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
    if cond['type'] == 'battery_pct':
        actual = live.get('battery_pct', 0)
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
    if d.weekday() not in rule['days']:
        return None
    if d.month not in rule['months']:
        return None
    return datetime(d.year, d.month, d.day, rule['hour'], rule['minute'])


def current_target_state(dt: datetime, rules: list, live: dict,
                         tou_periods: dict = None) -> dict:
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
        if not evaluate_conditions(rule['conditions'], live):
            continue
        for key in ('mode', 'reserve', 'grid_charging', 'grid_export'):
            if rule[key] is not None:
                state[key] = rule[key]

    # Holiday override: hold battery during super off-peak to preserve
    # charge for on-peak export.  Off-peak and on-peak follow normal rules.
    if is_sdge_holiday(dt.date()) and holiday_super_off_peak(dt.hour, tou_periods):
        state['reserve']    = 100
        state['grid_export'] = 'pv_only'
        state['_holiday']    = holiday_name(dt.date())

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


def get_live_state(pw) -> dict:
    try:
        return {'battery_pct': float(pw.level() or 0)}
    except Exception:
        return {'battery_pct': 0}


# ── Mode label for display ────────────────────────────────────────────────────
_MODE_LABEL = {
    'self_consumption': 'Self-Powered',
    'autonomous':       'Time-Based Control',
    'backup':           'Backup',
}


# ── Apply Settings ────────────────────────────────────────────────────────────
def apply_settings(pw, target: dict, last: dict,
                   conn=None, battery_pct=None, first_run=False) -> bool:
    """Apply target state to Powerwall. Logs one combined event row per call
    (skipped on first_run to avoid startup-sync noise)."""
    changes = []   # list of (label, event_type) for successful changes
    errors  = []   # list of label strings for failures

    if target['reserve'] is not None and target['reserve'] != last.get('reserve'):
        result = pw.set_reserve(target['reserve'])
        if result is not None:
            log.info('set_reserve(%d%%) → OK', target['reserve'])
            last['reserve'] = target['reserve']
            changes.append((f"Reserve → {target['reserve']}%", 'reserve_changed'))
        else:
            log.error('set_reserve(%d%%) failed', target['reserve'])
            errors.append(f"set_reserve({target['reserve']}%) failed")

    if target['mode'] is not None and target['mode'] != last.get('mode'):
        result = pw.set_mode(target['mode'])
        if result is not None:
            log.info('set_mode(%s) → OK', target['mode'])
            last['mode'] = target['mode']
            label = _MODE_LABEL.get(target['mode'], target['mode'])
            changes.append((f"Mode → {label}", 'mode_changed'))
        else:
            log.error('set_mode(%s) failed', target['mode'])
            errors.append(f"set_mode({target['mode']}) failed")

    if target['grid_charging'] is not None and target['grid_charging'] != last.get('grid_charging'):
        result = pw.set_grid_charging(target['grid_charging'])
        if result is not None:
            log.info('set_grid_charging(%s) → OK', target['grid_charging'])
            last['grid_charging'] = target['grid_charging']
            changes.append((f"Grid charging → {'ON' if target['grid_charging'] else 'OFF'}",
                            'grid_charging_changed'))
        else:
            log.error('set_grid_charging(%s) failed', target['grid_charging'])
            errors.append(f"set_grid_charging({target['grid_charging']}) failed")

    if target['grid_export'] is not None and target['grid_export'] != last.get('grid_export'):
        result = pw.set_grid_export(target['grid_export'])
        if result is not None:
            log.info('set_grid_export(%s) → OK', target['grid_export'])
            last['grid_export'] = target['grid_export']
            changes.append((f"Grid export → {target['grid_export']}", 'grid_export_changed'))
        else:
            log.error('set_grid_export(%s) failed', target['grid_export'])
            errors.append(f"set_grid_export({target['grid_export']}) failed")

    if conn and not first_run:
        if changes:
            title  = '  ·  '.join(label for label, _ in changes)
            etype  = changes[0][1] if len(changes) == 1 else 'automation_fired'
            log_event(conn, 'powerwall', etype, title,
                      result='ok', battery_pct=battery_pct)
        if errors:
            log_event(conn, 'powerwall', 'error',
                      '  ·  '.join(errors),
                      result='failed', battery_pct=battery_pct)

    return bool(changes or errors)


# ── Main Loop ─────────────────────────────────────────────────────────────────
def main_loop(stop_fn=None):
    os.chdir(BASE_DIR)
    log.info('Powerwall Rules Engine v2 starting.')

    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute('PRAGMA foreign_keys = ON')
    init_db(conn)
    seed_default_rules(conn)

    pw         = None
    last_eval  = 0.0
    last_state = {}
    first_run  = True
    last_holiday_logged = None   # date — log holiday override once per day

    while True:
        if stop_fn and stop_fn():
            log.info('Stop signal — exiting.')
            break

        now = time.time()

        if pw is None:
            try:
                log.info('Connecting to Powerwall (cloud mode)…')
                pw = pypowerwall.Powerwall('', cloudmode=True,
                                           email=PW_EMAIL, timeout=30,
                                           authpath=BASE_DIR)
                log.info('Connected.')
                last_state = {}
            except Exception as exc:
                log.error('Connection failed: %s — retry in %ds', exc, LOOP_SLEEP)
                time.sleep(LOOP_SLEEP)
                continue

        if now - last_eval >= EVAL_INTERVAL:
            try:
                rules = load_rules_from_db(conn)
                live  = get_live_state(pw)
                dt    = datetime.now()

                # Load configurable TOU periods from DB
                tou_row = conn.execute(
                    "SELECT value FROM settings WHERE key = 'tou_periods'"
                ).fetchone()
                tou_periods = None
                if tou_row:
                    try:
                        tou_periods = json.loads(tou_row[0])
                    except (json.JSONDecodeError, TypeError):
                        pass

                target = current_target_state(dt, rules, live, tou_periods)
                nxt    = next_rule_fire(dt, rules)

                # Log holiday override once per day
                hol = target.pop('_holiday', None)
                if hol and last_holiday_logged != dt.date():
                    log.info('Holiday override active: %s', hol)
                    log_event(conn, 'powerwall', 'holiday_override',
                              f'Holiday: {hol} — Reserve → 100%, '
                              f'Export → PV-only (super off-peak hold)',
                              result='ok', battery_pct=live.get('battery_pct'))
                    last_holiday_logged = dt.date()

                log.info(
                    'STATE  mode=%-16s reserve=%s  grid_charge=%-5s  grid_export=%s%s',
                    target['mode'],
                    f"{target['reserve']}%" if target['reserve'] is not None else 'none',
                    target['grid_charging'], target['grid_export'],
                    '  [HOLIDAY]' if hol else '',
                )
                if nxt:
                    log.info('Next rule fires at %s', nxt.strftime('%Y-%m-%d %H:%M'))

                changed = apply_settings(pw, target, last_state,
                                         conn=conn, battery_pct=live.get('battery_pct'),
                                         first_run=first_run)
                first_run = False
                if not changed:
                    log.info('No changes needed.')

                last_eval = now

            except Exception as exc:
                log.error('Evaluation error: %s', exc)
                pw = None

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
