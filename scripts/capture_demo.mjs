// The README's screenshots and demo GIF, captured from a seeded demo home.
//
//   python scripts/seed_demo.py --home /tmp/rr-demo --reset
//   RUNRAIL_HOME=/tmp/rr-demo runrail serve --port 8795      # in another terminal
//   python scripts/stage_demo.py http://127.0.0.1:8795 /tmp/rr-orders-repo
//   cd frontend && node ../scripts/capture_demo.mjs http://localhost:8795 /tmp/shots shots
//   for f in /tmp/shots/*.png; do ffmpeg -i "$f" -c:v libwebp -quality 88 -compression_level 6 "docs/img/$(basename "$f" .png).webp"; done
//   cd frontend && node ../scripts/capture_demo.mjs http://localhost:8795 /tmp/frames gif
//   ffmpeg -framerate 2.5 -i /tmp/frames/frame-%03d.png -vf "setpts=PTS/1.35,fps=4,scale=1800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=192:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" -loop 0 raw.gif
//   gifsicle -O3 --lossy=60 raw.gif -o docs/img/demo.gif
//
// Run from frontend/, where @playwright/test is installed; uses the Chrome on
// this machine (channel: 'chrome'). Stills are 1440x900 at 1.25x, dark theme,
// with animations switched off so a frame never lands mid-transition. Runs of
// the seeded quick workflows are started while capturing, so the board shows
// work beginning, progressing and landing. Stills are kept as WebP: the
// page's sky gradient makes a PNG of it four times the size.
import { createRequire } from 'node:module';
// Resolved from the working directory (frontend/), not from this file's.
const { chromium } = createRequire(`${process.cwd()}/`)('@playwright/test');
import { mkdirSync } from 'node:fs';

const [base, out, mode = 'shots'] = process.argv.slice(2);
mkdirSync(out, { recursive: true });

const browser = await chromium.launch({ channel: 'chrome' });
const context = await browser.newContext({
  viewport: { width: 1440, height: 900 }, deviceScaleFactor: 1.25, colorScheme: 'dark',
});
const page = await context.newPage();
// Dark theme, and no ambient motion: a GIF of a breathing glow is just noise.
await page.addInitScript(() => {
  try { localStorage.setItem('runrail-theme', 'dark'); } catch {}
});
const calm = () => page.addStyleTag({ content: `
  *, *::before, *::after { animation-duration: 0s !important; animation-iteration-count: 1 !important; transition-duration: 0s !important; }
  .wallboard-aurora { display: none !important; }
` });

async function shot(path, file, { scroll = 0, settle = 1500, at, then } = {}) {
  await page.goto(base + path, { waitUntil: 'networkidle' });
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await calm();  // a still must not land mid-glide when the board re-sorts
  await page.waitForTimeout(settle);
  if (scroll) await page.evaluate(y => window.scrollTo(0, y), scroll);
  // `at`: bring this element to just under the top bar.
  if (at) {
    await page.locator(at).first().waitFor({ timeout: 15000 });
    await page.evaluate(sel => {
      const top = document.querySelector(sel).getBoundingClientRect().top + window.scrollY;
      window.scrollTo(0, top - 84);
    }, at);
  }
  if (then) await then();
  await page.waitForTimeout(300);
  await page.screenshot({ path: `${out}/${file}`, fullPage: false });
  console.log('shot', file);
}

// Workflows that finish in 6–20s, by the stand-in pipelines' own timings: the
// ones worth starting while a frame is being taken.
async function quickWorkflows() {
  const flows = await (await page.request.get(base + '/api/workflows')).json();
  const picked = [];
  for (const w of flows) {
    if (!w.enabled) continue;
    const tasks = await (await page.request.get(`${base}/api/workflows/${w.id}/tasks`)).json();
    if (tasks.some(t => t.requires_approval)) continue;
    const total = tasks.reduce((sum, t) => sum + Number((t.command.match(/--seconds (\d+)/) ?? [0, 0])[1]), 0);
    if (total >= 6 && total <= 20) picked.push({ id: w.id, total });
    if (picked.length === 4) break;
  }
  return picked;
}
const startRun = id => page.request.post(`${base}/api/workflows/${id}/run`, { data: { parameters: {} } }).catch(() => {});

if (mode === 'shots') {
  // Two runs in flight for the dashboard and the wallboard.
  const quick = await quickWorkflows();
  for (const w of quick.slice(0, 2)) await startRun(w.id);
  await page.waitForTimeout(3000);
  await shot('/', '01-dashboard.png');
  await shot('/services', '06-services.png');
  const services = await (await page.request.get(base + '/api/services')).json();
  const running = services.find(s => s.state === 'running' && s.start_mode === 'schedule') ?? services[0];
  await shot(`/services/${running.id}`, '07-service.png');
  // A finished run with its timeline: the first quick run, once it has landed.
  const runs = await (await page.request.get(base + '/api/runs?limit=50')).json();
  const finished = runs.find(r => r.status === 'success' && quick.some(w => w.id === r.workflow_id))
    ?? runs.find(r => r.status === 'success');
  if (finished) await shot(`/runs/${finished.id}`, '02-run-detail.png');
  // A run waiting at an approval gate. The seeded home parks some; starting a
  // new one would only queue behind them (one run of a workflow at a time).
  const gates = await (await page.request.get(base + '/api/approvals')).json();
  if (gates.length) await shot(`/runs/${gates[0].run_id}`, '05-approval.png');
  // Settings once the home folder has been measured (a background walk).
  for (let i = 0; i < 60; i++) {
    const status = await (await page.request.get(base + '/api/system/status')).json();
    if (status.storage.measured_at) break;
    await page.waitForTimeout(500);
  }
  await shot('/settings', '08-settings.png');
  await shot('/settings', '11-variables.png', { at: 'section.settings-vars' });
  // Every run of the last day, laid out against time.
  await shot('/runs?view=timeline&span=24h', '12-timeline.png', { settle: 2500 });
  // The scenes scripts/stage_demo.py sets up: a run form, and a run that
  // failed after a code change, beside the one before it that succeeded.
  const flows = await (await page.request.get(base + '/api/workflows')).json();
  const orders = flows.find(w => w.name === 'orders_daily');
  if (orders) {
    await shot(`/workflows/${orders.id}`, '10-run-form.png', {
      then: async () => {
        await page.getByRole('button', { name: /Run with/ }).click();
        await page.locator('.modal').waitFor();
      },
    });
    const [failed] = await (await page.request.get(`${base}/api/runs?workflow_id=${orders.id}&status=failed&limit=1`)).json();
    if (failed) await shot(`/runs/${failed.id}`, '09-what-changed.png', { at: 'section.changes', settle: 2500 });
  }
} else {
  // GIF frames: the wallboard, one frame every 400ms for 24s, at the same scale.
  await page.goto(base + '/wallboard', { waitUntil: 'networkidle' });
  await calm();
  await page.waitForTimeout(1500);
  // Services and the timetable fill the frame: the headline above them holds
  // still, and they are where runs begin, progress and land.
  await page.evaluate(() => {
    const services = document.querySelector('section.wb-services');
    if (services) window.scrollTo(0, services.getBoundingClientRect().top + window.scrollY - 32);
  });
  const frames = Number(process.env.FRAMES ?? 60);
  // Runs started while recording, so the board shows work beginning,
  // progressing and landing rather than a still.
  const quick = await quickWorkflows();
  const delays = [0, 3, 8, 13];
  quick.forEach((w, i) => setTimeout(() => startRun(w.id), delays[i] * 1000));
  for (let i = 0; i < frames; i++) {
    const t = Date.now();
    await page.screenshot({ path: `${out}/frame-${String(i).padStart(3, '0')}.png` });
    const left = 400 - (Date.now() - t);
    if (left > 0) await page.waitForTimeout(left);
  }
  console.log('frames', frames);
}
await browser.close();
