'use client';

import { useState, useEffect } from 'react';
import { touPeriod } from '@/lib/tou';

// ── Energy YTD tile ──
function EnergyYTDTile() {
  const [data, setData] = useState<{ import_cost: number; export_credit: number; net_cost: number } | null>(null);

  useEffect(() => {
    async function refresh() {
      try {
        const d = await fetch('/api/costs/ytd').then((r) => r.json());
        setData(d);
      } catch (e) { /* leave dashes */ }
    }
    refresh();
    const id = setInterval(refresh, 300_000);
    return () => clearInterval(id);
  }, []);

  const fmt = (v: number) => '$' + v.toFixed(2);

  return (
    <div className="tile" id="costs-tile">
      <div className="tile-title">Energy YTD</div>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: '6px', marginTop: '6px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.88rem' }}>
          <span style={{ color: 'var(--dim)' }}>Grid imported</span>
          <span className={data ? '' : 'tile-na'}>{data ? fmt(data.import_cost) : '\u2014'}</span>
        </div>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.88rem' }}>
          <span style={{ color: 'var(--dim)' }}>Export credits</span>
          <span style={data ? { color: 'var(--green)' } : {}} className={data ? '' : 'tile-na'}>
            {data ? '\u2212' + fmt(data.export_credit) : '\u2014'}
          </span>
        </div>
        <div style={{ borderTop: '0.5px solid var(--border)', margin: '2px 0' }} />
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.88rem' }}>
          <span style={{ color: 'var(--dim)' }}>Net cost</span>
          <span className={data ? '' : 'tile-na'}>{data ? fmt(data.net_cost) : '\u2014'}</span>
        </div>
      </div>
    </div>
  );
}

// ── Current Rate tile ──
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

function CurrentRateTile() {
  const [r, setR] = useState<RateData | null>(null);

  useEffect(() => {
    async function refresh() {
      try {
        const data = await fetch('/api/rates').then((x) => x.json());
        setR(data);
      } catch (e) { /* leave dashes */ }
    }
    refresh();
    const id = setInterval(refresh, 600_000);
    return () => clearInterval(id);
  }, []);

  const fmt2 = (v?: number) => (v != null ? '$' + Number(v).toFixed(3) : '\u2014');

  const now = new Date();
  const h = now.getHours();
  const mon = now.getMonth() + 1;
  const dow = now.getDay();
  const isSummer = [6, 7, 8, 9, 10].includes(mon);
  const isWeekend = dow === 0 || dow === 6;
  const todayISO = now.toISOString().slice(0, 10);
  const isHoliday = r?.holidays?.includes(todayISO) || false;
  const season = isSummer ? 'summer' : 'winter';
  const period = r ? touPeriod(h, mon, isSummer, isWeekend || isHoliday, r.tou_periods) : 'off_peak';

  const periodLabels: Record<string, string> = { on_peak: 'ON-PEAK', off_peak: 'OFF-PEAK', super_off_peak: 'SUPER OFF-PEAK' };
  const periodAccents: Record<string, string> = { on_peak: 'var(--amber)', off_peak: 'var(--green)', super_off_peak: 'var(--blue)' };
  const accentColor = periodAccents[period];

  const rateKey = `${season}_${period}` as keyof RateData;
  const currentRate = r ? (r[rateKey] as number | undefined) : undefined;

  const rtRows: Record<string, string> = { on_peak: 'rt-row-on-peak', off_peak: 'rt-row-off-peak', super_off_peak: 'rt-row-super' };

  return (
    <div className="tile" id="rate-tile">
      <div className="tile-title">Current Rate</div>
      <div className="rt-top" style={{ marginTop: '6px' }}>
        <div>
          <div className="rt-amount-wrap">
            <span
              className={`rt-amount${currentRate == null ? ' tile-na' : ''}`}
              style={currentRate != null ? { color: accentColor } : {}}
            >
              {currentRate != null ? '$' + Number(currentRate).toFixed(3) : '\u2014'}
            </span>
            <span className="rt-unit">/kWh</span>
          </div>
          <div className="rt-period" style={{ color: accentColor }}>
            {periodLabels[period] || '\u2014'}
          </div>
        </div>
        <span className={`rt-season-badge ${season}`}>
          {isSummer ? 'SUMMER' : 'WINTER'}
        </span>
      </div>
      <table className="rt-mini">
        <tbody>
          <tr id="rt-row-on-peak" className={period === 'on_peak' ? 'rt-active' : ''}>
            <td>On-peak</td>
            <td className={r ? '' : 'tile-na'}>
              {r ? fmt2(r.summer_on_peak) + ' / ' + fmt2(r.winter_on_peak) : '\u2014'}
            </td>
          </tr>
          <tr id="rt-row-off-peak" className={period === 'off_peak' ? 'rt-active' : ''}>
            <td>Off-peak</td>
            <td className={r ? '' : 'tile-na'}>
              {r ? fmt2(r.summer_off_peak) + ' / ' + fmt2(r.winter_off_peak) : '\u2014'}
            </td>
          </tr>
          <tr id="rt-row-super" className={period === 'super_off_peak' ? 'rt-active' : ''}>
            <td>Super off-peak</td>
            <td className={r ? '' : 'tile-na'}>
              {r ? fmt2(r.summer_super_off_peak) + ' / ' + fmt2(r.winter_super_off_peak) : '\u2014'}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  );
}

// ── Pool tile ──
function PoolTile() {
  const [d, setD] = useState<any>(null);

  useEffect(() => {
    async function refresh() {
      try {
        const data = await fetch('/api/pool').then((r) => r.json());
        setD(data);
      } catch (e) {
        console.warn('Pool:', e);
      }
    }
    refresh();
    const id = setInterval(refresh, 60_000);
    return () => clearInterval(id);
  }, []);

  const temp = d?.temp_f != null ? d.temp_f + '\u00b0F' : '--\u00b0F';
  const tempNa = d?.temp_f == null;

  const pumpText = d ? 'Pump  ' + (d.pump_on ? 'On' : 'Off') + (d.pump_watts != null ? ` \u00b7 ${d.pump_watts}W` : '') : '';
  const edgeText = d ? 'Edge Pump  ' + (d.edge_pump_on ? 'On' : 'Off') : '';
  const cleanerText = d ? 'Cleaner  ' + (d.cleaner_on ? 'On' : 'Off') : '';

  let saltText = '';
  let saltColor = 'var(--dim)';
  if (d?.salt_ppm != null) {
    saltText = 'Salt  ' + d.salt_ppm.toLocaleString() + ' ppm';
    if (d.scg_active) saltText += ' · ' + (d.scg_pool_pct ?? '?') + '%';
    if (d.super_chlor) saltText += ' · Super';
    saltColor = d.scg_active ? 'var(--green)' : 'var(--dim)';
  }

  return (
    <div className="tile">
      <div className="tile-title">Pool</div>
      <div className="tile-split">
        <div className={`tile-value${tempNa ? ' tile-na' : ''}`}>{temp}</div>
        <div className="tile-detail">
          <div className={`tile-sub${!d ? ' tile-na' : ''}`} style={d ? { color: d.pump_on ? 'var(--green)' : 'var(--dim)' } : {}}>
            {pumpText}
          </div>
          <div className={`tile-sub${!d ? ' tile-na' : ''}`} style={d ? { color: d.edge_pump_on ? 'var(--green)' : 'var(--dim)' } : {}}>
            {edgeText}
          </div>
          <div className={`tile-sub${!d ? ' tile-na' : ''}`} style={d ? { color: d.cleaner_on ? 'var(--green)' : 'var(--dim)' } : {}}>
            {cleanerText}
          </div>
          <div className={`tile-sub${d?.salt_ppm == null ? ' tile-na' : ''}`} style={{ color: saltColor }}>
            {saltText}
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Security tile ──
function SecurityTile() {
  const [d, setD] = useState<any>(null);

  useEffect(() => {
    async function refresh() {
      try {
        const data = await fetch('/api/security').then((r) => r.json());
        setD(data);
      } catch (e) {
        console.warn('Security:', e);
      }
    }
    refresh();
    const id = setInterval(refresh, 60_000);
    return () => clearInterval(id);
  }, []);

  const notConnected = !d || !d.connected || d.mode == null;
  const modeText = notConnected ? '--' : d.mode_display || d.mode;
  const modeColors: Record<string, string> = { away: 'var(--amber)', home: 'var(--green)', standby: 'var(--dim)' };
  const modeColor = notConnected ? undefined : modeColors[d.mode] || 'var(--dim)';

  let issuesHtml = '';
  let issuesColor = 'var(--green)';
  if (notConnected) {
    issuesHtml = 'Not connected';
    issuesColor = '';
  } else if (!d.issues || d.issues.length === 0) {
    issuesHtml = 'All secure';
    issuesColor = 'var(--green)';
  } else {
    issuesHtml = d.issues.map((i: any) => i.name + ' ' + i.type).join('<br>');
    issuesColor = '#e05252';
  }

  return (
    <div className="tile" id="security-tile">
      <div className="tile-title">Security</div>
      <div className="tile-split">
        <div className={`tile-value${notConnected ? ' tile-na' : ''}`} style={modeColor ? { color: modeColor } : {}}>
          {modeText}
        </div>
        <div
          className={`tile-detail tile-sub${notConnected ? ' tile-na' : ''}`}
          style={issuesColor ? { color: issuesColor } : {}}
          dangerouslySetInnerHTML={{ __html: issuesHtml }}
        />
      </div>
    </div>
  );
}

export default function BottomTiles() {
  return (
    <div className="bottom-row">
      <EnergyYTDTile />
      <CurrentRateTile />
      <PoolTile />
      <SecurityTile />
    </div>
  );
}
