import { describe, expect, test } from 'vitest';
import {
  collect, draftOf, draftProblem, initialValue, initialValues, localDate, needsAsking, toDefinitions,
  type RunParameter,
} from './runForms';

/* ─── Run forms ────────────────────────────────────────────
   What "Run with…" starts a field with, what it sends, and what the editor
   refuses before the server has to. */

const NOW = new Date('2026-10-19T02:00:00Z');
const FORM: RunParameter[] = [
  { name: 'report_date', type: 'date', default: 'yesterday' },
  { name: 'region', type: 'choice', choices: ['EU', 'US'], default: 'EU' },
  { name: 'limit', type: 'number', default: 100 },
  { name: 'dry_run', type: 'bool', default: true },
  { name: 'customer', type: 'text', required: true, label: 'Customer' },
];

describe('starting values', () => {
  test('a relative date is the date it means on the workflow calendar', () => {
    expect(localDate('UTC', NOW)).toBe('2026-10-19');
    expect(localDate('America/Los_Angeles', NOW)).toBe('2026-10-18');
    expect(initialValue(FORM[0], 'UTC', NOW)).toBe('2026-10-18');
    expect(initialValue(FORM[0], 'America/Los_Angeles', NOW)).toBe('2026-10-17');
  });

  test('every other default is shown as it is', () => {
    expect(initialValues(FORM, 'UTC', NOW)).toEqual({
      report_date: '2026-10-18', region: 'EU', limit: '100', dry_run: true, customer: '',
    });
  });
});

describe('collect', () => {
  test('values are sent in their types, and empty ones are left to the server', () => {
    const result = collect(FORM, { report_date: '', region: 'US', limit: '25', dry_run: false, customer: 'acme' });
    expect(result).toEqual({ parameters: { region: 'US', limit: 25, dry_run: false, customer: 'acme' } });
  });

  test('the first problem is named', () => {
    expect(collect(FORM, { customer: '' })).toEqual({ error: 'Customer is required' });
    expect(collect(FORM, { limit: 'lots', customer: 'x' })).toEqual({ error: 'limit: "lots" is not a number' });
    expect(collect(FORM, { region: 'Mars', customer: 'x' })).toEqual({ error: 'region: pick one of EU, US' });
  });

  test('a required value without a default makes Run now ask first', () => {
    expect(needsAsking(FORM)).toEqual(['customer']);
    expect(needsAsking(null)).toEqual([]);
  });
});

describe('the editor', () => {
  test('a form survives a round trip through the editor', () => {
    expect(toDefinitions(FORM.map(p => draftOf(p)))).toEqual(FORM);
  });

  test('drafts that cannot be saved say why', () => {
    const problem = (patch: Partial<ReturnType<typeof draftOf>>) => {
      const draft = { ...draftOf(), ...patch };
      return draftProblem(draft, [draft]);
    };
    expect(problem({ name: '' })).toBe('Give it a name');
    expect(problem({ name: 'run_id' })).toMatch(/set by RunRail/);
    expect(problem({ name: 'ds', type: 'text' })).toMatch(/make it a date/);
    expect(problem({ name: 'r', type: 'choice', choicesText: '' })).toBe('List at least one option');
    expect(problem({ name: 'r', type: 'choice', choicesText: 'a, b', defaultText: 'c' })).toMatch(/must be one of a, b/);
    expect(problem({ name: 'd', type: 'date', defaultText: 'tomorrow' })).toMatch(/today or yesterday/);
    expect(problem({ name: 'd', type: 'date', defaultText: 'yesterday' })).toBeNull();
    const twin = draftOf({ name: 'a', type: 'text' });
    expect(draftProblem(twin, [twin, draftOf({ name: 'a', type: 'number' })])).toMatch(/twice/);
  });
});
