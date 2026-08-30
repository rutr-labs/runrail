import { useEffect, useId, useMemo, useState } from 'react';
import { AlertTriangle, Lock, Users } from 'lucide-react';
import clsx from 'clsx';
import { api } from '../api';

/* ─── Resource locks ───────────────────────────────────────
   Mutual exclusion between workflows on a named resource — the monthly
   maintenance job that owns one database while it runs, with everything else
   on that database queueing instead of racing it.

   Two words carry the whole feature and neither means anything on its own, so
   this component never shows "shared" or "exclusive" without the sentence that
   says what happens. The mode is also inert without a resource (the server
   forces it back to `shared`), which is why naming the resource is the first
   and only decision until it is made.

   The failure mode worth designing against is a typo: `warehouse-db` and
   `warehouse_db` are two separate locks that silently wait on nothing. So the
   names already in use are offered as chips and through a datalist, whoever
   else names this resource is listed as you type, and a near-miss is called
   out before it can be saved.

   Self-contained state published through hidden inputs, exactly like
   ScheduleBuilder and WatchdogFields, so the host's <form onSubmit={FormData}>
   flow is untouched. The visible controls carry no `lock_*` name — only the
   hidden inputs publish — but the resource box is `required` while the lock is
   switched on, so an emptied box fails native validation instead of quietly
   saving the lock away. */

export type LockMode = 'shared' | 'exclusive';

/** The lock-relevant slice of WorkflowOut. Structural, so a host can hand its
 *  own Workflow list straight in. */
export interface LockPeer {
  id: number;
  name: string;
  lock_resource?: string | null;
  lock_mode?: LockMode | null;
}

/** Reads what this component published. Spread into the workflow create/edit
 *  body — the two keys are already named exactly as WorkflowIn expects.
 *
 *  Mirrors the server's own validator (trim; an empty resource means no lock,
 *  and forces the mode back to `shared`), so what the modal sends is already
 *  what the server would normalise it to — the edit modal then re-opens on the
 *  same values it just saved. */
export function lockValues(f: FormData): {
  lock_resource: string | null;
  lock_mode: LockMode;
} {
  const resource = String(f.get('lock_resource') ?? '').trim();
  const mode: LockMode = f.get('lock_mode') === 'exclusive' ? 'exclusive' : 'shared';
  return { lock_resource: resource || null, lock_mode: resource ? mode : 'shared' };
}

/* ─── Near-miss detection ─────────────────────────────────
   A misspelled resource name is not an error anywhere: it saves, it validates,
   and it locks against nobody. Catching it here is the only place it can be
   caught. */

const normalize = (value: string) => value.toLowerCase().replace(/[^a-z0-9]/g, '');

/** Levenshtein, bailing out as soon as the answer cannot be within `limit`. */
function editDistance(a: string, b: string, limit: number): number {
  if (Math.abs(a.length - b.length) > limit) return limit + 1;
  let previous = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const row = [i];
    let best = i;
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      row[j] = Math.min(row[j - 1] + 1, previous[j] + 1, previous[j - 1] + cost);
      best = Math.min(best, row[j]);
    }
    if (best > limit) return limit + 1;
    previous = row;
  }
  return previous[b.length];
}

/** An existing resource name this one was probably meant to be, or null.
 *  Only consulted when nothing matches exactly — an exact match needs no help. */
function nearestMiss(typed: string, existing: string[]): string | null {
  if (typed.length < 4) return null;
  const flat = normalize(typed);
  // Case and separators first: `Warehouse_DB` vs `warehouse-db` is the common one.
  const sameLetters = existing.find(name => name !== typed && normalize(name) === flat);
  if (sameLetters) return sameLetters;
  const limit = typed.length >= 8 ? 2 : 1;
  let best: { name: string; distance: number } | null = null;
  for (const name of existing) {
    if (name === typed) continue;
    const distance = editDistance(flat, normalize(name), limit);
    if (distance <= limit && (!best || distance < best.distance)) best = { name, distance };
  }
  return best?.name ?? null;
}

/* ─── One mode choice ─────────────────────────────────────
   A real radio, so arrow keys and screen readers work; the card is the label. */
function ModeChoice({
  value, current, group, onPick, icon, title, sentence,
}: {
  value: LockMode;
  current: LockMode;
  group: string;
  onPick: (mode: LockMode) => void;
  icon: React.ReactNode;
  title: string;
  /** What actually happens, named in the operator's own resource. */
  sentence: React.ReactNode;
}) {
  const on = value === current;
  return (
    <label className={clsx('lock-mode', `lock-mode--${value}`, on && 'on')}>
      <input type="radio" name={group} value={value} checked={on}
             onChange={() => onPick(value)} />
      <span className="lock-mode-glyph">{icon}</span>
      <span className="lock-mode-text">
        <strong>{title}</strong>
        <small>{sentence}</small>
      </span>
    </label>
  );
}

/* ─── The field ───────────────────────────────────────────
   For the workflow create/edit modals. */
export function LockField({
  resource = null, mode = 'shared', workflowId, maxConcurrentRuns, peers,
}: {
  /** Current workflow.lock_resource. NULL/'' opens switched off — the default. */
  resource?: string | null;
  /** Current workflow.lock_mode. Inert until a resource is named. */
  mode?: LockMode | null;
  /** The workflow being edited, so it never lists itself among the others
   *  holding this resource. Omit when creating. */
  workflowId?: number;
  /** The workflow's max_concurrent_runs, if the host has it. An exclusive lock
   *  applies between a workflow's own runs too, which quietly overrides a
   *  concurrency budget above 1 — worth saying out loud rather than debugging. */
  maxConcurrentRuns?: number;
  /** Pass the already-loaded workflow list to skip this component's own fetch. */
  peers?: LockPeer[];
}) {
  const uid = useId();
  const [on, setOn] = useState(Boolean(resource));
  const [name, setName] = useState(resource ?? '');
  const [pick, setPick] = useState<LockMode>(mode === 'exclusive' ? 'exclusive' : 'shared');
  const [fetched, setFetched] = useState<LockPeer[]>([]);

  useEffect(() => {
    if (peers) return;
    // Read-only and non-blocking: without it the field still works, it just
    // stops being able to warn about a name that nearly matches another.
    let live = true;
    api<LockPeer[]>('/workflows')
      .then(list => { if (live) setFetched(list); })
      .catch(() => {});
    return () => { live = false; };
  }, [peers]);

  const all = peers ?? fetched;
  const trimmed = name.trim();

  /** Every OTHER workflow that names a resource. */
  const locked = useMemo(
    () => all.filter(w => w.lock_resource && w.id !== workflowId),
    [all, workflowId],
  );

  /** Distinct resource names in use, busiest first — the suggestion list. */
  const inUse = useMemo(() => {
    const counts = new Map<string, number>();
    for (const w of locked) counts.set(w.lock_resource!, (counts.get(w.lock_resource!) ?? 0) + 1);
    return [...counts.entries()]
      .map(([resourceName, count]) => ({ name: resourceName, count }))
      .sort((a, b) => b.count - a.count || a.name.localeCompare(b.name));
  }, [locked]);

  /** Exact matches only — an exact match is the whole mechanism. */
  const sharing = useMemo(
    () => locked.filter(w => w.lock_resource === trimmed),
    [locked, trimmed],
  );
  const nearMiss = useMemo(
    () => (trimmed && sharing.length === 0 ? nearestMiss(trimmed, inUse.map(r => r.name)) : null),
    [trimmed, sharing.length, inUse],
  );

  const exclusive = pick === 'exclusive';
  const named = <code className="lock-resource-name">{trimmed || 'this resource'}</code>;
  const selfSerialises = exclusive && (maxConcurrentRuns ?? 1) > 1;

  return (
    <div className="locks">
      <span className="locks-label">Resource lock</span>

      <div className={clsx('lock-field', on && 'is-on')}>
        {/* Exactly the two keys WorkflowIn expects, read back by lockValues(f). */}
        <input type="hidden" name="lock_resource" value={on ? trimmed : ''} />
        <input type="hidden" name="lock_mode" value={on && trimmed ? pick : 'shared'} />

        <label className="lock-head">
          <span className="lock-glyph"><Lock size={14} /></span>
          <span className="lock-text">
            <strong>Take turns on a shared resource</strong>
            <small>
              Name something that should not have two jobs in it at once — a database, a
              licence, a mounted share. Workflows that name the same thing queue for it
              instead of running over each other.
            </small>
          </span>
          <span className="toggle">
            <input
              type="checkbox"
              checked={on}
              onChange={e => setOn(e.target.checked)}
              aria-label="Lock a resource"
            />
            <span />
          </span>
        </label>

        {on && (
          <div className="lock-body">
            <label className="field">
              <span>Resource name <em>Free text — the name is the lock</em></span>
              <input
                list={`${uid}-resources`}
                value={name}
                onChange={e => setName(e.target.value)}
                placeholder="warehouse-db"
                required
                maxLength={255}
                autoComplete="off"
                spellCheck={false}
              />
              <datalist id={`${uid}-resources`}>
                {inUse.map(r => <option key={r.name} value={r.name} />)}
              </datalist>
              <small>
                Workflows are linked by this exact string, and by nothing else.{' '}
                <code>warehouse-db</code> and <code>Warehouse DB</code> are two separate
                locks that never wait for one another.
              </small>
            </label>

            {inUse.length > 0 && (
              <div className="lock-suggest">
                <span className="lock-suggest-label">Already in use</span>
                {inUse.map(r => (
                  <button
                    key={r.name}
                    type="button"
                    className={clsx('lock-suggest-chip', r.name === trimmed && 'on')}
                    aria-pressed={r.name === trimmed}
                    onClick={() => setName(r.name)}
                    title={`Join the ${r.count === 1 ? 'workflow' : `${r.count} workflows`} already using ${r.name}`}
                  >
                    {r.name}<em>{r.count}</em>
                  </button>
                ))}
              </div>
            )}

            {nearMiss && (
              <p className="lock-warn">
                <AlertTriangle size={12} />
                <span>
                  Nothing else uses <b>{trimmed}</b>, but <b>{nearMiss}</b> exists. If you
                  meant that one, the spelling has to match exactly — otherwise this
                  becomes a second lock that nothing ever waits on.
                </span>
                <button type="button" className="lock-warn-fix" onClick={() => setName(nearMiss)}>
                  Use “{nearMiss}”
                </button>
              </p>
            )}

            {trimmed && (
              <>
                <div className="lock-modes" role="radiogroup" aria-label="How this workflow uses the resource">
                  <ModeChoice
                    value="exclusive" current={pick} group={`${uid}-mode`} onPick={setPick}
                    icon={<Lock size={13} />}
                    title="Runs alone"
                    sentence={<>Anything else using {named} waits while this one runs.</>}
                  />
                  <ModeChoice
                    value="shared" current={pick} group={`${uid}-mode`} onPick={setPick}
                    icon={<Users size={13} />}
                    title="Runs alongside"
                    sentence={<>Overlaps other shared workflows on {named}, but waits for one that runs alone.</>}
                  />
                </div>

                <p className="lock-outcome">
                  {exclusive ? (
                    <>
                      While a run of this workflow is going, nothing else that names {named}
                      {' '}starts — not other workflows, and not this workflow’s own next run.
                      It first waits for whatever holds {named} now, and from the moment it is
                      waiting, no new shared run may start ahead of it. That barrier is what
                      gets a monthly job its turn against an hourly one.
                    </>
                  ) : (
                    <>
                      Runs at the same time as other shared workflows on {named}. It waits
                      while a run-alone workflow holds {named}, and also while one is queued
                      for it — so a steady drip of small runs cannot starve the heavy job.
                    </>
                  )}
                </p>

                {selfSerialises && (
                  <p className="lock-warn">
                    <AlertTriangle size={12} />
                    <span>
                      Max active runs is {maxConcurrentRuns}, but running alone applies to this
                      workflow’s own runs too — they will still go one at a time while it holds
                      {' '}{named}.
                    </span>
                  </p>
                )}

                <div className="lock-peers">
                  {sharing.length > 0 ? (
                    <>
                      <span className="lock-peers-label">
                        {sharing.length === 1
                          ? 'One other workflow uses this resource'
                          : `${sharing.length} other workflows use this resource`}
                      </span>
                      <ul>
                        {sharing.map(w => {
                          const overlaps = !exclusive && w.lock_mode !== 'exclusive';
                          return (
                            <li key={w.id}>
                              <b>{w.name}</b>
                              {/* Compact: every row here names the same resource. */}
                              <LockBadge resource={w.lock_resource} mode={w.lock_mode} compact />
                              <em>{overlaps ? 'can run at the same time' : 'takes turns with this one'}</em>
                            </li>
                          );
                        })}
                      </ul>
                    </>
                  ) : (
                    <span className="lock-peers-label lock-peers-label--alone">
                      No other workflow names {named} yet. The lock starts doing something the
                      moment a second workflow names it — spelled identically.
                    </span>
                  )}
                </div>
              </>
            )}

            <p className="lock-note">
              A run holds the resource from the moment it starts until it finishes, including
              while it sits waiting on an approval — it has half-finished work in there. Failing
              or cancelling releases it. One resource per workflow.
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

/* ─── The indicator ───────────────────────────────────────
   For the workflow card and the detail header. Renders nothing for a workflow
   that names no resource, which is most of them — the mode alone is not a
   state worth showing. */
export function LockBadge({
  resource, mode = 'shared', compact = false, className,
}: {
  resource?: string | null;
  mode?: LockMode | null;
  /** Drops the resource name, leaving the mode alone. For places that have
   *  already said which resource this is (the peers list, a tight card). */
  compact?: boolean;
  className?: string;
}) {
  if (!resource) return null;
  const exclusive = mode === 'exclusive';
  return (
    <span
      className={clsx('lock-badge', exclusive ? 'lock-badge--exclusive' : 'lock-badge--shared',
                      compact && 'lock-badge--compact', className)}
      title={exclusive
        ? `Runs alone on ${resource} — everything else using ${resource} waits for it, and it waits for them.`
        : `Shares ${resource} — runs alongside other shared workflows, but waits for one that runs alone.`}
    >
      {exclusive ? <Lock size={11} strokeWidth={2.5} /> : <Users size={11} strokeWidth={2.5} />}
      {!compact && <span className="lock-badge-name">{resource}</span>}
      <em>{exclusive ? 'alone' : 'shared'}</em>
    </span>
  );
}
