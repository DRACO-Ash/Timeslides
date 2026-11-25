/* The test registry, apart from run.mjs: the test modules import it, and
 * run.mjs imports them while it is still running, so keeping the registry in
 * run.mjs would be an import cycle across a top-level await, a deadlock. */
export const TESTS = [];
export function test(name, fn) { TESTS.push({name, fn}); }

export function assert(cond, message = "assertion failed") {
  if (!cond) throw new Error(message);
}
export function equal(got, want, message = "") {
  const g = JSON.stringify(got);
  const w = JSON.stringify(want);
  if (g !== w) throw new Error(`${message}\n    got:  ${g}\n    want: ${w}`);
}
