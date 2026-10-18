import { expect, test } from '@playwright/test';

/* The Settings page against the real server: the status cards read what
   /api/system/status says, a setting saved from a group survives a reload and
   says where it came from, a retention number is previewed before it may be
   saved, and the pause switch — behind a confirm, because it is an operator
   action — pauses and resumes. */

interface Status { version: string; worker: { paused: boolean; concurrency: number } }

const status = async (request: import('@playwright/test').APIRequestContext): Promise<Status> =>
  (await request.get('/api/system/status')).json();

test('the status cards show this server', async ({ page, request }) => {
  const { version } = await status(request);
  await page.goto('/settings');
  const cards = page.locator('.settings-status');
  await expect(cards.locator('.sys-card', { hasText: 'Version' })).toContainText(`v${version}`);
  await expect(cards.locator('.sys-card', { hasText: 'Uptime' })).toContainText(/\d+[smhd]/);
  await expect(cards.locator('.sys-card', { hasText: 'Database' })).toContainText('sqlite');
  await expect(cards.locator('.sys-card', { hasText: 'Worker' })).toContainText('running');
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
  await group.getByLabel('Keep forever').uncheck();
  await group.getByLabel('Keep runs for').fill('30');
  const preview = group.locator('.retention-preview');
  await expect(preview).toContainText(/runs? kept/);
  await expect(preview).toContainText(/Would delete|Nothing to delete/);
  await expect(save).toBeEnabled();
  // Back to never: the number is irrelevant and nothing needs previewing.
  await group.getByLabel('Keep forever').check();
  await expect(preview).toHaveText('Nothing is ever deleted.');
  await expect(save).toBeDisabled();
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
