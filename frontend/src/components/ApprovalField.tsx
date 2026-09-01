import { useState } from 'react';
import { ShieldCheck } from 'lucide-react';
import clsx from 'clsx';

/* ─── Approval gate ────────────────────────────────────────
   Marks a task as one a person has to release. The run parks on it, gives up
   its worker thread while it waits, and shows the prompt written here beside
   the exact command it is about to run.

   Self-contained state published through form fields, exactly like
   ScheduleBuilder, WatchdogFields and LockField, so the host's
   <form onSubmit={FormData}> flow is untouched.

   This control is the whole reason the feature is reachable: the gate columns
   existed, the run-time behaviour existed, and the approve/reject UI existed,
   but nothing in the app could ever turn one on. */

/** Reads what this component published. Spread into the task create/edit body —
 *  both keys are already named exactly as TaskIn expects. */
export function approvalValues(f: FormData): {
  requires_approval: boolean;
  approval_prompt: string | null;
} {
  const on = String(f.get('requires_approval') ?? '') === '1';
  const prompt = String(f.get('approval_prompt') ?? '').trim();
  // A prompt without a gate would be dead text, and a gate keeps its prompt
  // optional: "why" is useful, not mandatory.
  return { requires_approval: on, approval_prompt: on && prompt ? prompt : null };
}

export function ApprovalField({ defaultOn = false, defaultPrompt = '' }: {
  defaultOn?: boolean;
  defaultPrompt?: string;
}) {
  const [on, setOn] = useState(defaultOn);

  return (
    <div className={clsx('watchdog', on && 'is-on')}>
      <input type="hidden" name="requires_approval" value={on ? '1' : ''} />

      <label className="watchdog-head">
        <span className="watchdog-glyph"><ShieldCheck size={15} /></span>
        <span className="watchdog-text">
          <strong>Wait for approval</strong>
          <small>The run stops before this task and waits for you to release it.</small>
        </span>
        <span className="toggle">
          <input
            type="checkbox"
            checked={on}
            onChange={e => setOn(e.target.checked)}
          />
          <span />
        </span>
      </label>

      {on && (
        <div className="watchdog-body">
          <label className="field">
            <span>What should the approver check? <em>Optional</em></span>
            <textarea
              name="approval_prompt"
              defaultValue={defaultPrompt}
              maxLength={2000}
              rows={2}
              placeholder="Pushes tomorrow's price list to partner endpoints. Check the diff in the previous task's log first."
            />
          </label>
          <span className="watchdog-summary">
            Shown beside the exact command when the run parks here. Waiting costs
            nothing: the task releases its worker until you approve or reject.
          </span>
        </div>
      )}
    </div>
  );
}
