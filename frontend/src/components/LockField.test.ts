import { describe, expect, test } from 'vitest';
import { lockValues } from './LockField';

/* ─── Lock values published to the workflow body ───────────
   lockValues mirrors the server's own normaliser, and the reason that matters
   is the edit modal: it re-opens on whatever it just sent, so any field the two
   sides normalise differently makes the form appear to change a setting nobody
   touched. */

const form = (entries: Record<string, string>) => {
  const data = new FormData();
  for (const [key, value] of Object.entries(entries)) data.append(key, value);
  return data;
};

describe('lockValues', () => {
  test('a named resource and a mode pass straight through', () => {
    expect(lockValues(form({ lock_resource: 'warehouse-db', lock_mode: 'exclusive' })))
      .toEqual({ lock_resource: 'warehouse-db', lock_mode: 'exclusive' });
    expect(lockValues(form({ lock_resource: 'warehouse-db', lock_mode: 'shared' })))
      .toEqual({ lock_resource: 'warehouse-db', lock_mode: 'shared' });
  });

  test('the resource is trimmed', () => {
    // `warehouse-db ` and `warehouse-db` would be two locks that wait on
    // nothing — the exact failure this component is designed against.
    expect(lockValues(form({ lock_resource: '  warehouse-db  ', lock_mode: 'exclusive' })))
      .toEqual({ lock_resource: 'warehouse-db', lock_mode: 'exclusive' });
  });

  test('no resource means no lock, and forces the mode back to shared', () => {
    // The mode is inert without a resource; sending `exclusive` alongside a
    // null resource would come back from the server as `shared` and the modal
    // would look like it had lost a setting.
    const cases: Record<string, string>[] = [
      {},
      { lock_mode: 'exclusive' },
      { lock_resource: '', lock_mode: 'exclusive' },
      { lock_resource: '   ', lock_mode: 'exclusive' },
      { lock_resource: '\t\n ', lock_mode: 'shared' },
    ];
    for (const entries of cases) {
      expect(lockValues(form(entries)), JSON.stringify(entries))
        .toEqual({ lock_resource: null, lock_mode: 'shared' });
    }
  });

  test('anything that is not exactly "exclusive" is shared', () => {
    // The switch is off-by-default in the safe direction: an unexpected value
    // must never widen a lock into exclusive.
    for (const mode of ['', 'Exclusive', 'EXCLUSIVE', 'exclusive ', 'x', 'true', 'shared']) {
      expect(lockValues(form({ lock_resource: 'db', lock_mode: mode })).lock_mode, mode)
        .toBe('shared');
    }
  });

  test('the keys are already named as the API expects them', () => {
    // They are spread straight into the create/edit body; renaming either one
    // silently drops the lock instead of failing.
    expect(Object.keys(lockValues(form({ lock_resource: 'db' }))).sort())
      .toEqual(['lock_mode', 'lock_resource']);
  });
});
