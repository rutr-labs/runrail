/* Placing a timeline lane's bars.

   A lane holds one task's attempts (and any approval gate), one after another.
   Each bar needs a minimum width to be seen and hovered, and that minimum is
   what made bars overlap: a 0.1-second attempt retried at once is drawn wider
   than the time it took, so it ran over the next attempt, and two bars near
   the end of the axis were both clamped to the same last position. Failed
   runs are where this showed, because that is where fast failures and quick
   retries are.

   So a lane is placed as a whole: every bar keeps its true start where it
   can, gets at least the minimum width, and is nudged just far enough that
   it neither covers its neighbour nor leaves the track. The tooltips still
   carry the true times. */

export interface LaneSpan {
  /** Percent of the axis where the bar truly starts. */
  left: number;
  /** Percent of the axis the bar truly lasts. */
  width: number;
}

/** Place one lane's bars, given in execution order, inside 0–100%.
 *  `minWidth` and `gap` are percentages of the track. */
export function packLane(spans: LaneSpan[], minWidth: number, gap: number): LaneSpan[] {
  const n = spans.length;
  if (!n) return [];
  // A lane with more attempts than fit at the minimum width shares the track
  // out evenly instead, so the bars still sit side by side.
  const min = Math.max(0, Math.min(minWidth, (100 - gap * (n - 1)) / n));
  const out = spans.map(s => ({
    left: Math.min(Math.max(s.left, 0), 100),
    width: Math.min(Math.max(s.width, min), 100),
  }));

  // Forward: no bar starts before the previous one has ended.
  for (let i = 1; i < n; i++) {
    const floor = out[i - 1].left + out[i - 1].width + gap;
    if (out[i].left < floor) out[i].left = floor;
  }
  // Backward: nothing runs past the end of the track or into the next bar.
  // A bar longer than its room is shortened first (never below the minimum),
  // and only then moved left.
  let ceiling = 100;
  for (let i = n - 1; i >= 0; i--) {
    const bar = out[i];
    if (bar.left + bar.width > ceiling) {
      bar.width = Math.max(min, ceiling - bar.left);
      if (bar.left + bar.width > ceiling) bar.left = ceiling - bar.width;
    }
    bar.left = Math.max(bar.left, 0);
    ceiling = bar.left - gap;
  }
  return out;
}
