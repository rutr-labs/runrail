// The README's screenshots and demo GIF, captured from a seeded demo home.
//
//   python scripts/seed_demo.py --home /tmp/rr-demo --reset
//   RUNRAIL_HOME=/tmp/rr-demo runrail serve --port 8795      # in another terminal
//   cd frontend && node ../scripts/capture_demo.mjs http://localhost:8795 /tmp/shots shots
//   cd frontend && node ../scripts/capture_demo.mjs http://localhost:8795 /tmp/frames gif
//   ffmpeg -framerate 2.5 -i /tmp/frames/frame-%03d.png -vf "setpts=PTS/1.35,fps=4,scale=1800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=192:stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle" -loop 0 raw.gif
//   gifsicle -O3 --lossy=60 raw.gif -o docs/img/demo.gif
//
// Run from frontend/, where @playwright/test is installed; uses the Chrome on
// this machine (channel: 'chrome'). Stills are 1440x900 at 1.25x, dark theme,
// with animations switched off so a frame never lands mid-transition. Runs of
// the seeded quick workflows are started while capturing, so the board shows
// work beginning, progressing and landing.
import { chromium } from '@playwright/test';
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

async function shot(path, file, { scroll = 0, settle = 1500 } = {}) {
  await page.goto(base + path, { waitUntil: 'networkidle' });
  await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
  await calm();  // a still must not land mid-glide when the board re-sorts
  await page.waitForTimeout(settle);
  if (scroll) await page.evaluate(y => window.scrollTo(0, y), scroll);
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
  // Fresh runs in flight for the board, started just before the shot.
  for (const w of quick.slice(0, 3)) await startRun(w.id);
  await page.waitForTimeout(4000);
  await shot('/wallboard', '03-wallboard.png', { settle: 800 });
} else {
  // GIF frames: the wallboard, one frame every 400ms for 24s, at the same scale.
  await page.goto(base + '/wallboard', { waitUntil: 'networkidle' });
  await calm();
  await page.waitForTimeout(1500);
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
