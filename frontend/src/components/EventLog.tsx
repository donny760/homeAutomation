'use client';

import { useState, useEffect, useRef } from 'react';

interface EventItem {
  ts: number;
  system: string;
  title: string;
  result?: string;
  event_type?: string;
}

const SYSTEM_META: Record<string, { icon: string; label: string; color: string }> = {
  powerwall: { icon: '\u26a1', label: 'Powerwall', color: 'var(--amber)' },
  rachio: { icon: '\ud83c\udf3f', label: 'Rachio', color: 'var(--rachio)' },
  abode: { icon: '\ud83d\udd12', label: 'Abode', color: 'var(--abode)' },
  pool: { icon: '\ud83c\udfca', label: 'Pool', color: 'var(--pool)' },
  myq: { icon: '\ud83d\ude97', label: 'MyQ', color: 'var(--gray)' },
};

const FILTERS: { key: string; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'powerwall', label: '\u26a1 Powerwall' },
  { key: 'rachio', label: '\ud83c\udf3f Rachio/Sprinklers' },
  { key: 'abode', label: '\ud83d\udd12 Abode' },
  { key: 'pool', label: '\ud83c\udfca Pool' },
  { key: 'errors', label: 'Errors' },
];

function isErrorEvent(e: EventItem): boolean {
  return e.result === 'failed' || e.event_type === 'error';
}

interface EventLogProps {
  isActive: boolean;
}

export default function EventLog({ isActive }: EventLogProps) {
  const [events, setEvents] = useState<EventItem[]>([]);
  const [filter, setFilter] = useState('all');
  const [updated, setUpdated] = useState('');
  const [loading, setLoading] = useState(true);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    async function refresh() {
      try {
        const data = await fetch('/api/events?limit=200&days=7').then((r) => r.json());
        setEvents(data);
        setUpdated('updated ' + new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }));
        setLoading(false);
      } catch (e) {
        console.warn('Events:', e);
      }
    }

    if (isActive) {
      refresh();
      intervalRef.current = setInterval(refresh, 60_000);
    }

    return () => {
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, [isActive]);

  const filtered =
    filter === 'all'
      ? events
      : filter === 'errors'
        ? events.filter((e) => isErrorEvent(e))
        : events.filter((e) => e.system === filter);

  let lastDate = '';

  return (
    <div id="page-events" className="page active">
      <div className="events-toolbar">
        <div className="page-title">Event Log</div>
        <div className="events-toolbar-center">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              className={`evf-btn${filter === f.key ? ' active' : ''}`}
              data-system={f.key}
              onClick={() => setFilter(f.key)}
            >
              {f.label}
            </button>
          ))}
        </div>
        <div className="events-updated">{updated}</div>
      </div>
      <div className="events-card" id="events-list">
        {loading ? (
          <div className="events-empty">Loading&hellip;</div>
        ) : filtered.length === 0 ? (
          <div className="events-empty">No events found.</div>
        ) : (
          filtered.map((e, i) => {
            const d = new Date(e.ts * 1000);
            const dateKey = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
            const showDivider = dateKey !== lastDate;
            if (showDivider) lastDate = dateKey;
            const meta = SYSTEM_META[e.system] || { icon: '?', label: e.system, color: 'var(--dim)' };
            const timeStr = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
            const isErr = isErrorEvent(e);
            return (
              <span key={i}>
                {showDivider && (
                  <div className="event-date-divider">
                    <span>{dateKey}</span>
                  </div>
                )}
                <div className={`event-row${isErr ? ' error' : ''}`} data-system={e.system}>
                  <div className="event-accent" style={{ background: isErr ? '#e05252' : meta.color }} />
                  <div className="event-ts">{timeStr}</div>
                  <div className="event-system" style={{ color: meta.color }}>
                    {meta.icon} {meta.label}
                  </div>
                  <div className="event-title">{e.title}</div>
                </div>
              </span>
            );
          })
        )}
      </div>
    </div>
  );
}
