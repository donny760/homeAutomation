'use client';

import { useState, useEffect, useCallback } from 'react';
import { touPeriod } from '@/lib/tou';
import { usePolling } from '@/lib/usePolling';

interface PoolData {
  temp_f?: number | null;
  pump_on?: boolean;
  pump_watts?: number | null;
  pump_rpm?: number | null;
  pump_gpm?: number | null;
  edge_pump_on?: boolean;
  edge_pump_watts?: number | null;
  edge_pump_rpm?: number | null;
  edge_pump_gpm?: number | null;
  cleaner_on?: boolean;
  pool_light_on?: boolean;
  water_light_on?: boolean;
  spa_light_on?: boolean;
  waterfall_on?: boolean;
  spillway_on?: boolean;
  salt_ppm?: number | null;
  scg_active?: boolean;
  scg_pool_pct?: number | null;
  super_chlor?: boolean;
  gallons_today?: number | null;
  gallons_target?: number | null;
}

interface SecurityIssue {
  name: string;
  type: string;
}
interface SecurityData {
  connected?: boolean;
  mode?: string | null;
  mode_display?: string | null;
  issues?: SecurityIssue[];
}

// ── Energy YTD tile ──
function EnergyYTDTile() {
  const [data, setData] = useState<{ import_cost: number; export_credit: number; net_cost: number } | null>(null);

  const refresh = useCallback(async () => {
    try {
      const d = await fetch('/api/costs/ytd').then((r) => r.json());
      setData(d);
    } catch { /* leave dashes */ }
  }, []);
  usePolling(refresh, 300_000);

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
  const [now, setNow] = useState<Date>(() => new Date());

  const refresh = useCallback(async () => {
    try {
      const data = await fetch('/api/rates').then((x) => x.json());
      setR(data);
    } catch { /* leave dashes */ }
  }, []);
  usePolling(refresh, 600_000);

  // Re-tick `now` every minute so the period highlight crosses TOU boundaries
  // (4 PM on-peak start, 9 PM end, etc.) without waiting for the rates poll.
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 60_000);
    return () => clearInterval(id);
  }, []);

  const fmt2 = (v?: number) => (v != null ? '$' + Number(v).toFixed(3) : '\u2014');

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
  const [d, setD] = useState<PoolData | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await fetch('/api/pool').then((r) => r.json());
      setD(data);
    } catch (e) {
      console.warn('Pool:', e);
    }
  }, []);
  usePolling(refresh, 60_000);

  const temp = d?.temp_f != null ? d.temp_f + '\u00b0F' : '--\u00b0F';
  const tempNa = d?.temp_f == null;

  // Build list of active circuits
  const circuits: { label: string; on: boolean }[] = d
    ? [
        {
          label: 'Pump'
            + (d.pump_watts != null ? ` \u00b7 ${d.pump_watts}W`       : '')
            + (d.pump_rpm   != null ? ` \u00b7 ${d.pump_rpm}rpm`       : '')
            + (d.pump_gpm   != null ? ` \u00b7 ${d.pump_gpm}gpm`       : ''),
          on: !!d.pump_on,
        },
        {
          label: 'Edge'
            + (d.edge_pump_watts != null ? ` \u00b7 ${d.edge_pump_watts}W`   : '')
            + (d.edge_pump_rpm   != null ? ` \u00b7 ${d.edge_pump_rpm}rpm`   : '')
            + (d.edge_pump_gpm   != null ? ` \u00b7 ${d.edge_pump_gpm}gpm`   : ''),
          on: !!d.edge_pump_on,
        },
        { label: 'Cleaner', on: !!d.cleaner_on },
        { label: 'Pool Light', on: !!d.pool_light_on },
        { label: 'Water Light', on: !!d.water_light_on },
        { label: 'Spa Light', on: !!d.spa_light_on },
        { label: 'Waterfall', on: !!d.waterfall_on },
        { label: 'Spillway', on: !!d.spillway_on },
      ]
    : [];
  const activeCircuits = circuits.filter((c) => c.on);

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
          {!d && <div className="tile-sub tile-na" />}
          {d && activeCircuits.length === 0 && (
            <div className="tile-sub" style={{ color: 'var(--dim)' }}>All off</div>
          )}
          {activeCircuits.map((c) => (
            <div key={c.label} className="tile-sub" style={{ color: 'var(--green)' }}>
              {c.label}
            </div>
          ))}
          {saltText && (
            <div className={`tile-sub${d?.salt_ppm == null ? ' tile-na' : ''}`} style={{ color: saltColor }}>
              {saltText}
            </div>
          )}
          {d?.gallons_today != null && d?.gallons_target != null && (
            <div
              className="tile-sub"
              style={{
                color:
                  d.gallons_today >= d.gallons_target          ? 'var(--green)'
                  : d.gallons_today >= d.gallons_target * 0.75 ? 'var(--amber)'
                  : 'var(--dim)',
              }}
            >
              {d.gallons_today.toLocaleString()} / {d.gallons_target.toLocaleString()} gal
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ── Temperature tile ──
interface TempRow { label: string; temp_f: number | null; humidity: number | null; }

function TemperatureTile() {
  const [outside, setOutside] = useState<{ temp_f: number | null; humidity: number | null } | null>(null);
  const [rows, setRows] = useState<TempRow[]>([]);

  const refresh = useCallback(async () => {
    try {
      const [wx, switches] = await Promise.all([
        fetch('/api/weather').then((r) => r.json()),
        fetch('/api/switches').then((r) => r.json()),
      ]);
      setOutside({ temp_f: wx.temp_f ?? null, humidity: wx.humidity ?? null });
      const newRows: TempRow[] = [];
      for (const sw of switches) {
        if (sw.kind === 'thermostat' && sw.reachable) {
          const label = sw.name.replace(/^Nest Thermostat\s*/i, '');
          newRows.push({ label, temp_f: sw.detail?.ambient_f ?? null, humidity: sw.detail?.humidity ?? null });
        }
        if (sw.kind === 'sensor' && sw.reachable) {
          newRows.push({ label: 'Attic', temp_f: sw.detail?.temp_f ?? null, humidity: sw.detail?.humidity ?? null });
        }
      }
      setRows(newRows);
    } catch (e) {
      console.warn('Temperature tile:', e);
    }
  }, []);
  usePolling(refresh, 60_000);

  const fmt = (t: number | null) => t != null ? `${t}°` : '--';
  const fmtH = (h: number | null) => h != null ? `${h}%` : '';
  const tempColor = (t: number | null) =>
    t == null ? undefined
    : t >= 80  ? 'var(--amber)'
    : t >= 68  ? undefined
    : 'var(--blue)';

  return (
    <div className="tile">
      <div className="tile-title">Temperature</div>
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'center', gap: '5px', marginTop: '4px' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
          <span style={{ fontSize: '0.82rem', color: 'var(--dim)' }}>Outside</span>
          <span style={{ fontSize: '0.95rem', display: 'flex', gap: '6px', alignItems: 'baseline' }}>
            <span className={outside?.temp_f == null ? 'tile-na' : ''} style={{ color: tempColor(outside?.temp_f ?? null) }}>{fmt(outside?.temp_f ?? null)}</span>
            {outside?.humidity != null && (
              <span style={{ fontSize: '0.75rem', color: 'var(--dim)' }}>{fmtH(outside.humidity)}</span>
            )}
          </span>
        </div>
        {rows.map((row) => (
          <div key={row.label} style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
            <span style={{ fontSize: '0.82rem', color: 'var(--dim)' }}>{row.label}</span>
            <span style={{ fontSize: '0.95rem', display: 'flex', gap: '6px', alignItems: 'baseline' }}>
              <span className={row.temp_f == null ? 'tile-na' : ''} style={{ color: tempColor(row.temp_f) }}>{fmt(row.temp_f)}</span>
              {row.humidity != null && (
                <span style={{ fontSize: '0.75rem', color: 'var(--dim)' }}>{fmtH(row.humidity)}</span>
              )}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Security tile ──
function SecurityTile() {
  const [d, setD] = useState<SecurityData | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await fetch('/api/security').then((r) => r.json());
      setD(data);
    } catch (e) {
      console.warn('Security:', e);
    }
  }, []);
  usePolling(refresh, 60_000);

  const notConnected = !d || !d.connected || d.mode == null;
  const modeText = notConnected ? '--' : d!.mode_display || d!.mode;
  const modeColors: Record<string, string> = { away: 'var(--amber)', home: 'var(--green)', standby: 'var(--dim)' };
  const modeColor = notConnected ? undefined : (modeColors[d!.mode!] || 'var(--dim)');

  const issues = !notConnected ? (d!.issues || []) : [];
  const hasIssues = issues.length > 0;
  const issuesColor = notConnected ? '' : (hasIssues ? '#e05252' : 'var(--green)');

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
        >
          {notConnected
            ? 'Not connected'
            : !hasIssues
              ? 'All secure'
              : issues.map((i, idx) => (
                  <div key={idx}>{i.name}</div>
                ))}
        </div>
      </div>
    </div>
  );
}

export default function BottomTiles() {
  return (
    <div className="bottom-row">
      <EnergyYTDTile />
      <CurrentRateTile />
      <TemperatureTile />
      <PoolTile />
      <SecurityTile />
    </div>
  );
}
