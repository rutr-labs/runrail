import { useEffect, useRef, useState } from 'react';

/** Ticks every second while `active`, so live durations count up. */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  return now;
}


/** Every poll in the app, on one rule: it runs only while `active` AND the
 *  tab is visible, and fires once on the way back from hidden. A window left
 *  open on another desktop must not spend a request every few seconds on a
 *  page nobody is looking at, and must not be showing yesterday when it is
 *  looked at again. Matches the notification bell and the approvals poll. */
export function useVisibleInterval(active: boolean, tick: () => void, intervalMs: number) {
  // Keep the latest callback in a ref so a new `tick` identity every render
  // (common — callers define it inline, and a 1s useNow ticker re-renders while
  // a run is live) does not tear down and recreate the interval before it ever
  // fires. Without this the poll silently never runs and live views go stale.
  const saved = useRef(tick);
  saved.current = tick;
  useEffect(() => {
    if (!active) return;
    let timer = 0;
    const start = () => { timer = window.setInterval(() => saved.current(), intervalMs); };
    const onVisibility = () => {
      window.clearInterval(timer);
      if (document.hidden) return;
      saved.current();
      start();
    };
    if (!document.hidden) start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [active, intervalMs]);
}
