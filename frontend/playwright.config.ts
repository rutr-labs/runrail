import { defineConfig } from '@playwright/test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { isAbsolute, join, resolve } from 'node:path';

/* ─── End-to-end ───────────────────────────────────────────
   The real server — API, worker and scheduler in one `runrail serve` — over the
   built UI, against a database that exists only for this run. Each flow here is
   one a person found broken by using the app while every unit test passed: a
   setting with no control, a tab that opened the wrong attempt, a cancel that
   left the work running, a page that scrolled sideways.

   Needs the UI built into the Python package (`npm run build`) and RunRail
   installed; RUNRAIL_BIN points at a specific install, e.g. ../.venv/bin/runrail.
   PW_CHANNEL=chrome uses an installed Chrome instead of Playwright's own. */

const PORT = Number(process.env.RUNRAIL_E2E_PORT ?? 8765);

// Set once in the main process; test workers inherit it through the environment.
process.env.RUNRAIL_E2E_HOME ??= mkdtempSync(join(tmpdir(), 'runrail-e2e-'));
const home = process.env.RUNRAIL_E2E_HOME;

const bin = process.env.RUNRAIL_BIN ?? 'runrail';
const command = bin.includes('/') && !isAbsolute(bin) ? resolve(bin) : bin;

export default defineConfig({
  testDir: 'e2e',
  // One server and one database: flows run in sequence so none of them sees
  // another's runs land mid-assertion.
  workers: 1,
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [['github'], ['list']] : 'list',
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    channel: process.env.PW_CHANNEL || undefined,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: `"${command}" serve --host 127.0.0.1 --port ${PORT}`,
    // Run from the scratch directory, so a developer's .env in the repository
    // can never point this suite at their real database.
    cwd: home,
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: false,
    timeout: 60_000,
    env: {
      RUNRAIL_HOME: join(home, 'home'),
      RUNRAIL_DB_URL: `sqlite:///${join(home, 'runrail.db')}`,
      RUNRAIL_BROWSE_ROOT: home,
      RUNRAIL_WORKER_POLL_SECONDS: '0.2',
    },
  },
});
