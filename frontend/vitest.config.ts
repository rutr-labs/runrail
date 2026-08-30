import { defineConfig } from 'vitest/config';

/* Pinned before the pool forks, and inherited by every worker. Several
   assertions are about what a viewer in one zone reads about a schedule in
   another, so the viewer's zone has to be a constant rather than whatever the
   laptop or the CI runner happens to be set to. */
process.env.TZ = 'UTC';

/* Deliberately not a `test` block inside vite.config.ts: nothing here renders a
   component tree, so the React plugin is dead weight — and vite.config.ts points
   build.outDir at the packaged Python static directory, which a test run must
   never be in a position to empty. */
export default defineConfig({
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts', 'src/**/*.test.tsx'],
  },
});
