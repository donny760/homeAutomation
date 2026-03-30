'use client';

import { useState, useEffect } from 'react';
import { fmtNet, fmtKwh } from '@/lib/format';

interface DayRow {
  date: string;
  on_peak_kwh: number;
  off_peak_kwh: number;
  super_off_peak_kwh: number;
  on_peak_cost: number;
  off_peak_cost: number;
  super_off_peak_cost: number;
  net_cost: number;
}

interface EnergyCostsProps {
  isActive: boolean;
}

export default function EnergyCosts({ isActive }: EnergyCostsProps) {
  const [days, setDays] = useState<DayRow[]>([]);
  const [ratesNote, setRatesNote] = useState('');
  const [loading, setLoading] = useState(true);
  const [rebuilding, setRebuilding] = useState(false);

  useEffect(() => {
    if (isActive) refreshCostsPage();
  }, [isActive]);

  async function refreshCostsPage() {
    try {
      const data = await fetch('/api/costs/daily').then((r) => r.json());
      setDays(data.days || []);
      if (data.rates_as_of) setRatesNote('rates as of ' + data.rates_as_of);
      setLoading(false);
    } catch (e) {
      console.warn('CostsPage:', e);
    }
  }

  async function costsRebuild() {
    setRebuilding(true);
    try {
      await fetch('/api/costs/rebuild', { method: 'POST' });
      setTimeout(() => refreshCostsPage(), 3000);
    } catch (e) {
      console.warn('Rebuild:', e);
    } finally {
      setTimeout(() => setRebuilding(false), 3500);
    }
  }

  // YTD summary
  let ytdOn = 0, ytdOff = 0, ytdSuper = 0;
  days.forEach((d) => {
    ytdOn += d.on_peak_cost;
    ytdOff += d.off_peak_cost;
    ytdSuper += d.super_off_peak_cost;
  });
  const ytdNet = ytdOn + ytdOff + ytdSuper;

  // Group by month
  const months: Record<string, DayRow[]> = {};
  const monthOrder: string[] = [];
  for (const d of days) {
    const mk = d.date.slice(0, 7);
    if (!months[mk]) {
      months[mk] = [];
      monthOrder.push(mk);
    }
    months[mk].push(d);
  }

  return (
    <div id="page-costs" className="page active">
      <div className="costs-toolbar">
        <div className="page-title">Energy Breakdown</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: '10px' }}>
          <span className="costs-rates-note">{ratesNote}</span>
          <button className="costs-rebuild-btn" onClick={costsRebuild} disabled={rebuilding}>
            {rebuilding ? '\u21ba Rebuilding\u2026' : '\u21ba Rebuild'}
          </button>
        </div>
      </div>
      <div className="costs-summary">
        <div className="costs-summary-col">
          <div className="costs-summary-label" style={{ color: 'var(--amber)' }}>On-Peak</div>
          <div className="costs-summary-value" style={{ color: 'var(--amber)' }}>
            {days.length ? fmtNet(ytdOn) : '\u2014'}
          </div>
        </div>
        <div className="costs-summary-col">
          <div className="costs-summary-label" style={{ color: 'var(--green)' }}>Off-Peak</div>
          <div className="costs-summary-value" style={{ color: 'var(--green)' }}>
            {days.length ? fmtNet(ytdOff) : '\u2014'}
          </div>
        </div>
        <div className="costs-summary-col">
          <div className="costs-summary-label" style={{ color: 'var(--blue)' }}>Super Off-Peak</div>
          <div className="costs-summary-value" style={{ color: 'var(--blue)' }}>
            {days.length ? fmtNet(ytdSuper) : '\u2014'}
          </div>
        </div>
        <div className="costs-summary-col">
          <div className="costs-summary-label">Net Cost</div>
          <div
            className="costs-summary-value"
            style={{ color: days.length ? (ytdNet <= 0 ? 'var(--green)' : 'var(--text)') : undefined }}
          >
            {days.length ? fmtNet(ytdNet) : '\u2014'}
          </div>
        </div>
      </div>
      <div className="costs-scroll" id="costs-list">
        {loading ? (
          <div className="costs-empty">Loading&hellip;</div>
        ) : days.length === 0 ? (
          <div className="costs-empty">No cost data. Run backfill.py then Rebuild.</div>
        ) : (
          <>
            <div className="costs-col-header">
              <div className="cr-date" />
              <div className="cr-pkwh" style={{ color: 'var(--amber)' }}>kWh</div>
              <div className="cr-pcost" style={{ color: 'var(--amber)' }}>On-Pk</div>
              <div className="cr-pkwh" style={{ color: 'var(--green)' }}>kWh</div>
              <div className="cr-pcost" style={{ color: 'var(--green)' }}>Off-Pk</div>
              <div className="cr-pkwh" style={{ color: 'var(--blue)' }}>kWh</div>
              <div className="cr-pcost" style={{ color: 'var(--blue)' }}>Sup Off</div>
              <div className="cr-net">Net</div>
            </div>
            {monthOrder.map((mk) => {
              const mDays = months[mk];
              const [y, m] = mk.split('-');
              const monthLabel = new Date(Number(y), Number(m) - 1, 1).toLocaleDateString('en-US', {
                month: 'long',
                year: 'numeric',
              });
              let mOn = 0, mOff = 0, mSuper = 0;
              mDays.forEach((d) => {
                mOn += d.on_peak_cost;
                mOff += d.off_peak_cost;
                mSuper += d.super_off_peak_cost;
              });
              const mNet = mOn + mOff + mSuper;
              const netColor = mNet <= 0 ? 'var(--green)' : 'var(--amber)';

              return (
                <span key={mk}>
                  <div className="costs-month-header">
                    <div className="cr-date costs-month-label">{monthLabel}</div>
                    <div className="cr-pkwh" />
                    <div className="cr-pcost cr-period-on">{fmtNet(mOn)}</div>
                    <div className="cr-pkwh" />
                    <div className="cr-pcost cr-period-off">{fmtNet(mOff)}</div>
                    <div className="cr-pkwh" />
                    <div className="cr-pcost cr-period-super">{fmtNet(mSuper)}</div>
                    <div className="cr-net" style={{ color: netColor }}>{fmtNet(mNet)}</div>
                  </div>
                  {mDays.map((d) => {
                    const dateLabel = new Date(d.date + 'T12:00:00').toLocaleDateString('en-US', {
                      month: 'short',
                      day: 'numeric',
                    });
                    const netColor2 = d.net_cost <= 0 ? 'var(--green)' : 'var(--amber)';
                    return (
                      <div key={d.date} className="costs-row">
                        <div className="cr-date">{dateLabel}</div>
                        <div className="cr-pkwh">{fmtKwh(d.on_peak_kwh)}</div>
                        <div className="cr-pcost cr-period-on">{fmtNet(d.on_peak_cost)}</div>
                        <div className="cr-pkwh">{fmtKwh(d.off_peak_kwh)}</div>
                        <div className="cr-pcost cr-period-off">{fmtNet(d.off_peak_cost)}</div>
                        <div className="cr-pkwh">{fmtKwh(d.super_off_peak_kwh)}</div>
                        <div className="cr-pcost cr-period-super">{fmtNet(d.super_off_peak_cost)}</div>
                        <div className="cr-net" style={{ color: netColor2 }}>{fmtNet(d.net_cost)}</div>
                      </div>
                    );
                  })}
                </span>
              );
            })}
          </>
        )}
      </div>
    </div>
  );
}
