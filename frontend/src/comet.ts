/* ============================================================
   Particle-comet engine — the shared treatment for every live
   progress surface (wallboard fills, running Gantt bars, and the
   indeterminate LoadingBar).

   Fine embers stream rightward and accelerate into a bright
   layered core at the progress frontier; sparks occasionally
   shed off the head. Indeterminate bars loop the comet instead.

   One shared rAF loop drives every mounted canvas; glow sprites
   are pre-rendered and cached; a single ResizeObserver tracks
   host geometry (fills change width every poll tick). The loop
   only runs while at least one canvas is mounted, and parks
   entirely when the tab is hidden.

   Per-host knobs (read from computed style each frame):
   • --dot-color   — base color (LoadingBar tones override it)
   • --over-ratio  — 0→1 past-median overrun; registered in CSS
     so it interpolates, and the palette crystallizes toward
     --warning: body embers amberize ahead of the head core.
   ============================================================ */

type RGB = [number, number, number];

interface Spark {
  px: number; py: number; vx: number; vy: number;
  life: number; ttl: number; sz: number;
}

interface BodyParticle {
  x: number; yF: number; amp: number; freq: number;
  ph: number; sp: number; sz: number; al: number;
}

export type CometKind = 'fill' | 'loader' | 'still';

interface CometBar {
  host: HTMLElement;
  kind: CometKind;
  canvas: HTMLCanvasElement;
  ctx: CanvasRenderingContext2D;
  w: number; h: number; dpr: number;
  emitAcc: number; sparkAcc: number;
  ti: number; si: number;
  seed: number;
  body: BodyParticle[] | null;
  trail: Spark[] | null;
  sparks: Spark[];
}

const DPR_CAP = 2;
const MAX_BODY = 120;   // hard cap: body particles per determinate bar
const MAX_TRAIL = 90;   // loader trail pool (ring buffer)
const MAX_SPARK = 10;   // sparks shed off the head (ring buffer)
const LOOP_T = 1.9;     // indeterminate sweep period, seconds
const STATIC_T = 0.9;   // frozen timestamp for reduced-motion renders

const reducedMq = typeof window.matchMedia === 'function'
  ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
const reduced = () => !!reducedMq?.matches;

/* ---------------- color helpers ---------------- */

function mix(a: RGB, b: RGB, t: number): RGB {
  return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
}

function rgba(c: RGB, a: number): string {
  return `rgba(${Math.round(c[0])},${Math.round(c[1])},${Math.round(c[2])},${a})`;
}

const parseCache = new Map<string, RGB>();
function parseColor(raw: string, fallback: RGB): RGB {
  const key = raw.trim();
  if (!key) return fallback;
  const hit = parseCache.get(key);
  if (hit) return hit;
  let c: RGB | null = null;
  let m: RegExpExecArray | null;
  if ((m = /^#([0-9a-f]{3})$/i.exec(key))) {
    c = [
      parseInt(m[1][0] + m[1][0], 16),
      parseInt(m[1][1] + m[1][1], 16),
      parseInt(m[1][2] + m[1][2], 16),
    ];
  } else if ((m = /^#([0-9a-f]{6})$/i.exec(key))) {
    c = [
      parseInt(m[1].slice(0, 2), 16),
      parseInt(m[1].slice(2, 4), 16),
      parseInt(m[1].slice(4, 6), 16),
    ];
  } else if ((m = /^rgba?\(([^)]+)\)$/i.exec(key))) {
    const parts = m[1].split(/[\s,/]+/).map(parseFloat);
    if (parts.length >= 3 && parts.slice(0, 3).every(n => !isNaN(n))) {
      c = [parts[0], parts[1], parts[2]];
    }
  }
  if (!c) return fallback;
  parseCache.set(key, c);
  return c;
}

/* --------- pre-rendered glow sprites (built once, cached) --------- */

const spriteCache = new Map<string, HTMLCanvasElement>();
function sprite(color: RGB, whiteness: number): HTMLCanvasElement {
  const key = `${Math.round(color[0])},${Math.round(color[1])},${Math.round(color[2])}|${whiteness}`;
  const hit = spriteCache.get(key);
  if (hit) return hit;
  const S = 64, R = S / 2;
  const cv = document.createElement('canvas');
  cv.width = S;
  cv.height = S;
  const g = cv.getContext('2d');
  if (g) {
    const core = mix(color, [255, 255, 255], whiteness);
    const grad = g.createRadialGradient(R, R, 0, R, R, R);
    grad.addColorStop(0, rgba(core, 1));
    grad.addColorStop(0.16, rgba(mix(color, core, 0.6), 0.9));
    grad.addColorStop(0.42, rgba(color, 0.3));
    grad.addColorStop(1, rgba(color, 0));
    g.fillStyle = grad;
    g.fillRect(0, 0, S, S);
  }
  spriteCache.set(key, cv);
  return cv;
}

/* Quantize color-blend ratios so sprite cache keys stay bounded. */
const quant = (x: number) => Math.round(Math.max(0, Math.min(1, x)) * 8) / 8;

/* ---------------- per-frame theme + palette ---------------- */

interface FrameCtx {
  light: boolean;
  warn: RGB;
  comp: GlobalCompositeOperation;
  coreWhite: number;
  bodyWhite: number;
  aMul: number;
}

function frameCtx(): FrameCtx {
  const light = document.documentElement.dataset.theme === 'light';
  const rs = getComputedStyle(document.documentElement);
  let warn = parseColor(rs.getPropertyValue('--warning'),
    light ? [217, 119, 6] : [251, 191, 36]);
  if (light) warn = mix(warn, [0, 0, 0], 0.1);
  return {
    light,
    warn,
    comp: light ? 'source-over' : 'lighter',
    coreWhite: light ? 0.5 : 0.85,
    bodyWhite: light ? 0.3 : 0.5,
    aMul: light ? 0.95 : 1,
  };
}

/* ---------------- registry + shared loop ---------------- */

const bars: CometBar[] = [];
const stills: CometBar[] = [];
let rafId = 0;
let pollId = 0;
let lastT = 0;
let themeMo: MutationObserver | null = null;

/* Resizing a canvas CLEARS it, and per the HTML spec's "update the rendering"
   steps ResizeObserver callbacks run AFTER requestAnimationFrame callbacks but
   BEFORE paint. A host whose width animates every frame (.wb-progress-fill has
   `transition: width .9s linear`; Gantt bars grow against the wall clock) would
   therefore have the frame we just drew wiped before it was ever painted —
   measured at ~86% blank frames in Chromium, which reads as a bar that shifts
   without animating. So every resize must be followed immediately by a redraw,
   in the same callback. */
const ro = typeof ResizeObserver === 'function'
  ? new ResizeObserver(entries => {
      let C: FrameCtx | null = null;   // computed at most once, only if needed
      for (const e of entries) {
        const bar = bars.find(b => b.host === e.target)
          ?? stills.find(b => b.host === e.target);
        if (!bar) continue;
        const resized = setSize(bar, e.contentRect.width, e.contentRect.height);
        if (bar.kind === 'still') renderStill(bar);
        else if (resized && bar.w >= 2 && bar.h >= 2) {
          // Repaint this frame's content at the new size. dt=0: advance nothing,
          // just re-render — the rAF loop still owns the simulation.
          renderBar(bar, lastT / 1000, 0, (C ??= frameCtx()));
        }
      }
      if (reduced()) renderAll(STATIC_T, 0);
    })
  : null;

/* Stills only repaint on demand, so watch for theme flips. */
function ensureThemeWatch() {
  if (themeMo || typeof MutationObserver !== 'function') return;
  themeMo = new MutationObserver(() => {
    for (const s of stills) renderStill(s);
  });
  themeMo.observe(document.documentElement, {
    attributes: true, attributeFilter: ['data-theme'],
  });
}

/** Returns true when the backing store was reallocated — which blanks it, so
 *  the caller owes the canvas an immediate redraw. */
function setSize(bar: CometBar, w: number, h: number): boolean {
  const dpr = Math.min(window.devicePixelRatio || 1, DPR_CAP);
  const pw = Math.max(1, Math.round(w * dpr));
  const ph = Math.max(1, Math.round(h * dpr));
  let resized = false;
  if (bar.canvas.width !== pw || bar.canvas.height !== ph) {
    bar.canvas.width = pw;
    bar.canvas.height = ph;
    resized = true;
  }
  bar.w = w;
  bar.h = h;
  bar.dpr = dpr;
  return resized;
}

function makePool<T>(n: number, maker: () => T): T[] {
  return Array.from({ length: n }, maker);
}

const newSpark = (): Spark => ({ px: 0, py: 0, vx: 0, vy: 0, life: 1, ttl: 0, sz: 1 });

/* ---------------- head + sparks ---------------- */

function drawHead(
  ctx: CanvasRenderingContext2D, x: number, y: number, h: number,
  t: number, C: FrameCtx, headCol: RGB,
) {
  const pulse = 1 + 0.05 * Math.sin(t * 3.1);
  const glow = sprite(headCol, 0.08);
  const core = sprite(headCol, C.coreWhite);
  let d = h * 3.3 * pulse;
  ctx.globalAlpha = C.light ? 0.4 : 0.5;
  ctx.drawImage(glow, x - d / 2, y - d / 2, d, d);
  d = h * 1.8 * pulse;
  ctx.globalAlpha = C.light ? 0.6 : 0.8;
  ctx.drawImage(glow, x - d / 2, y - d / 2, d, d);
  d = h;
  ctx.globalAlpha = 0.95;
  ctx.drawImage(core, x - d / 2, y - d / 2, d, d);
}

function spawnSpark(bar: CometBar, hx: number, hy: number, sc: number) {
  const s = bar.sparks[bar.si];
  bar.si = (bar.si + 1) % MAX_SPARK;
  s.px = hx - 2;
  s.py = hy;
  s.vx = -(25 + Math.random() * 75) * sc;
  s.vy = (Math.random() - 0.5) * 80 * sc;
  s.life = 0;
  s.ttl = 0.3 + Math.random() * 0.5;
  s.sz = (2.5 + Math.random() * 2.5) * sc;
}

function updateDrawSparks(
  bar: CometBar, ctx: CanvasRenderingContext2D, dt: number, C: FrameCtx, col: RGB,
) {
  const sp = sprite(col, C.coreWhite);
  for (const s of bar.sparks) {
    if (s.ttl <= 0) continue;
    s.life += dt;
    if (s.life >= s.ttl) { s.ttl = 0; continue; }
    s.px += s.vx * dt;
    s.py += s.vy * dt;
    s.vx *= Math.max(0, 1 - dt * 2.2);
    s.vy *= Math.max(0, 1 - dt * 1.2);
    const k = 1 - s.life / s.ttl;
    ctx.globalAlpha = k * 0.85 * C.aMul;
    const d = s.sz * (0.6 + k * 0.9) * 2;
    ctx.drawImage(sp, s.px - d / 2, s.py - d / 2, d, d);
  }
}

/* ---------------- determinate: ember stream + frontier core ------- */

function renderFill(
  bar: CometBar, ctx: CanvasRenderingContext2D, w: number, h: number,
  t: number, dt: number, C: FrameCtx, bodyCol: RGB, headCol: RGB, sc: number,
) {
  const hx = w - 2, hy = h / 2;
  /* density scales with fill width, capped */
  const n = Math.max(8, Math.min(MAX_BODY, Math.round(w * 0.55)));
  const ps = sprite(bodyCol, C.bodyWhite);
  const body = bar.body!;

  if (reduced()) {
    /* Static frame anchored to the frontier in ABSOLUTE px. The animated path
       stores embers as a fraction of the bar (p.x in 0..1), so a frozen field
       re-laid on a widening bar slides and stretches bodily — under reduced
       motion that made the comet look like it was shifting rather than still,
       which is the opposite of the intent. Spacing keys off height, not width,
       so growth moves only the head and its tail.
       Gate on reduced(), never on dt === 0: the resize repaint above renders a
       legitimate frame with dt = 0 while the rAF loop owns the simulation. */
    for (let i = 0; i < 26; i++) {
      const k = 1 - i / 26;
      const sx = hx - 4 - i * Math.max(3, h * 0.55);
      if (sx < 1) break;
      ctx.globalAlpha = k * k * 0.7 * C.aMul;
      const d = (3 + k * 5) * sc;
      ctx.drawImage(ps, sx - d / 2, hy - d / 2 + Math.sin(i * 1.7) * 2 * sc, d, d);
    }
    drawHead(ctx, hx, hy, h, 0, C, headCol);
    return;
  }
  for (let i = 0; i < n; i++) {
    const p = body[i];
    /* accelerate toward the frontier */
    p.x += p.sp * (0.5 + 2.8 * p.x * p.x) * dt;
    if (p.x >= 1) {
      p.x = Math.random() * 0.18;
      p.yF = 0.12 + Math.random() * 0.76;
      p.ph = Math.random() * Math.PI * 2;
    }
    const px = p.x * (w - 3);
    const py = p.yF * (h - 4) + 2 + Math.sin(t * p.freq + p.ph) * p.amp * sc;
    ctx.globalAlpha = p.al * (0.25 + 0.75 * p.x) * C.aMul;
    const d = (3 + p.sz * 3.2) * (0.75 + p.x * p.x * 0.7) * sc;
    ctx.drawImage(ps, px - d / 2, py - d / 2, d, d);
  }

  if (dt > 0) {
    bar.sparkAcc += dt * 2.2;
    if (bar.sparkAcc >= 1) {
      bar.sparkAcc -= 1 + Math.random() * 0.8; /* irregular shedding */
      spawnSpark(bar, hx, hy, sc);
    }
  }
  updateDrawSparks(bar, ctx, dt, C, headCol);
  drawHead(ctx, hx, hy, h, t, C, headCol);
}

/* ---------------- indeterminate: looping comet ---------------- */

function renderLoader(
  bar: CometBar, ctx: CanvasRenderingContext2D, w: number, h: number,
  t: number, dt: number, C: FrameCtx, bodyCol: RGB, headCol: RGB, sc: number,
) {
  const hy = h / 2;
  const span = w + h * 4;
  let hx = -h * 2 + span * ((t / LOOP_T) % 1);
  const ps = sprite(bodyCol, C.bodyWhite);
  const trail = bar.trail!;

  if (reduced()) {
    /* static comet: procedural trail, frozen head */
    hx = w * 0.62;
    for (let i = 0; i < 26; i++) {
      const k = 1 - i / 26;
      const sx = hx - 4 - i * Math.max(3, w * 0.013);
      if (sx < -8) break;
      ctx.globalAlpha = k * k * 0.7 * C.aMul;
      const d = (3 + k * 5) * sc;
      ctx.drawImage(ps, sx - d / 2, hy - d / 2 + Math.sin(i * 1.7) * 2 * sc, d, d);
    }
    drawHead(ctx, hx, hy, h, 0, C, headCol);
    return;
  }

  /* emit trail while the head is on screen */
  if (hx > -h && hx < w + h) {
    bar.emitAcc += dt * 80;
    while (bar.emitAcc >= 1) {
      bar.emitAcc -= 1;
      const p = trail[bar.ti];
      bar.ti = (bar.ti + 1) % MAX_TRAIL;
      p.px = hx - Math.random() * 5;
      p.py = hy + (Math.random() - 0.5) * 5 * sc;
      p.vx = -(8 + Math.random() * 30) * sc;
      p.vy = (Math.random() - 0.5) * 14 * sc;
      p.life = 0;
      p.ttl = 0.45 + Math.random() * 0.6;
      p.sz = 0.6 + Math.random() * 1.1;
    }
    bar.sparkAcc += dt * 1.6;
    if (bar.sparkAcc >= 1) {
      bar.sparkAcc -= 1 + Math.random();
      spawnSpark(bar, hx, hy, sc);
    }
  }

  for (const p of trail) {
    if (p.ttl <= 0) continue;
    p.life += dt;
    if (p.life >= p.ttl) { p.ttl = 0; continue; }
    p.px += p.vx * dt;
    p.py += p.vy * dt;
    const k = 1 - p.life / p.ttl;
    ctx.globalAlpha = k * k * 0.8 * C.aMul;
    const d = (2.5 + p.sz * 3) * (0.5 + k * 0.6) * sc;
    ctx.drawImage(ps, p.px - d / 2, p.py - d / 2, d, d);
  }

  updateDrawSparks(bar, ctx, dt, C, headCol);
  if (hx > -h * 2 && hx < w + h * 2) drawHead(ctx, hx, hy, h, t, C, headCol);
}

/* ---------------- finished: the comet at rest ----------------
   One static frame for bars whose run is over. Stillness comes
   from ORDER: the live comet is a chaos of drifting embers, so
   the finished bar is the opposite — a perfectly even dot rail
   on the centre line (identical dots, fixed spacing, the faintest
   brightness ramp), closed by a compact solid endcap orb. A track
   laid down and cooled, not a snapshot of flight. The status
   colour (--dot-color: success green, danger red) says how it
   ended. Fully deterministic — nothing to reshuffle on repaint. */

function renderStill(bar: CometBar) {
  const { ctx, w, h } = bar;
  if (w < 2 || h < 2) return;
  const C = frameCtx();
  const cs = getComputedStyle(bar.host);
  const col = parseColor(cs.getPropertyValue('--dot-color'),
    C.light ? [5, 150, 105] : [52, 211, 153]);

  ctx.setTransform(bar.dpr, 0, 0, bar.dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  ctx.globalCompositeOperation = C.comp;

  const sc = Math.max(0.4, Math.min(1.25, h / 16));
  const hy = h / 2;
  const capX = w - 2 - h * 0.2;

  /* even dot rail: identical dots on the centre line, subtle ramp */
  const gap = Math.max(6, 8 * sc);
  const dot = sprite(col, C.bodyWhite * 0.55);
  const d = 3.4 * sc;
  const n = Math.min(120, Math.floor((capX - h * 0.9) / gap));
  for (let i = 0; i < n; i++) {
    const px = capX - h * 0.9 - i * gap;   /* anchored at the endcap */
    if (px < 3) break;
    const x = px / w;
    ctx.globalAlpha = (0.22 + 0.34 * x) * C.aMul;
    ctx.drawImage(dot, px - d / 2, hy - d / 2, d, d);
  }

  /* endcap: a solid, cooled orb — a period, not a flame */
  const halo = sprite(col, 0.08);
  const core = sprite(col, C.coreWhite * 0.6);
  let cd = h * 1.5;
  ctx.globalAlpha = C.light ? 0.22 : 0.3;
  ctx.drawImage(halo, capX - cd / 2, hy - cd / 2, cd, cd);
  cd = h * 0.8;
  ctx.globalAlpha = 0.92;
  ctx.drawImage(core, capX - cd / 2, hy - cd / 2, cd, cd);

  ctx.globalAlpha = 1;
  ctx.globalCompositeOperation = 'source-over';
}

/* ---------------- frame orchestration ---------------- */

function renderBar(bar: CometBar, t: number, dt: number, C: FrameCtx) {
  const { ctx, w, h } = bar;
  ctx.setTransform(bar.dpr, 0, 0, bar.dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);

  const cs = getComputedStyle(bar.host);
  let ratio = parseFloat(cs.getPropertyValue('--over-ratio')) || 0;
  ratio = Math.max(0, Math.min(1, ratio));
  const run = parseColor(cs.getPropertyValue('--dot-color'),
    C.light ? [2, 132, 199] : [56, 189, 248]);

  /* body/trail amberize ahead of the head core */
  const bodyCol = mix(run, C.warn, quant(Math.min(1, ratio * 1.6)));
  const headCol = mix(run, C.warn, quant(Math.max(0, (ratio - 0.45) / 0.55)));

  /* Bars come in several heights (16px wallboard, 10px loader,
     6px sm loader, Gantt lanes) — scale particle geometry to fit. */
  const sc = Math.max(0.4, Math.min(1.25, h / 16));

  ctx.globalCompositeOperation = C.comp;
  if (bar.kind === 'fill') {
    renderFill(bar, ctx, w, h, t, dt, C, bodyCol, headCol, sc);
  } else {
    renderLoader(bar, ctx, w, h, t, dt, C, bodyCol, headCol, sc);
  }
  ctx.globalAlpha = 1;
  ctx.globalCompositeOperation = 'source-over';
}

function renderAll(t: number, dt: number) {
  if (!bars.length) return;
  const C = frameCtx();
  for (const bar of bars) {
    if (!ro) setSize(bar, bar.host.clientWidth, bar.host.clientHeight);
    if (bar.w < 2 || bar.h < 2) continue;
    renderBar(bar, t, dt, C);
  }
}

function frame(ts: number) {
  rafId = window.requestAnimationFrame(frame);
  if (document.hidden) { lastT = 0; return; }
  if (!lastT) { lastT = ts; return; }
  let dt = (ts - lastT) / 1000;
  lastT = ts;
  if (dt <= 0) return;
  if (dt > 0.05) dt = 0.05; /* clamp: tab-switch / jank spikes */
  renderAll(ts / 1000, dt);
}

function resetClock() { lastT = 0; }

function ensureLoop() {
  if (reduced()) {
    renderAll(STATIC_T, 0);
    /* slow poll keeps theme + over-ratio honest without motion */
    if (!pollId) {
      pollId = window.setInterval(() => {
        if (!document.hidden) renderAll(STATIC_T, 0);
      }, 500);
    }
    return;
  }
  if (!rafId) {
    lastT = 0;
    document.addEventListener('visibilitychange', resetClock);
    rafId = window.requestAnimationFrame(frame);
  }
}

/* The OS motion preference can flip while the app is open (and on Windows it
   is the general "Animation effects" toggle, not a motion-specific one, so it
   flips more often than you would think). Without this the engine stays parked
   on its frozen frame — or keeps animating — until a full reload.
   Optional-call: older WebKit MediaQueryList only has addListener. */
reducedMq?.addEventListener?.('change', () => {
  if (rafId) {
    window.cancelAnimationFrame(rafId);
    document.removeEventListener('visibilitychange', resetClock);
    rafId = 0;
  }
  if (pollId) { window.clearInterval(pollId); pollId = 0; }
  lastT = 0;
  if (bars.length) ensureLoop();
  for (const s of stills) renderStill(s);
});

function stopLoopIfIdle() {
  if (bars.length) return;
  if (rafId) {
    window.cancelAnimationFrame(rafId);
    document.removeEventListener('visibilitychange', resetClock);
    rafId = 0;
  }
  if (pollId) {
    window.clearInterval(pollId);
    pollId = 0;
  }
}

/* ---------------- public API ---------------- */

/** Bind the comet to a canvas that fills its positioned parent.
    'fill' streams into the host's right edge, 'loader' loops, and
    'still' paints the landed comet once (finished bars).
    Returns a detach function (for effect cleanup). */
export function attachComet(canvas: HTMLCanvasElement, kind: CometKind): () => void {
  const host = canvas.parentElement as HTMLElement | null;
  const ctx = canvas.getContext('2d');
  if (!host || !ctx) return () => {};

  const bar: CometBar = {
    host, kind, canvas, ctx,
    w: 0, h: 0, dpr: 1,
    emitAcc: 0, sparkAcc: 0, ti: 0, si: 0,
    seed: (Math.random() * 2147483648) | 0,
    body: kind === 'fill'
      ? makePool(MAX_BODY, (): BodyParticle => ({
          x: Math.random(),                    // 0..1 across current fill width
          yF: 0.12 + Math.random() * 0.76,     // vertical anchor (fraction)
          amp: 0.6 + Math.random() * 1.6,      // waver amplitude, px
          freq: 1.5 + Math.random() * 3.5,     // waver frequency
          ph: Math.random() * Math.PI * 2,
          sp: 0.05 + Math.random() * 0.13,     // base speed, widths/sec
          sz: 0.55 + Math.random() * 1.0,
          al: 0.35 + Math.random() * 0.5,
        }))
      : null,
    trail: kind === 'loader' ? makePool(MAX_TRAIL, newSpark) : null,
    sparks: makePool(MAX_SPARK, newSpark),
  };
  setSize(bar, host.clientWidth, host.clientHeight);

  if (kind === 'still') {
    stills.push(bar);
    ro?.observe(host);        /* RO fires on observe → first paint */
    if (!ro) renderStill(bar);
    ensureThemeWatch();
    return () => {
      ro?.unobserve(host);
      const i = stills.indexOf(bar);
      if (i !== -1) stills.splice(i, 1);
      if (!stills.length && themeMo) {
        themeMo.disconnect();
        themeMo = null;
      }
    };
  }

  bars.push(bar);
  ro?.observe(host);
  ensureLoop();

  return () => {
    ro?.unobserve(host);
    const i = bars.indexOf(bar);
    if (i !== -1) bars.splice(i, 1);
    stopLoopIfIdle();
  };
}
