/** Live monitoring tests (Sprint 8): selector reading + the
 * alarm check flow with fake deps. No real Chrome, no server.
 *
 * Run: npm test (pretest compiles src/monitor.ts to tests-dist/).
 */
import test from "node:test";
import assert from "node:assert/strict";
import { parseHTML } from "linkedom";

import {
  checkDueWatches,
  readSelectorValue,
  watchAlarmName,
  BRIEFING_ALARM,
} from "../tests-dist/monitor.js";

function doc(html) {
  return parseHTML(html).document;
}

test("readSelectorValue reads a CSS selector", () => {
  const d = doc(`<html><body><div id="status">Selected</div></body></html>`);
  assert.deepEqual(readSelectorValue(d, "#status"), {
    found: true,
    value: "Selected",
  });
});

test("readSelectorValue misses cleanly", () => {
  const d = doc(`<html><body><div>Nothing here</div></body></html>`);
  assert.deepEqual(readSelectorValue(d, "#missing"), {
    found: false,
    value: null,
  });
});

test("readSelectorValue rejects a bad selector instead of throwing", () => {
  const d = doc(`<html><body><div>x</div></body></html>`);
  assert.deepEqual(readSelectorValue(d, "[[[bad"), {
    found: false,
    value: null,
  });
});

test("readSelectorValue supports text: search (case-insensitive, returns a snippet)", () => {
  const d = doc(`<html><body><p>Your result: Selected for the role</p></body></html>`);
  const hit = readSelectorValue(d, "text:selected");
  assert.equal(hit.found, true);
  // M14: the value is the matched snippet, not the phrase —
  // so a `changed` watch on a text: selector can fire.
  assert.match(hit.value, /Selected for the role/);
  assert.notEqual(hit.value, "selected");
  assert.deepEqual(readSelectorValue(d, "text:Rejected"), {
    found: false,
    value: null,
  });
});

test("readSelectorValue trims and collapses whitespace", () => {
  const d = doc(`<html><body><div id="s">  Hello\n   world  </div></body></html>`);
  assert.equal(readSelectorValue(d, "#s").value, "Hello world");
});

test("watchAlarmName and briefing alarm are distinct", () => {
  assert.equal(watchAlarmName("w_abc"), "eclipse-watch-w_abc");
  assert.equal(BRIEFING_ALARM, "eclipse-briefing");
});

test("checkDueWatches reports a firing and skips a watch with no open tab", async () => {
  const watches = [
    { id: "w_1", url: "https://example.com/results", selector: "#status", condition: "changed", interval_min: 15 },
    { id: "w_2", url: "https://example.com/other", selector: "#x", condition: "changed", interval_min: 15 },
  ];
  const summary = await checkDueWatches({
    fetchDue: async () => watches,
    findTab: async (url) => (url.includes("results") ? 42 : null),
    observeTab: async () => ({ found: true, value: "Selected" }),
    reportCheck: async (id) => ({
      fired: id === "w_1",
      broken: false,
      status: "active",
      reason: "changed",
    }),
  });
  assert.equal(summary.checked, 1);
  assert.equal(summary.fired, 1);
  assert.equal(summary.skippedNoTab, 1);
  assert.deepEqual(summary.errors, []);
});

test("checkDueWatches records errors per watch and keeps going", async () => {
  const watches = [
    { id: "w_1", url: "https://a.example", selector: "#s", condition: "changed", interval_min: 15 },
    { id: "w_2", url: "https://b.example", selector: "#s", condition: "changed", interval_min: 15 },
  ];
  const summary = await checkDueWatches({
    fetchDue: async () => watches,
    findTab: async () => 7,
    observeTab: async (_tab, watch) => {
      if (watch.id === "w_1") throw new Error("tab closed");
      return { found: true, value: "v" };
    },
    reportCheck: async () => ({ fired: false, broken: false, status: "active", reason: "no change" }),
  });
  assert.equal(summary.checked, 1);
  assert.equal(summary.errors.length, 1);
  assert.match(summary.errors[0], /w_1/);
});

test("checkDueWatches survives a failed due fetch", async () => {
  const summary = await checkDueWatches({
    fetchDue: async () => {
      throw new Error("server down");
    },
    findTab: async () => null,
    observeTab: async () => ({ found: false, value: null }),
    reportCheck: async () => ({ fired: false, broken: false, status: "active", reason: "" }),
  });
  assert.equal(summary.checked, 0);
  assert.equal(summary.errors.length, 1);
});

test("checkDueWatches survives a throwing findTab and keeps going (B4)", async () => {
  const watches = [
    { id: "w_bad", url: "not a url", selector: "#s", condition: "changed", interval_min: 15 },
    { id: "w_ok", url: "https://b.example", selector: "#s", condition: "changed", interval_min: 15 },
  ];
  const summary = await checkDueWatches({
    fetchDue: async () => watches,
    findTab: async (url) => {
      if (url === "not a url") throw new Error("Invalid URL");
      return 7;
    },
    observeTab: async () => ({ found: true, value: "v" }),
    reportCheck: async () => ({ fired: false, broken: false, status: "active", reason: "no change" }),
  });
  assert.equal(summary.checked, 1);
  assert.equal(summary.errors.length, 1);
  assert.match(summary.errors[0], /w_bad/);
});
