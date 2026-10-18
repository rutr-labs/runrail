import { expect, test } from '@playwright/test';

/* The Settings page against the real server: the status cards read what
   /api/system/status says, a setting saved from a group survives a reload and
   says where it came from, a retention number is previewed before it may be
   saved, and the pause switch — behind a confirm, because it is an operator
   action — pauses and resumes. */

interface Status { version: string; worker: { paused: boolean; concurrency: number } }

const status = async (request: import('@playwright/test').APIRequestContext): Promise<Status> =>
  (await request.get('/api/system/status')).json();

test('the hero and the tiles show this server', async ({ page, request }) => {
  const { version } = await status(request);
  await page.goto('/settings');
  const hero = page.getByRole('region', { name: 'System' });
  await expect(hero).toContainText('Running');
  await expect(hero).toContainText(/Up \d+[smhd]/);
  const cards = page.locator('.settings-status');
  await expect(cards.locator('.sys-card', { hasText: 'Database' })).toContainText('SQLite');
  await expect(cards.locator('.sys-card', { hasText: 'Worker' })).toContainText('running');
  // The version is in the footer, quietly; paths are chips, not text.
  await expect(page.locator('.settings-footer')).toContainText(`RunRail v${version}`);
  await expect(cards.locator('.path-chip').first()).toHaveAttribute('aria-label', /Copy the database path: \//);
});

test('a worker setting saved from its group survives a reload and says so', async ({ page, request }) => {
  await page.goto('/settings');
  const group = page.getByRole('region', { name: 'Workers' });
  const runs = group.getByLabel('Concurrent runs');
  await expect(runs).toHaveValue(/\d+/);
  await runs.fill('6');
  await group.getByRole('button', { name: 'Save' }).click();
  await expect(group.locator('.settings-field', { hasText: 'Concurrent runs' }).locator('.settings-provenance'))
    .toContainText('saved');

  await page.reload();
  const again = page.getByRole('region', { name: 'Workers' });
  await expect(again.getByLabel('Concurrent runs')).toHaveValue('6');
  await expect(again.locator('.settings-field', { hasText: 'Concurrent runs' }).locator('.settings-provenance'))
    .toContainText('saved');
  // The worker reads the same setting; the status card agrees.
  expect((await status(request)).worker.concurrency).toBe(6);
  await expect(page.locator('.sys-card', { hasText: 'Worker' })).toContainText('6 at once');
});

test('the retention group previews what a number would delete before it can be saved', async ({ page }) => {
  await page.goto('/settings');
  const group = page.getByRole('region', { name: 'Retention' });
  const save = group.getByRole('button', { name: 'Save' });
  await expect(save).toBeDisabled();
  await group.getByText('30 days', { exact: true }).click();
  const preview = group.locator('.retention-preview');
  await expect(preview).toContainText(/runs? kept/);
  await expect(preview).toContainText(/Would delete|Nothing to delete/);
  await expect(save).toBeEnabled();
  // A custom figure is typed, and previewed in turn.
  await group.getByText('Custom', { exact: true }).click();
  await group.getByLabel('Days to keep runs').fill('45');
  await expect(preview).toContainText(/runs? kept/);
  // Back to forever: nothing needs previewing, and nothing is unsaved.
  await group.getByText('Forever', { exact: true }).click();
  await expect(preview).toHaveText('Nothing is ever deleted.');
  await expect(save).toBeDisabled();
});

test('saving a retention figure cleans up now, and says so', async ({ page }) => {
  page.on('dialog', dialog => void dialog.accept());
  await page.goto('/settings');
  const group = page.getByRole('region', { name: 'Retention' });
  await group.getByText('1 year', { exact: true }).click();
  await expect(group.locator('.retention-preview')).toContainText(/runs? kept/);
  await group.getByRole('button', { name: 'Save' }).click();
  // Not "within six hours": the cleanup runs at once and reports back.
  await expect(group.locator('.retention-cleanup')).toContainText(/Cleaned up|Deleted/);
  await expect(group.locator('.retention-preview')).toContainText(/Up to date/);
  // Leave the home as the other tests expect it.
  await group.getByText('Forever', { exact: true }).click();
  await group.getByRole('button', { name: 'Save' }).click();
  await expect(group.locator('.settings-provenance')).toContainText('saved');
});

test('pausing asks first, shows a banner, and resuming clears it', async ({ page, request }) => {
  const dialogs: string[] = [];
  page.on('dialog', dialog => { dialogs.push(dialog.message()); void dialog.accept(); });
  await page.goto('/settings');
  await page.getByRole('button', { name: 'Pause everything new' }).click();
  const banner = page.locator('.settings-pause-banner');
  await expect(banner).toContainText('Paused');
  await expect(banner).toContainText('queued runs and services wait');
  expect(dialogs).toHaveLength(1);
  expect(dialogs[0]).toContain('Pause everything new?');
  expect((await status(request)).worker.paused).toBe(true);
  await expect(page.locator('.sys-card', { hasText: 'Worker' })).toContainText('Paused');

  await banner.getByRole('button', { name: 'Resume' }).click();
  await expect(banner).toBeHidden();
  await expect(page.getByRole('button', { name: 'Pause everything new' })).toBeVisible();
  expect((await status(request)).worker.paused).toBe(false);
  // Resuming is not an operator-grade question; no second dialog.
  expect(dialogs).toHaveLength(1);
});
