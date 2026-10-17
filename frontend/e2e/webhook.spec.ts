import { expect, test } from '@playwright/test';
import { createWorkflow } from './api';

/* A failing webhook used to fail silently forever. The test button posts to
   the saved URL and says what came back; the workflow page warns when the
   last real delivery failed. */

test('the webhook test button reports what the receiver answered', async ({ page, request, baseURL }) => {
  // The server's own health endpoint refuses POST: a receiver that answers,
  // and answers no.
  const workflow = await createWorkflow(request, 'hooked');
  await request.put(`/api/workflows/${workflow.id}`, {
    data: { name: workflow.name, notify_webhook_url: `${baseURL}/api/health` } });

  await page.goto(`/workflows/${workflow.id}`);
  await page.getByRole('button', { name: 'Edit' }).first().click();
  const form = page.locator('form', { has: page.getByText('Failure webhook') });
  await form.getByRole('button', { name: 'Send a test message' }).click();
  await expect(form.locator('.webhook-test-result')).toContainText('answered: HTTP 405');

  // The failure is recorded on the workflow and shown on its page.
  await page.keyboard.press('Escape');
  await page.reload();
  await expect(page.locator('.webhook-warn')).toContainText('HTTP 405');
});
