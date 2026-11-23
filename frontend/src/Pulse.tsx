import { createContext, ReactNode, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { api } from './api';
import { rrws } from './ws';
import { useVisibleInterval } from './hooks';
import { useOpenApprovals } from './components/ApprovalGate';
import { serviceCounts, useServices } from './Services';
import { failingFlows, moodOf, type Failing, type Mood } from './pulseModel';
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
  const approvals = useOpenApprovals();
  const services = useServices({ supervisor: false, pollIdle: false });
  const home = useLocation().pathname === '/';

  const refresh = () => {
    api<Run[]>(`/runs?limit=${PULSE_RUNS}`).then(setRuns).catch(() => setRuns(prev => prev ?? []));
    api<Workflow[]>('/workflows').then(setFlows).catch(() => setFlows(prev => prev ?? []));
    api<Summary>('/stats/summary').then(setSummary).catch(() => {});
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
    const offs = [rrws.on('run_created', onEvent), rrws.on('run_updated', onEvent), rrws.on('task_run_updated', onEvent)];
    return () => { offs.forEach(off => off()); window.clearTimeout(pending.current); };
  }, []);

  // The socket is the fast path; this is what keeps the picture honest when
  // the API and the worker are separate processes and nothing is pushed.
  const live = Boolean(runs?.some(r => LIVE(r.status)));
  useVisibleInterval(true, refresh, live ? (home ? 3000 : 10_000) : 30_000);

  const serviceCount = useMemo(() => serviceCounts(services.items), [services.items]);
  const waitingRuns = useMemo(() => [...new Set((approvals ?? []).map(row => row.run_id))], [approvals]);
  const failing = useMemo(
    () => (runs && flows ? failingFlows(runs, flows, Date.now()) : []),
    [runs, flows]);
  const mood = moodOf({ down: serviceCount.down, waiting: waitingRuns.length, failing: failing.length });

  // The light behind every page: style.css reads these off <html>.
  const running = summary?.running ?? runs?.filter(r => r.status === 'running').length ?? 0;
  useEffect(() => {
    document.documentElement.dataset.mood = mood;
    document.documentElement.dataset.live = running > 0 ? 'true' : 'false';
  }, [mood, running]);

  const value: Pulse = {
    runs, flows, summary, complete: (runs?.length ?? 0) < PULSE_RUNS,
    services, serviceCount, approvals, waitingRuns, failing, mood, refresh,
  };
  return <PulseContext.Provider value={value}>{children}</PulseContext.Provider>;
}
