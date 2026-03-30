'use client';

import { useState, useEffect } from 'react';
import { fmtFireTime, settingsBadges } from '@/lib/format';

interface ScheduleEntry {
  fire_time: string;
  name: string;
  source?: string;
  skip?: boolean;
  duration_min?: number;
  mode?: string | null;
  reserve?: number | null;
  grid_charging?: boolean | null;
  grid_export?: string | null;
}

export default function AutomationsPanel() {
  const [items, setItems] = useState<ScheduleEntry[]>([]);
  const [updated, setUpdated] = useState('');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function refresh() {
      try {
        const data = await fetch('/api/schedule').then((r) => r.json());
        setItems((data.schedule || []).slice(0, 5));
        setUpdated(new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }));
        setLoading(false);
      } catch (e) {
        console.warn('Automations:', e);
      }
    }
    refresh();
    const id = setInterval(refresh, 60_000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="automations-col">
      <div className="auto-card card">
        <div className="auto-header">
          <div className="auto-title">Upcoming Automations</div>
          <div className="auto-updated">{updated}</div>
        </div>
        <div className="auto-list">
          {loading ? (
            <div className="auto-empty">Loading&hellip;</div>
          ) : items.length === 0 ? (
            <div className="auto-empty">No upcoming automations.</div>
          ) : (
            items.map((e, i) => {
              const isRachio = e.source === 'rachio';
              const isSkip = !!e.skip;
              const nextUp = i === 0 && !isSkip ? 'next-up' : '';
              const racchioCls = isRachio ? 'rachio' : '';
              const skipCls = isSkip ? 'rain-delay' : '';

              let badges: { text: string; cls: string }[] = [];
              if (isSkip) {
                badges = [{ text: 'Rain Delay', cls: 'skip' }];
              } else if (isRachio) {
                if (e.duration_min) badges = [{ text: `${e.duration_min} min`, cls: 'rachio' }];
              } else {
                badges = settingsBadges(e);
              }

              return (
                <div key={i} className={`auto-row ${nextUp} ${racchioCls} ${skipCls}`.trim()}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: '8px' }}>
                    <div className="auto-time">{fmtFireTime(e.fire_time)}</div>
                    <div className="auto-source">{isRachio ? 'Rachio/Sprinklers' : 'Powerwall'}</div>
                  </div>
                  <div className="auto-name">{e.name}</div>
                  {badges.length > 0 && (
                    <div className="auto-badges">
                      {badges.map((b, j) => (
                        <span key={j} className={`sched-badge ${b.cls}`}>
                          {b.text}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      </div>
    </div>
  );
}
