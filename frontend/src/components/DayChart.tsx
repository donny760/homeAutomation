'use client';

import { useEffect, useRef, useCallback } from 'react';
import { Chart, registerables } from 'chart.js';
import 'chartjs-adapter-date-fns';
import { fmtW } from '@/lib/format';

Chart.register(...registerables);

export default function DayChart() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const chartRef = useRef<Chart | null>(null);

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

  useEffect(() => {
    async function refreshChart() {
      try {
        const rows = await fetch('/api/today').then((r) => r.json());
        const chart = chartRef.current;
        if (!chart) return;
        chart.data.datasets[0].data = rows.map((r: any) => ({ x: r.ts * 1000, y: Math.max(0, r.solar_w) }));
        chart.data.datasets[1].data = rows.map((r: any) => ({ x: r.ts * 1000, y: Math.max(0, r.home_w) }));
        chart.update('none');
      } catch (e) {
        console.warn('Chart:', e);
      }
    }

    refreshChart();

    // chart interval set by parent via settings; use default 60s here, parent overrides via ref
    const id = setInterval(refreshChart, 60_000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="card chart-card">
      <div className="chart-header">
        <div className="chart-title">Solar vs Home Load &mdash; Today</div>
        <div className="chart-legend">
          <div className="legend-item">
            <div className="legend-dot" style={{ background: '#EF9F27' }} />
            Solar
          </div>
          <div className="legend-item">
            <div className="legend-dot" style={{ background: '#378ADD' }} />
            Home
          </div>
        </div>
      </div>
      <div className="chart-wrap">
        <canvas ref={canvasRef} id="day-chart" />
      </div>
    </div>
  );
}
