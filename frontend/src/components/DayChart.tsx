'use client';

import { useEffect, useRef, useCallback, useState } from 'react';
import { Chart, registerables } from 'chart.js';
import 'chartjs-adapter-date-fns';
import { fmtW } from '@/lib/format';
import { usePolling } from '@/lib/usePolling';

Chart.register(...registerables);

function isoDate(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  return `${y}-${m}-${day}`;
}

function dayLabel(viewedDate: Date): string {
  const today = isoDate(new Date());
  if (isoDate(viewedDate) === today) return 'Today';
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);
  if (isoDate(viewedDate) === isoDate(yesterday)) return 'Yesterday';
  return viewedDate.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

export default function DayChart() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart<'line'> | null>(null);
  const solarDataRef = useRef<{ x: number; y: number }[]>([]);
  const homeDataRef = useRef<{ x: number; y: number }[]>([]);
  const forecastRef = useRef<{ x: number; y: number }[]>([]);
  const gridDataRef = useRef<{ x: number; y: number }[]>([]);
  const visibilityRef = useRef([true, true, true, true]);
  const [visible, setVisible] = useState([true, true, true, true]);
  const [viewedDate, setViewedDate] = useState<Date>(() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return d;
  });

  const isToday = isoDate(viewedDate) === isoDate(new Date());

  const syncChartTheme = useCallback((light: boolean) => {
    const chart = chartRef.current;
    if (!chart) return;
    const gl = light ? 'transparent' : '#222226';
    const tc = light ? '#6c6c70' : '#9e9c96';
    const bc = light ? '#d8d8da' : '#333336';
    const xScale = chart.options.scales!.x!;
    const yScale = chart.options.scales!.y!;
    (xScale as any).grid.color = gl;
    (yScale as any).grid.color = gl;
    (xScale as any).ticks.color = tc;
    (yScale as any).ticks.color = tc;
    (xScale as any).border.color = bc;
    (yScale as any).border.color = bc;
    chart.update('none');
  }, []);

  // Refs holding refs — stable identity across renders so the toggleDataset
  // closure (empty deps) always sees the latest dataset payloads.
  const dataRefs = useRef([solarDataRef, homeDataRef, forecastRef, gridDataRef]);

  const toggleDataset = useCallback((index: number) => {
    setVisible((prev) => {
      const next = [...prev];
      next[index] = !next[index];
      visibilityRef.current = next;
      const chart = chartRef.current;
      if (chart) {
        chart.data.datasets[index].data = next[index] ? dataRefs.current[index].current : [];
        chart.update('none');
      }
      return next;
    });
  }, []);

  function shiftDay(delta: number) {
    setViewedDate((d) => {
      const next = new Date(d);
      next.setDate(next.getDate() + delta);
      next.setHours(0, 0, 0, 0);
      // Don't allow forward past today
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      if (next > today) return d;
      return next;
    });
  }

  useEffect(() => {
    if (!canvasRef.current) return;
    const ctx = canvasRef.current.getContext('2d')!;

    const chart = new Chart(ctx, {
      type: 'line',
      data: {
        datasets: [
          {
            label: 'Solar',
            data: [],
            borderColor: '#EF9F27',
            backgroundColor: 'rgba(239,159,39,0.07)',
            fill: true,
            tension: 0.35,
            pointRadius: 0,
            borderWidth: 2,
          },
          {
            label: 'Home',
            data: [],
            borderColor: '#378ADD',
            backgroundColor: 'rgba(55,138,221,0.07)',
            fill: true,
            tension: 0.35,
            pointRadius: 0,
            borderWidth: 2,
          },
          {
            label: 'Solar Forecast',
            data: [],
            borderColor: 'rgba(239,159,39,0.45)',
            backgroundColor: 'transparent',
            fill: false,
            tension: 0.35,
            pointRadius: 0,
            borderWidth: 1.5,
            borderDash: [6, 4],
          },
          {
            label: 'Grid',
            data: [],
            borderColor: '#52C27A',
            backgroundColor: 'rgba(82,194,122,0.07)',
            fill: true,
            tension: 0,
            pointRadius: 0,
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        scales: {
          x: {
            type: 'time',
            time: { unit: 'hour', displayFormats: { hour: 'h a' } },
            grid: { color: '#222226' },
            ticks: { color: '#9e9c96', maxTicksLimit: 12, font: { size: 13 } },
            border: { color: '#333336' },
          },
          y: {
            beginAtZero: true,
            grid: { color: '#222226' },
            ticks: {
              color: '#9e9c96',
              font: { size: 13 },
              callback: (v) => (Number(v) >= 1000 ? (Number(v) / 1000).toFixed(1) + 'k' : v),
            },
            border: { color: '#333336' },
          },
        },
        plugins: {
          legend: { display: false },
          tooltip: {
            mode: 'nearest',
            axis: 'x',
            intersect: false,
            backgroundColor: '#222224',
            borderColor: '#333336',
            borderWidth: 1,
            titleColor: '#9e9c96',
            bodyColor: '#eeece8',
            filter: (item) => item.parsed.y != null && item.parsed.y > 0,
            callbacks: {
              title: (items) => {
                if (!items.length || items[0].parsed.x == null) return '';
                const d = new Date(items[0].parsed.x);
                return d.toLocaleString([], {
                  month: 'short', day: 'numeric',
                  hour: 'numeric', minute: '2-digit', second: '2-digit',
                  hour12: true,
                });
              },
              label: (ctx) => ` ${ctx.dataset.label}: ${fmtW(ctx.parsed.y ?? 0)}`,
            },
          },
        },
      },
    });

    chartRef.current = chart;
    syncChartTheme(document.body.classList.contains('light'));

    const onTheme = (e: Event) => {
      syncChartTheme((e as CustomEvent).detail.light);
    };
    window.addEventListener('themechange', onTheme);

    const wrap = canvasRef.current.parentElement;
    let ro: ResizeObserver | null = null;
    if (wrap && typeof ResizeObserver !== 'undefined') {
      ro = new ResizeObserver(() => {
        chart.resize();
      });
      ro.observe(wrap);
    }

    return () => {
      window.removeEventListener('themechange', onTheme);
      if (ro) ro.disconnect();
      chart.destroy();
    };
  }, [syncChartTheme]);

  // Fetch chart data — lifted so usePolling can reference it
  const chartAbortRef = useRef<AbortController | null>(null);
  const refreshChart = useCallback(async () => {
    chartAbortRef.current?.abort();
    const ctrl = new AbortController();
    chartAbortRef.current = ctrl;
    try {
      const url = isToday ? '/api/today' : `/api/day?date=${isoDate(viewedDate)}`;
      const rows = await fetch(url, { signal: ctrl.signal }).then((r) => r.json());
      const chart = chartRef.current;
      if (!chart) return;
      solarDataRef.current = rows.filter((r: any) => r.solar_w > 0).map((r: any) => ({ x: r.ts * 1000, y: r.solar_w }));
      homeDataRef.current = rows.map((r: any) => ({ x: r.ts * 1000, y: Math.max(0, r.home_w) }));
      gridDataRef.current = rows.map((r: any) => ({ x: r.ts * 1000, y: Math.max(0, r.grid_w ?? 0) }));
      if (visibilityRef.current[0]) chart.data.datasets[0].data = solarDataRef.current;
      if (visibilityRef.current[1]) chart.data.datasets[1].data = homeDataRef.current;
      if (visibilityRef.current[3]) chart.data.datasets[3].data = gridDataRef.current;
      chart.update('none');
    } catch (e) {
      if ((e as Error).name !== 'AbortError') console.warn('Chart:', e);
    }
  }, [isToday, viewedDate]);

  // Initial fetch + refetch whenever date or isToday changes
  useEffect(() => {
    refreshChart();
    return () => { chartAbortRef.current?.abort(); };
  }, [refreshChart]);

  // Recurring 60s poll — paused for historical dates and hidden tabs
  usePolling(refreshChart, 60_000, isToday);

  // Solar Forecast — only meaningful for today
  const refreshForecast = useCallback(async () => {
    try {
      const points = await fetch('/api/solar-forecast').then((r) => r.json());
      const c = chartRef.current;
      if (!c) return;
      forecastRef.current = points.map((p: any) => ({ x: p.ts * 1000, y: p.solar_w }));
      if (visibilityRef.current[2]) {
        c.data.datasets[2].data = forecastRef.current;
        c.update('none');
      }
    } catch (e) {
      console.warn('Solar forecast:', e);
    }
  }, []);

  // Clear forecast data when navigating to a historical date
  useEffect(() => {
    if (!isToday) {
      forecastRef.current = [];
      const c = chartRef.current;
      if (c) { c.data.datasets[2].data = []; c.update('none'); }
    }
  }, [isToday]);

  // Fetch + poll forecast — paused for historical dates and hidden tabs
  usePolling(refreshForecast, 3_600_000, isToday);

  return (
    <div className="card chart-card">
      <div className="chart-header">
        <div className="chart-title-wrap">
          <div className="chart-title">Power &mdash; {dayLabel(viewedDate)}</div>
          <div className="chart-nav">
            <button className="chart-nav-btn" onClick={() => shiftDay(-1)} aria-label="Previous day">&#9664;</button>
            <button className="chart-nav-btn" onClick={() => shiftDay(1)} disabled={isToday} aria-label="Next day">&#9654;</button>
          </div>
        </div>
        <div className="chart-legend">
          <div
            className={`legend-item${visible[0] ? '' : ' legend-inactive'}`}
            onClick={() => toggleDataset(0)}
          >
            <div className="legend-dot" style={{ background: '#EF9F27' }} />
            Solar
          </div>
          <div
            className={`legend-item${visible[1] ? '' : ' legend-inactive'}`}
            onClick={() => toggleDataset(1)}
          >
            <div className="legend-dot" style={{ background: '#378ADD' }} />
            Home
          </div>
          {isToday && (
            <div
              className={`legend-item${visible[2] ? '' : ' legend-inactive'}`}
              onClick={() => toggleDataset(2)}
            >
              <div className="legend-dash" style={{ borderColor: 'rgba(239,159,39,0.45)' }} />
              Forecast
            </div>
          )}
          <div
            className={`legend-item${visible[3] ? '' : ' legend-inactive'}`}
            onClick={() => toggleDataset(3)}
          >
            <div className="legend-dot" style={{ background: '#52C27A' }} />
            Grid
          </div>
        </div>
      </div>
      <div className="chart-wrap">
        <canvas ref={canvasRef} id="day-chart" />
      </div>
    </div>
  );
}
