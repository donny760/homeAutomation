import json
import math
import sqlite3
import time
from datetime import datetime, date, timedelta

import requests as _requests

from lib.state import DB_PATH, _live, _lock
from lib.settings import get_setting, get_setting_int, get_setting_bool, _load_tou_periods
from lib.fetch_rates import load_rates, tou_period, SDGE_HOLIDAYS, holiday_name, is_sdge_holiday
from lib.costs import _load_rate_history, _rate_for_date
from lib.rule_helpers import _load_all_rules, _fmt_hour, _analyze_rules

def _gemini_system_prompt(tou_periods=None):
    """Build the Gemini system prompt with TOU times derived from settings."""
    _tp = tou_periods or {}
    _wd = _tp.get('weekday', {})
    _wh = _tp.get('weekend_holiday', {})

    wd_sop = _wd.get('super_off_peak', [[0, 6], [10, 14]])
    wd_sop_overnight = [r for r in wd_sop if r[0] < 8] or [[0, 6]]
    wd_sop_daytime   = [r for r in wd_sop if r[0] >= 8] or [[10, 14]]
    wd_sop_night_end = _fmt_hour(max(e for _, e in wd_sop_overnight))
    wd_day_start     = _fmt_hour(min(s for s, _ in wd_sop_daytime))
    wd_day_end       = _fmt_hour(max(e for _, e in wd_sop_daytime))

    on_pk = _wd.get('on_peak', [[16, 21]])
    on_start = _fmt_hour(min(s for s, _ in on_pk))
    on_end   = _fmt_hour(max(e for _, e in on_pk))

    hol_sop = _wh.get('super_off_peak', [[0, 14]])
    hol_sop_end = _fmt_hour(max(e for _, e in hol_sop))

    hol_on = _wh.get('on_peak', [[16, 21]])
    hol_on_start = _fmt_hour(min(s for s, _ in hol_on))
    hol_on_end   = _fmt_hour(max(e for _, e in hol_on))

    return f"""\
You are an energy optimization advisor for a specific home in San Diego, CA.

## System
- 3× Tesla Powerwall 2 (40.5 kWh total usable capacity, ~90% round-trip efficiency)
- Rooftop solar — production varies seasonally (summer months have longer daylight
  and higher production than winter months)
- SDG&E EV-TOU-2 rate plan — use the EXACT rate values from the rates object in
  the provided data, never guess or use generic values
- Annual true-up in January
- IMPORTANT: SDG&E does NOT pay out excess true-up credits at a meaningful rate.
  The homeowner's goal is to land in a small credit range ($100–$500 credit).
  **Overproducing credits is wasted energy** — do not recommend maximizing exports.
  The strategy is a balance: capture enough on-peak credit to offset winter imports,
  while preserving battery for post-sunset self-consumption to avoid expensive
  grid imports during the remaining evening hours.
- Location: San Diego — mild winters, long sunny summers. Use actual rule times
  and rate period boundaries from the data; do not hardcode specific times.

## Data conventions — read carefully
- Battery (W): positive = charging, negative = discharging
- Grid (W): positive = importing from grid, negative = exporting to grid
- On-Peak Net / Off-Peak Net / Super Off-Peak Net (kWh): signed net values —
  negative = net export credit earned during that period
- **CRITICAL — projection Net column sign convention:**
  - POSITIVE Net = deficit (homeowner OWES SDG&E this amount)
  - NEGATIVE Net = credit (SDG&E OWES the homeowner this amount)
  - Example: Net = -$328.15 means a CREDIT of $328.15 (good outcome, within $100-$500 target)
  - Example: Net = +$328.15 means a DEFICIT of $328.15 (bad outcome, need more exports)
  - Never describe a negative number as a "deficit" — a negative Net is ALWAYS a credit
  - Always check the sign before labeling the outcome
- Rule-Based Findings: deterministic gaps already identified by a separate analysis
  engine — do NOT repeat these findings, go deeper or synthesize across them
- Rule names are DESCRIPTIVE, not authoritative. If a rule's name disagrees with
  its actual values, the values are what the system executes — but flag the
  disagreement as a likely bug for the user to review.

## Rate structure
Use the exact summer_on_peak, summer_off_peak, summer_super_off_peak, winter_on_peak,
winter_off_peak, winter_super_off_peak values from the rates object provided.

Key EV-TOU-2 nuances:
- On-peak ({on_start}–{on_end}) applies EVERY day including weekends and holidays — no exemptions
- Weekday super off-peak: midnight–{wd_sop_night_end} and {wd_day_start}–{wd_day_end} year-round
- Holidays follow weekend schedule: super off-peak midnight–{hol_sop_end}, on-peak {hol_on_start}–{hol_on_end}, off-peak fills the remaining hours
- Summer rates apply June–October; winter rates apply November–May

## How to read the rules
The rules array defines the automation schedule. Each rule fires at hour:minute on
the specified days (0=Mon..6=Sun) and months (1=Jan..12=Dec). Rules change only the
fields they specify — null fields carry forward from the previous rule.

Key fields:
- mode: controls how the Powerwall sources home power
  - autonomous (Time-Based Control): home draws ALL power from the grid; battery does NOT
    discharge to power the home; solar (if available) charges the battery instead of exporting
  - self_consumption: home draws from solar + battery first; grid is fallback; excess solar exports
  - backup: battery reserved for outages only
- reserve: battery floor % — personal preference, not strategy-driven
- grid_charging (true/false): explicit control to charge battery FROM grid; separate from mode;
  most useful overnight when no solar is available
- grid_export (battery_ok | pv_only): whether battery actively discharges to grid

## Rule intent validation
Some rules include an "Intent:" note written by the homeowner describing what the rule
is supposed to do. When a rule has an intent note:
1. Treat the note as an assertion to verify, not just context.
2. Check whether the rule's actual values (mode, grid_export, reserve, grid_charging,
   conditions, days, months) produce the behavior described in the note.
3. Check whether other rules that fire before or after this rule on the same day type
   (weekday / Saturday / Sunday / holiday) interfere with the stated intent.
4. If the rule does what the note says: confirm it briefly and move on.
5. If the rule does NOT do what the note says: flag the specific discrepancy clearly.
   Explain what the rule actually does vs. what the note claims, and identify which
   specific value or interaction is causing the gap.
6. If the note is ambiguous or partially correct: say so and explain what is and isn't accurate.
Do not treat intent notes as authoritative. They are the homeowner's best understanding,
which may be incomplete or incorrect.

IMPORTANT: grid_export = battery_ok PERMITS battery discharge to grid (up to ~15 kW
combined, 3× Powerwall; at 1% reserve nearly the full 40.5 kWh is available). However,
in TBC (autonomous) mode the Powerwall only actually exports during windows its internal
schedule marks as peak (4–9 PM daily). Outside that window, battery_ok is effectively
a no-op in TBC mode — the battery will not export even though export is permitted.
This is NOT passive solar overflow. If an intent note claims a rule exports to grid,
verify that the rule fires within 4–9 PM; if it fires outside that window in TBC mode,
flag it — the export will likely not occur as intended.

The rules are the homeowner's deliberate automation design. Your job is to understand
what they do by reading the rules array — not to assume what they "should" do. Read
rules in firing order (by hour/minute within each day-type) to understand daily behavior.

INFER the homeowner's strategy from the rules — do not impose a strategy. Common
patterns you may see:
- TBC (autonomous) during super off-peak: home draws ALL power from the grid at the cheapest
  rate; battery does not discharge. During the daytime window (10 AM–2 PM), solar charges the
  battery instead of exporting at the low super off-peak credit rate — a deliberate counter to
  SDG&E pricing that reduces export credit during peak solar hours. Overnight TBC (midnight–6 AM)
  has no solar; a short grid_charging: true window may be added to explicitly fill the battery
  from grid before solar arrives.
- Self-Powered during on-peak: draw from stored battery + solar to minimize grid imports at the
  most expensive rate.
- Active battery export (grid_export: battery_ok) during on-peak evening: discharge stored energy
  to grid for maximum credit. This is NOT passive solar overflow — it actively drains the battery.
- Battery idle during on-peak daytime: may be intentional if solar alone covers home load and
  the battery is being held for evening export or self-consumption.

These are all valid design choices. A battery sitting idle during on-peak hours is
NOT automatically a problem — it may be intentional passive-export mode or reserved
for later self-consumption. Verify the actual flow before suggesting otherwise.

## Prior year data — context for projections
The `Prior Year Monthly Summary` contains ACTUAL monthly performance from the previous
year. Real measured data from the same house, same solar panels, same location.

Prior year behavior reflects a DIFFERENT automation strategy (Tesla's Time-Based Control
algorithm). Current year uses custom rules — the behavior may differ. Compare thoughtfully,
but do not assume current-year patterns will match prior-year patterns for projected
months. Summer and winter behave differently; do not extrapolate from the most recent
month alone.

## Your analysis — cover all four areas:

**1. True-up trajectory**

The `trueup_projection_table` field contains a PRE-RENDERED markdown table.
These numbers were computed server-side with exact arithmetic.

The table is displayed separately in the UI — DO NOT reproduce it in your response.
DO NOT output a markdown table of the projection numbers.
Instead, reference the numbers directly in your analysis (e.g., "June shows -$234 credit").

Analyze:
- Report the full-year projected Net with correct sign interpretation (positive = deficit,
  negative = credit).
- **If Net is between -$500 and -$100 (projected credit in the $100-$500 target range),
  explicitly state "the projection is within target" and do NOT frame this as a problem
  requiring intervention.**
- If Net is outside target (overshoot >$500 credit, undershoot <$100 credit, or deficit),
  identify the main drivers.
- Which months drive the most credit? Which are the biggest costs?
- Flag real risks (data quality issues, unusual month patterns) — not hypothetical ones.

The table MUST appear before any rule change recommendations.

**2. Seasonal transition impact**
Based on the current season and when the next season starts:
- Walk through what happens on a typical day in the upcoming season based on the
  current rules — what mode is the system in at each key time of day? Describe this
  in plain English (e.g., "Around 4:00 AM the battery charges from the grid...").
  Do not quote rule names directly — describe what the system does, not which rule fires.
- How will the season shift affect solar production, electricity rates, and the
  opportunity to sell power back to the grid?
- What rule changes should be made BEFORE the transition?
- Address the battery export window timing given longer summer daylight hours.

**3. Rule review (not "optimization" by default)**
**ONLY discuss rules where you have identified an actual issue — name-vs-value
inconsistency, sequencing gap, or month coverage error. Do not narrate rules that
are functioning correctly. Silence on a rule means it is fine.**

**ONLY suggest rule changes if you can point to a specific inconsistency or gap that
the homeowner likely did not intend.** If the rules appear internally consistent and
the projection is within target, say so — do not invent optimizations for their own sake.

Focus on these checks:
- **Name-vs-value consistency.** The rule's quoted name describes intent; the resolved
  values after "|" show actual behavior. If a name disagrees with its values (e.g., name
  says "export solar only" but action says "active battery export enabled", or name
  mentions a specific time but the resolved time differs), flag it as a likely bug.
- **Rule sequencing.** Trace rule firings in time order per day-type (weekday / Saturday
  / Sunday). Flag cases where (a) a rule enables battery export and no later rule on
  the same day-type switches to solar-only export or changes mode away from active
  discharge before the next day starts, OR (b) two rules fire within 30 minutes and
  the later one contradicts the earlier (the earlier rule has no lasting effect).
- **Month coverage.** Check that each rule's months list actually contains the months
  the name implies. Do not invent month-coverage claims; verify them in the data.
- **Day-type asymmetry (optional observation).** If one day-type has a "stop export"
  rule but another doesn't, mention it as an observation — not a requirement — unless
  it's creating a measurable problem.

**When a Rule-Based Finding flags that battery export starts later than the on-peak
window opens:** Do NOT simply recommend moving it earlier. Instead, reason through
the full energy picture for that window:
- Is solar production still strong between the on-peak start and the current export
  start time? If so, the battery may be charging or at capacity — active export
  during that window would cut into solar charging, not idle capacity.
- After the export window closes, how much battery capacity remains? Would starting
  export earlier leave insufficient reserve for post-on-peak self-consumption,
  potentially causing grid imports at off-peak rates that offset the credit gains?
- Only recommend an earlier export start if you can demonstrate from the data that
  the battery is genuinely full AND idle during that window AND adequate reserve
  would remain for evening self-consumption. If you cannot demonstrate both
  conditions, note the timing as likely intentional and explain the probable rationale
  (e.g., "the later start preserves a full battery for solar charging earlier in the
  afternoon, then exports once solar tapers off").

For any change you suggest, describe the directional impact only (e.g., "captures
more on-peak credit", "reduces morning grid imports"). Do NOT estimate or invent
dollar figures — you do not have the granular hourly data needed to compute them
accurately. Alternative perspectives are welcome as observations, but not required.

**The rule_based_insights findings are already displayed to the user above your
response. Do not restate them.** Your job is to synthesize: do multiple findings
point to a pattern? Does a finding connect to something measurable in the daily
data or projection? A finding is only worth mentioning if you can add context
that the finding itself does not contain — otherwise, leave it out.

Concrete example of what NOT to do: "The Rule-Based Findings correctly identify
that export starts at 7 PM, missing the first three hours of on-peak..." — that
is pure repetition. Instead, connect to data: "On May 17 the battery was at 0%
at 7 AM, which is consistent with the short charging window flagged above."

**4. Daily data observations**
Looking at the daily cost data, comment ONLY if you notice:
- Days with markedly lower credits than expected given the weather/solar context
- Inconsistent patterns between day-types that may indicate a rule gap

Do not push "untapped opportunities" unless the overall projection is undershooting
the target credit range. Exporting more is only valuable if the projection is below
target; overshooting wastes energy at SDG&E's poor surplus payout rate.

## Data quality awareness
The `data_quality` object tells you how reliable each projection input is:
- `actual_months`: months with real measured data — treat these as ground truth
- `projected_months`: months estimated from prior year patterns — flag as projections
- `period_weights_source`: per-season, tells you if TOU period distributions are from
  'current_year' (measured), 'prior_year' (historical), or 'default' (hardcoded estimate).
  If 'default', explicitly note that the import/export rate mix is estimated, not measured.
- `optimized_export_source`: 'actual_months' means the optimized scenario uses real export
  data from months with active rules. 'cross_season_estimate' or 'capacity_estimate' means
  it's hypothetical — frame it as "potential" or "estimated" savings, not guaranteed.
- `prior_year_daily_costs`: false means no historical baseline exists — projections for
  future months are less reliable. Note this limitation clearly.

When data sources are estimated rather than measured, hedge your language accordingly.

## Format
Use markdown. Use at most ### for headings — never #### or deeper. Use the actual rate
values and cost figures from the data — no generic estimates.
Do not repeat findings already listed in rule_based_insights.

CRITICAL — Write for a homeowner, not an engineer:
- Use plain English terms like "solar production", "grid imports", "battery level",
  "on-peak credits" — NOT technical or code-like identifiers.
- Use 12-hour time: "5:00 PM" — never "hour: 17" or "19:15".
- For rule recommendations: explain WHY the change helps and the expected dollar impact.
  Do NOT walk the user through how to create or edit a rule — they know how.
  Use actual times from the current rules and rate periods; never invent times.
- Never output JSON, arrays, code blocks, underscore_identifiers, or field syntax in recommendations.
- Use dollar amounts to justify every recommendation.

Keep the total response focused — depth over breadth.

After all rule recommendations, end with:

**5. Projected impact (informational)**
Check `optimized_identical` in Data Quality Notes.

- If `true`: the optimized projection is identical to the baseline — all projected
  months already have export rules configured. **Skip this section entirely. Do not
  mention the optimized projection or the "After Changes" table.**
- If `false`: a pre-calculated "After Changes" projection is displayed in the UI
  alongside the baseline. These numbers are computed server-side — DO NOT reproduce
  them as a table. Analyze:
  - Does the optimized projection bring the baseline within the $100-$500 target?
  - If it overshoots into >$500 credit, suggest scaling back (fewer months, higher
    reserve). If it still falls short, note what additional changes might help.
  - State the difference between baseline total and optimized total.
"""


def _aggregate_monthly_power(c, year):
    """Aggregate solar_w and home_w from readings into monthly kWh."""
    result = {}
    for month in range(1, 13):
        start = int(datetime(year, month, 1).timestamp())
        end = int(datetime(year + (1 if month == 12 else 0),
                           (month % 12) + 1, 1).timestamp())
        row = c.execute(
            'SELECT COUNT(*), SUM(solar_w), SUM(home_w), '
            '       (MAX(timestamp) - MIN(timestamp)) / NULLIF(COUNT(*) - 1.0, 0) '
            'FROM readings WHERE timestamp >= ? AND timestamp < ? AND solar_w IS NOT NULL',
            (start, end)
        ).fetchone()
        count = row[0] or 0
        if count < 100:
            result[month] = {'solar_kwh': 0, 'home_kwh': 0}
            continue
        avg_interval_h = (row[3] or 300) / 3600.0
        result[month] = {
            'solar_kwh': round((row[1] or 0) * avg_interval_h / 1000, 1),
            'home_kwh': round((row[2] or 0) * avg_interval_h / 1000, 1),
        }
    return result


_PERIODS = ('on_peak', 'off_peak', 'super_off_peak')
_DEFAULT_WEIGHTS = {
    'winter': {
        'import': {'on_peak': 0.05, 'off_peak': 0.25, 'super_off_peak': 0.70},
        'export': {'on_peak': 0.30, 'off_peak': 0.50, 'super_off_peak': 0.20},
    },
    'summer': {
        'import': {'on_peak': 0.05, 'off_peak': 0.25, 'super_off_peak': 0.70},
        'export': {'on_peak': 0.55, 'off_peak': 0.40, 'super_off_peak': 0.05},
    },
}


def _compute_period_weights(c, year) -> dict:
    """Derive actual TOU period weights from daily_costs per-period data.

    Returns dict keyed by season ('winter'/'summer'), each containing
    'import' and 'export' sub-dicts with fractional weights per period.
    Falls back to _DEFAULT_WEIGHTS for seasons with insufficient data.
    """
    rows = c.execute(
        'SELECT date, on_peak_kwh, off_peak_kwh, super_off_peak_kwh '
        'FROM daily_costs WHERE date >= ? AND date < ?',
        (f'{year}-01-01', f'{year + 1}-01-01')
    ).fetchall()

    # Accumulate import/export kWh by season and period.
    # We split on kWh sign, not cost sign. This is intentional — weights are
    # multiplied by rates to get avg rate, so kWh gives the correct distribution.
    # Using cost would double-count rate differences between periods.
    buckets = {
        'winter': {'import': {p: 0.0 for p in _PERIODS}, 'export': {p: 0.0 for p in _PERIODS}},
        'summer': {'import': {p: 0.0 for p in _PERIODS}, 'export': {p: 0.0 for p in _PERIODS}},
    }
    for d, on_kwh, off_kwh, sop_kwh in rows:
        month = int(d[5:7])
        season = 'summer' if month in (6, 7, 8, 9, 10) else 'winter'
        for period, val in zip(_PERIODS, (on_kwh or 0, off_kwh or 0, sop_kwh or 0)):
            if val > 0:
                buckets[season]['import'][period] += val
            elif val < 0:
                buckets[season]['export'][period] += abs(val)

    # Normalize to fractions; fall back to defaults if no data
    result = {}
    for season in ('winter', 'summer'):
        result[season] = {}
        for direction in ('import', 'export'):
            totals = buckets[season][direction]
            total = sum(totals.values())
            if total > 0:
                result[season][direction] = {p: totals[p] / total for p in _PERIODS}
            else:
                result[season][direction] = dict(_DEFAULT_WEIGHTS[season][direction])
    return result


def _render_projection_table(projection):
    """Render a projection list as a markdown table."""
    lines = ['| Month | Label | Import kWh | Export kWh | Import Cost | Export Credit | Base Charge | Net |',
             '|---|---|---|---|---|---|---|---|']
    t_ikwh = t_ekwh = t_icost = t_ecred = t_base = t_net = 0
    for p in projection:
        lines.append(f'| {p["month"]} | {p["label"]} | {p["import_kwh"]:.1f} | {p["export_kwh"]:.1f} '
                     f'| ${p["import_cost"]:.2f} | ${p["export_credit"]:.2f} '
                     f'| ${p["base_charge"]:.2f} | ${p["net"]:.2f} |')
        t_ikwh += p['import_kwh']; t_ekwh += p['export_kwh']
        t_icost += p['import_cost']; t_ecred += p['export_credit']
        t_base += p['base_charge']; t_net += p['net']
    lines.append(f'| **Total** | | **{t_ikwh:.1f}** | **{t_ekwh:.1f}** '
                 f'| **${t_icost:.2f}** | **${t_ecred:.2f}** '
                 f'| **${t_base:.2f}** | **${t_net:.2f}** |')
    return '\n'.join(lines)


def _build_trueup_projection(c, rates, base_charge_per_day):
    """Pre-calculate baseline + optimized projection tables using solar-based approach."""
    import calendar
    now = datetime.now()
    this_year = now.year
    prior_year = this_year - 1
    CAPACITY = 40.5
    EFFICIENCY = 0.90

    # ── Gather data ──────────────────────────────────────────────────────────
    # Current year actuals from daily_costs
    cy_rows = c.execute(
        'SELECT substr(date,1,7) as m, SUM(import_kwh), SUM(export_kwh), '
        '       SUM(import_cost), SUM(export_credit), COUNT(date) '
        'FROM daily_costs WHERE date >= ? AND date < ? '
        'GROUP BY substr(date,1,7) ORDER BY 1',
        (f'{this_year}-01-01', f'{this_year + 1}-01-01')
    ).fetchall()
    cy_data = {}
    for row in cy_rows:
        cy_data[row[0]] = {
            'import_kwh': row[1] or 0, 'export_kwh': row[2] or 0,
            'import_cost': row[3] or 0, 'export_credit': row[4] or 0,
            'days': row[5],
        }

    # Prior year solar + home from readings (for context)
    py_power = _aggregate_monthly_power(c, prior_year)
    cy_power = _aggregate_monthly_power(c, this_year)

    # Prior year monthly import/export from daily_costs (for projection baseline)
    py_dc_rows = c.execute(
        'SELECT substr(date,1,7) as m, SUM(import_kwh), SUM(export_kwh) '
        'FROM daily_costs WHERE date >= ? AND date < ? '
        'GROUP BY substr(date,1,7) ORDER BY 1',
        (f'{prior_year}-01-01', f'{prior_year + 1}-01-01')
    ).fetchall()
    py_dc_data = {}
    for row in py_dc_rows:
        py_dc_data[f'{prior_year}-{row[0][5:7]}'] = {
            'import_kwh': row[1] or 0, 'export_kwh': row[2] or 0,
        }

    # Home consumption ratio — only use months where both CY and PY have readings data.
    # This prevents ratio explosion when CY is missing future months (e.g. Nov/Dec
    # haven't happened yet) while PY has full-year data.
    # Winter = Nov–May (SDG&E), summer = Jun–Oct.
    winter_months = {1, 2, 3, 4, 5, 11, 12}
    summer_months = {6, 7, 8, 9, 10}

    def _ratio_for_season(months):
        cy_tot = py_tot = 0.0
        for m in months:
            cy_h = cy_power.get(m, {}).get('home_kwh', 0)
            py_h = py_power.get(m, {}).get('home_kwh', 0)
            if cy_h > 0 and py_h > 0:
                cy_tot += cy_h
                py_tot += py_h
        return cy_tot / py_tot if py_tot > 0 else 1.0

    winter_home_ratio = _ratio_for_season(winter_months)
    raw_summer_ratio = _ratio_for_season(summer_months)
    summer_home_ratio = raw_summer_ratio if raw_summer_ratio != 1.0 else min(winter_home_ratio, 1.10)

    # Rate periods
    rate_periods = c.execute(
        'SELECT effective_date, end_date, '
        '       summer_on_peak, summer_off_peak, summer_super_off_peak, '
        '       winter_on_peak, winter_off_peak, winter_super_off_peak, '
        '       COALESCE(base_services_charge_per_day, 0) '
        'FROM rate_history ORDER BY effective_date'
    ).fetchall()

    # Data-derived TOU period weights (current year, with prior year fallback)
    cy_weights = _compute_period_weights(c, this_year)
    py_weights = _compute_period_weights(c, prior_year)
    # For each season: prefer current year if it has real data, else prior year
    period_weights = {}
    weights_source = {}  # track source per season for data_quality
    for season in ('winter', 'summer'):
        period_weights[season] = {}
        if cy_weights[season]['import'] != _DEFAULT_WEIGHTS[season]['import']:
            weights_source[season] = 'current_year'
        elif py_weights[season]['import'] != _DEFAULT_WEIGHTS[season]['import']:
            weights_source[season] = 'prior_year'
        else:
            weights_source[season] = 'default'
        for direction in ('import', 'export'):
            if cy_weights[season][direction] == _DEFAULT_WEIGHTS[season][direction]:
                period_weights[season][direction] = py_weights[season][direction]
            else:
                period_weights[season][direction] = cy_weights[season][direction]

    # ── Estimate grid charging + export from current rules ───────────────────
    # Read rules to determine: which months have grid charging? which have export?
    rules = c.execute(
        'SELECT months, hour, minute, grid_charging, grid_export, days '
        'FROM rules WHERE enabled = 1 ORDER BY hour, minute'
    ).fetchall()

    def _rule_charging_hours(month):
        """Estimate daily grid charging hours for a given month."""
        charge_start = charge_end = None
        for months_j, hour, minute, gc, ge, days_j in rules:
            months = json.loads(months_j) if isinstance(months_j, str) else months_j
            if month not in months:
                continue
            if gc == 1:  # grid_charging ON
                charge_start = hour + minute / 60.0
            elif gc == 0 and charge_start is not None:  # grid_charging OFF
                charge_end = hour + minute / 60.0
        if charge_start is not None and charge_end is not None and charge_end > charge_start:
            return charge_end - charge_start
        return 0

    # Cache TOU periods once — _rule_export_hours is called ~24× per
    # projection build, and the value never changes mid-request.
    _tou_periods_cache = _load_tou_periods() or {}

    def _rule_export_hours(month):
        """Check if any export rules exist for a given month and estimate the window.

        Returns >0 if any rule enables battery_ok for this month (used as boolean
        by callers). Does not weight by days-of-week — actual export kWh comes from
        daily_costs data which reflects real-world day coverage.
        """
        # Find earliest battery_ok start and latest pv_only end for this month
        earliest_start = None
        latest_end = None
        for months_j, hour, minute, gc, ge, days_j in rules:
            months = json.loads(months_j) if isinstance(months_j, str) else months_j
            if month not in months:
                continue
            t = hour + minute / 60.0
            if ge == 'battery_ok':
                if earliest_start is None or t < earliest_start:
                    earliest_start = t
            elif ge == 'pv_only' and earliest_start is not None:
                if latest_end is None or t > latest_end:
                    latest_end = t
        if earliest_start is not None and latest_end is None:
            # Fall back to on-peak end from TOU periods (cached above)
            _on_pk = _tou_periods_cache.get('weekday', {}).get('on_peak', [[16, 21]])
            latest_end = float(max(e for _, e in _on_pk))
        if earliest_start is not None and latest_end is not None and latest_end > earliest_start:
            return latest_end - earliest_start
        return 0

    # ── Build baseline projection ────────────────────────────────────────────
    has_prior_year_data = bool(py_dc_data)
    actual_months = []
    projected_months = []
    projection_basis = []
    baseline = []
    for month_num in range(1, 13):
        m_key = f'{this_year}-{month_num:02d}'
        days_in_month = calendar.monthrange(this_year, month_num)[1]
        is_summer = month_num in (6, 7, 8, 9, 10)
        # Look up base charge from rate_history for this month; fall back to passed-in value
        mid_date = f'{this_year}-{month_num:02d}-15'
        month_rates = _rate_for_date(rate_periods, mid_date)
        month_base_per_day = (month_rates or {}).get('base_services_charge_per_day', 0) or base_charge_per_day
        base_charge = round(month_base_per_day * days_in_month, 2)

        if m_key in cy_data:
            d = cy_data[m_key]
            # Use calendar days for complete past months; recorded days for current month
            is_current_month = (month_num == now.month and this_year == now.year)
            base_days = d['days'] if is_current_month else days_in_month
            baseline.append({
                'month': m_key, 'label': 'actual',
                'import_kwh': round(d['import_kwh'], 1),
                'export_kwh': round(d['export_kwh'], 1),
                'import_cost': round(d['import_cost'], 2),
                'export_credit': round(d['export_credit'], 2),
                'base_charge': round(month_base_per_day * base_days, 2),
                'net': round(d['import_cost'] - d['export_credit']
                             + month_base_per_day * base_days, 2),
            })
            actual_months.append(m_key)
        else:
            # Use prior year's actual import/export from daily_costs as the baseline
            # (captures real solar overflow behavior that monthly solar/home can't)
            py_key = f'{prior_year}-{month_num:02d}'
            py_dc = py_dc_data.get(py_key, {'import_kwh': 0, 'export_kwh': 0})

            # Scale imports: winter uses home_ratio (higher consumption + grid charging),
            # summer uses a modest ratio (solar covers most, grid charging similar)
            if is_summer:
                proj_imp_kwh = py_dc['import_kwh'] * summer_home_ratio
                proj_exp_kwh = py_dc['export_kwh']  # solar exports stay ~same
            else:
                proj_imp_kwh = py_dc['import_kwh'] * winter_home_ratio
                proj_exp_kwh = py_dc['export_kwh']  # unscaled — exports driven by solar + rules, not consumption

            # Apply current rates with data-derived TOU period weights
            r = month_rates or rates
            season = 'summer' if is_summer else 'winter'
            w = period_weights[season]

            avg_imp_rate = sum(r[f'{season}_{p}'] * w['import'][p] for p in _PERIODS)
            avg_exp_rate = sum(r[f'{season}_{p}'] * w['export'][p] for p in _PERIODS)

            proj_imp_cost = round(proj_imp_kwh * avg_imp_rate, 2)
            proj_exp_credit = round(proj_exp_kwh * avg_exp_rate, 2)
            net = round(proj_imp_cost - proj_exp_credit + base_charge, 2)

            baseline.append({
                'month': m_key, 'label': 'projected',
                'import_kwh': round(proj_imp_kwh, 1),
                'export_kwh': round(proj_exp_kwh, 1),
                'import_cost': proj_imp_cost,
                'export_credit': proj_exp_credit,
                'base_charge': base_charge,
                'net': net,
            })
            projected_months.append(m_key)
            projection_basis.append({
                'month': m_key,
                'basis': 'prior_year' if py_dc['import_kwh'] > 0 else 'no_data',
                'py_import_kwh': round(py_dc['import_kwh'], 1),
                'py_export_kwh': round(py_dc['export_kwh'], 1),
                'home_ratio': round(summer_home_ratio if is_summer else winter_home_ratio, 3),
                'weights_source': weights_source.get(season, 'default'),
            })

    # ── Compute actual daily export from months with export rules ──────────────
    # Query per-day on-peak net export for months that have export rules active
    export_months = [m for m in range(1, 13) if _rule_export_hours(m) > 0]
    avg_daily_export = {'winter': 0.0, 'summer': 0.0}
    if export_months:
        # Build date range filters for months with export rules
        winter_export_months = [m for m in export_months if m not in (6, 7, 8, 9, 10)]
        summer_export_months = [m for m in export_months if m in (6, 7, 8, 9, 10)]
        for season, months in [('winter', winter_export_months), ('summer', summer_export_months)]:
            if not months:
                continue
            like_clauses = ' OR '.join(f"date LIKE '{this_year}-{m:02d}-%'" for m in months)
            row = c.execute(
                f'SELECT SUM(CASE WHEN on_peak_kwh < 0 THEN ABS(on_peak_kwh) ELSE 0 END), '
                f'       COUNT(DISTINCT date) '
                f'FROM daily_costs WHERE ({like_clauses})'
            ).fetchone()
            total_export = row[0] or 0
            day_count = row[1] or 0
            if day_count > 0:
                avg_daily_export[season] = total_export / day_count

    # Determine optimized export data source
    if avg_daily_export['winter'] > 0 or avg_daily_export['summer'] > 0:
        optimized_export_source = 'actual_months'
    else:
        optimized_export_source = 'capacity_estimate'

    # ── Build optimized projection (add battery export to months without rules) ─
    optimized = []
    for bp in baseline:
        month_num = int(bp['month'][5:7])
        is_summer = month_num in (6, 7, 8, 9, 10)
        season = 'summer' if is_summer else 'winter'
        days_in_month = calendar.monthrange(this_year, month_num)[1]

        has_export = _rule_export_hours(month_num) > 0
        if bp['label'] == 'actual' or has_export:
            # Actual months or months that already have export rules — no change
            optimized.append(dict(bp))
        else:
            # Month with no export rules — estimate what adding export rules could yield
            mid_date = f'{this_year}-{month_num:02d}-15'
            r = _rate_for_date(rate_periods, mid_date) or rates
            w = period_weights[season]

            # Use actual average daily export if available; prior-year seasonal fallback otherwise
            daily_exp = avg_daily_export[season]
            if daily_exp <= 0:
                # No current-year data — use prior year's same-season avg daily export
                py_season_months = [m for m in range(1, 13)
                                    if (m in (6, 7, 8, 9, 10)) == (season == 'summer')]
                py_total = sum(py_dc_data.get(f'{prior_year}-{m:02d}', {}).get('export_kwh', 0)
                               for m in py_season_months)
                py_days = sum(calendar.monthrange(prior_year, m)[1] for m in py_season_months)
                if py_total > 0 and py_days > 0:
                    daily_exp = py_total / py_days
                    optimized_export_source = 'prior_year_seasonal'
                else:
                    daily_exp = CAPACITY * 0.50
                    optimized_export_source = 'capacity_estimate'

            add_export_kwh = daily_exp * days_in_month
            add_charge_kwh = add_export_kwh / EFFICIENCY
            # Use data-derived export weights for credit, import weights for charge cost
            avg_exp_rate = sum(r[f'{season}_{p}'] * w['export'][p] for p in _PERIODS)
            avg_imp_rate = sum(r[f'{season}_{p}'] * w['import'][p] for p in _PERIODS)
            credit_gain = add_export_kwh * avg_exp_rate
            charge_cost = add_charge_kwh * avg_imp_rate

            new_imp_kwh = bp['import_kwh'] + add_charge_kwh
            new_exp_kwh = bp['export_kwh'] + add_export_kwh
            new_imp_cost = round(bp['import_cost'] + charge_cost, 2)
            new_exp_credit = round(bp['export_credit'] + credit_gain, 2)
            new_net = round(new_imp_cost - new_exp_credit + bp['base_charge'], 2)

            optimized.append({
                'month': bp['month'], 'label': 'optimized',
                'import_kwh': round(new_imp_kwh, 1),
                'export_kwh': round(new_exp_kwh, 1),
                'import_cost': new_imp_cost,
                'export_credit': new_exp_credit,
                'base_charge': bp['base_charge'],
                'net': new_net,
            })

    baseline_md = _render_projection_table(baseline)
    optimized_md = _render_projection_table(optimized)

    meta = {
        'prior_year_daily_costs': has_prior_year_data,
        'period_weights_source': weights_source,
        'optimized_export_source': optimized_export_source,
        'actual_months': actual_months,
        'projected_months': projected_months,
        'projection_basis': projection_basis,
        'optimized_identical': (baseline_md == optimized_md),
    }
    return baseline, baseline_md, optimized, optimized_md, meta


def _build_prior_year_note(rules, prior_year, current_year):
    """Build a prior_year_note with the actual charging window from rules."""
    def _fmt_time(h, m):
        if h == 0 and m == 0:
            return 'midnight'
        period = 'AM' if h < 12 else 'PM'
        display_h = h if h <= 12 else h - 12
        if display_h == 0:
            display_h = 12
        return f'{display_h}:{m:02d} {period}' if m else f'{display_h} {period}'

    # Find earliest grid_charging ON and OFF from enabled rules
    gc_on = gc_off = None
    for r in rules:
        if not r.get('enabled'):
            continue
        gc = r.get('grid_charging')
        t = (r['hour'], r['minute'])
        if gc is True and (gc_on is None or t < gc_on):
            gc_on = t
        elif gc is False and (gc_off is None or t > gc_off):
            gc_off = t

    note = (f'{prior_year} used Time-Based Control (Tesla automatic algorithm). '
            f'Current {current_year} rules are custom')
    if gc_on is not None and gc_off is not None:
        window = f'{_fmt_time(*gc_on)}\u2013{_fmt_time(*gc_off)}'
        note += (f' \u2014 they deliberately import more during '
                 f'super off-peak (grid charging {window}) to store energy for on-peak export.')
    else:
        note += '.'
    note += (f' Q1 imports may be higher vs {prior_year} '
             f'but summer export credits should more than offset this.')
    return note


def _build_ai_context():
    """Gather all relevant data for the Gemini prompt."""
    now = datetime.now()
    today = now.date()
    rates = load_rates() or {}
    holidays = sorted(d.isoformat() for d in SDGE_HOLIDAYS if d >= today)

    with sqlite3.connect(DB_PATH) as c:
        rules = _load_all_rules(c)

        # Current year monthly summaries
        cy_monthly_rows = c.execute(
            'SELECT substr(date,1,7), SUM(import_kwh), SUM(export_kwh), '
            '       SUM(import_cost), SUM(export_credit), '
            '       SUM(on_peak_kwh), SUM(off_peak_kwh), SUM(super_off_peak_kwh) '
            'FROM daily_costs WHERE date >= ? AND date < ? '
            'GROUP BY substr(date,1,7) ORDER BY 1',
            (f'{now.year}-01-01', f'{now.year + 1}-01-01')
        ).fetchall()

        # Last 7 days of daily costs (for recent pattern analysis)
        d7 = (today - timedelta(days=7)).isoformat()
        cost_rows = c.execute(
            'SELECT date, import_kwh, export_kwh, import_cost, export_credit, '
            '       on_peak_kwh, off_peak_kwh, super_off_peak_kwh '
            'FROM daily_costs WHERE date >= ? ORDER BY date', (d7,)
        ).fetchall()

        # Prior year monthly summaries (2025) for seasonal baseline
        prior_year = now.year - 1
        py_rows = c.execute(
            'SELECT substr(date,1,7), SUM(import_kwh), SUM(export_kwh), '
            '       SUM(import_cost), SUM(export_credit), '
            '       SUM(on_peak_kwh), SUM(off_peak_kwh), SUM(super_off_peak_kwh) '
            'FROM daily_costs WHERE date >= ? AND date < ? '
            'GROUP BY substr(date,1,7) ORDER BY 1',
            (f'{prior_year}-01-01', f'{prior_year + 1}-01-01')
        ).fetchall()

        # Pre-calculated true-up projections (baseline + optimized)
        # Derive base_charge from rate_history first, then rates.json, then hardcoded fallback
        _rh = _load_rate_history()
        _today_rate = _rate_for_date(_rh, today.isoformat()) if _rh else None
        base_charge = float((_today_rate or {}).get('base_services_charge_per_day', 0)
                            or rates.get('base_services_charge_per_day', 0.79343))
        baseline, baseline_md, optimized, optimized_md, projection_meta = _build_trueup_projection(c, rates, base_charge)

        # Last 7 days of readings (sample every ~60 min)
        t7 = int((now - timedelta(days=7)).timestamp())
        reading_rows = c.execute(
            'SELECT timestamp, solar_w, home_w, battery_w, grid_w, battery_pct '
            'FROM readings WHERE timestamp >= ? ORDER BY timestamp', (t7,)
        ).fetchall()

    # Sample readings to ~3-hourly
    sampled = []
    last_ts = 0
    for row in reading_rows:
        if row[0] - last_ts >= 10800:
            sampled.append({
                'Time': datetime.fromtimestamp(row[0]).strftime('%Y-%m-%d %H:%M'),
                'Solar (W)': round(row[1] or 0),
                'Home Load (W)': round(row[2] or 0),
                'Battery (W)': round(row[3] or 0),
                'Grid (W)': round(row[4] or 0),
                'Battery Level (%)': round(row[5] or 0, 1),
            })
            last_ts = row[0]

    # Current year monthly summaries
    current_year_monthly = []
    for row in cy_monthly_rows:
        current_year_monthly.append({
            'Month': row[0],
            'Grid Import (kWh)': round(row[1] or 0, 1),
            'Grid Export (kWh)': round(row[2] or 0, 1),
            'Import Cost ($)': round(row[3] or 0, 2),
            'Export Credit ($)': round(row[4] or 0, 2),
            'On-Peak Net (kWh)': round(row[5] or 0, 1),
            'Off-Peak Net (kWh)': round(row[6] or 0, 1),
            'Super Off-Peak Net (kWh)': round(row[7] or 0, 1),
        })

    # Last 7 days of daily costs
    daily_costs_7d = []
    for row in cost_rows:
        daily_costs_7d.append({
            'Date': row[0],
            'Grid Import (kWh)': round(row[1] or 0, 2),
            'Grid Export (kWh)': round(row[2] or 0, 2),
            'Import Cost ($)': round(row[3] or 0, 2),
            'Export Credit ($)': round(row[4] or 0, 2),
            'On-Peak Net (kWh)': round(row[5] or 0, 2),
            'Off-Peak Net (kWh)': round(row[6] or 0, 2),
            'Super Off-Peak Net (kWh)': round(row[7] or 0, 2),
        })

    # Rules as natural-language strings (prevents JSON leakage in recommendations)
    _DAY_NAMES = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
    _MONTH_NAMES = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
    _MODE_LABELS = {
        'self_consumption': 'Self-Powered mode',
        'autonomous': 'Time-Based Control mode',
        'backup': 'Backup mode',
    }
    _EXPORT_LABELS = {
        'battery_ok': 'active battery export enabled',
        'pv_only': 'battery export disabled (solar-only export)',
    }
    _COND_LABELS = {
        'battery_pct':        'battery %',
        'net_cost':           'net cost today $',
        'net_cost_ytd':       'YTD net cost $',
        'tomorrow_solar_kwh': 'tomorrow solar forecast kWh',
    }

    def _fmt_days(days):
        if set(days) == {0, 1, 2, 3, 4, 5, 6}:
            return 'Every day'
        if set(days) == {0, 1, 2, 3, 4}:
            return 'Weekdays'
        if set(days) == {5, 6}:
            return 'Weekends'
        if set(days) == {0, 1, 2, 3, 4, 6}:
            return 'Mon-Fri and Sun'
        return ', '.join(_DAY_NAMES[d] for d in sorted(days))

    def _fmt_months(months):
        s = set(months)
        if s == set(range(1, 13)):
            return 'All year'
        if s == {6, 7, 8, 9, 10}:
            return 'June-October (summer)'
        if s == {1, 2, 3, 4, 5, 11, 12}:
            return 'November-May (winter)'
        # Check for contiguous range
        sorted_m = sorted(s)
        if sorted_m == list(range(sorted_m[0], sorted_m[-1] + 1)):
            return f'{_MONTH_NAMES[sorted_m[0]-1]}-{_MONTH_NAMES[sorted_m[-1]-1]}'
        return ', '.join(_MONTH_NAMES[m-1] for m in sorted_m)

    def _fmt_time(h, m):
        period = 'AM' if h < 12 else 'PM'
        display_h = h if h <= 12 else h - 12
        if display_h == 0:
            display_h = 12
        return f'{display_h}:{m:02d} {period}'

    rule_descriptions = []
    for r in rules:
        parts = [f'"{r["name"]}"']
        parts.append(f'{"ENABLED" if r["enabled"] else "DISABLED"}')
        parts.append(f'{_fmt_days(r["days"])}, {_fmt_months(r["months"])} at {_fmt_time(r["hour"], r["minute"])}')
        actions = []
        if r.get('mode'):
            actions.append(_MODE_LABELS.get(r['mode'], r['mode']))
        else:
            actions.append('mode unchanged')
        if r.get('reserve') is not None:
            actions.append(f'battery reserve {r["reserve"]}%')
        if r.get('grid_charging') is True:
            actions.append('grid charging ON')
        elif r.get('grid_charging') is False:
            actions.append('grid charging OFF')
        if r.get('grid_export'):
            actions.append(_EXPORT_LABELS.get(r['grid_export'], r['grid_export']))
        parts.append('→ ' + ', '.join(actions))
        conds = r.get('conditions', [])
        if conds:
            cond_parts = []
            for c in conds:
                label = _COND_LABELS.get(c['type'], c['type'])
                cond_parts.append(f'{c["logic"]} {label} {c["operator"]} {c["value"]}')
            # strip leading 'AND '/'OR ' from first condition
            first = cond_parts[0].split(' ', 1)[1] if cond_parts else ''
            rest = cond_parts[1:]
            parts.append('if ' + first + (' ' + ' '.join(rest) if rest else ''))
        if r.get('notes'):
            parts.append(f'Intent: {r["notes"]}')
        rule_descriptions.append(' | '.join(parts))

    # Prior year monthly summaries
    prior_year_monthly = []
    for row in py_rows:
        prior_year_monthly.append({
            'Month': row[0],
            'Grid Import (kWh)': round(row[1] or 0, 1),
            'Grid Export (kWh)': round(row[2] or 0, 1),
            'Import Cost ($)': round(row[3] or 0, 2),
            'Export Credit ($)': round(row[4] or 0, 2),
            'On-Peak Net (kWh)': round(row[5] or 0, 1),
            'Off-Peak Net (kWh)': round(row[6] or 0, 1),
            'Super Off-Peak Net (kWh)': round(row[7] or 0, 1),
        })

    # Rule-based insights for additional context
    rule_insights = _analyze_rules(rules, rates, SDGE_HOLIDAYS, _load_tou_periods())

    is_summer = now.month in (6, 7, 8, 9, 10)
    jan1_next = date(now.year + 1, 1, 1)
    days_until_trueup = (jan1_next - today).days

    with _lock:
        live_snapshot = dict(_live)

    return json.dumps({
        "Today's Date": today.isoformat(),
        'Current Season': 'summer' if is_summer else 'winter',
        'Next Season Change': 'June 1' if not is_summer else 'November 1',
        'Days Until True-Up': days_until_trueup,
        'Battery Capacity (kWh)': 40.5,
        'Powerwall Count': 3,
        'SDG&E Rates': {k: v for k, v in rates.items()},
        'Upcoming Holidays': holidays,
        'Rules': rule_descriptions,
        'Rule-Based Findings': [{'Title': i['title'], 'Action': i['action']} for i in rule_insights],
        'True-Up Projection Table': baseline_md,
        'Optimized Projection Table': None if projection_meta.get('optimized_identical') else optimized_md,
        'Prior Year Monthly Summary': prior_year_monthly,
        'Prior Year Note': _build_prior_year_note(rules, prior_year, now.year),
        'Current Year Monthly Summary': current_year_monthly,
        'Daily Costs (Last 7 Days)': daily_costs_7d,
        'Power Readings (Last 7 Days, 3-hourly samples)': sampled,
        'Current State': {
            'Battery Level (%)': round(live_snapshot.get('battery_pct', 0), 1),
            'Solar (W)': round(live_snapshot.get('solar_w', 0)),
            'Home Load (W)': round(live_snapshot.get('home_w', 0)),
            'Grid (W)': round(live_snapshot.get('grid_w', 0)),
            'Mode': _MODE_LABELS.get(live_snapshot.get('mode', ''), live_snapshot.get('mode', 'unknown')),
        },
        'Data Quality Notes': projection_meta,
    }, indent=None, default=str), baseline_md, optimized_md


_ai_cache = {'text': None, 'model': None, 'ts': 0, 'table': None}

# Cache never auto-expires. Invalidated only on:
#   - rule create/update/delete (sets ts=0)
#   - explicit refresh request (?refresh=1 query param)
#   - server restart (in-memory only)


class _ProviderError(Exception):
    """Raised by provider call helpers. Carries transient/permanent flag."""
    def __init__(self, message, status=None, transient=False):
        super().__init__(message)
        self.status = status
        self.transient = transient


def _extract_api_error(resp, max_len: int = 200) -> str:
    """Pull a human-readable error message out of a JSON API response.
    Falls back to a truncated body when JSON parsing fails."""
    try:
        return resp.json().get('error', {}).get('message', resp.text[:max_len])
    except Exception:
        return resp.text[:max_len]


def _call_gemini(system_prompt: str, user_msg: str, model: str, api_key: str) -> str:
    """Call Gemini once via google-genai SDK. Returns response text or raises _ProviderError."""
    try:
        from google import genai
        from google.genai import types as _gtypes
    except ImportError as exc:
        raise _ProviderError(f'google-genai not installed: {exc}', transient=False)
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=user_msg,
            config=_gtypes.GenerateContentConfig(
                system_instruction=system_prompt,
                temperature=0.2,
                max_output_tokens=65536,
                thinking_config=_gtypes.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = response.text or ''
    except Exception as exc:
        msg = str(exc)
        transient = any(k in msg.lower() for k in ('timeout', 'rate', '429', '500', '503'))
        raise _ProviderError(msg, transient=transient)
    if not text:
        raise _ProviderError('Empty response', status=200, transient=True)
    return text


def _call_azure_openai(system_prompt: str, user_msg: str,
                       endpoint: str, deployment: str, api_key: str,
                       api_version: str) -> str:
    """Call Azure OpenAI chat/completions once. Returns response text or raises _ProviderError."""
    endpoint = endpoint.rstrip('/')
    url = f'{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}'
    payload = {
        'messages': [
            {'role': 'system', 'content': system_prompt},
            {'role': 'user', 'content': user_msg},
        ],
        'temperature': 0.2,
        'max_tokens': 4000,
    }
    headers = {'api-key': api_key, 'Content-Type': 'application/json'}
    try:
        resp = _requests.post(url, json=payload, headers=headers, timeout=300)
    except _requests.exceptions.Timeout:
        raise _ProviderError('Timeout', status=504, transient=True)
    except _requests.exceptions.ConnectionError as exc:
        raise _ProviderError(f'Connection error: {exc}', status=None, transient=True)

    if resp.status_code >= 400:
        transient = resp.status_code == 429 or resp.status_code >= 500
        raise _ProviderError(_extract_api_error(resp), status=resp.status_code, transient=transient)

    data = resp.json()
    choices = data.get('choices', [])
    if not choices:
        raise _ProviderError('Empty response', status=200, transient=True)
    text = choices[0].get('message', {}).get('content', '')
    if not text:
        raise _ProviderError('Empty content', status=200, transient=True)
    return text


def _azure_configured() -> bool:
    return bool(
        get_setting('azure_openai_endpoint', '') and
        get_setting('azure_openai_api_key', '') and
        get_setting('azure_openai_deployment', '')
    )
