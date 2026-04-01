'use client';

import { useEffect, useState } from 'react';
import { MONTHS_LBL } from '@/lib/format';

export type PageName = 'dashboard' | 'events' | 'rules' | 'costs' | 'settings';

interface NavProps {
  activePage: PageName;
  onPageChange: (page: PageName) => void;
}

const PAGES: { key: PageName; label: string; sparkle?: boolean }[] = [
  { key: 'dashboard', label: 'Dashboard' },
  { key: 'events', label: 'Event Log' },
  { key: 'rules', label: 'Powerwall Rules', sparkle: true },
  { key: 'costs', label: 'Energy Breakdown' },
  { key: 'settings', label: 'Settings' },
];

const WMO_ICON: Record<number, string> = {
  0: '\u2600',     // ☀ Clear
  1: '\uD83C\uDF24', // 🌤 Mainly Clear
  2: '\u26C5',     // ⛅ Partly Cloudy
  3: '\u2601',     // ☁ Overcast
  45: '\uD83C\uDF2B', // 🌫 Fog
  48: '\uD83C\uDF2B', // 🌫 Icy Fog
  51: '\uD83C\uDF26', // 🌦 Light Drizzle
  53: '\uD83C\uDF26', // 🌦 Drizzle
  55: '\uD83C\uDF26', // 🌦 Heavy Drizzle
  61: '\uD83C\uDF27', // 🌧 Light Rain
  63: '\uD83C\uDF27', // 🌧 Rain
  65: '\uD83C\uDF27', // 🌧 Heavy Rain
  71: '\u2744',     // ❄ Light Snow
  73: '\u2744',     // ❄ Snow
  75: '\u2744',     // ❄ Heavy Snow
  80: '\uD83C\uDF27', // 🌧 Rain Showers
  81: '\uD83C\uDF27', // 🌧 Showers
  82: '\uD83C\uDF27', // 🌧 Heavy Showers
  95: '\u26C8',     // ⛈ Thunderstorm
  96: '\u26C8',     // ⛈ Thunderstorm
  99: '\u26C8',     // ⛈ Thunderstorm
};

function aqiColor(aqi: number): string {
  if (aqi <= 50) return 'var(--green)';
  if (aqi <= 100) return 'var(--amber)';
  if (aqi <= 150) return '#FF8C00';
  return '#e05252';
}

export default function Nav({ activePage, onPageChange }: NavProps) {
  const [isLight, setIsLight] = useState(false);
  const [clock, setClock] = useState('');
  const [wx, setWx] = useState<{ icon: string; temp: string; aqi: number | null }>({
    icon: '',
    temp: '',
    aqi: null,
  });

  useEffect(() => {
    if (localStorage.getItem('theme') === 'light') {
      document.body.classList.add('light');
      setIsLight(true);
    }
  }, []);

  // Clock — update every 30s (no need for per-second in nav)
  useEffect(() => {
    function tick() {
      const n = new Date();
      const h = n.getHours(), m = n.getMinutes();
      const ampm = h >= 12 ? 'PM' : 'AM';
      const h12 = h % 12 || 12;
      const time = `${h12}:${String(m).padStart(2, '0')} ${ampm}`;
      const date = `${MONTHS_LBL[n.getMonth()]} ${n.getDate()}`;
      setClock(`${time} \u00b7 ${date}`);
    }
    tick();
    const id = setInterval(tick, 30_000);
    return () => clearInterval(id);
  }, []);

  // Weather + AQI
  useEffect(() => {
    async function refresh() {
      try {
        const w = await fetch('/api/weather').then((r) => r.json());
        const icon = WMO_ICON[w.weathercode] || '';
        const temp = w.temp_f != null ? `${w.temp_f}\u00b0F` : '';
        setWx({ icon, temp, aqi: w.aqi ?? null });
      } catch (e) {
        console.warn('Weather:', e);
      }
    }
    refresh();
    const id = setInterval(refresh, 600_000);
    return () => clearInterval(id);
  }, []);

  function toggleTheme() {
    const light = document.body.classList.toggle('light');
    localStorage.setItem('theme', light ? 'light' : 'dark');
    setIsLight(light);
    window.dispatchEvent(new CustomEvent('themechange', { detail: { light } }));
  }

  return (
    <nav className="nav">
      <div className="nav-links">
        {PAGES.map((p) => (
          <button
            key={p.key}
            className={`nav-link${activePage === p.key ? ' active' : ''}`}
            data-page={p.key}
            onClick={() => onPageChange(p.key)}
          >
            {p.sparkle && <span className="nav-sparkle">&#10022;</span>}{' '}
            {p.label}
          </button>
        ))}
      </div>
      <div className="nav-right">
        {clock && <span className="nav-time">{clock}</span>}
        {wx.temp && (
          <span className="nav-wx">
            <span className="nav-wx-icon">{wx.icon}</span>
            <span className="nav-wx-temp">{wx.temp}</span>
          </span>
        )}
        {wx.aqi != null && (
          <span className="nav-aqi" style={{ color: aqiColor(wx.aqi) }}>
            AQI {wx.aqi}
          </span>
        )}
        <button className="theme-btn" onClick={toggleTheme}>
          {isLight ? 'Dark' : 'Light'}
        </button>
      </div>
    </nav>
  );
}
