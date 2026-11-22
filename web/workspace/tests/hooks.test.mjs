/** Regression tests for the timestamp formatters in src/hooks.ts.
 *
 * Run with `npm test` — the pretest script compiles hooks.ts
 * (esbuild, bundled; it ships with vite, no new dependency)
 * into tests-dist/, mirroring extension/tests. No test
 * framework: node --test + node:assert only.
 *
 * Bug 2 (HIGH): audit created_at is Unix MILLISECONDS, but the
 * Audit view rendered it through fmtDateTime, which multiplies
 * by 1000 assuming seconds — every entry showed in year 58863
 * (e.g. 1795378522614 → "4/13/58863"). Audit entries now go
 * through fmtAuditDateTime; these tests pin both units. */
import { test } from "node:test";
import assert from "node:assert/strict";
import { fmtAuditDateTime, fmtDateTime, fmtTime } from "../tests-dist/hooks.js";

/** The real ms timestamp from the merged-code live test
 * (server/core/audit.py writes int(time.time() * 1000)). */
const AUDIT_MS = 1795378522614;
/** The same instant in seconds — the unit every other server
 * timestamp uses. */
const SAME_INSTANT_S = 1795378522;

test("fmtAuditDateTime renders a real ms audit timestamp in 2026, not 58863", () => {
  const rendered = fmtAuditDateTime(AUDIT_MS);
  assert.match(rendered, /2026/, `expected year 2026, got ${rendered}`);
  assert.doesNotMatch(rendered, /58863/, `seconds assumption resurfaced: ${rendered}`);
});

test("fmtDateTime still renders seconds timestamps correctly", () => {
  const rendered = fmtDateTime(SAME_INSTANT_S);
  assert.match(rendered, /2026/, `expected year 2026, got ${rendered}`);
});

test("fmtAuditDateTime and fmtDateTime agree on the same instant", () => {
  // Same instant, different units — both formatters must land
  // on the same date (times may differ by the ms remainder's
  // rounding, so compare the date part only).
  const fromMs = fmtAuditDateTime(AUDIT_MS);
  const fromS = fmtDateTime(SAME_INSTANT_S);
  const dateOf = (s) => s.split(",")[0];
  assert.equal(dateOf(fromMs), dateOf(fromS), `${fromMs} vs ${fromS}`);
});

test("fmtTime still renders seconds timestamps", () => {
  const rendered = fmtTime(SAME_INSTANT_S);
  assert.notEqual(rendered, "—");
  assert.match(rendered, /\d/);
});

test("formatters return an em dash for null/undefined", () => {
  for (const fn of [fmtAuditDateTime, fmtDateTime, fmtTime]) {
    assert.equal(fn(null), "—");
    assert.equal(fn(undefined), "—");
  }
});
