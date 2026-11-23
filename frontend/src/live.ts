/* ─── Live data ────────────────────────────────────────────
   How a page stays current: it reloads when the socket announces a change
   it shows, and polls only as a safety net — at its old pace while the
   socket is down, and once a minute while it is live, in case a change was
   made somewhere that does not announce itself. See ws.ts. */

import { useEffect, useRef, useState } from 'react';
import { rrws, type SocketState } from './ws';
import { useVisibleInterval } from './hooks';

/** While the socket is live a page still re-reads this often, for changes
 *  nothing announces (a row edited by hand, a clock-driven state). */
export const SAFETY_POLL_MS = 60_000;

/** The socket's state, re-rendering when it changes. */
export function useSocketState(): SocketState {
  const [state, setState] = useState<SocketState>(rrws.state);
  useEffect(() => {
    setState(rrws.state);
    return rrws.onState(setState);
  }, []);
  return state;
}

/** Calls `handler` when any of `types` is announced (and `filter` accepts
 *  it), and after the socket reconnects, when anything may have been missed.
 *  A burst of events is one call at its start and one when it settles, so a
 *  run finishing twenty tasks does not mean twenty reloads. */
export function useLiveEvents(
  types: readonly string[],
  handler: () => void,
  { filter, settleMs = 400 }: { filter?: (event: Record<string, unknown>) => boolean; settleMs?: number } = {},
): void {
  const saved = useRef(handler);
  saved.current = handler;
  const accept = useRef(filter);
  accept.current = filter;
  const key = types.join('|');
  useEffect(() => {
    let timer = 0;
    let last = 0;
    const fire = () => { last = Date.now(); saved.current(); };
    const onEvent = (event: Record<string, unknown>) => {
      if (event.type !== 'resync' && accept.current && !accept.current(event)) return;
      const since = Date.now() - last;
      window.clearTimeout(timer);
      if (since > settleMs) fire();
      else timer = window.setTimeout(fire, settleMs - since);
    };
    const offs = [...types, 'resync'].map(type => rrws.on(type, onEvent));
    return () => { offs.forEach(off => off()); window.clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, settleMs]);
}

/** A poll that steps back while the socket is live: every `fallbackMs` when
 *  it is not, every `safetyMs` when it is. Only while `active` and the tab is
 *  visible, like every poll here. */
export function useDataPoll(active: boolean, tick: () => void, fallbackMs: number, safetyMs = SAFETY_POLL_MS): void {
  const live = useSocketState() === 'live';
  useVisibleInterval(active, tick, live ? Math.max(fallbackMs, safetyMs) : fallbackMs);
}

/** The usual pair: reload on the events that matter, poll as a fallback. */
export function useLive(
  types: readonly string[],
  reload: () => void,
  { active = true, fallbackMs = 5000, filter }: { active?: boolean; fallbackMs?: number; filter?: (event: Record<string, unknown>) => boolean } = {},
): void {
  useLiveEvents(types, reload, { filter });
  useDataPoll(active, reload, fallbackMs);
}
