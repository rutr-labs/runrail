import { expect, test } from '@playwright/test';
import { addShellTask, createWorkflow, expectRunStatus, startRun } from './api';

/* Pages pushed sideways between ~1100 and 1350px and on phones, because grid
   children could not shrink below their content — found by resizing a window,
   not by any test. This sweep is the check that found the rest of them, made
   permanent: no page may scroll horizontally at any of these widths. */

const WIDTHS = [390, 768, 962, 1180, 1440];

test('no page scrolls sideways at phone, tablet or laptop widths', async ({ page, request }) => {
  // Content that stresses a layout: a long unbroken name, a retried task, a
  // failed run, and a run with a gate.
  const long = await createWorkflow(request, 'a-workflow-name-long-enough-to-overflow-a-narrow-card-header');
  await addShellTask(request, long.id, 'extract', 'printf ok');
  await addShellTask(request, long.id, 'transform-with-a-long-task-name', 'exit 1',
                     { depends_on_json: ['extract'], retries: 1 });
  const failed = await startRun(request, long.id);
  await expectRunStatus(request, failed, 'failed');
  const gated = await createWorkflow(request, 'gated-layout');
  await addShellTask(request, gated.id, 'publish', 'printf p', { requires_approval: true });
  const waiting = await startRun(request, gated.id);
  await expectRunStatus(request, waiting, 'waiting_approval');

  const service = await (await request.post('/api/services', { data: {
    name: 'a-service-name-long-enough-to-overflow-a-narrow-card-header', command: 'sleep 600',
    start_mode: 'schedule', timezone: 'UTC', lock_resource: 'gpu',
    windows: [{ days: ['mon', 'tue', 'wed', 'thu', 'fri'], start: '08:00', end: '12:30' },
              { days: ['fri'], start: '22:00', end: '06:00' }],
  } })).json();

  const pages = ['/', '/workflows', `/workflows/${long.id}`, '/runs', `/runs/${failed}`,
                 `/runs/${waiting}`, '/services', `/services/${service.id}`, '/projects',
                 '/environments', '/logs', '/artifacts', '/settings', '/wallboard'];
  const overflowing: string[] = [];
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 900 });
    for (const path of pages) {
      await page.goto(path);
      await page.waitForLoadState('networkidle');
      const excess = await page.evaluate(() =>
        document.documentElement.scrollWidth - document.documentElement.clientWidth);
      if (excess > 1) overflowing.push(`${path} at ${width}px: ${excess}px too wide`);
    }
  }
  expect(overflowing).toEqual([]);
});
