import { createContext, ReactNode, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { api } from './api';
import { rrws } from './ws';
import { useDataPoll } from './live';
import { useOpenApprovals } from './components/ApprovalGate';
import { serviceCounts, useServices } from './Services';
import { failingFlows, moodOf, newestFirst, unjudgedFailures, type Failing, type Mood } from './pulseModel';
import type { Run, Summary, Workflow } from './types';

/* ─── The pulse ────────────────────────────────────────────
   The app's one live picture of the system: the newest runs, the workflows,
   the headline counts, open approvals and services. The shell holds it so the
   verdict is available on every page — the dock shows it as a signal, and the
   light behind the page takes its colour — and the home screen draws from the
   same copy instead of fetching its own. */

/** How many of the newest runs are held. Fewer than this coming back means it
 *  is the whole history, which the timetable needs to know. */
export const PULSE_RUNS = 200;

const LIVE = (status: string) =>
  status === 'running' || status === 'queued' || status === 'waiting_approval';

type Pulse = {
  runs: Run[] | null;
  flows: Workflow[] | null;
  summary: Summary | null;
  /** True when `runs` is everything there is. */
  complete: boolean;
  services: ReturnType<typeof useServices>;
  serviceCount: ReturnType<typeof serviceCounts>;
  approvals: ReturnType<typeof useOpenApprovals>;
  /** Distinct runs parked on an approval. */
  waitingRuns: number[];
  failing: Failing<Workflow, Run>[];
  mood: Mood;
  refresh: () => void;
};

const PulseContext = createContext<Pulse | null>(null);

export function usePulse(): Pulse {
  const pulse = useContext(PulseContext);
  if (!pulse) throw new Error('usePulse outside PulseProvider');
  return pulse;
}

export function PulseProvider({ children }: { children: ReactNode }) {
  const [runs, setRuns] = useState<Run[] | null>(null);
  const [flows, setFlows] = useState<Workflow[] | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  // Runs from before the newest page, fetched only for workflows that failed
  // recently and have not run since — see `refresh`.
  const [older, setOlder] = useState<Run[]>([]);
  const approvals = useOpenApprovals();
  const services = useServices({ supervisor: false, pollIdle: false });
  const home = useLocation().pathname === '/';

  const refresh = () => {
    api<Workflow[]>('/workflows').then(setFlows).catch(() => setFlows(prev => prev ?? []));
    api<Summary>('/stats/summary').then(setSummary).catch(() => {});
    void (async () => {
      try {
        const [recent, failed] = await Promise.all([
          api<Run[]>(`/runs?limit=${PULSE_RUNS}`),
          api<Run[]>('/runs?status=failed&limit=40').catch(() => [] as Run[]),
        ]);
        setRuns(recent);
        // The verdict must not depend on how busy the install is. A workflow
        // that failed and then stopped running (auto-paused, or just rare)
        // scrolls out of the newest page within minutes on a busy one, and
        // the home screen would call that "on track". So any workflow with a
        // recent failure that the newest page cannot judge is asked about
        // directly — normally none, and never more than a handful.
        const ask = unjudgedFailures(recent, failed, Date.now());
        const lists = await Promise.all(ask.map(id =>
          api<Run[]>(`/runs?workflow_id=${id}&limit=10`).catch(() => [] as Run[])));
        setOlder(lists.flat());
      } catch { setRuns(prev => prev ?? []); }
    })();
  };
  useEffect(refresh, []);

  // A burst of task updates is one change to the picture: ask once, at the
  // start, and once more when the burst has settled.
  const pending = useRef(0);
  const last = useRef(0);
  useEffect(() => {
    const onEvent = () => {
      const since = Date.now() - last.current;
      window.clearTimeout(pending.current);
      if (since > 900) { last.current = Date.now(); refresh(); }
      else pending.current = window.setTimeout(() => { last.current = Date.now(); refresh(); }, 900 - since);
    };
    const offs = ['run_created', 'run_updated', 'task_run_updated', 'workflow_updated', 'resync'].map(type => rrws.on(type, onEvent));
    return () => { offs.forEach(off => off()); window.clearTimeout(pending.current); };
  }, []);

  // The socket is the fast path. This poll covers it being down, and steps
  // back to a once-a-minute safety net while it is live.
  const live = Boolean(runs?.some(r => LIVE(r.status)));
  useDataPoll(true, refresh, live ? (home ? 3000 : 10_000) : 30_000);

  const serviceCount = useMemo(() => serviceCounts(services.items), [services.items]);
  const waitingRuns = useMemo(() => [...new Set((approvals ?? []).map(row => row.run_id))], [approvals]);
  const failing = useMemo(
    () => (runs && flows ? failingFlows(newestFirst(runs, older), flows, Date.now()) : []),
    [runs, flows, older]);
  const mood = moodOf({ down: serviceCount.down, waiting: waitingRuns.length, failing: failing.length });

  // The light behind every page: style.css reads these off <html>.
  const running = summary?.running ?? runs?.filter(r => r.status === 'running').length ?? 0;
  useEffect(() => {
    document.documentElement.dataset.mood = mood;
    document.documentElement.dataset.live = running > 0 ? 'true' : 'false';
    // Remembered for the next load (index.html reads it), so a reload opens
    // under the sky it closed under instead of flashing calm first.
    if (runs && flows) { try { sessionStorage.setItem('runrail-mood', mood); } catch { /* private mode */ } }
  }, [mood, running, Boolean(runs && flows)]);

  const value: Pulse = {
    runs, flows, summary, complete: (runs?.length ?? 0) < PULSE_RUNS,
    services, serviceCount, approvals, waitingRuns, failing, mood, refresh,
  };
  return <PulseContext.Provider value={value}>{children}</PulseContext.Provider>;
}
