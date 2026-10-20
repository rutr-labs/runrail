import { describe, expect, test } from 'vitest';
import { gitLine, headline, inputsNote, packageLine, quietNote, shortDuration, taskChanged, type RunDiff, type TaskDiff } from './runDiff';

/* ─── Run comparison ───────────────────────────────────────
   The headline a failed run opens with: what differs, most telling first,
   and an honest line when nothing in RunRail's view differs. */

const side = (status: string, duration = 10) =>
  ({ task_run_id: 1, status, duration, exit_code: status === 'failed' ? 1 : 0, attempts: 1, error: null });
const run = { id: 2, status: 'failed', trigger: 'schedule', created_at: '', started_at: null, duration: 1 };
const base: RunDiff = { run, baseline: { ...run, id: 1, status: 'success' }, parameters: [], tasks: [] };
const task = (patch: Partial<TaskDiff>): TaskDiff =>
  ({ task_id: 1, name: 'load', type: 'shell', before: side('success'), after: side('success'), ...patch });

describe('headline', () => {
  test('the failure first, then what moved under it', () => {
    const diff: RunDiff = {
      ...base,
      parameters: [{ name: 'region', change: 'changed', before: 'EU', after: 'US' }],
      tasks: [task({
        after: side('failed'),
        code: { root: '/srv/orders', mode: 'project', used_changed: 1, other_changed: 2,
                files: [{ path: 'etl/load.py', change: 'modified', used: true, diff: [] },
                        { path: 'docs/a.py', change: 'modified', used: false }, { path: 'b.py', change: 'added', used: false }] },
        environment: { name: 'etl', packages: [{ name: 'pandas', before: 'pandas==2.1.0', after: 'pandas==2.2.1' }] },
        logs: { new: [], new_total: 3, gone: [], gone_total: 0, changed: [], changed_total: 0, first_error: null, truncated: false },
      })],
    };
    expect(headline(diff).map(p => p.text)).toEqual([
      'load failed (passed last time)', 'parameter region changed', 'load.py changed',
      '2 other files in orders changed', 'pandas 2.1.0 → 2.2.1', 'load: 3 new log lines',
    ]);
  });

  test('a much slower task is news; a little slower is not', () => {
    expect(headline({ ...base, tasks: [task({ after: side('success', 300), before: side('success', 40) })] })[0].text)
      .toBe('load took 5m (was 40s)');
    expect(headline({ ...base, tasks: [task({ after: side('success', 12), before: side('success', 10) })] })).toEqual([]);
  });

  test('nothing differing is said plainly, and so is not knowing', () => {
    expect(quietNote({ ...base, tasks: [task({})] })).toMatch(/outside RunRail/);
    expect(inputsNote({ ...base, tasks: [task({ inputs_unknown: true })] })).toMatch(/before RunRail recorded/);
    expect(quietNote({ ...base, baseline: null })).toBeNull();
  });
});

describe('pieces', () => {
  test('package lines', () => {
    expect(packageLine({ name: 'pyarrow', before: null, after: 'pyarrow==15.0.0' })).toBe('+ pyarrow 15.0.0');
    expect(packageLine({ name: 'requests', before: 'requests==2.31.0', after: null })).toBe('− requests 2.31.0');
  });
  test('durations', () => {
    expect(shortDuration(4.25)).toBe('4.3s');
    expect(shortDuration(95)).toBe('1m 35s');
    expect(shortDuration(3720)).toBe('1h 2m');
    expect(shortDuration(null)).toBe('—');
  });
  test('an unchanged task folds away', () => {
    expect(taskChanged(task({}))).toBe(false);
    expect(taskChanged(task({ command: { before: 'a', after: 'b' } }))).toBe(true);
    expect(taskChanged(task({ before: null }))).toBe(true);
  });
});

describe('gitLine', () => {
  const at = (commit: string, dirty = false) => ({ commit, branch: 'main', dirty });
  test('commits move, edits are named, nothing new says nothing', () => {
    expect(gitLine({ before: at('a1b2c3d4e5'), after: at('f6e5d4c3b2') })).toBe('commit a1b2c3d → f6e5d4c');
    expect(gitLine({ before: at('a1b2c3d4e5'), after: at('a1b2c3d4e5', true) })).toBe('same commit a1b2c3d, with uncommitted edits');
    expect(gitLine({ before: at('a1b2c3d4e5'), after: at('a1b2c3d4e5') })).toBeNull();
    expect(gitLine(undefined)).toBeNull();
  });
});
