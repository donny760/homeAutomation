import { useEffect, useRef } from 'react';

/**
 * Run `fn` immediately and then every `intervalMs` while the tab is visible.
 * Pauses on `visibilitychange` when hidden; resumes (with an immediate run)
 * when visible. Optional `enabled` flag turns the whole thing off (e.g. when
 * a parent isActive=false).
 *
 * Caller is responsible for using a stable `fn` (wrap in useCallback) or
 * accepting that an unstable fn restarts the interval.
 */
export function usePolling(
  fn: () => void | Promise<void>,
  intervalMs: number,
  enabled: boolean = true,
): void {
  const fnRef = useRef(fn);
  fnRef.current = fn;

  useEffect(() => {
    if (!enabled) return;
    let id: ReturnType<typeof setInterval> | null = null;
    let cancelled = false;

    const tick = () => {
      if (cancelled) return;
      try {
        const r = fnRef.current();
        if (r && typeof (r as Promise<void>).catch === 'function') {
          (r as Promise<void>).catch(() => {});
        }
      } catch {
        /* swallow — caller logs */
      }
    };

    const start = () => {
      if (id !== null) return;
      tick();
      id = setInterval(tick, intervalMs);
    };
    const stop = () => {
      if (id !== null) {
        clearInterval(id);
        id = null;
      }
    };
    const onVisibility = () => {
      if (document.hidden) stop();
      else start();
    };

    if (!document.hidden) start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      cancelled = true;
      stop();
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [intervalMs, enabled]);
}
