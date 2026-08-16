'use client';

import { useState, useCallback } from 'react';
import { fmtFireTime, settingsBadges } from '@/lib/format';
import { usePolling } from '@/lib/usePolling';

interface ScheduleEntry {
  fire_time: string;
  name: string;
  source?: string;
  skip?: boolean;
  skip_reason?: string;
  duration_min?: number;
  mode?: string | null;
  reserve?: number | null;
  grid_charging?: boolean | null;
  grid_export?: string | null;
  rule_id?: number;
  enabled?: boolean;
  pinned?: boolean;
}

export default function AutomationsPanel() {
  const [items, setItems] = useState<ScheduleEntry[]>([]);
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const data = await fetch('/api/schedule').then((r) => r.json());
      const all: ScheduleEntry[] = data.schedule || [];
      // Paused rules are pinned: they survive the top-5 cut so they can always be resumed.
      const pinned = all.filter((e) => e.pinned);
      const upcoming = all.filter((e) => !e.pinned).slice(0, 5);
      setItems([...upcoming, ...pinned]);
      setLoading(false);
    } catch (e) {
      console.warn('Automations:', e);
    }
  }, []);
  usePolling(refresh, 60_000);

  async function toggleRule(ruleId: number) {
    try {
      await fetch(`/api/rules/${ruleId}/toggle`, { method: 'PUT' });
      refresh();
    } catch (e) {
      console.warn('Toggle rule:', e);
    }
  }

  return (
    <div className="automations-col">
      <div className="auto-card card">
        <div className="auto-header">
          <div className="auto-title">Upcoming Automations</div>
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
              const isPaused = e.rule_id != null && e.enabled === false;
              const nextUp = i === 0 && !isSkip && !isPaused ? 'next-up' : '';
              const racchioCls = isRachio ? 'rachio' : '';
              const skipCls = isSkip ? 'rain-delay' : '';

              let badges: { text: string; cls: string }[] = [];
              if (isSkip) {
                // Skip annotation rendered inline with the name; no badge needed
              } else if (isRachio) {
                if (e.duration_min) badges = [{ text: `${e.duration_min} min`, cls: 'rachio' }];
              } else {
                badges = settingsBadges(e);
              }

              const skipSuffix = isSkip
                ? ` (${e.skip_reason || 'Skipped due to Rain'})`
                : '';

              return (
                <div
                  key={i}
                  className={`auto-row ${nextUp} ${racchioCls} ${skipCls}`.trim()}
                  style={isPaused ? { opacity: 0.45 } : undefined}
                >
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: '8px' }}>
                    <div className="auto-time">{fmtFireTime(e.fire_time)}</div>
                    <div className="auto-source">{isRachio ? 'Rachio/Sprinklers' : 'Powerwall'}</div>
                    {isPaused && (
                      <span style={{
                        fontSize: '10px', fontWeight: 600, letterSpacing: '0.05em',
                        color: '#9e9c96', background: '#2a2a2c', borderRadius: '4px',
                        padding: '1px 5px', lineHeight: '16px',
                      }}>PAUSED</span>
                    )}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' }}>
                    <div className="auto-name" style={{ flex: 1 }}>
                      {e.name}
                      {skipSuffix && <span className="skip-reason">{skipSuffix}</span>}
                    </div>
                    {e.rule_id != null && (
                      <button
                        onClick={() => toggleRule(e.rule_id!)}
                        title={isPaused ? 'Resume rule' : 'Pause rule'}
                        style={{
                          background: 'none', border: '1px solid #444',
                          borderRadius: '4px', color: '#9e9c96',
                          cursor: 'pointer', fontSize: '12px',
                          padding: '2px 7px', flexShrink: 0,
                          lineHeight: '18px',
                        }}
                      >
                        {isPaused ? '▶' : '⏸'}
                      </button>
                    )}
                  </div>
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
