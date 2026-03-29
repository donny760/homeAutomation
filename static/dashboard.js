// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtW(w) {
  const a = Math.abs(w);
  if (a >= 1000) return (w / 1000).toFixed(1) + ' kW';
  return Math.round(w) + ' W';
}
function fmtWabs(w) { return fmtW(Math.abs(w)); }

// ── Nav / page switching ──────────────────────────────────────────────────────
let _activePage = 'dashboard';

function showPage(name, pushState = true) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-link').forEach(b => b.classList.remove('active'));
  document.getElementById('page-' + name).classList.add('active');
  document.querySelectorAll('.nav-link').forEach(b => {
    if (b.dataset.page === name) b.classList.add('active');
  });
  _activePage = name;
  if (pushState) history.pushState({ page: name }, '', '#' + name);
  if (name === 'rules') refreshRules();
  if (name === 'events') refreshEvents();
  if (name === 'costs') refreshCostsPage();
  if (name === 'settings') refreshSettings();
}

window.addEventListener('popstate', (e) => {
  const name = e.state?.page || location.hash.replace('#', '') || 'dashboard';
  showPage(name, false);
});

// Restore page from URL hash on initial load
(function() {
  const hash = location.hash.replace('#', '');
  if (hash && document.getElementById('page-' + hash)) {
    showPage(hash);
  } else {
    history.replaceState({ page: 'dashboard' }, '', '#dashboard');
  }
})();

// ── Clock ─────────────────────────────────────────────────────────────────────
const DAYS_LBL   = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
const MONTHS_LBL = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

function tickClock() {
  const n = new Date();
  const h = n.getHours(), m = n.getMinutes();
  const ampm = h >= 12 ? 'PM' : 'AM';
  const h12  = h % 12 || 12;
  const ts   = `${h12}:${String(m).padStart(2,'0')} ${ampm}`;
  const ds   = `${DAYS_LBL[n.getDay()]}, ${MONTHS_LBL[n.getMonth()]} ${n.getDate()}`;
  const clk  = document.getElementById('clock');
  const dstr = document.getElementById('date-str');
  if (clk)  clk.textContent  = ts;
  if (dstr) dstr.textContent = ds;
}
setInterval(tickClock, 1000);
tickClock();

// ── Theme toggle ──────────────────────────────────────────────────────────────
function toggleTheme() {
  const isLight = document.body.classList.toggle('light');
  localStorage.setItem('theme', isLight ? 'light' : 'dark');
  document.getElementById('theme-btn').textContent = isLight ? 'Dark' : 'Light';
}
(function applyTheme() {
  if (localStorage.getItem('theme') === 'light') {
    document.body.classList.add('light');
    const btn = document.getElementById('theme-btn');
    if (btn) btn.textContent = 'Dark';
  }
})();

// ── Chart ─────────────────────────────────────────────────────────────────────
const dayChart = new Chart(document.getElementById('day-chart').getContext('2d'), {
  type: 'line',
  data: {
    datasets: [
      { label: 'Solar', data: [], borderColor: '#EF9F27', backgroundColor: 'rgba(239,159,39,0.07)', fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2 },
      { label: 'Home',  data: [], borderColor: '#378ADD', backgroundColor: 'rgba(55,138,221,0.07)',  fill: true, tension: 0.35, pointRadius: 0, borderWidth: 2 },
    ]
  },
  options: {
    responsive: true, maintainAspectRatio: false, animation: false,
    scales: {
      x: {
        type: 'time', time: { unit: 'hour', displayFormats: { hour: 'h a' } },
        grid: { color: '#222226' }, ticks: { color: '#9e9c96', maxTicksLimit: 12, font: { size: 13 } }, border: { color: '#333336' },
      },
      y: {
        beginAtZero: true, grid: { color: '#222226' },
        ticks: { color: '#9e9c96', font: { size: 13 }, callback: v => v >= 1000 ? (v/1000).toFixed(1)+'k' : v },
        border: { color: '#333336' },
      }
    },
    plugins: {
      legend: { display: false },
      tooltip: {
        mode: 'index', intersect: false,
        backgroundColor: '#222224', borderColor: '#333336', borderWidth: 1,
        titleColor: '#9e9c96', bodyColor: '#eeece8',
        callbacks: { label: ctx => ` ${ctx.dataset.label}: ${fmtW(ctx.parsed.y)}` }
      }
    }
  }
});

async function refreshChart() {
  try {
    const rows = await fetch('/api/today').then(r => r.json());
    dayChart.data.datasets[0].data = rows.map(r => ({ x: r.ts * 1000, y: Math.max(0, r.solar_w) }));
    dayChart.data.datasets[1].data = rows.map(r => ({ x: r.ts * 1000, y: Math.max(0, r.home_w)  }));
    dayChart.update('none');
  } catch(e) { console.warn('Chart:', e); }
}

// ── Power Flow ────────────────────────────────────────────────────────────────
const THRESHOLD = 50;

function setFlow(id, active, watts) {
  const el = document.getElementById(id);
  if (!el) return;
  if (active && watts > THRESHOLD) {
    el.classList.add('active');
    const dur = Math.max(0.4, Math.min(2.5, 400 / Math.sqrt(watts + 100)));
    el.style.setProperty('--dur', dur + 's');
  } else {
    el.classList.remove('active');
  }
}

function updateDashboard(d) {
  const solarOn = d.solar_w   >  THRESHOLD;
  const battChg = d.battery_w >  THRESHOLD;
  const battDis = d.battery_w < -THRESHOLD;
  const gridIn  = d.grid_w    >  THRESHOLD;
  const gridOut = d.grid_w    < -THRESHOLD;

  setFlow('flow-solar-home',    solarOn, d.solar_w);
  setFlow('flow-solar-battery', battChg, d.battery_w);
  setFlow('flow-solar-grid',    gridOut, Math.abs(d.grid_w));
  setFlow('flow-battery-home',  battDis, Math.abs(d.battery_w));
  setFlow('flow-grid-home',     gridIn,  d.grid_w);
  setFlow('flow-battery-grid',  battDis && gridOut, Math.abs(d.battery_w));

  const pSolarW = document.getElementById('p-solar-w');
  if (pSolarW) pSolarW.textContent = fmtW(d.solar_w);

  const pSolarToday = document.getElementById('p-solar-today');
  if (pSolarToday) pSolarToday.textContent = (d.solar_kwh_today || 0).toFixed(1) + ' kWh today';

  const pBattW      = document.getElementById('p-batt-w');
  const pBattPct    = document.getElementById('p-batt-pct');
  const pBattStatus = document.getElementById('p-batt-status');
  if (pBattW)      pBattW.textContent      = fmtWabs(d.battery_w);
  if (pBattPct)    pBattPct.textContent    = Math.round(d.battery_pct || 0) + '%';
  if (pBattStatus) pBattStatus.textContent = d.battery_status ? '\u00b7 ' + d.battery_status : '';

  const pHomeW = document.getElementById('p-home-w');
  if (pHomeW) pHomeW.textContent = fmtW(d.home_w);

  const pGridW   = document.getElementById('p-grid-w');
  const pGridDir = document.getElementById('p-grid-dir');
  const pGridDot = document.getElementById('p-grid-dot');
  if (pGridW)   pGridW.textContent   = fmtWabs(d.grid_w);
  if (pGridDir) pGridDir.textContent = gridIn ? 'importing' : gridOut ? 'exporting' : '';
  if (pGridDot) {
    pGridDot.style.background = gridOut ? '#1D9E75' : '#888780';
    if (pGridW) pGridW.style.color = gridOut ? '#1D9E75' : '#888780';
  }

  const fillEl = document.getElementById('batt-icon-fill');
  if (fillEl) fillEl.setAttribute('width', Math.max(2, Math.round(22 * (d.battery_pct || 0) / 100)));

  const ringColor = (d.battery_pct || 0) > 30 ? '#1D9E75' : (d.battery_pct || 0) > 15 ? '#EF9F27' : '#e05252';
  const battRing = document.getElementById('batt-flow-ring');
  if (battRing) battRing.setAttribute('stroke', ringColor);

  // Grid today label (SVG text near grid node)
  const gkEl = document.getElementById('p-grid-today');
  if (gkEl && d.grid_kwh_today != null) {
    gkEl.textContent = d.grid_kwh_today.toFixed(1) + ' kWh today';
  }

  // Mode label — upper-left, color-coded by mode
  const pMode       = document.getElementById('p-mode');
  const pModeBg     = document.getElementById('p-mode-bg');
  const pModeAccent = document.getElementById('p-mode-accent');
  const pModeDot    = document.getElementById('p-mode-dot');
  if (pMode && d.mode) {
    const label = modeLabel(d.mode) || d.mode;
    const modeColors = {
      'self_consumption': '#1D9E75',
      'autonomous':       '#EF9F27',
      'backup':           '#e05252',
    };
    const col = modeColors[d.mode] || '#888780';
    pMode.textContent = label.toUpperCase();
    pMode.setAttribute('fill', col);
    if (pModeAccent) pModeAccent.setAttribute('fill', col);
    if (pModeDot)    pModeDot.setAttribute('fill', col);
    if (pModeBg) {
      pModeBg.setAttribute('stroke', col);
      pModeBg.style.opacity = '0.85';
      const w = Math.max(100, label.length * 6.8 + 42);
      pModeBg.setAttribute('width', w);
    }
  }
}

// ── Weather ───────────────────────────────────────────────────────────────────
async function refreshWeather() {
  try {
    const w = await fetch('/api/weather').then(r => r.json());
    const wt = document.getElementById('wx-temp');
    const wd = document.getElementById('wx-desc');
    const wf = document.getElementById('wx-fcst');
    if (wt && w.temp_f != null)  wt.textContent = w.temp_f + '\u00b0F';
    if (wd && w.desc)            wd.textContent = w.desc;
    if (wf && w.tomorrow_cloud != null) {
      const cl   = Math.round(w.tomorrow_cloud);
      const rain = w.tomorrow_rain != null ? `, ${w.tomorrow_rain.toFixed(1)}mm rain` : '';
      const warn = w.bad_forecast ? '  !' : '';
      wf.textContent = `Tomorrow: ${cl}% clouds${rain}${warn}`;
    }
  } catch(e) { console.warn('Weather:', e); }
}

// ── Pool ──────────────────────────────────────────────────────────────────────
async function refreshPool() {
  try {
    const d = await fetch('/api/pool').then(r => r.json());

    const tempEl     = document.getElementById('pool-temp');
    const pumpEl     = document.getElementById('pool-pump');
    const edgeEl     = document.getElementById('pool-edge-pump');
    const cleanerEl  = document.getElementById('pool-cleaner');
    const saltEl     = document.getElementById('pool-salt');
    if (!tempEl) return;

    // Temperature
    if (d.temp_f != null) {
      tempEl.textContent = d.temp_f + '\u00b0F';
      tempEl.classList.remove('tile-na');
    } else {
      tempEl.textContent = '--\u00b0F';
      tempEl.classList.add('tile-na');
    }

    // Pool pump
    if (pumpEl) {
      const watts = d.pump_watts != null ? ` \u00b7 ${d.pump_watts}W` : '';
      pumpEl.textContent = 'Pump  ' + (d.pump_on ? 'On' : 'Off') + watts;
      pumpEl.style.color = d.pump_on ? 'var(--green)' : 'var(--dim)';
      pumpEl.classList.remove('tile-na');
    }

    // Edge pump
    if (edgeEl) {
      edgeEl.textContent = 'Edge Pump  ' + (d.edge_pump_on ? 'On' : 'Off');
      edgeEl.style.color = d.edge_pump_on ? 'var(--green)' : 'var(--dim)';
      edgeEl.classList.remove('tile-na');
    }

    // Cleaner
    if (cleanerEl) {
      cleanerEl.textContent = 'Cleaner  ' + (d.cleaner_on ? 'On' : 'Off');
      cleanerEl.style.color = d.cleaner_on ? 'var(--green)' : 'var(--dim)';
      cleanerEl.classList.remove('tile-na');
    }

    // Salt / Chlorinator
    if (saltEl) {
      if (d.salt_ppm != null) {
        let txt = 'Salt  ' + d.salt_ppm.toLocaleString() + ' ppm';
        if (d.scg_active) txt += ' · ' + (d.scg_pool_pct ?? '?') + '%';
        if (d.super_chlor) txt += ' · Super';
        saltEl.textContent = txt;
        saltEl.style.color = d.scg_active ? 'var(--green)' : 'var(--dim)';
        saltEl.classList.remove('tile-na');
      } else {
        saltEl.textContent = '';
        saltEl.classList.add('tile-na');
      }
    }
  } catch(e) { console.warn('Pool:', e); }
}

// ── Security ──────────────────────────────────────────────────────────────────
async function refreshSecurity() {
  try {
    const d = await fetch('/api/security').then(r => r.json());
    const modeEl   = document.getElementById('sec-mode');
    const issuesEl = document.getElementById('sec-issues');
    if (!modeEl) return;

    if (!d.connected || d.mode == null) {
      modeEl.textContent = '--';
      modeEl.classList.add('tile-na');
      if (issuesEl) { issuesEl.textContent = 'Not connected'; issuesEl.classList.add('tile-na'); }
      return;
    }

    modeEl.textContent = d.mode_display || d.mode;
    modeEl.classList.remove('tile-na');
    const modeColors = {away: 'var(--amber)', home: 'var(--green)', standby: 'var(--dim)'};
    modeEl.style.color = modeColors[d.mode] || 'var(--dim)';

    if (issuesEl) {
      issuesEl.classList.remove('tile-na');
      if (!d.issues || d.issues.length === 0) {
        issuesEl.textContent = 'All secure';
        issuesEl.style.color = 'var(--green)';
      } else {
        const labels = d.issues.map(i => i.name + ' ' + i.type);
        issuesEl.innerHTML = labels.join('<br>');
        issuesEl.style.color = '#e05252';
      }
    }
  } catch(e) { console.warn('Security:', e); }
}


// ── Poll ──────────────────────────────────────────────────────────────────────
async function poll() {
  try {
    const d = await fetch('/api/live').then(r => r.json());
    updateDashboard(d);
  } catch(e) { console.warn('Poll:', e); }
}

// ── Upcoming Automations ──────────────────────────────────────────────────────
function fmtFireTime(iso) {
  const d = new Date(iso);
  const day  = DAYS_LBL[d.getDay()].slice(0,3);
  const h    = d.getHours(), m = d.getMinutes();
  const ampm = h >= 12 ? 'PM' : 'AM';
  const h12  = h % 12 || 12;
  return `${day} ${h12}:${String(m).padStart(2,'0')} ${ampm}`;
}

const _MODE_LABELS = {
  'self_consumption': 'Self-Powered',
  'autonomous':       'Time-Based Control',
  'backup':           'Backup',
};
function modeLabel(m) { return m ? (_MODE_LABELS[m] || m.replace(/_/g,' ')) : null; }

function settingsBadges(e) {
  const badges = [];
  if (e.mode)              badges.push({ text: modeLabel(e.mode), cls: 'amber' });
  if (e.reserve != null)   badges.push({ text: `reserve ${e.reserve}%`, cls: '' });
  if (e.grid_charging != null) badges.push({ text: `grid charge ${e.grid_charging ? 'ON' : 'OFF'}`, cls: e.grid_charging ? 'green' : '' });
  if (e.grid_export)       badges.push({ text: e.grid_export === 'battery_ok' ? 'export solar+batt' : 'export solar only', cls: 'blue' });
  return badges;
}

// ── Energy YTD tile ───────────────────────────────────────────────────────────
async function refreshCosts() {
  try {
    const d = await fetch('/api/costs/ytd').then(r => r.json());
    const fmt = v => '$' + v.toFixed(2);
    const importEl  = document.getElementById('cost-import');
    const creditEl  = document.getElementById('cost-credit');
    const netEl     = document.getElementById('cost-net');
    if (!importEl) return;
    importEl.textContent  = fmt(d.import_cost);
    importEl.classList.remove('tile-na');
    creditEl.textContent  = '\u2212' + fmt(d.export_credit);
    creditEl.style.color  = 'var(--green)';
    creditEl.classList.remove('tile-na');
    netEl.textContent     = fmt(d.net_cost);
    netEl.classList.remove('tile-na');
  } catch (e) { /* leave dashes */ }
}


// ── TOU period classification (mirrors fetch_rates.py tou_period) ────────────
function _inRanges(h, ranges) {
  return ranges && ranges.some(([s, e]) => h >= s && h < e);
}

function _touPeriod(h, mon, isSummer, isWeekendOrHoliday, touCfg) {
  const defaultCfg = {
    weekday:         { on_peak: [[16,21]], super_off_peak: [[0,6]], super_off_peak_winter_mar_apr: [[10,14]] },
    weekend_holiday: { on_peak: [[16,21]], super_off_peak: [[0,14]] },
  };
  const p = touCfg || defaultCfg;
  const dayType = isWeekendOrHoliday ? 'weekend_holiday' : 'weekday';
  const rules = p[dayType] || {};

  if (_inRanges(h, rules.on_peak))        return 'on_peak';
  if (_inRanges(h, rules.super_off_peak)) return 'super_off_peak';
  if (dayType === 'weekday' && !isSummer && [3,4].includes(mon)
      && _inRanges(h, rules.super_off_peak_winter_mar_apr)) return 'super_off_peak';
  return 'off_peak';
}


// ── Rate card + Current Rate tile ─────────────────────────────────────────────
async function refreshRates(force = false) {
  try {
    if (force) await fetch('/api/rates/refresh', {method: 'POST'});
    const r = await fetch('/api/rates').then(x => x.json());
    const fmt3 = v => v != null ? '$' + Number(v).toFixed(3) : '\u2014';
    const fmt2 = v => v != null ? '$' + Number(v).toFixed(3) : '\u2014';

    // ── Populate rate card cells ──
    document.getElementById('rate-sum-on').textContent  = fmt3(r.summer_on_peak);
    document.getElementById('rate-win-on').textContent  = fmt3(r.winter_on_peak);
    document.getElementById('rate-sum-off').textContent = fmt3(r.summer_off_peak);
    document.getElementById('rate-win-off').textContent = fmt3(r.winter_off_peak);
    document.getElementById('rate-sum-sup').textContent = fmt3(r.summer_super_off_peak);
    document.getElementById('rate-win-sup').textContent = fmt3(r.winter_super_off_peak);

    if (r.updated) {
      const upd = new Date(r.updated);
      document.getElementById('rate-updated').textContent =
        'updated ' + upd.toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric'});
    }

    // ── Compute current season + period ──
    const now       = new Date();
    const h         = now.getHours();
    const mon       = now.getMonth() + 1;
    const dow       = now.getDay();
    const isSummer  = [6,7,8,9,10].includes(mon);
    const isWeekend = dow === 0 || dow === 6;
    const todayISO  = now.toISOString().slice(0, 10);
    const isHoliday = r.holidays && r.holidays.includes(todayISO);
    const season    = isSummer ? 'summer' : 'winter';
    const period    = _touPeriod(h, mon, isSummer, isWeekend || isHoliday, r.tou_periods);

    const periodLabels  = {on_peak: 'ON-PEAK', off_peak: 'OFF-PEAK', super_off_peak: 'SUPER OFF-PEAK'};
    const periodAccents = {on_peak: 'var(--amber)', off_peak: 'var(--green)', super_off_peak: 'var(--blue)'};
    const rateRowIds    = {on_peak: 'rate-row-on-peak', off_peak: 'rate-row-off-peak', super_off_peak: 'rate-row-super-off-peak'};
    const accentColor   = periodAccents[period];

    // ── Rate card: active period row ──
    document.querySelectorAll('.rate-table tr.rate-active').forEach(tr => tr.classList.remove('rate-active'));
    const activeRow = document.getElementById(rateRowIds[period]);
    if (activeRow) activeRow.classList.add('rate-active');

    // ── Rate card: Now badge (color matches period) ──
    const badge = document.getElementById('rate-now-badge');
    if (badge) { badge.textContent = 'NOW: ' + periodLabels[period]; badge.style.color = accentColor; }

    // ── Rate card: season column headers ──
    const thSum = document.getElementById('rate-th-summer');
    const thWin = document.getElementById('rate-th-winter');
    if (thSum) { thSum.classList.toggle('rate-season-active', isSummer); }
    if (thWin) { thWin.classList.toggle('rate-season-active', !isSummer); }

    // ── Rate card: active season column cells — color + class ──
    const sumCellIds = ['rate-sum-on', 'rate-sum-off', 'rate-sum-sup'];
    const winCellIds = ['rate-win-on', 'rate-win-off', 'rate-win-sup'];
    [...sumCellIds, ...winCellIds].forEach(id => {
      const el = document.getElementById(id);
      if (!el) return;
      el.classList.remove('rate-td-season-active', 'summer', 'winter',
                          'rate-cell-now', 'on_peak', 'off_peak', 'super_off_peak');
    });
    const activeCols = isSummer ? sumCellIds : winCellIds;
    activeCols.forEach(id => {
      const el = document.getElementById(id);
      if (el) { el.classList.add('rate-td-season-active', season); }
    });

    // ── Rate card: highlight intersection cell ──
    const colPrefix  = isSummer ? 'rate-sum-' : 'rate-win-';
    const cellSuffix = {on_peak: 'on', off_peak: 'off', super_off_peak: 'sup'}[period];
    const nowCell    = document.getElementById(colPrefix + cellSuffix);
    if (nowCell) nowCell.classList.add('rate-cell-now', period);

    // ── Current Rate tile ──
    const currentRate = r[`${season}_${period}`];
    const tileAmount  = document.getElementById('tile-rate-amount');
    const tilePeriod  = document.getElementById('tile-rate-period');
    const tileSeason  = document.getElementById('tile-rate-season');
    if (tileAmount) {
      tileAmount.textContent = currentRate != null ? '$' + Number(currentRate).toFixed(3) : '\u2014';
      tileAmount.classList.remove('tile-na');
      tileAmount.style.color = accentColor;
    }
    if (tilePeriod) { tilePeriod.textContent = periodLabels[period]; tilePeriod.style.color = accentColor; }
    if (tileSeason) {
      tileSeason.textContent = isSummer ? 'SUMMER' : 'WINTER';
      tileSeason.className   = 'rt-season-badge ' + season;
    }

    // Mini rate table in tile
    const rtRows = {on_peak: 'rt-row-on-peak', off_peak: 'rt-row-off-peak', super_off_peak: 'rt-row-super'};
    document.querySelectorAll('.rt-mini tr.rt-active').forEach(tr => tr.classList.remove('rt-active'));
    const rtActive = document.getElementById(rtRows[period]);
    if (rtActive) rtActive.classList.add('rt-active');

    const onEl  = document.getElementById('rt-on-rates');
    const offEl = document.getElementById('rt-off-rates');
    const supEl = document.getElementById('rt-sup-rates');
    if (onEl)  { onEl.textContent  = fmt2(r.summer_on_peak)  + ' / ' + fmt2(r.winter_on_peak);  onEl.classList.remove('tile-na'); }
    if (offEl) { offEl.textContent = fmt2(r.summer_off_peak) + ' / ' + fmt2(r.winter_off_peak); offEl.classList.remove('tile-na'); }
    if (supEl) { supEl.textContent = fmt2(r.summer_super_off_peak) + ' / ' + fmt2(r.winter_super_off_peak); supEl.classList.remove('tile-na'); }

  } catch (e) { /* leave dashes */ }
}


async function refreshAutomations() {
  try {
    const data = await fetch('/api/schedule').then(r => r.json());
    const list = document.getElementById('auto-list');
    if (!list) return;

    const items = (data.schedule || []).slice(0, 5);
    if (!items.length) {
      list.innerHTML = '<div class="auto-empty">No upcoming automations.</div>';
    } else {
      list.innerHTML = items.map((e, i) => {
        const isRachio = e.source === 'rachio';
        const isSkip   = !!e.skip;
        const nextUp   = i === 0 && !isSkip ? 'next-up' : '';
        const racchioCls = isRachio ? 'rachio' : '';
        const skipCls  = isSkip ? 'rain-delay' : '';
        const sourceLabel = isRachio ? 'Rachio/Sprinklers' : 'Powerwall';

        let badges = '';
        if (isSkip) {
          badges = `<span class="sched-badge skip">Rain Delay</span>`;
        } else if (isRachio) {
          if (e.duration_min) {
            badges = `<span class="sched-badge rachio">${e.duration_min} min</span>`;
          }
        } else {
          badges = settingsBadges(e)
            .map(b => `<span class="sched-badge ${b.cls}">${b.text}</span>`).join('');
        }

        return `
          <div class="auto-row ${nextUp} ${racchioCls} ${skipCls}">
            <div style="display:flex;align-items:baseline;gap:8px">
              <div class="auto-time">${fmtFireTime(e.fire_time)}</div>
              <div class="auto-source">${sourceLabel}</div>
            </div>
            <div class="auto-name">${e.name}</div>
            ${badges ? `<div class="auto-badges">${badges}</div>` : ''}
          </div>`;
      }).join('');
    }

    const upd = document.getElementById('auto-updated');
    if (upd) upd.textContent = new Date().toLocaleTimeString([], {hour:'numeric',minute:'2-digit'});
  } catch(e) { console.warn('Automations:', e); }
}

// ── Rules page ────────────────────────────────────────────────────────────────
let _rules = [];

function nextFireForRule(rule) {
  const now  = new Date();
  const days   = new Set(rule.days);
  const months = new Set(rule.months);
  for (let delta = 0; delta <= 7; delta++) {
    const d = new Date(now);
    d.setDate(d.getDate() + delta);
    d.setHours(rule.hour, rule.minute, 0, 0);
    const weekday = (d.getDay() + 6) % 7; // JS Sun=0 -> Mon=0
    if (d > now && days.has(weekday) && months.has(d.getMonth() + 1)) {
      return fmtFireTime(d.toISOString());
    }
  }
  return '\u2014';
}

async function refreshRules() {
  try {
    _rules = await fetch('/api/rules').then(r => r.json());
    renderRulesTable();
  } catch(e) { console.warn('Rules:', e); }
}

function renderRulesTable() {
  const tbody = document.getElementById('rules-tbody');
  if (!tbody) return;
  if (!_rules.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="color:var(--very-dim);padding:20px">No rules defined.</td></tr>';
    return;
  }
  tbody.innerHTML = _rules.map(r => `
    <tr>
      <td>
        <label class="toggle">
          <input type="checkbox" ${r.enabled ? 'checked' : ''} onchange="toggleRule(${r.id}, this)">
          <span class="toggle-slider"></span>
        </label>
      </td>
      <td>${r.name}</td>
      <td class="next-fire">${nextFireForRule(r)}</td>
      <td>
        <div class="rule-actions">
          <button class="btn-icon" onclick="openModal(${r.id})">Edit</button>
          <button class="btn-icon btn-delete" onclick="deleteRule(${r.id})">Delete</button>
        </div>
      </td>
      <td style="color:var(--dim);font-size:0.82rem">${modeLabel(r.mode) || '\u2014'}</td>
      <td style="color:var(--dim);font-size:0.82rem">${r.reserve != null ? r.reserve + '%' : '\u2014'}</td>
    </tr>
  `).join('');
}

async function toggleRule(id, checkbox) {
  try {
    const res = await fetch(`/api/rules/${id}/toggle`, { method: 'PUT' });
    if (!res.ok) { checkbox.checked = !checkbox.checked; return; }
    const data = await res.json();
    checkbox.checked = data.enabled;
    const rule = _rules.find(r => r.id === id);
    if (rule) rule.enabled = data.enabled;
  } catch(e) { checkbox.checked = !checkbox.checked; }
}

async function deleteRule(id) {
  if (!confirm('Delete this rule?')) return;
  try {
    await fetch(`/api/rules/${id}`, { method: 'DELETE' });
    await refreshRules();
  } catch(e) { console.warn('Delete:', e); }
}

// ── Modal ─────────────────────────────────────────────────────────────────────
let _condCount = 0;

function openModal(id) {
  _condCount = 0;
  document.getElementById('cond-list').innerHTML = '';
  document.getElementById('modal-title').textContent = id ? 'Edit Rule' : 'Add Rule';
  document.getElementById('edit-id').value = id || '';

  document.getElementById('f-name').value    = '';
  document.getElementById('f-hour').value    = '0';
  document.getElementById('f-minute').value  = '0';
  document.getElementById('f-mode').value    = '';
  document.getElementById('f-reserve').value = '';
  document.getElementById('f-grid-charging').value = '';
  document.getElementById('f-grid-export').value   = '';
  document.querySelectorAll('.day-btn').forEach(b => b.classList.remove('selected'));
  document.querySelectorAll('.month-btn').forEach(b => b.classList.remove('selected'));

  if (id) {
    const rule = _rules.find(r => r.id === id);
    if (!rule) return;
    document.getElementById('f-name').value   = rule.name;
    document.getElementById('f-hour').value   = rule.hour;
    document.getElementById('f-minute').value = rule.minute;
    document.getElementById('f-mode').value   = rule.mode || '';
    document.getElementById('f-reserve').value = rule.reserve != null ? rule.reserve : '';
    document.getElementById('f-grid-charging').value = rule.grid_charging == null ? '' : (rule.grid_charging ? 'true' : 'false');
    document.getElementById('f-grid-export').value   = rule.grid_export || '';
    rule.days.forEach(d => {
      const b = document.querySelector(`.day-btn[data-day="${d}"]`);
      if (b) b.classList.add('selected');
    });
    rule.months.forEach(m => {
      const b = document.querySelector(`.month-btn[data-month="${m}"]`);
      if (b) b.classList.add('selected');
    });
    (rule.conditions || []).forEach(c => addCondRow(c));
  }

  document.getElementById('modal-backdrop').classList.add('open');
}

function closeModal() {
  document.getElementById('modal-backdrop').classList.remove('open');
}

document.querySelectorAll('.day-btn').forEach(b => b.addEventListener('click', () => b.classList.toggle('selected')));
document.querySelectorAll('.month-btn').forEach(b => b.addEventListener('click', () => b.classList.toggle('selected')));

function addCondRow(data) {
  const idx = _condCount++;
  const logic    = data?.logic    || 'AND';
  const type     = data?.type     || 'battery_pct';
  const operator = data?.operator || '>';
  const value    = data?.value    ?? '';

  const row = document.createElement('div');
  row.className = 'cond-row';
  row.id = `cond-${idx}`;
  row.innerHTML = `
    <select class="form-select cond-logic">
      <option value="AND" ${logic==='AND'?'selected':''}>AND</option>
      <option value="OR"  ${logic==='OR' ?'selected':''}>OR</option>
    </select>
    <select class="form-select cond-type">
      <option value="battery_pct" ${type==='battery_pct'?'selected':''}>Battery %</option>
    </select>
    <select class="form-select cond-op">
      <option value=">"  ${ operator==='>'  ?'selected':''}>></option>
      <option value="<"  ${ operator==='<'  ?'selected':''}}>&#60;</option>
      <option value=">=" ${ operator==='>=' ?'selected':''}>>=</option>
      <option value="<=" ${ operator==='<=' ?'selected':''}}>&#60;=</option>
    </select>
    <input class="form-input cond-val" type="number" min="0" max="100" value="${value}" placeholder="0">
    <button type="button" class="btn-remove-cond" onclick="this.parentElement.remove()">\u00d7</button>
  `;
  document.getElementById('cond-list').appendChild(row);
}

function getConditions() {
  return Array.from(document.querySelectorAll('#cond-list .cond-row')).map(row => ({
    logic:    row.querySelector('.cond-logic').value,
    type:     row.querySelector('.cond-type').value,
    operator: row.querySelector('.cond-op').value,
    value:    parseFloat(row.querySelector('.cond-val').value) || 0,
  }));
}

async function saveRule() {
  const id   = document.getElementById('edit-id').value;
  const name = document.getElementById('f-name').value.trim();
  if (!name) { alert('Name is required.'); return; }

  const days   = Array.from(document.querySelectorAll('.day-btn.selected')).map(b => parseInt(b.dataset.day));
  const months = Array.from(document.querySelectorAll('.month-btn.selected')).map(b => parseInt(b.dataset.month));
  if (!days.length)   { alert('Select at least one day.');   return; }
  if (!months.length) { alert('Select at least one month.'); return; }

  const gc = document.getElementById('f-grid-charging').value;
  const reserveVal = document.getElementById('f-reserve').value;

  const body = {
    name,
    enabled: true,
    days,
    months,
    hour:          parseInt(document.getElementById('f-hour').value) || 0,
    minute:        parseInt(document.getElementById('f-minute').value) || 0,
    mode:          document.getElementById('f-mode').value || null,
    reserve:       reserveVal !== '' ? parseInt(reserveVal) : null,
    grid_charging: gc === '' ? null : gc === 'true',
    grid_export:   document.getElementById('f-grid-export').value || null,
    conditions:    getConditions(),
  };

  try {
    const url    = id ? `/api/rules/${id}` : '/api/rules';
    const method = id ? 'PUT' : 'POST';
    const res = await fetch(url, { method, headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
    if (!res.ok) { alert('Save failed.'); return; }
    closeModal();
    await refreshRules();
  } catch(e) { alert('Save error: ' + e); }
}

// ── Init ──────────────────────────────────────────────────────────────────────
poll();
refreshChart();
refreshWeather();
refreshAutomations();
refreshPool();
refreshSecurity();
refreshCosts();
refreshRates();

// Fetch settings and set up intervals dynamically
(async function initIntervals() {
  let s = {};
  try {
    const data = await fetch('/api/settings').then(r => r.json());
    s = data.settings || {};
  } catch(e) { /* use defaults below */ }

  const iv = (key, fallback) => parseInt(s[key]) || fallback;

  setInterval(poll,               iv('fe_poll_interval',        10_000));
  setInterval(refreshChart,       iv('fe_chart_interval',       60_000));
  setInterval(refreshWeather,     iv('fe_weather_interval',    600_000));
  setInterval(refreshAutomations, iv('fe_automations_interval', 60_000));
  setInterval(refreshPool,        iv('fe_pool_interval',        60_000));
  setInterval(refreshSecurity,   iv('fe_security_interval',    60_000));
  setInterval(refreshCosts,       iv('fe_costs_interval',      300_000));
  setInterval(refreshRates,       iv('fe_rates_interval',      600_000));
  const evInterval = iv('fe_events_interval', 60_000);
  setInterval(() => { if (_activePage === 'events') refreshEvents(); }, evInterval);
})();

// ── Energy Costs Page ─────────────────────────────────────────────────────────
function fmtNet(v) {
  return v < 0 ? '\u2212$' + (-v).toFixed(2) : '$' + v.toFixed(2);
}
function fmtKwh(v) {
  return v < 0 ? '\u2212' + (-v).toFixed(1) : v.toFixed(1);
}

async function refreshCostsPage() {
  try {
    const data = await fetch('/api/costs/daily').then(r => r.json());
    const days = data.days || [];

    // Rates note
    const note = document.getElementById('costs-rates-note');
    if (note && data.rates_as_of)
      note.textContent = 'rates as of ' + data.rates_as_of;

    // YTD summary — per-period net costs
    let ytdOn = 0, ytdOff = 0, ytdSuper = 0;
    days.forEach(d => {
      ytdOn    += d.on_peak_cost;
      ytdOff   += d.off_peak_cost;
      ytdSuper += d.super_off_peak_cost;
    });
    const ytdNet = ytdOn + ytdOff + ytdSuper;

    const csOn    = document.getElementById('cs-on-peak');
    const csOff   = document.getElementById('cs-off-peak');
    const csSuper = document.getElementById('cs-super-off-peak');
    const csNet   = document.getElementById('cs-net');
    if (csOn)    { csOn.textContent = fmtNet(ytdOn);    csOn.style.color = 'var(--amber)'; }
    if (csOff)   { csOff.textContent = fmtNet(ytdOff);  csOff.style.color = 'var(--green)'; }
    if (csSuper) { csSuper.textContent = fmtNet(ytdSuper); csSuper.style.color = 'var(--blue)'; }
    if (csNet) {
      csNet.textContent = fmtNet(ytdNet);
      csNet.style.color = ytdNet <= 0 ? 'var(--green)' : 'var(--text)';
    }

    const list = document.getElementById('costs-list');
    if (!list) return;

    if (!days.length) {
      list.innerHTML = '<div class="costs-empty">No cost data. Run backfill.py then Rebuild.</div>';
      return;
    }

    // Group by month (days already sorted newest-first)
    const months = {};
    const monthOrder = [];
    for (const d of days) {
      const mk = d.date.slice(0, 7); // YYYY-MM
      if (!months[mk]) { months[mk] = []; monthOrder.push(mk); }
      months[mk].push(d);
    }

    // Column header
    let html = `
      <div class="costs-col-header">
        <div class="cr-date"></div>
        <div class="cr-pkwh" style="color:var(--amber)">kWh</div>
        <div class="cr-pcost" style="color:var(--amber)">On-Pk</div>
        <div class="cr-pkwh" style="color:var(--green)">kWh</div>
        <div class="cr-pcost" style="color:var(--green)">Off-Pk</div>
        <div class="cr-pkwh" style="color:var(--blue)">kWh</div>
        <div class="cr-pcost" style="color:var(--blue)">Sup Off</div>
        <div class="cr-net">Net</div>
      </div>`;

    for (const mk of monthOrder) {
      const mDays = months[mk];
      const [y, m] = mk.split('-');
      const monthLabel = new Date(y, m - 1, 1).toLocaleDateString('en-US', {month: 'long', year: 'numeric'});

      let mOn = 0, mOff = 0, mSuper = 0;
      mDays.forEach(d => {
        mOn += d.on_peak_cost; mOff += d.off_peak_cost; mSuper += d.super_off_peak_cost;
      });
      const mNet = mOn + mOff + mSuper;
      const netColor = mNet <= 0 ? 'var(--green)' : 'var(--amber)';

      html += `
        <div class="costs-month-header">
          <div class="cr-date costs-month-label">${monthLabel}</div>
          <div class="cr-pkwh"></div>
          <div class="cr-pcost cr-period-on">${fmtNet(mOn)}</div>
          <div class="cr-pkwh"></div>
          <div class="cr-pcost cr-period-off">${fmtNet(mOff)}</div>
          <div class="cr-pkwh"></div>
          <div class="cr-pcost cr-period-super">${fmtNet(mSuper)}</div>
          <div class="cr-net" style="color:${netColor}">${fmtNet(mNet)}</div>
        </div>`;

      for (const d of mDays) {
        const dateLabel = new Date(d.date + 'T12:00:00').toLocaleDateString('en-US', {month: 'short', day: 'numeric'});
        const netColor2 = d.net_cost <= 0 ? 'var(--green)' : 'var(--amber)';
        html += `
          <div class="costs-row">
            <div class="cr-date">${dateLabel}</div>
            <div class="cr-pkwh">${fmtKwh(d.on_peak_kwh)}</div>
            <div class="cr-pcost cr-period-on">${fmtNet(d.on_peak_cost)}</div>
            <div class="cr-pkwh">${fmtKwh(d.off_peak_kwh)}</div>
            <div class="cr-pcost cr-period-off">${fmtNet(d.off_peak_cost)}</div>
            <div class="cr-pkwh">${fmtKwh(d.super_off_peak_kwh)}</div>
            <div class="cr-pcost cr-period-super">${fmtNet(d.super_off_peak_cost)}</div>
                <div class="cr-net" style="color:${netColor2}">${fmtNet(d.net_cost)}</div>
          </div>`;
      }
    }
    list.innerHTML = html;

  } catch(e) { console.warn('CostsPage:', e); }
}

async function costsRebuild() {
  const btn = document.querySelector('.costs-rebuild-btn');
  if (btn) { btn.textContent = '\u21ba Rebuilding\u2026'; btn.disabled = true; }
  try {
    await fetch('/api/costs/rebuild', {method: 'POST'});
    setTimeout(() => refreshCostsPage(), 3000);
  } catch(e) { console.warn('Rebuild:', e); }
  finally {
    setTimeout(() => {
      if (btn) { btn.textContent = '\u21ba Rebuild'; btn.disabled = false; }
    }, 3500);
  }
}

// ── Settings Page ─────────────────────────────────────────────────────────────
let _settingsData = null;

async function refreshSettings() {
  try {
    const data = await fetch('/api/settings').then(r => r.json());
    _settingsData = data;
    renderSettings(data);
  } catch(e) { console.warn('Settings:', e); }
}

// Convert raw seconds to the best human-readable unit
function secondsToBestUnit(secs) {
  secs = Number(secs);
  if (secs >= 3600 && secs % 3600 === 0) return { value: secs / 3600, unit: 'hr' };
  if (secs >= 60   && secs % 60   === 0) return { value: secs / 60,   unit: 'min' };
  return { value: secs, unit: 's' };
}

// Convert ms to best human-readable unit
function msToBestUnit(ms) {
  ms = Number(ms);
  if (ms >= 3600000 && ms % 3600000 === 0) return { value: ms / 3600000, unit: 'hr' };
  if (ms >= 60000   && ms % 60000   === 0) return { value: ms / 60000,   unit: 'min' };
  if (ms >= 1000    && ms % 1000    === 0) return { value: ms / 1000,    unit: 's' };
  return { value: ms, unit: 'ms' };
}

// Convert display value + unit back to storage unit (seconds or ms)
function toStorageValue(displayVal, displayUnit, storageUnit) {
  const v = Number(displayVal);
  if (storageUnit === 's') {
    if (displayUnit === 'hr')  return v * 3600;
    if (displayUnit === 'min') return v * 60;
    return v;
  }
  if (storageUnit === 'ms') {
    if (displayUnit === 'hr')  return v * 3600000;
    if (displayUnit === 'min') return v * 60000;
    if (displayUnit === 's')   return v * 1000;
    return v;
  }
  return v; // days or other units pass through
}

function renderSettings(data) {
  const grid = document.getElementById('settings-grid');
  if (!grid) return;

  const settings = data.settings || {};
  const connectors = data.connectors || [];
  const typeLabels = {
    continuous: 'Continuous Poller',
    'on-demand': 'On-demand',
    websocket: 'Websocket (event-driven)',
    scheduled: 'Scheduled Tasks',
    frontend: 'Browser Intervals',
    configurable: 'Configuration',
  };

  // Define which units are available per storage unit
  const unitOptions = {
    's':      ['s', 'min', 'hr'],
    'ms':     ['ms', 's', 'min', 'hr'],
    'days':   null,   // plain label, no dropdown
    'months': null,   // plain label, no dropdown
    'url':    null,    // text input, no dropdown
    'text':   null,    // text input, no dropdown
    'date':   null,    // date input, no dropdown
  };

  let html = '';
  for (const conn of connectors) {
    const hasToggle = !!conn.enabled_key;
    const enabled = hasToggle ? settings[conn.enabled_key] === '1' : true;
    const dotCls = enabled ? 'on' : 'off';

    let intervalsHtml = '';
    for (const iv of conn.intervals) {
      const rawVal = settings[iv.key] || '0';
      const opts = unitOptions[iv.unit];

      if (opts) {
        // Convert to best display unit
        const best = iv.unit === 'ms' ? msToBestUnit(rawVal) : secondsToBestUnit(rawVal);
        const optionsHtml = opts.map(u =>
          `<option value="${u}" ${u === best.unit ? 'selected' : ''}>${u}</option>`
        ).join('');
        intervalsHtml += `
          <div class="settings-interval">
            <label>${iv.label}</label>
            <input type="number" min="1" data-key="${iv.key}" data-storage-unit="${iv.unit}" value="${best.value}">
            <select class="settings-unit-select" data-for="${iv.key}">${optionsHtml}</select>
          </div>`;
      } else if (iv.unit === 'url' || iv.unit === 'text') {
        // Text input (url or free text)
        const inputVal = settings[iv.key] || '';
        const widthStyle = iv.unit === 'url' ? 'flex:1;width:auto;' : 'width:140px;';
        intervalsHtml += `
          <div class="settings-interval">
            <label>${iv.label}</label>
            <input type="text" data-key="${iv.key}" data-storage-unit="${iv.unit}" value="${inputVal}"
                   style="${widthStyle}">
          </div>`;
      } else if (iv.unit === 'date') {
        // Date input
        const inputVal = settings[iv.key] || '';
        intervalsHtml += `
          <div class="settings-interval">
            <label>${iv.label}</label>
            <input type="date" data-key="${iv.key}" data-storage-unit="${iv.unit}" value="${inputVal}">
          </div>`;
      } else {
        // Plain label (e.g. days, months)
        intervalsHtml += `
          <div class="settings-interval">
            <label>${iv.label}</label>
            <input type="number" min="1" data-key="${iv.key}" data-storage-unit="${iv.unit}" value="${rawVal}">
            <span class="settings-unit">${iv.unit}</span>
          </div>`;
      }
    }

    const toggleHtml = hasToggle ? `
      <label class="toggle">
        <input type="checkbox" ${enabled ? 'checked' : ''}
               data-key="${conn.enabled_key}"
               onchange="saveSettingToggle(this)">
        <span class="toggle-slider"></span>
      </label>` : '';

    html += `
      <div class="settings-card" data-connector="${conn.key}">
        <div class="settings-card-header">
          <div class="settings-card-title">
            ${hasToggle ? `<span class="settings-dot ${dotCls}"></span>` : ''}
            ${conn.label}
          </div>
          ${toggleHtml}
        </div>
        <div class="settings-type">${typeLabels[conn.type] || conn.type}</div>
        ${intervalsHtml}
        ${conn.intervals.length ? '<button class="settings-save-btn" onclick="saveSettingsCard(this)">Save</button>' : ''}
      </div>`;
  }
  grid.innerHTML = html;
}

async function saveSettingToggle(checkbox) {
  const key = checkbox.dataset.key;
  const value = checkbox.checked ? '1' : '0';
  try {
    await fetch('/api/settings', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({[key]: value})
    });
    // Update dot
    const card = checkbox.closest('.settings-card');
    const dot = card.querySelector('.settings-dot');
    if (dot) { dot.className = 'settings-dot ' + (checkbox.checked ? 'on' : 'off'); }
    showSettingsStatus('Saved');
  } catch(e) { console.warn('Settings toggle:', e); }
}

async function saveSettingsCard(btn) {
  const card = btn.closest('.settings-card');
  const inputs = card.querySelectorAll('input[data-key]:not([type="checkbox"])');
  const updates = {};
  inputs.forEach(inp => {
    const key = inp.dataset.key;
    const storageUnit = inp.dataset.storageUnit || 's';
    if (storageUnit === 'url' || storageUnit === 'text' || storageUnit === 'date') {
      updates[key] = inp.value;
    } else {
      const unitSelect = card.querySelector(`select[data-for="${key}"]`);
      const displayUnit = unitSelect ? unitSelect.value : storageUnit;
      updates[key] = String(toStorageValue(inp.value, displayUnit, storageUnit));
    }
  });
  try {
    await fetch('/api/settings', {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(updates)
    });
    showSettingsStatus('Saved');
  } catch(e) { console.warn('Settings save:', e); }
}

function showSettingsStatus(msg) {
  const el = document.getElementById('settings-status');
  if (el) {
    el.textContent = msg;
    setTimeout(() => { el.textContent = ''; }, 2000);
  }
}

// ── Event Log ─────────────────────────────────────────────────────────────────
const SYSTEM_META = {
  powerwall: { icon: '\u26a1', label: 'Powerwall', color: 'var(--amber)' },
  rachio:    { icon: '\ud83c\udf3f', label: 'Rachio',    color: 'var(--rachio)' },
  abode:     { icon: '\ud83d\udd12', label: 'Abode',     color: 'var(--abode)' },
  pool:      { icon: '\ud83c\udfca', label: 'Pool',      color: 'var(--pool)' },
  myq:       { icon: '\ud83d\ude97', label: 'MyQ',       color: 'var(--gray)' },
};

let _events = [];
let _eventsFilter = 'all';

function _isErrorEvent(e) {
  return e.result === 'failed' || e.event_type === 'error';
}

async function refreshEvents() {
  try {
    const data = await fetch('/api/events?limit=200&days=7').then(r => r.json());
    _events = data;
    renderEvents();
    const upd = document.getElementById('events-updated');
    if (upd) upd.textContent = 'updated ' + new Date().toLocaleTimeString(
      [], {hour: 'numeric', minute: '2-digit'});
  } catch(e) { console.warn('Events:', e); }
}

function setEventsFilter(system) {
  _eventsFilter = system;
  document.querySelectorAll('.evf-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.system === system);
  });
  renderEvents();
}

function renderEvents() {
  const list = document.getElementById('events-list');
  if (!list) return;

  const filtered = _eventsFilter === 'all'
    ? _events
    : _eventsFilter === 'errors'
      ? _events.filter(e => _isErrorEvent(e))
      : _events.filter(e => e.system === _eventsFilter);

  if (!filtered.length) {
    list.innerHTML = '<div class="events-empty">No events found.</div>';
    return;
  }

  let lastDate = null;
  let html = '';
  for (const e of filtered) {
    const d = new Date(e.ts * 1000);
    const dateKey = d.toLocaleDateString('en-US', {month: 'short', day: 'numeric'});
    if (dateKey !== lastDate) {
      html += `<div class="event-date-divider"><span>${dateKey}</span></div>`;
      lastDate = dateKey;
    }
    const meta = SYSTEM_META[e.system] || {icon: '?', label: e.system, color: 'var(--dim)'};
    const timeStr = d.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
    const isErr = _isErrorEvent(e);
    html += `
      <div class="event-row${isErr ? ' error' : ''}" data-system="${e.system}">
        <div class="event-accent" style="background:${isErr ? '#e05252' : meta.color}"></div>
        <div class="event-ts">${timeStr}</div>
        <div class="event-system" style="color:${meta.color}">${meta.icon} ${meta.label}</div>
        <div class="event-title">${e.title}</div>
      </div>`;
  }
  list.innerHTML = html;
}
