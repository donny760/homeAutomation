'use client';

import { useState, useEffect, useRef, useCallback } from 'react';

interface EventItem {
  ts: number;
  system: string;
  title: string;
  result?: string;
  event_type?: string;
  detail?: string;
}

function extractUrl(detail?: string): string | null {
  if (!detail) return null;
  const m = detail.match(/https?:\/\/\S+/);
  return m ? m[0] : null;
}

const SMART_SWITCH = { icon: '\ud83d\udd0c', label: 'Smart Switch', color: 'var(--gray)' };
const SDGE         = { icon: '\ud83d\udcb2', label: 'SDG&E',        color: 'var(--amber)' };

const SYSTEM_META: Record<string, { icon: string; label: string; color: string }> = {
  powerwall: { icon: '\u26a1', label: 'Powerwall', color: 'var(--amber)' },
  rachio: { icon: '\ud83c\udf3f', label: 'Rachio', color: 'var(--rachio)' },
  abode: { icon: '\ud83d\udd12', label: 'Abode', color: 'var(--abode)' },
  pool: { icon: '\ud83c\udfca', label: 'Pool', color: 'var(--pool)' },
  myq: { icon: '\ud83d\ude97', label: 'MyQ', color: 'var(--gray)' },
  nest: { icon: '\ud83d\udcf7', label: 'Cameras', color: 'var(--nest)' },
  system: { icon: '\u2699\ufe0f', label: 'System', color: 'var(--gray)' },
  home_control: { icon: '\ud83c\udfe0', label: 'Home Control', color: 'var(--purple)' },
  kasa: SMART_SWITCH,
  tuya: SMART_SWITCH,
  rates: SDGE,
  holidays: SDGE,
};

const FILTERS: { key: string; label: string; systems?: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'powerwall', label: '\u26a1 Powerwall', systems: 'powerwall' },
  { key: 'home_control', label: '\ud83c\udfe0 Home Control', systems: 'home_control' },
  { key: 'smart_switch', label: '\ud83d\udd0c Smart Switch', systems: 'kasa,tuya' },
  { key: 'rachio', label: '\ud83c\udf3f Rachio/Sprinklers', systems: 'rachio' },
  { key: 'abode', label: '\ud83d\udd12 Abode', systems: 'abode' },
  { key: 'pool', label: '\ud83c\udfca Pool', systems: 'pool' },
  { key: 'nest', label: '\ud83d\udcf7 Cameras', systems: 'nest' },
  { key: 'sdge', label: '\ud83d\udcb2 SDG&E', systems: 'rates,holidays' },
  { key: 'system', label: '\u2699\ufe0f System', systems: 'system' },
  { key: 'errors', label: 'Errors' },
];

const PAGE_SIZE = 50;

function isErrorEvent(e: EventItem): boolean {
  return e.result === 'failed' || e.event_type === 'error';
}

function toDateStr(d: Date): string {
  return d.toISOString().slice(0, 10);
}

function dateToUnix(dateStr: string, endOfDay: boolean): number {
  const d = new Date(dateStr + (endOfDay ? 'T23:59:59' : 'T00:00:00'));
  return Math.floor(d.getTime() / 1000);
}

interface EventLogProps {
  isActive: boolean;
}

export default function EventLog({ isActive }: EventLogProps) {
  const today = toDateStr(new Date());
  const weekAgo = toDateStr(new Date(Date.now() - 7 * 86400_000));

  const [events, setEvents] = useState<EventItem[]>([]);
  const [filter, setFilter] = useState('all');
  const [updated, setUpdated] = useState('');
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [hasMore, setHasMore] = useState(true);
  const [startDate, setStartDate] = useState(weekAgo);
  const [endDate, setEndDate] = useState(today);

  const offsetRef = useRef(0);
  const listRef = useRef<HTMLDivElement>(null);
  const fetchingRef = useRef(false);

  const buildUrl = useCallback(
    (offset: number, filterKey: string) => {
      const params = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String(offset),
        start: String(dateToUnix(startDate, false)),
        end: String(dateToUnix(endDate, true)),
      });
      if (filterKey === 'errors') {
        params.set('system', 'errors');
      } else if (filterKey !== 'all') {
        const f = FILTERS.find((x) => x.key === filterKey);
        if (f?.systems) params.set('system', f.systems);
      }
      return `/api/events?${params}`;
    },
    [startDate, endDate],
  );

  const fetchEvents = useCallback(
    async (reset: boolean, currentFilter: string) => {
      if (fetchingRef.current) return;
      fetchingRef.current = true;

      const offset = reset ? 0 : offsetRef.current;
      if (reset) setLoading(true);
      else setLoadingMore(true);

      try {
        const data = await fetch(buildUrl(offset, currentFilter)).then((r) => r.json());
        const newEvents: EventItem[] = data.events;
        setHasMore(data.has_more);
        offsetRef.current = offset + newEvents.length;

        if (reset) {
          setEvents(newEvents);
        } else {
          setEvents((prev) => [...prev, ...newEvents]);
        }
        setUpdated('updated ' + new Date().toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' }));
      } catch (e) {
        console.warn('Events:', e);
      } finally {
        setLoading(false);
        setLoadingMore(false);
        fetchingRef.current = false;
      }
    },
    [buildUrl],
  );

  // Initial load + refresh on filter/date change
  useEffect(() => {
    if (!isActive) return;
    fetchEvents(true, filter);
  }, [isActive, filter, startDate, endDate, fetchEvents]);

  // Scroll handler for infinite scroll
  useEffect(() => {
    const el = listRef.current;
    if (!el) return;

    function onScroll() {
      if (!el) return;
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 100;
      if (nearBottom && !fetchingRef.current && hasMore) {
        fetchEvents(false, filter);
      }
    }

    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, [hasMore, filter, fetchEvents]);

  const filtered = events;

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
        <div className="events-date-range">
          <input
            type="date"
            value={startDate}
            max={endDate}
            onChange={(e) => setStartDate(e.target.value)}
          />
          <span className="events-date-sep">&ndash;</span>
          <input
            type="date"
            value={endDate}
            min={startDate}
            max={today}
            onChange={(e) => setEndDate(e.target.value)}
          />
        </div>
        <div className="events-updated">{updated}</div>
      </div>
      <div className="events-card" id="events-list" ref={listRef}>
        {loading ? (
          <div className="events-empty">Loading&hellip;</div>
        ) : filtered.length === 0 ? (
          <div className="events-empty">No events found.</div>
        ) : (
          <>
            {filtered.map((e, i) => {
              const d = new Date(e.ts * 1000);
              const dateKey = d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
              const showDivider = dateKey !== lastDate;
              if (showDivider) lastDate = dateKey;
              const meta = SYSTEM_META[e.system] || { icon: '?', label: e.system, color: 'var(--dim)' };
              const timeStr = d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
              const isErr = isErrorEvent(e);
              const url = extractUrl(e.detail);
              const rowClass = `event-row${isErr ? ' error' : ''}${url ? ' linkable' : ''}`;
              const onRowClick = url
                ? () => window.open(url, '_blank', 'noopener,noreferrer')
                : undefined;
              return (
                <span key={i}>
                  {showDivider && (
                    <div className="event-date-divider">
                      <span>{dateKey}</span>
                    </div>
                  )}
                  <div className={rowClass} data-system={e.system} onClick={onRowClick}>
                    <div className="event-accent" style={{ background: isErr ? '#e05252' : meta.color }} />
                    <div className="event-ts">{timeStr}</div>
                    <div className="event-system" style={{ color: meta.color }}>
                      {meta.icon} {meta.label}
                    </div>
                    <div className="event-title">
                      {e.title}
                      {url && <span className="event-link-icon">{'\u{1F517}'}</span>}
                    </div>
                  </div>
                </span>
              );
            })}
            {loadingMore && (
              <div className="events-loading-more">Loading more&hellip;</div>
            )}
            {!hasMore && filtered.length > 0 && (
              <div className="events-loading-more">All events loaded</div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
