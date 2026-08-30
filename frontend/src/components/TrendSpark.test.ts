import { describe, expect, test } from 'vitest';
import insightsSource from '../../../src/runrail/api/routes_insights.py?raw';
import { MIN_SPARK_SAMPLES, SLOW_MIN_SAMPLES, SLOW_SIGMA } from './TrendSpark';

/* ─── Constants that mirror the backend ────────────────────
   TrendSpark shades the band the server's own rule calls unusual, but the
   verdict itself arrives as `series.slow`. If these numbers drift apart the
   chip and the shading contradict each other: a task flagged slow with the
   marker sitting inside the normal band, or the reverse.

   Read out of the Python rather than copied, because a copy is the thing that
   drifts. The comments on the two exports promise exactly this. Pulled in with
   Vite's `?raw` rather than node:fs so the file needs no Node type definitions —
   adding @types/node would retype every bare setTimeout in the app. */

function pythonConstant(name: string): number {
  const match = insightsSource.match(new RegExp(`^${name}\\s*=\\s*([0-9.]+)\\s*$`, 'm'));
  if (!match) throw new Error(`${name} is no longer a module constant in routes_insights.py`);
  return Number(match[1]);
}

describe('slow-task thresholds match routes_insights.py', () => {
  test('SLOW_SIGMA', () => {
    expect(SLOW_SIGMA).toBe(pythonConstant('SLOW_SIGMA'));
  });

  test('SLOW_MIN_SAMPLES', () => {
    expect(SLOW_MIN_SAMPLES).toBe(pythonConstant('SLOW_MIN_SAMPLES'));
  });

  test('the spark needs fewer samples than the verdict does', () => {
    // A line can be drawn well before the server is willing to call anything
    // slow; the reverse would mean a flagged task with no line under it.
    expect(MIN_SPARK_SAMPLES).toBeLessThan(SLOW_MIN_SAMPLES);
  });
});
