import { describe, expect, test } from 'vitest';
import { approvalValues } from './ApprovalField';

/* ─── Approval values published to the task body ───────────
   The gate columns, the worker parking on them and the approve/reject card all
   shipped while no control could switch a gate on, so the one thing these tests
   guard is that a form actually produces the two keys the server stores. */

const form = (entries: Record<string, string>) => {
  const data = new FormData();
  for (const [key, value] of Object.entries(entries)) data.append(key, value);
  return data;
};

describe('approvalValues', () => {
  test('a gate with a prompt passes both keys through', () => {
    expect(approvalValues(form({ requires_approval: '1', approval_prompt: 'Check the diff' })))
      .toEqual({ requires_approval: true, approval_prompt: 'Check the diff' });
  });

  test('a gate without a prompt is still a gate', () => {
    // The prompt is help for the approver, not a condition of gating.
    expect(approvalValues(form({ requires_approval: '1' })))
      .toEqual({ requires_approval: true, approval_prompt: null });
    expect(approvalValues(form({ requires_approval: '1', approval_prompt: '   ' })))
      .toEqual({ requires_approval: true, approval_prompt: null });
  });

  test('an untouched form sends the gate off, explicitly', () => {
    // Explicitly, because the server treats an omitted key as "unchanged":
    // sending nothing would leave a gate switched on forever.
    expect(approvalValues(form({})))
      .toEqual({ requires_approval: false, approval_prompt: null });
  });

  test('switching the gate off drops a prompt left behind in the box', () => {
    // A prompt with no gate is dead text that would reappear if the gate were
    // ever switched back on, describing a decision nobody made.
    expect(approvalValues(form({ requires_approval: '', approval_prompt: 'stale text' })))
      .toEqual({ requires_approval: false, approval_prompt: null });
  });

  test('the prompt is trimmed', () => {
    expect(approvalValues(form({ requires_approval: '1', approval_prompt: '  Check totals  ' })))
      .toEqual({ requires_approval: true, approval_prompt: 'Check totals' });
  });
});
