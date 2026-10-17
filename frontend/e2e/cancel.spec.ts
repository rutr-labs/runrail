import { expect, test } from '@playwright/test';
import { addShellTask, createWorkflow, expectRunStatus, getRun, startRun } from './api';

/* Cancel used to relabel the run and leave the task's process running until it
   finished on its own — then retry it. The backend suite proves the process
   dies; this proves the button a person presses gets there. */

test('cancelling from the run page stops the running task, with no retry', async ({ page, request }) => {
  const workflow = await createWorkflow(request, 'long');
  await addShellTask(request, workflow.id, 'crunch', 'sleep 120', { retries: 2 });
  const runId = await startRun(request, workflow.id);
  await expectRunStatus(request, runId, 'running');

  await page.goto(`/runs/${runId}`);
  await page.locator('.cancel-run-btn').first().click();

  // A one-second poll and a process-group kill: seconds, not the 120 the task would take.
  await expectRunStatus(request, runId, 'cancelled', 10_000);
  await expect.poll(async () => (await getRun(request, runId)).task_runs.map(row => [row.attempt, row.status]),
                    { timeout: 10_000 }).toEqual([[1, 'cancelled']]);
  await expect(page.locator('.status-badge', { hasText: /cancelled/i }).first()).toBeVisible();
});
