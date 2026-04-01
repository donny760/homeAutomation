'use client';

import { useState, useEffect } from 'react';
import PowerflowSVG, { LiveData } from './PowerflowSVG';
import DayChart from './DayChart';
import BottomTiles from './BottomTiles';
import AutomationsPanel from './AutomationsPanel';

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
