'use client';

import { useState, useEffect, useRef, useCallback } from 'react';
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

const PAGE_SIZE = 60;

export default function EnergyCosts({ isActive }: EnergyCostsProps) {
  const today = new Date().toISOString().slice(0, 10);
  const yearStart = today.slice(0, 4) + '-01-01';

  const [days, setDays] = useState<DayRow[]>([]);
  const [ratesNote, setRatesNote] = useState('');
  const [loading, setLoading] = useState(true);
  const [rebuilding, setRebuilding] = useState(false);
  const [startDate, setStartDate] = useState(yearStart);
  const [endDate, setEndDate] = useState(today);
  const [total, setTotal] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  const scrollRef = useRef<HTMLDivElement>(null);
  const sentinelRef = useRef<HTMLDivElement>(null);

  // Fetch a page of data, optionally appending to existing
  const fetchPage = useCallback(async (start: string, end: string, offset: number, append: boolean) => {
    if (!append) setLoading(true);
    else setLoadingMore(true);
    try {
      const params = new URLSearchParams({ start, end, limit: String(PAGE_SIZE), offset: String(offset) });
      const data = await fetch(`/api/costs/daily?${params}`).then((r) => r.json());
      const newDays: DayRow[] = data.days || [];
      if (append) {
        setDays((prev) => [...prev, ...newDays]);
      } else {
        setDays(newDays);
        setExpanded({});
      }
      setTotal(data.total || 0);
      setHasMore(offset + newDays.length < (data.total || 0));
      if (data.rates_as_of) setRatesNote('rates as of ' + data.rates_as_of);
    } catch (e) {
      console.warn('CostsPage:', e);
    } finally {
      setLoading(false);
      setLoadingMore(false);
    }
  }, []);

  // Initial load and on filter change
  useEffect(() => {
    if (isActive) fetchPage(startDate, endDate, 0, false);
  }, [isActive, startDate, endDate, fetchPage]);

  // Infinite scroll via IntersectionObserver
  useEffect(() => {
    const sentinel = sentinelRef.current;
    const container = scrollRef.current;
    if (!sentinel || !container) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries[0].isIntersecting && hasMore && !loadingMore && !loading) {
          fetchPage(startDate, endDate, days.length, true);
        }
      },
      { root: container, threshold: 0.1 }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [hasMore, loadingMore, loading, days.length, startDate, endDate, fetchPage]);

  async function costsRebuild() {
    setRebuilding(true);
    try {
      await fetch('/api/costs/rebuild', { method: 'POST' });
      setTimeout(() => fetchPage(startDate, endDate, 0, false), 3000);
    } catch (e) {
      console.warn('Rebuild:', e);
    } finally {
      setTimeout(() => setRebuilding(false), 3500);
    }
  }

  function toggleMonth(mk: string) {
    setExpanded((prev) => ({ ...prev, [mk]: !prev[mk] }));
  }

  // YTD summary from loaded data
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

      {/* Date filters */}
      <div className="costs-filters">
        <label className="costs-filter-label">
          Start
          <input type="date" className="costs-date-input" value={startDate}
            onChange={(e) => setStartDate(e.target.value)} />
        </label>
        <label className="costs-filter-label">
          End
          <input type="date" className="costs-date-input" value={endDate}
            onChange={(e) => setEndDate(e.target.value)} />
        </label>
        <span className="costs-total-badge">{total} days</span>
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
      <div className="costs-scroll" id="costs-list" ref={scrollRef}>
        {loading ? (
          <div className="costs-empty">Loading&hellip;</div>
        ) : days.length === 0 ? (
          <div className="costs-empty">No cost data for this range.</div>
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
              const isCollapsed = !expanded[mk];

              return (
                <span key={mk}>
                  <div className={`costs-month-header ${isCollapsed ? 'collapsed' : ''}`}
                       onClick={() => toggleMonth(mk)}>
                    <div className="cr-date costs-month-label">
                      <span className="costs-chevron">{isCollapsed ? '\u25b6' : '\u25bc'}</span>
                      {monthLabel}
                    </div>
                    <div className="cr-pkwh" />
                    <div className="cr-pcost cr-period-on">{fmtNet(mOn)}</div>
                    <div className="cr-pkwh" />
                    <div className="cr-pcost cr-period-off">{fmtNet(mOff)}</div>
                    <div className="cr-pkwh" />
                    <div className="cr-pcost cr-period-super">{fmtNet(mSuper)}</div>
                    <div className="cr-net" style={{ color: netColor }}>{fmtNet(mNet)}</div>
                  </div>
                  {!isCollapsed && mDays.map((d) => {
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
            {/* Sentinel for infinite scroll */}
            <div ref={sentinelRef} className="costs-sentinel">
              {loadingMore && <span className="costs-loading-more">Loading more&hellip;</span>}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
