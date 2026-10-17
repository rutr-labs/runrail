import { describe, expect, test } from 'vitest';
import { attemptLabel, attemptTag, groupAttempts, isGateRow } from './attempts';

/* ─── Attempt grouping ─────────────────────────────────────
   The task cards, the notebook rows and the timeline all lean on this, and the
   failure it replaces was every attempt of a task getting its own identically
   named entry — "build_report, build_report" with nothing to tell them apart. */

const row = (id: number, task_id: number, attempt: number, status: string, resume_index = 0) =>
  ({ id, task_id, task_name: `task-${task_id}`, attempt, status, resume_index });

describe('groupAttempts', () => {
  test('a retried task is one group, attempts oldest first', () => {
    const groups = groupAttempts([row(3, 1, 2, 'success'), row(2, 1, 1, 'failed'), row(4, 2, 1, 'success')]);
    expect(groups.map(g => g.name)).toEqual(['task-1', 'task-2']);
    expect(groups[0].attempts.map(a => a.attempt)).toEqual([1, 2]);
    expect(groups[0].attempts.at(-1)!.status).toBe('success');
  });

  test('an approval decision is never counted as an attempt', () => {
    const [group] = groupAttempts([row(1, 1, 0, 'approved'), row(2, 1, 1, 'success')]);
    expect(group.attempts).toHaveLength(1);
    expect(group.gates.map(g => g.status)).toEqual(['approved']);
    expect(isGateRow({ status: 'awaiting_approval' })).toBe(true);
    expect(isGateRow({ status: 'failed' })).toBe(false);
  });

  test('a resumed segment sorts after the original attempts', () => {
    const [group] = groupAttempts([row(5, 1, 1, 'success', 1), row(2, 1, 2, 'failed'), row(1, 1, 1, 'failed')]);
    expect(group.attempts.map(a => a.id)).toEqual([1, 2, 5]);
    expect(group.resumed).toBe(true);
  });
});

describe('labels', () => {
  test('plain attempts read as "Attempt N"', () => {
    expect(attemptLabel(row(1, 1, 2, 'failed'), false)).toBe('Attempt 2');
    expect(attemptTag(row(1, 1, 2, 'failed'), false)).toBe('#2');
  });

  test('after a resume, the segment is named, because numbering restarts', () => {
    // Two different rows would otherwise both be "Attempt 1".
    expect(attemptLabel(row(1, 1, 1, 'failed', 0), true)).toBe('Attempt 1');
    expect(attemptLabel(row(2, 1, 1, 'success', 1), true)).toBe('Resume 1 · attempt 1');
    expect(attemptTag(row(2, 1, 1, 'success', 1), true)).toBe('R1·1');
  });
});
