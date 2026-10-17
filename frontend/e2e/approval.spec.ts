import { expect, test } from '@playwright/test';
import { createWorkflow, expectRunStatus, getRun } from './api';

/* Approval gates shipped fully built — the worker parking on them, the approve
   card, the dashboard panel — and nothing in the app could turn one on. Every
   gate that ever existed had been made by a test helper or by curl. This is the
   whole path a person takes, starting from the control that was missing. */

test('a gate set in the task form holds the run until someone approves it', async ({ page, request }) => {
  const workflow = await createWorkflow(request, 'gated');
  await page.goto(`/workflows/${workflow.id}`);

  await page.getByRole('button', { name: 'Add task' }).first().click();
  const form = page.locator('form', { has: page.getByText('Wait for approval') });
  await form.locator('input[name="name"]').fill('publish');
  await form.locator('textarea[name="command"]').fill('printf published');
  await form.getByText('Wait for approval').click();
  await form.locator('textarea[name="approval_prompt"], input[name="approval_prompt"]').fill('Check the totals first');
  await form.getByRole('button', { name: 'Add task' }).click();
  await expect(form).toBeHidden();

  await page.getByRole('button', { name: 'Run now' }).click();
  await expect(page).toHaveURL(/\/runs\/\d+$/);
  const runId = Number(page.url().split('/').pop());

  // Parked on the gate, showing what the approver was asked to check.
  await expectRunStatus(request, runId, 'waiting_approval');
  await expect(page.getByText('Check the totals first').first()).toBeVisible();

  await page.getByRole('button', { name: 'Approve and continue' }).first().click();
  await expectRunStatus(request, runId, 'success');
  const run = await getRun(request, runId);
  expect(run.task_runs.some(row => row.task_name === 'publish' && row.status === 'success')).toBe(true);
});
