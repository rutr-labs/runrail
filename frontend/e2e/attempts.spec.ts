import { expect, test } from '@playwright/test';
import { addShellTask, createWorkflow, expectRunStatus, startRun } from './api';

/* A retried task used to be listed once per attempt under the same name, and
   the tabs selected by name — so the second tab opened the first attempt. */

test('a retried task is one card, and each attempt opens its own output', async ({ page, request }, info) => {
  const flag = info.outputPath('failed-once');
  const workflow = await createWorkflow(request, 'flaky');
  // Fails the first time (on stderr, which a failed attempt opens on), succeeds
  // the second: the flag survives between attempts.
  await addShellTask(request, workflow.id, 'load',
    `if [ -f "${flag}" ]; then printf second-attempt-ok; else touch "${flag}"; printf first-attempt-boom >&2; exit 1; fi`,
    { retries: 1 });
  const runId = await startRun(request, workflow.id);
  await expectRunStatus(request, runId, 'success');

  await page.goto(`/runs/${runId}`);
  await expect(page.locator('article.run-task')).toHaveCount(1);
  const card = page.locator('article.run-task');
  await expect(card.getByText('2 attempts')).toBeVisible();

  const switcher = card.getByRole('group', { name: 'load attempts' });
  // Opens on the attempt the task ended as.
  await expect(switcher.getByRole('button', { name: /Attempt 2/ })).toHaveAttribute('aria-pressed', 'true');
  // The log itself, not the command line above it, which contains both strings.
  const log = card.locator('pre.log-content');
  await expect(log).toHaveText('second-attempt-ok');

  await switcher.getByRole('button', { name: /Attempt 1/ }).click();
  await expect(log).toHaveText('first-attempt-boom');
});
