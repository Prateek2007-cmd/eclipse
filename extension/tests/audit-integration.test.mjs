/** Audit regression tests (coordinator integration fixes, 2026-11-22).
 *
 * Each test failed on the code as audited and passes after the fix:
 * the muted-watch alarm gap, the Amex pattern missing from the
 * extension's first PII scan, and the proof screenshot returned
 * even when the gate blocks.
 *
 * The background service worker and offscreen document are bundled
 * with esbuild into a temp dir (never into the repo tree) and driven
 * with a fake `chrome` global, following the pattern in
 * audit-executor.test.mjs. onnxruntime-web is aliased to a stub that
 * rejects every session create (no real models in Node), and
 * @xenova/transformers stays external (only loaded on demand by the
 * ground path, which these tests never call).
 *
 * Run: npm test (pretest compiles src/vision/*.ts + src/executor.ts).
 */
import test from "node:test";
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import { findPatternPii } from "../tests-dist/vision/pii.js";

const require = createRequire(import.meta.url);
const { build } = require("esbuild");

const EXT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = mkdtempSync(join(tmpdir(), "eclipse-audit-int-"));

const unhandledRejections = [];
process.on("unhandledRejection", (err) => { unhandledRejections.push(err); });

// 1x1 transparent PNG data URL.
const PNG_1PX =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==";

async function bundle(entry, name, extra = {}) {
  const outfile = join(OUT, name);
  await build({
    entryPoints: [join(EXT, entry)],
    bundle: true,
    format: "esm",
    platform: "node",
    outfile,
    logLevel: "warning",
    ...extra,
  });
  return pathToFileURL(outfile).href;
}

/** Fake chrome API. `overrides` is mutable so each test sets the
 *  behaviour it needs; `calls` records what the code under test did. */
function makeChrome() {
  const calls = [];
  const listeners = { onMessage: [] };
  const overrides = { watches: [] };
  const chrome = {
    runtime: {
      ContextType: { OFFSCREEN_DOCUMENT: "OFFSCREEN_DOCUMENT" },
      onMessage: { addListener: (fn) => listeners.onMessage.push(fn) },
      onStartup: { addListener: () => {} },
      getURL: (p) => `chrome-extension://fake/${p}`,
      getContexts: async () => [],
      sendMessage: async () => ({}),
    },
    storage: {
      local: { get: async () => ({ eclipseToken: "test-token" }) },
      onChanged: { addListener: () => {} },
    },
    alarms: {
      getAll: async () => [],
      create: async (name, info) => { calls.push(["alarms.create", name, info]); },
      clear: async (name) => { calls.push(["alarms.clear", name]); },
      onAlarm: { addListener: () => {} },
    },
    tabs: {
      query: async () => [],
      get: async (id) => ({ id, active: true, windowId: 1 }),
      update: async (id, props) => ({ id, ...props }),
      sendMessage: async () => ({}),
      captureVisibleTab: async () => PNG_1PX,
    },
    offscreen: {
      Reason: { USER_MEDIA: "USER_MEDIA" },
      createDocument: async () => {},
    },
  };
  return { chrome, calls, listeners, overrides };
}

function installBrowserGlobals() {
  if (typeof globalThis.createImageBitmap === "undefined") {
    globalThis.createImageBitmap = async () => ({ width: 2, height: 2, close() {} });
  }
  if (typeof globalThis.OffscreenCanvas === "undefined") {
    globalThis.OffscreenCanvas = class {
      constructor(width, height) { this.width = width; this.height = height; }
      getContext() {
        return {
          drawImage() {},
          getImageData: (_x, _y, w, h) => ({ data: new Uint8ClampedArray(w * h * 4), width: w, height: h }),
        };
      }
    };
  }
}

const { chrome, calls, listeners, overrides } = makeChrome();
globalThis.chrome = chrome;
installBrowserGlobals();

// fetch: the server answers /monitor/watches from `overrides`;
// anything else (the OCR char-list fetch) throws, which
// loadSessions handles by marking OCR unavailable.
globalThis.fetch = async (url) => {
  if (String(url).endsWith("/monitor/watches")) {
    return { ok: true, json: async () => ({ watches: overrides.watches }) };
  }
  throw new Error(`fetch stub: no such URL ${url}`);
};

const ortStubPath = join(OUT, "ort-stub.mjs");
writeFileSync(
  ortStubPath,
  `export const env = { wasm: {} };\nexport const InferenceSession = {\n  create: async () => { throw new Error("ort unavailable in tests"); },\n};\n`,
);

await import(await bundle("src/background.ts", "background.mjs"));
await import(await bundle("src/offscreen.ts", "offscreen.mjs", {
  alias: { "onnxruntime-web": ortStubPath },
  external: ["@xenova/transformers"],
}));

// Registration order: background reschedule, background main,
// offscreen document.
assert.equal(listeners.onMessage.length, 3, "expected 3 onMessage listeners");
const [bgReschedule, _bgMain, offscreenListener] = listeners.onMessage;

/** Dispatch one message to one listener; rejects on no response. */
function dispatch(listener, msg, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`no response to ${msg.type} within ${timeoutMs}ms`)), timeoutMs);
    const ret = listener(msg, { id: "test" }, (resp) => { clearTimeout(timer); resolve(resp); });
    if (ret === false) { clearTimeout(timer); resolve(undefined); }
  });
}

// --- I4: Amex in the extension's first PII scan ----------------

test("audit integration: an Amex number is detected as a card number", () => {
  const hits = findPatternPii("card 3782 822463 10005 ends here");
  assert.equal(hits.length, 1);
  assert.equal(hits[0].type, "card_number");
  assert.equal(hits[0].value, "3782 822463 10005");
});

test("audit integration: an Amex number without separators is detected", () => {
  const hits = findPatternPii("378282246310005");
  assert.equal(hits.length, 1);
  assert.equal(hits[0].type, "card_number");
  assert.equal(hits[0].value, "378282246310005");
});

test("audit integration: a 16-digit card and an Aadhaar still match their own types", () => {
  const card = findPatternPii("4111 1111 1111 1111");
  assert.equal(card.length, 1);
  assert.equal(card[0].type, "card_number");
  const aadhaar = findPatternPii("2345 6789 0123");
  assert.equal(aadhaar.length, 1);
  assert.equal(aadhaar[0].type, "aadhaar");
});

// --- I3: muted watches get an alarm ------------------------------

test("audit integration: a muted watch gets an alarm (it becomes due when the mute expires)", async () => {
  overrides.watches = [
    { id: "w_active", status: "active", interval_min: 30 },
    { id: "w_muted", status: "muted", interval_min: 30, muted_until: 9999999999 },
    { id: "w_broken", status: "broken", interval_min: 30 },
    { id: "w_fast", status: "active", interval_min: 5 },
  ];
  calls.length = 0;
  const resp = await dispatch(bgReschedule, { type: "eclipse.rescheduleWatches" });
  assert.deepEqual(resp, { ok: true });
  const created = calls
    .filter(([kind]) => kind === "alarms.create")
    .map(([, name]) => name);
  assert.ok(created.includes("eclipse-watch-w_active"), "active watch gets an alarm");
  assert.ok(created.includes("eclipse-watch-w_muted"), "muted watch gets an alarm");
  assert.ok(created.includes("eclipse-briefing"), "briefing alarm is created");
  assert.ok(!created.includes("eclipse-watch-w_broken"), "a broken watch gets no alarm (/due never returns it until repaired)");
  assert.ok(!created.includes("eclipse-watch-w_fast"), "interval_min < 15 still gets no alarm");
});

// --- I5: no proof screenshot when the gate blocks ----------------

test("audit integration: a blocked step returns no proof screenshot", async () => {
  // In Node the model sessions are all null, so OCR/BlazeFace
  // fail and the gate blocks an image task. The proof image
  // would be the raw screenshot (no blackout boxes exist when
  // the detectors failed), so it must be null — not the page.
  // (On the old code this test fails: paintBlackout returns a
  // non-null image for the blocked step.)
  const resp = await dispatch(offscreenListener, {
    type: "observe",
    url: "https://example.com/checkout",
    viewport: { width: 800, height: 600 },
    elements: [],
    screenshot: PNG_1PX,
    egressTarget: "https://akash-antseed.duckdns.org",
    taskKind: "image",
    taskId: "test-blocked",
  });
  assert.equal(resp.error, undefined, `expected no error, got: ${resp.error}`);
  assert.equal(resp.gate.allow, false, "the gate must block (OCR failed in Node)");
  assert.equal(resp.redactedPayload, null);
  assert.equal(resp.proofScreenshot, null, "a blocked step must not return the raw screenshot");
  assert.deepEqual(unhandledRejections, []);
});
