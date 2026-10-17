import { expect, test } from '@playwright/test';
import { unique } from './api';

/* Services, the whole path a person takes: make one in the form, start it,
   watch its output arrive, stop it — and build a schedule by dragging across
   the week. The supervisor is the real one inside `runrail serve`. */

interface ServiceOut { id: number; name: string; state: string; windows: { days: string[]; start: string; end: string }[] }

async function serviceNamed(request: import('@playwright/test').APIRequestContext, name: string) {
  const all: ServiceOut[] = await (await request.get('/api/services')).json();
  return all.find(s => s.name === name);
}

test('a service made in the form starts, logs, and stops', async ({ page, request }) => {
  const name = unique('ticker');
  await page.goto('/services');
  await page.getByRole('button', { name: 'New service' }).click();
  const form = page.locator('form.service-form');
  await form.locator('input[name="name"]').fill(name);
  await form.locator('textarea[name="command"]').fill('while true; do echo "tick from $0"; sleep 1; done');
  await form.getByRole('radio', { name: 'On demand' }).click();
  await form.getByRole('button', { name: 'Create service' }).click();
  await expect(form).toBeHidden();

  const card = page.locator('article.service-card', { hasText: name });
  await expect(card.locator('.status-badge')).toHaveText(/Stopped/);
  await card.getByRole('button', { name: 'Start' }).click();
  await expect(card.locator('.status-badge')).toHaveText(/Running/, { timeout: 15_000 });

  await card.getByRole('link', { name: 'Logs & history →' }).click();
  await expect(page.locator('pre.service-log-body')).toContainText('tick from', { timeout: 15_000 });

  await page.getByRole('button', { name: 'Stop' }).first().click();
  await expect(page.locator('.service-status .status-badge')).toHaveText(/Stopped/, { timeout: 20_000 });
  await expect(page.locator('table')).toContainText('Stopped by you');
});

test('dragging across the week adds a window that the service keeps', async ({ page, request }) => {
  const name = unique('sync');
  await page.goto('/services');
  await page.getByRole('button', { name: 'New service' }).click();
  const form = page.locator('form.service-form');
  await form.locator('input[name="name"]').fill(name);
  await form.locator('textarea[name="command"]').fill('sleep 600');
  await form.getByRole('radio', { name: 'Scheduled' }).click();

  // The default window is weekdays 09:00–17:00; drag 12:00 → 18:00 on Sunday.
  const sunday = form.locator('.week-row', { hasText: 'Sun' }).locator('.week-track');
  await sunday.scrollIntoViewIfNeeded();
  const box = (await sunday.boundingBox())!;
  const x = (hour: number) => box.x + (box.width * hour) / 24;
  await page.mouse.move(x(12), box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(x(15), box.y + box.height / 2, { steps: 4 });
  await page.mouse.move(x(18), box.y + box.height / 2, { steps: 4 });
  await page.mouse.up();
  await expect(form.locator('.window-row')).toHaveCount(2);
  await expect(form.getByLabel('Window 2 start')).toHaveValue('12:00');
  await expect(form.getByLabel('Window 2 end')).toHaveValue('18:00');

  await form.getByRole('button', { name: 'Create service' }).click();
  await expect(form).toBeHidden();
  const saved = await serviceNamed(request, name);
  expect(saved?.windows).toEqual([
    { days: ['mon', 'tue', 'wed', 'thu', 'fri'], start: '09:00', end: '17:00' },
    { days: ['sun'], start: '12:00', end: '18:00' },
  ]);
});

test('the dashboard lists services and starts one in a click', async ({ page, request }) => {
  const name = unique('dash');
  const created = await request.post('/api/services', {
    data: { name, command: 'while true; do echo up; sleep 1; done', start_mode: 'manual' } });
  expect(created.ok()).toBeTruthy();

  await page.goto('/');
  const row = page.locator('.svc-row', { hasText: name });
  await expect(row.locator('.svc-word')).toHaveText('Off');
  await row.getByRole('button', { name: `Start ${name}` }).click();
  await expect(row.locator('.svc-word')).toHaveText('Up', { timeout: 15_000 });
  await row.getByRole('button', { name: `Stop ${name}` }).click();
  await expect(row.locator('.svc-word')).toHaveText('Off', { timeout: 20_000 });

  await page.goto('/wallboard');
  await expect(page.locator('.wb-service', { hasText: name })).toBeVisible();
});
