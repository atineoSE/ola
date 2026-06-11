/*
 * Test-only render instrumentation for TaskGrid's Cell component.
 *
 * The dashboard's correctness depends on React.memo skipping Cell renders
 * for cells whose props didn't change — without that, a single arriving
 * event would re-render every grid cell. The scale smoke test reads
 * `__cellRenderCount` to assert memo is in fact short-circuiting.
 *
 * Gated on Vite's compile-time `import.meta.env.MODE` so production
 * builds drop both the counter object and the call to `recordCellRender`
 * via dead-code elimination (verified by inspecting the built bundle).
 */

const TEST_MODE = import.meta.env.MODE === "test";

export const __cellRenderCount: { count: number } | null = TEST_MODE
  ? { count: 0 }
  : null;

export function recordCellRender(): void {
  if (__cellRenderCount) __cellRenderCount.count += 1;
}
