'use client';

import { useEffect, useRef, useCallback, useState } from 'react';
import { Chart, registerables } from 'chart.js';
import 'chartjs-adapter-date-fns';
import { fmtW } from '@/lib/format';

Chart.register(...registerables);

export default function DayChart() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart<'line'> | null>(null);
  const solarDataRef = useRef<{ x: number; y: number }[]>([]);
  const homeDataRef = useRef<{ x: number; y: number }[]>([]);
  const forecastRef = useRef<{ x: number; y: number }[]>([]);
  const visibilityRef = useRef([true, true, true]);
  const [visible, setVisible] = useState([true, true, true]);

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

  const dataRefs = [solarDataRef, homeDataRef, forecastRef];

  const toggleDataset = useCallback((index: number) => {
    setVisible((prev) => {
      const next = [...prev];
      next[index] = !next[index];
      visibilityRef.current = next;
      const chart = chartRef.current;
      if (chart) {
        chart.data.datasets[index].data = next[index] ? dataRefs[index].current : [];
        chart.update('none');
      }
      return next;
    });
  }, []);

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
            mode: 'index',
            intersect: false,
            backgroundColor: '#222224',
            borderColor: '#333336',
            borderWidth: 1,
            titleColor: '#9e9c96',
            bodyColor: '#eeece8',
            filter: (item) => item.parsed.y != null && item.parsed.y > 0,
            callbacks: {
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

    return () => {
      window.removeEventListener('themechange', onTheme);
      chart.destroy();
    };
  }, [syncChartTheme]);

  // Refresh actual Solar + Home data every 60s
  useEffect(() => {
    async function refreshChart() {
      try {
        const rows = await fetch('/api/today').then((r) => r.json());
        const chart = chartRef.current;
        if (!chart) return;
        solarDataRef.current = rows.filter((r: any) => r.solar_w > 0).map((r: any) => ({ x: r.ts * 1000, y: r.solar_w }));
        homeDataRef.current = rows.map((r: any) => ({ x: r.ts * 1000, y: Math.max(0, r.home_w) }));
        if (visibilityRef.current[0]) chart.data.datasets[0].data = solarDataRef.current;
        if (visibilityRef.current[1]) chart.data.datasets[1].data = homeDataRef.current;
        chart.update('none');
      } catch (e) {
        console.warn('Chart:', e);
      }
    }

    refreshChart();
    const id = setInterval(refreshChart, 60_000);
    return () => clearInterval(id);
  }, []);

  // Refresh Solar Forecast every 60 min
  useEffect(() => {
    async function refreshForecast() {
      try {
        const points = await fetch('/api/solar-forecast').then((r) => r.json());
        const chart = chartRef.current;
        if (!chart) return;
        forecastRef.current = points.map((p: any) => ({ x: p.ts * 1000, y: p.solar_w }));
        if (visibilityRef.current[2]) {
          chart.data.datasets[2].data = forecastRef.current;
          chart.update('none');
        }
      } catch (e) {
        console.warn('Solar forecast:', e);
      }
    }

    refreshForecast();
    const id = setInterval(refreshForecast, 3_600_000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="card chart-card">
      <div className="chart-header">
        <div className="chart-title">Solar vs Home Load &mdash; Today</div>
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
          <div
            className={`legend-item${visible[2] ? '' : ' legend-inactive'}`}
            onClick={() => toggleDataset(2)}
          >
            <div className="legend-dash" style={{ borderColor: 'rgba(239,159,39,0.45)' }} />
            Forecast
          </div>
        </div>
      </div>
      <div className="chart-wrap">
        <canvas ref={canvasRef} id="day-chart" />
      </div>
    </div>
  );
}
