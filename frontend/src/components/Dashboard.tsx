'use client';

import { useState, useCallback } from 'react';
import PowerflowSVG, { LiveData } from './PowerflowSVG';
import DayChart from './DayChart';
import BottomTiles from './BottomTiles';
import AutomationsPanel from './AutomationsPanel';
import { usePolling } from '../lib/usePolling';

export default function Dashboard() {
  const [liveData, setLiveData] = useState<LiveData | null>(null);

  const poll = useCallback(async () => {
    try {
      const d = await fetch('/api/live').then((r) => r.json());
      setLiveData(d);
    } catch (e) {
      console.warn('Poll:', e);
    }
  }, []);
  usePolling(poll, 10_000);

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
