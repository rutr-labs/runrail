import { describe, expect, it } from 'vitest';
import { pipeColumns, pipeSinks, pipeSizes, type PipeTask } from './pipeline';

const task = (name: string, ...depends_on: string[]): PipeTask => ({ name, task_type: 'shell', depends_on });

describe('pipeColumns', () => {
  it('puts a task one column past its deepest dependency', () => {
    const tasks = [task('extract'), task('clean', 'extract'), task('load', 'clean'), task('report', 'extract', 'load')];
    expect(pipeColumns(tasks)).toEqual([['extract'], ['clean'], ['load'], ['report']]);
  });

  it('stacks parallel branches in one column', () => {
    const tasks = [task('extract'), task('a', 'extract'), task('b', 'extract'), task('join', 'a', 'b')];
    expect(pipeColumns(tasks)).toEqual([['extract'], ['a', 'b'], ['join']]);
  });

  it('keeps a branch level with the task it came from', () => {
    // Declared out of order: without sorting, `under-b` would sit above `under-a`.
    const tasks = [task('a'), task('b'), task('under-b', 'b'), task('under-a', 'a')];
    expect(pipeColumns(tasks)).toEqual([['a', 'b'], ['under-a', 'under-b']]);
  });

  it('ignores a dependency that is not in the workflow', () => {
    expect(pipeColumns([task('only', 'ghost')])).toEqual([['only']]);
  });

  it('does not loop on a cycle', () => {
    const columns = pipeColumns([task('a', 'b'), task('b', 'a')]);
    expect(columns.flat().sort()).toEqual(['a', 'b']);
  });
});

describe('pipeSinks', () => {
  it('names the tasks nothing else waits for', () => {
    const tasks = [task('extract'), task('a', 'extract'), task('b', 'extract'), task('report', 'a')];
    expect(pipeSinks(tasks)).toEqual(['b', 'report']);
  });
});

describe('pipeSizes', () => {
  it('is roomy when there is room', () => {
    expect(pipeSizes(3, 1, 1300)).toEqual({ node: 244, end: 196, gap: 68 });
  });

  it('tightens in steps before it gives up and scrolls', () => {
    const snug = pipeSizes(5, 2, 1264);
    expect(snug.gap).toBeLessThan(68);
    expect(snug.node).toBeGreaterThanOrEqual(164);
    // Five columns, two ends and six gaps fit, or nearly: never the 2000px
    // the roomy sizes would need.
    const width = 5 * snug.node + 2 * snug.end + 6 * snug.gap + 20;
    expect(width).toBeLessThan(1400);
  });

  it('never draws a node too narrow to read', () => {
    expect(pipeSizes(12, 2, 600).node).toBe(164);
  });
});
