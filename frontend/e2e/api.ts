import { expect, type APIRequestContext } from '@playwright/test';

/* Setup goes through the API; the flow under test goes through the UI. A test
   that clicked its way through creating every fixture would fail for reasons
   that have nothing to do with what it is about. */

export interface Workflow { id: number; name: string }
export interface Run { id: number; status: string; task_runs: { id: number; task_name: string; attempt: number; status: string }[] }

/** Unique per call, so reruns against a reused database never collide. */
export const unique = (name: string) => `${name}-${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`;

export async function createWorkflow(request: APIRequestContext, name: string): Promise<Workflow> {
  const response = await request.post('/api/workflows', { data: { name: unique(name), enabled: true, max_concurrent_runs: 1 } });
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}

export async function addShellTask(request: APIRequestContext, workflowId: number, name: string,
                                   command: string, extra: Record<string, unknown> = {}) {
  const response = await request.post(`/api/workflows/${workflowId}/tasks`, {
    data: { name, task_type: 'shell', command, depends_on_json: [], retries: 0, retry_delay_seconds: 0, ...extra },
  });
  expect(response.ok(), await response.text()).toBeTruthy();
  return response.json();
}

export async function startRun(request: APIRequestContext, workflowId: number): Promise<number> {
  const response = await request.post(`/api/workflows/${workflowId}/run`, { data: { parameters: {} } });
  expect(response.ok(), await response.text()).toBeTruthy();
  return (await response.json()).id;
}

export async function getRun(request: APIRequestContext, runId: number): Promise<Run> {
  return (await request.get(`/api/runs/${runId}`)).json();
}

/** Waits for the run to reach `status`, as the API reports it. */
export async function expectRunStatus(request: APIRequestContext, runId: number, status: string,
                                      timeout = 20_000) {
  await expect.poll(async () => (await getRun(request, runId)).status, { timeout }).toBe(status);
}
