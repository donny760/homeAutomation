'use client';

import { useState, useEffect, useRef } from 'react';
import { modeLabel, settingsBadges, DAYS_LBL } from '@/lib/format';
import { touPeriod } from '@/lib/tou';
import { mdToHtml } from '@/lib/markdown';

interface Rule {
  id: number;
  name: string;
  enabled: boolean;
  days: number[];
  months: number[];
  hour: number;
  minute: number;
  mode: string | null;
  reserve: number | null;
  grid_charging: boolean | null;
  grid_export: string | null;
  conditions?: Condition[];
}

interface Condition {
  logic: string;
  type: string;
  operator: string;
  value: number;
}

interface RateData {
  summer_on_peak?: number;
  winter_on_peak?: number;
  summer_off_peak?: number;
  winter_off_peak?: number;
  summer_super_off_peak?: number;
  winter_super_off_peak?: number;
  updated?: string;
  holidays?: string[];
  tou_periods?: any;
}

interface RulesProps {
  isActive: boolean;
}

function nextFireForRule(rule: Rule): string {
  const now = new Date();
  const days = new Set(rule.days);
  const months = new Set(rule.months);
  for (let delta = 0; delta <= 7; delta++) {
    const d = new Date(now);
    d.setDate(d.getDate() + delta);
    d.setHours(rule.hour, rule.minute, 0, 0);
    const weekday = (d.getDay() + 6) % 7;
    if (d > now && days.has(weekday) && months.has(d.getMonth() + 1)) {
      const day = DAYS_LBL[d.getDay()].slice(0, 3);
      const h = d.getHours(), m = d.getMinutes();
      const ampm = h >= 12 ? 'PM' : 'AM';
      const h12 = h % 12 || 12;
      return `${day} ${h12}:${String(m).padStart(2, '0')} ${ampm}`;
    }
  }
  return '\u2014';
}

// ── Thinking animation terms ──
const THINKING_TERMS = [
  'Rules', 'Rate Schedule', 'Usage Patterns', 'Solar Production',
  'Battery Cycles', 'Export Credits', 'Seasonal Trends', 'Holidays',
  'Cost Projections', 'TOU Periods', 'Grid Imports', 'True-Up Target',
  'Overnight Charging', 'On-Peak Windows', 'Self-Powered Mode', 'Sunset Timing',
  'Prior Year Data', 'Monthly Costs', 'Export Timing', 'Super Off-Peak Usage',
  'Battery Reserve', 'Net Energy Balance', 'Daily Cycle', 'Weather Patterns',
];

const SEVERITY_ICON: Record<string, string> = { warning: '\u26a0\ufe0f', suggestion: '\ud83d\udca1', info: '\u2139\ufe0f' };
const SEVERITY_CLASS: Record<string, string> = { warning: 'severity-warning', suggestion: 'severity-suggestion', info: 'severity-info' };

export default function Rules({ isActive }: RulesProps) {
  const [rules, setRules] = useState<Rule[]>([]);
  const [rates, setRates] = useState<RateData | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [editId, setEditId] = useState<number | null>(null);
  const [insightsOpen, setInsightsOpen] = useState(false);
  const [insightsHtml, setInsightsHtml] = useState('');
  const [insightsLoaded, setInsightsLoaded] = useState(false);
  const [insightsElapsed, setInsightsElapsed] = useState('');
  const thinkingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Form state
  const [fName, setFName] = useState('');
  const [fHour, setFHour] = useState(0);
  const [fMinute, setFMinute] = useState(0);
  const [fMode, setFMode] = useState('');
  const [fReserve, setFReserve] = useState('');
  const [fGridCharging, setFGridCharging] = useState('');
  const [fGridExport, setFGridExport] = useState('');
  const [fDays, setFDays] = useState<Set<number>>(new Set());
  const [fMonths, setFMonths] = useState<Set<number>>(new Set());
  const [fConditions, setFConditions] = useState<Condition[]>([]);

  useEffect(() => {
    if (isActive) {
      refreshRules();
      refreshRates(false);
    }
  }, [isActive]);

  async function refreshRules() {
    try {
      const data = await fetch('/api/rules').then((r) => r.json());
      setRules(data);
    } catch (e) {
      console.warn('Rules:', e);
    }
  }

  async function refreshRates(force: boolean) {
    try {
      if (force) await fetch('/api/rates/refresh', { method: 'POST' });
      const r = await fetch('/api/rates').then((x) => x.json());
      setRates(r);
    } catch (e) { /* leave dashes */ }
  }

  async function toggleRule(id: number, checked: boolean) {
    try {
      const res = await fetch(`/api/rules/${id}/toggle`, { method: 'PUT' });
      if (!res.ok) return;
      const data = await res.json();
      setRules((prev) => prev.map((r) => (r.id === id ? { ...r, enabled: data.enabled } : r)));
    } catch (e) {
      console.warn('Toggle:', e);
    }
  }

  async function deleteRule(id: number) {
    if (!confirm('Delete this rule?')) return;
    try {
      await fetch(`/api/rules/${id}`, { method: 'DELETE' });
      await refreshRules();
    } catch (e) {
      console.warn('Delete:', e);
    }
  }

  function openModal(id: number | null) {
    setEditId(id);
    if (id) {
      const rule = rules.find((r) => r.id === id);
      if (!rule) return;
      setFName(rule.name);
      setFHour(rule.hour);
      setFMinute(rule.minute);
      setFMode(rule.mode || '');
      setFReserve(rule.reserve != null ? String(rule.reserve) : '');
      setFGridCharging(rule.grid_charging == null ? '' : rule.grid_charging ? 'true' : 'false');
      setFGridExport(rule.grid_export || '');
      setFDays(new Set(rule.days));
      setFMonths(new Set(rule.months));
      setFConditions(rule.conditions || []);
    } else {
      setFName('');
      setFHour(0);
      setFMinute(0);
      setFMode('');
      setFReserve('');
      setFGridCharging('');
      setFGridExport('');
      setFDays(new Set());
      setFMonths(new Set());
      setFConditions([]);
    }
    setModalOpen(true);
  }

  async function saveRule() {
    if (!fName.trim()) {
      alert('Name is required.');
      return;
    }
    if (fDays.size === 0) {
      alert('Select at least one day.');
      return;
    }
    if (fMonths.size === 0) {
      alert('Select at least one month.');
      return;
    }

    const body = {
      name: fName.trim(),
      enabled: true,
      days: Array.from(fDays),
      months: Array.from(fMonths),
      hour: fHour,
      minute: fMinute,
      mode: fMode || null,
      reserve: fReserve !== '' ? parseInt(fReserve) : null,
      grid_charging: fGridCharging === '' ? null : fGridCharging === 'true',
      grid_export: fGridExport || null,
      conditions: fConditions,
    };

    try {
      const url = editId ? `/api/rules/${editId}` : '/api/rules';
      const method = editId ? 'PUT' : 'POST';
      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        alert('Save failed.');
        return;
      }
      setModalOpen(false);
      setInsightsLoaded(false);
      await refreshRules();
    } catch (e) {
      alert('Save error: ' + e);
    }
  }

  function toggleDay(d: number) {
    setFDays((prev) => {
      const next = new Set(prev);
      next.has(d) ? next.delete(d) : next.add(d);
      return next;
    });
  }

  function toggleMonth(m: number) {
    setFMonths((prev) => {
      const next = new Set(prev);
      next.has(m) ? next.delete(m) : next.add(m);
      return next;
    });
  }

  function addCondition() {
    setFConditions((prev) => [...prev, { logic: 'AND', type: 'battery_pct', operator: '>', value: 0 }]);
  }

  function updateCondition(idx: number, field: keyof Condition, val: string | number) {
    setFConditions((prev) => prev.map((c, i) => (i === idx ? { ...c, [field]: val } : c)));
  }

  function removeCondition(idx: number) {
    setFConditions((prev) => prev.filter((_, i) => i !== idx));
  }

  // ── Insights drawer ──
  async function handleToggleInsights() {
    const opening = !insightsOpen;
    setInsightsOpen(opening);

    if (opening && !insightsLoaded) {
      setInsightsHtml('<div class="ai-loading-pulse" id="ai-thinking">Analyzing with Rules\u2026</div>');
      setInsightsElapsed('');

      let idx = 0;
      thinkingRef.current = setInterval(() => {
        idx = (idx + 1) % THINKING_TERMS.length;
        const el = document.getElementById('ai-thinking');
        if (el) el.textContent = 'Analyzing with ' + THINKING_TERMS[idx] + '\u2026';
      }, 1800);

      const t0 = Date.now();
      try {
        const res = await fetch('/api/rules/ai-insights', { method: 'POST' });
        if (thinkingRef.current) clearInterval(thinkingRef.current);
        const elapsed = ((Date.now() - t0) / 1000).toFixed(1);
        const data = await res.json();

        if (data.ok) {
          const now = new Date();
          const timeStr = now.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
          const cached = data.cached ? ' (cached)' : '';
          setInsightsElapsed(elapsed + 's \u00b7 ' + timeStr + cached);
          const tableHtml = data.projection_table
            ? '<h3>True-up Projection</h3><div class="ai-content">' + mdToHtml(data.projection_table) + '</div><hr>'
            : '';
          const optHtml = data.optimized_table
            ? '<h3>Projected True-Up After Changes</h3><div class="ai-content">' + mdToHtml(data.optimized_table) + '</div>'
            : '';
          setInsightsHtml(tableHtml + '<div class="ai-content">' + mdToHtml(data.insights) + '</div>' + optHtml);
        } else {
          const fallback = await fetch('/api/rules/insights').then((r) => r.json());
          setInsightsHtml(
            '<div class="ai-error">' + (data.error || 'Gemini error') + '</div>' +
            renderFallback(fallback)
          );
        }
        setInsightsLoaded(true);
      } catch (e) {
        if (thinkingRef.current) clearInterval(thinkingRef.current);
        try {
          const fallback = await fetch('/api/rules/insights').then((r) => r.json());
          setInsightsHtml(
            '<div class="ai-error">Could not reach Gemini API.</div>' + renderFallback(fallback)
          );
        } catch (e2) {
          setInsightsHtml('<div class="ai-error">Failed to load insights.</div>');
        }
        setInsightsLoaded(true);
      }
    }
  }

  function renderFallback(insights: any[]): string {
    if (!insights.length) return '<div class="insights-empty">No insights \u2014 your rules look good!</div>';
    return '<div class="ai-fallback-note">Gemini unavailable \u2014 showing rule-based analysis</div>' +
      insights.map((i: any) => `
        <div class="insight-card ${SEVERITY_CLASS[i.severity] || ''}">
          <div class="insight-title">${SEVERITY_ICON[i.severity] || ''} ${i.title}</div>
          <div class="insight-detail">${i.detail}</div>
          <div class="insight-action">${i.action}</div>
        </div>
      `).join('');
  }

  // ── Rate card rendering ──
  const fmt3 = (v?: number) => (v != null ? '$' + Number(v).toFixed(3) : '\u2014');

  const now = new Date();
  const h = now.getHours();
  const mon = now.getMonth() + 1;
  const dow = now.getDay();
  const isSummer = [6, 7, 8, 9, 10].includes(mon);
  const isWeekend = dow === 0 || dow === 6;
  const todayISO = now.toISOString().slice(0, 10);
  const isHoliday = rates?.holidays?.includes(todayISO) || false;
  const season = isSummer ? 'summer' : 'winter';
  const period = rates ? touPeriod(h, mon, isSummer, isWeekend || isHoliday, rates.tou_periods) : 'off_peak';

  const periodLabels: Record<string, string> = { on_peak: 'ON-PEAK', off_peak: 'OFF-PEAK', super_off_peak: 'SUPER OFF-PEAK' };
  const periodAccents: Record<string, string> = { on_peak: 'var(--amber)', off_peak: 'var(--green)', super_off_peak: 'var(--blue)' };
  const accentColor = periodAccents[period];

  const rateUpdated = rates?.updated
    ? 'updated ' + new Date(rates.updated).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })
    : '';

  function rateCellClass(cellPeriod: string, cellSeason: string): string {
    const classes: string[] = [];
    if (cellSeason === season) {
      classes.push('rate-td-season-active', cellSeason);
    }
    if (cellSeason === season && cellPeriod === period) {
      classes.push('rate-cell-now', cellPeriod);
    }
    return classes.join(' ');
  }

  return (
    <>
      <div id="page-rules" className="page active">
        <div className="rules-toolbar">
          <div className="page-title">Powerwall Rules</div>
          <div className="rules-toolbar-btns">
            <button className="btn-insights" onClick={handleToggleInsights}>
              &#10022; Insights
            </button>
            <button className="btn-add" onClick={() => openModal(null)}>
              + Add Rule
            </button>
          </div>
        </div>

        {/* Rate card */}
        <div className="rate-card">
          <div className="rate-card-header">
            <span className="rate-card-title">SDG&amp;E EV-TOU-2</span>
            <span className="rate-card-updated">{rateUpdated}</span>
            <span className="rate-now-badge" style={{ color: accentColor }}>
              NOW: {periodLabels[period] || '\u2014'}
            </span>
          </div>
          <table className="rate-table">
            <thead>
              <tr>
                <th>Period</th>
                <th id="rate-th-summer" className={isSummer ? 'rate-season-active' : ''}>Summer</th>
                <th id="rate-th-winter" className={!isSummer ? 'rate-season-active' : ''}>Winter</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              <tr
                id="rate-row-on-peak"
                data-period="on_peak"
                className={period === 'on_peak' ? 'rate-active' : ''}
              >
                <td>On-peak</td>
                <td className={rateCellClass('on_peak', 'summer')}>{fmt3(rates?.summer_on_peak)}</td>
                <td className={rateCellClass('on_peak', 'winter')}>{fmt3(rates?.winter_on_peak)}</td>
                <td>4pm &ndash; 9pm daily</td>
              </tr>
              <tr
                id="rate-row-off-peak"
                data-period="off_peak"
                className={period === 'off_peak' ? 'rate-active' : ''}
              >
                <td>Off-peak</td>
                <td className={rateCellClass('off_peak', 'summer')}>{fmt3(rates?.summer_off_peak)}</td>
                <td className={rateCellClass('off_peak', 'winter')}>{fmt3(rates?.winter_off_peak)}</td>
                <td>6am &ndash; 4pm, 9pm &ndash; midnight</td>
              </tr>
              <tr
                id="rate-row-super-off-peak"
                data-period="super_off_peak"
                className={period === 'super_off_peak' ? 'rate-active' : ''}
              >
                <td>Super off-peak</td>
                <td className={rateCellClass('super_off_peak', 'summer')}>{fmt3(rates?.summer_super_off_peak)}</td>
                <td className={rateCellClass('super_off_peak', 'winter')}>{fmt3(rates?.winter_super_off_peak)}</td>
                <td>midnight &ndash; 6am + weekends</td>
              </tr>
            </tbody>
          </table>
          <div className="rate-card-footer">
            <button className="rate-refresh-link" onClick={() => refreshRates(true)}>
              Refresh rates
            </button>
          </div>
        </div>

        {/* Rules table */}
        <div className="rules-table-wrap">
          <table className="rules-table">
            <thead>
              <tr>
                <th>On</th>
                <th>Name</th>
                <th>Next Fire</th>
                <th>Actions</th>
                <th>Mode</th>
                <th>Reserve</th>
              </tr>
            </thead>
            <tbody>
              {rules.length === 0 ? (
                <tr>
                  <td colSpan={6} style={{ color: 'var(--very-dim)', padding: '20px' }}>
                    No rules defined.
                  </td>
                </tr>
              ) : (
                rules.map((r) => (
                  <tr key={r.id}>
                    <td>
                      <label className="toggle">
                        <input
                          type="checkbox"
                          checked={r.enabled}
                          onChange={(e) => toggleRule(r.id, e.target.checked)}
                        />
                        <span className="toggle-slider" />
                      </label>
                    </td>
                    <td>{r.name}</td>
                    <td className="next-fire">{nextFireForRule(r)}</td>
                    <td>
                      <div className="rule-actions">
                        <button className="btn-icon" onClick={() => openModal(r.id)}>Edit</button>
                        <button className="btn-icon btn-delete" onClick={() => deleteRule(r.id)}>Delete</button>
                      </div>
                    </td>
                    <td style={{ color: 'var(--dim)', fontSize: '0.82rem' }}>{modeLabel(r.mode) || '\u2014'}</td>
                    <td style={{ color: 'var(--dim)', fontSize: '0.82rem' }}>{r.reserve != null ? r.reserve + '%' : '\u2014'}</td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      </div>

      {/* Insights Drawer */}
      <div
        className={`insights-backdrop${insightsOpen ? ' open' : ''}`}
        onClick={() => setInsightsOpen(false)}
      />
      <div className={`insights-drawer${insightsOpen ? ' open' : ''}`}>
        <div className="insights-header">
          <span className="insights-title">
            &#10022; Insights{' '}
            <span className="insights-elapsed">{insightsElapsed}</span>
          </span>
          <button className="insights-close" onClick={() => setInsightsOpen(false)}>
            &times;
          </button>
        </div>
        <div className="insights-scroll">
          <div
            className="insights-body"
            dangerouslySetInnerHTML={{
              __html: insightsHtml || '<div class="ai-loading-pulse">Analyzing with Gemini\u2026</div>',
            }}
          />
        </div>
      </div>

      {/* Rule Edit Modal */}
      {modalOpen && (
        <div
          className="modal-backdrop open"
          onClick={(e) => { if (e.target === e.currentTarget) setModalOpen(false); }}
        >
          <div className="modal">
            <div className="modal-title">{editId ? 'Edit Rule' : 'Add Rule'}</div>

            <div className="form-grid">
              <div className="form-group full">
                <label className="form-label">Name</label>
                <input className="form-input" type="text" placeholder="Rule name" value={fName} onChange={(e) => setFName(e.target.value)} />
              </div>

              <div className="form-group full">
                <label className="form-label">Days</label>
                <div className="day-grid">
                  {['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].map((lbl, i) => (
                    <button
                      key={i}
                      type="button"
                      className={`day-btn${fDays.has(i) ? ' selected' : ''}`}
                      onClick={() => toggleDay(i)}
                    >
                      {lbl}
                    </button>
                  ))}
                </div>
              </div>

              <div className="form-group full">
                <label className="form-label">Months</label>
                <div className="month-grid">
                  {['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'].map((lbl, i) => (
                    <button
                      key={i + 1}
                      type="button"
                      className={`month-btn${fMonths.has(i + 1) ? ' selected' : ''}`}
                      onClick={() => toggleMonth(i + 1)}
                    >
                      {lbl}
                    </button>
                  ))}
                </div>
              </div>

              <div className="form-group">
                <label className="form-label">Hour (0&ndash;23)</label>
                <input className="form-input" type="number" min={0} max={23} value={fHour} onChange={(e) => setFHour(parseInt(e.target.value) || 0)} />
              </div>
              <div className="form-group">
                <label className="form-label">Minute</label>
                <select className="form-select" value={fMinute} onChange={(e) => setFMinute(parseInt(e.target.value))}>
                  <option value={0}>:00</option>
                  <option value={15}>:15</option>
                  <option value={30}>:30</option>
                  <option value={45}>:45</option>
                </select>
              </div>

              <div className="form-group">
                <label className="form-label">Mode</label>
                <select className="form-select" value={fMode} onChange={(e) => setFMode(e.target.value)}>
                  <option value="">(no change)</option>
                  <option value="self_consumption">Self-Powered</option>
                  <option value="autonomous">Time-Based Control</option>
                  <option value="backup">Backup</option>
                </select>
              </div>
              <div className="form-group">
                <label className="form-label">Reserve %</label>
                <input className="form-input" type="number" min={0} max={100} placeholder="(no change)" value={fReserve} onChange={(e) => setFReserve(e.target.value)} />
              </div>

              <div className="form-group">
                <label className="form-label">Grid Charging</label>
                <select className="form-select" value={fGridCharging} onChange={(e) => setFGridCharging(e.target.value)}>
                  <option value="">(no change)</option>
                  <option value="true">On</option>
                  <option value="false">Off</option>
                </select>
              </div>
              <div className="form-group">
                <label className="form-label">Grid Export</label>
                <select className="form-select" value={fGridExport} onChange={(e) => setFGridExport(e.target.value)}>
                  <option value="">(no change)</option>
                  <option value="battery_ok">Solar + Battery</option>
                  <option value="pv_only">Solar Only</option>
                </select>
              </div>

              <div className="form-group full">
                <label className="form-label">Conditions</label>
                <div className="cond-list">
                  {fConditions.map((c, idx) => (
                    <div key={idx} className="cond-row">
                      <select className="form-select" value={c.logic} onChange={(e) => updateCondition(idx, 'logic', e.target.value)}>
                        <option value="AND">AND</option>
                        <option value="OR">OR</option>
                      </select>
                      <select className="form-select" value={c.type} onChange={(e) => updateCondition(idx, 'type', e.target.value)}>
                        <option value="battery_pct">Battery %</option>
                      </select>
                      <select className="form-select" value={c.operator} onChange={(e) => updateCondition(idx, 'operator', e.target.value)}>
                        <option value="&gt;">&gt;</option>
                        <option value="&lt;">&lt;</option>
                        <option value="&gt;=">&gt;=</option>
                        <option value="&lt;=">&lt;=</option>
                      </select>
                      <input className="form-input" type="number" min={0} max={100} value={c.value} onChange={(e) => updateCondition(idx, 'value', parseFloat(e.target.value) || 0)} placeholder="0" />
                      <button type="button" className="btn-remove-cond" onClick={() => removeCondition(idx)}>
                        &times;
                      </button>
                    </div>
                  ))}
                </div>
                <button type="button" className="btn-add-cond" onClick={addCondition}>
                  + Add Condition
                </button>
              </div>
            </div>

            <div className="modal-footer">
              <button className="btn-cancel" onClick={() => setModalOpen(false)}>Cancel</button>
              <button className="btn-save" onClick={saveRule}>Save</button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
