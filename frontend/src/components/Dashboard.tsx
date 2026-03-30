'use client';

import { useState, useEffect } from 'react';
import PowerflowSVG, { LiveData } from './PowerflowSVG';
import DayChart from './DayChart';
import BottomTiles from './BottomTiles';
import AutomationsPanel from './AutomationsPanel';
import { DAYS_LBL, MONTHS_LBL } from '@/lib/format';

function Topbar() {
  const [clock, setClock] = useState('--:--');
  const [dateStr, setDateStr] = useState('---');
  const [wx, setWx] = useState<{ temp: string; desc: string; fcst: string }>({
    temp: '--\u00b0F',
    desc: '--',
    fcst: 'Tomorrow: --',
  });

  useEffect(() => {
    function tick() {
      const n = new Date();
      const h = n.getHours(),
        m = n.getMinutes();
      const ampm = h >= 12 ? 'PM' : 'AM';
      const h12 = h % 12 || 12;
      setClock(`${h12}:${String(m).padStart(2, '0')} ${ampm}`);
      setDateStr(`${DAYS_LBL[n.getDay()]}, ${MONTHS_LBL[n.getMonth()]} ${n.getDate()}`);
    }
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);

  useEffect(() => {
    async function refreshWeather() {
      try {
        const w = await fetch('/api/weather').then((r) => r.json());
        const temp = w.temp_f != null ? w.temp_f + '\u00b0F' : '--\u00b0F';
        const desc = w.desc || '--';
        let fcst = 'Tomorrow: --';
        if (w.tomorrow_cloud != null) {
          const cl = Math.round(w.tomorrow_cloud);
          const rain = w.tomorrow_rain != null ? `, ${w.tomorrow_rain.toFixed(1)}mm rain` : '';
          const warn = w.bad_forecast ? '  !' : '';
          fcst = `Tomorrow: ${cl}% clouds${rain}${warn}`;
        }
        setWx({ temp, desc, fcst });
      } catch (e) {
        console.warn('Weather:', e);
      }
    }
    refreshWeather();
    const id = setInterval(refreshWeather, 600_000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="topbar">
      <div className="topbar-left">
        <div className="clock">{clock}</div>
        <div className="date-str">{dateStr}</div>
      </div>
      <div className="topbar-right">
        <div className="wx-temp">{wx.temp}</div>
        <div className="wx-desc">{wx.desc}</div>
        <div className="wx-fcst">{wx.fcst}</div>
      </div>
    </div>
  );
}

export default function Dashboard() {
  const [liveData, setLiveData] = useState<LiveData | null>(null);

  useEffect(() => {
    async function poll() {
      try {
        const d = await fetch('/api/live').then((r) => r.json());
        setLiveData(d);
      } catch (e) {
        console.warn('Poll:', e);
      }
    }
    poll();
    const id = setInterval(poll, 10_000);
    return () => clearInterval(id);
  }, []);

  return (
    <div id="page-dashboard" className="page active">
      <Topbar />
      <div className="main-row">
        <div className="flow-col">
          <PowerflowSVG data={liveData} />
        </div>
        <AutomationsPanel />
      </div>
      <DayChart />
      <BottomTiles />
    </div>
  );
}
