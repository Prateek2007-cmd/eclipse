/** Audit regression tests (2026-11-22, executor / messaging area).
 *
 * Executor flaws are tested against the pretest bundle
 * (../tests-dist) like the existing executor tests. The content
 * script, background service worker and offscreen document are NOT
 * in the pretest bundle, so this file bundles them itself with
 * esbuild into a temp dir (never into the repo tree) and drives
 * them with a fake `chrome` global + linkedom DOM. onnxruntime-web
 * is aliased to a stub that rejects every session create (no real
 * models in Node), and @xenova/transformers stays external (only
 * loaded on demand by the ground path, which these tests never
 * call).
 *
 * A regression must fail the test, not crash the runner: the old
 * offscreen handler let a throw escape as an unhandled rejection,
 * so one is recorded here instead of killing the process.
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
import { parseHTML } from "linkedom";

import { walkDomWithNodes } from "../tests-dist/vision/dom-walker.js";
import { executeAction, resetExecutedSubmits } from "../tests-dist/executor.js";

const require = createRequire(import.meta.url);
const { build } = require("esbuild");

const EXT = join(dirname(fileURLToPath(import.meta.url)), "..");
const OUT = mkdtempSync(join(tmpdir(), "eclipse-audit-"));

const unhandledRejections = [];
process.on("unhandledRejection", (err) => { unhandledRejections.push(err); });

// 1x1 transparent PNG data URL (captureVisibleTab stub).
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

/** Fake chrome API. One shared stub for all three bundles (they
 *  resolve `chrome` from the global at call time); `overrides` is
 *  mutable so each test sets the behaviour it needs. */
function makeChrome() {
  const calls = [];
  const listeners = { onMessage: [] };
  const overrides = {
    tabsGet: null,
    tabsSendMessage: null,
    captureVisibleTab: null,
    runtimeSendMessage: null,
    tabsQuery: null,
  };
  const chrome = {
    runtime: {
      ContextType: { OFFSCREEN_DOCUMENT: "OFFSCREEN_DOCUMENT" },
      onMessage: { addListener: (fn) => listeners.onMessage.push(fn) },
      onStartup: { addListener: () => {} },
      getURL: (p) => `chrome-extension://fake/${p}`,
      getContexts: async () => [],
      sendMessage: async (msg) => {
        calls.push(["runtime.sendMessage", msg]);
        if (overrides.runtimeSendMessage) return overrides.runtimeSendMessage(msg);
        return {};
      },
    },
    storage: {
      local: { get: async () => ({}) },
      onChanged: { addListener: () => {} },
    },
    alarms: {
      getAll: async () => [],
      create: async () => {},
      clear: async () => {},
      onAlarm: { addListener: () => {} },
    },
    tabs: {
      query: async (q) => {
        calls.push(["tabs.query", q]);
        return overrides.tabsQuery ? overrides.tabsQuery(q) : [];
      },
      get: async (id) => {
        calls.push(["tabs.get", id]);
        return overrides.tabsGet ? overrides.tabsGet(id) : { id, active: true, windowId: 1 };
      },
      update: async (id, props) => {
        calls.push(["tabs.update", id, props]);
        return { id, ...props };
      },
      sendMessage: async (tabId, msg) => {
        calls.push(["tabs.sendMessage", tabId, msg.type]);
        if (overrides.tabsSendMessage) return overrides.tabsSendMessage(tabId, msg);
        return {};
      },
      captureVisibleTab: async () => {
        calls.push(["captureVisibleTab"]);
        if (overrides.captureVisibleTab) return overrides.captureVisibleTab();
        return PNG_1PX;
      },
    },
    offscreen: {
      Reason: { USER_MEDIA: "USER_MEDIA" },
      createDocument: async () => { calls.push(["offscreen.createDocument"]); },
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

// --- Bundle + import the three entry points (top-level await) ---

const { chrome, calls, listeners, overrides } = makeChrome();
globalThis.chrome = chrome;
installBrowserGlobals();

const ortStubPath = join(OUT, "ort-stub.mjs");
writeFileSync(
  ortStubPath,
  `export const env = { wasm: {} };
export const InferenceSession = {
  create: async () => { throw new Error("ort unavailable in tests"); },
};
`,
);

await import(await bundle("src/background.ts", "background.mjs"));
await import(await bundle("src/content.ts", "content.mjs"));
await import(await bundle("src/offscreen.ts", "offscreen.mjs", {
  alias: { "onnxruntime-web": ortStubPath },
  external: ["@xenova/transformers"],
}));

// Registration order: background reschedule, background main,
// content script, offscreen document.
assert.equal(listeners.onMessage.length, 4, "expected 4 onMessage listeners");
const [_bgReschedule, bgMain, contentListener, offscreenListener] = listeners.onMessage;

/** Dispatch one message to one listener; rejects on no response. */
function dispatch(listener, msg, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`no response to ${msg.type} within ${timeoutMs}ms`)), timeoutMs);
    const ret = listener(msg, { id: "test" }, (resp) => { clearTimeout(timer); resolve(resp); });
    if (ret === false) { clearTimeout(timer); resolve(undefined); }
  });
}

/** Dispatch to several listeners with one shared sendResponse:
 *  the FIRST response wins, as in Chrome. */
function dispatchAll(msg, fns, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`no response to ${msg.type} within ${timeoutMs}ms`)), timeoutMs);
    let settled = false;
    const respond = (resp) => {
      if (!settled) { settled = true; clearTimeout(timer); resolve(resp); }
    };
    for (const fn of fns) fn(msg, { id: "test" }, respond);
  });
}

function observeStubs({ active = true } = {}) {
  overrides.tabsGet = async (id) => ({ id, active, windowId: 1 });
  overrides.tabsSendMessage = async (_tabId, msg) => {
    if (msg.type === "walk") {
      return { url: "https://example.com/jobs", viewport: { width: 800, height: 600 }, elements: [] };
    }
    return { ok: true };
  };
  overrides.runtimeSendMessage = async (msg) => (msg.type === "observe" ? { blackoutBoxes: [] } : {});
  calls.length = 0;
}

// --- Executor: submit guard -------------------------------------

test("audit executor: <input type=button> in a form is a submit (second click refused)", async () => {
  // The guard treated <button type="button"> in a form as a submit
  // (multi-step "Continue") but not <input type="button"> in a form
  // — the same pattern, so a failed verify could double-submit it.
  resetExecutedSubmits();
  const { document } = parseHTML(
    '<form><input id="el_cont" type="button" value="Continue"></form>',
  );
  const { nodes } = walkDomWithNodes(document);
  const action = { action: "click", element_id: "el_cont" };
  const first = await executeAction(action, document, nodes, { taskId: "task-1" });
  assert.equal(first.ok, true, first.detail);
  const second = await executeAction(action, document, nodes, { taskId: "task-1" });
  assert.equal(second.ok, false);
  assert.match(second.detail, /already executed/);
});

test("audit executor: <input type=button> outside a form is still not a submit", async () => {
  resetExecutedSubmits();
  const { document } = parseHTML('<input id="el_b" type="button" value="Open">');
  const { nodes } = walkDomWithNodes(document);
  const action = { action: "click", element_id: "el_b" };
  assert.equal((await executeAction(action, document, nodes, { taskId: "task-1" })).ok, true);
  assert.equal((await executeAction(action, document, nodes, { taskId: "task-1" })).ok, true);
});

test("audit executor: a button associated via the form attribute is a submit", async () => {
  // closest("form") is null for <button form="f"> outside the form,
  // so the guard missed it. .form is stubbed here: linkedom does
  // not implement the IDL form-owner lookup, real browsers do.
  resetExecutedSubmits();
  const { document } = parseHTML(
    '<form id="f"></form><button id="el_g" type="button" form="f">Continue</button>',
  );
  const { nodes } = walkDomWithNodes(document);
  Object.defineProperty(nodes.get("el_g"), "form", {
    value: document.getElementById("f"),
    configurable: true,
  });
  const action = { action: "click", element_id: "el_g" };
  const first = await executeAction(action, document, nodes, { taskId: "task-1" });
  assert.equal(first.ok, true, first.detail);
  const second = await executeAction(action, document, nodes, { taskId: "task-1" });
  assert.equal(second.ok, false);
  assert.match(second.detail, /already executed/);
});

// --- Executor: fingerprint --------------------------------------

test("audit executor: fingerprint refuses a type change (button -> submit)", async () => {
  // Same tag, same accessible name, different consequence: the
  // planner saw a plain button, the live node is now a submit.
  const { document } = parseHTML('<button id="el_b" type="button">Continue</button>');
  const { info, nodes } = walkDomWithNodes(document);
  const expected = info.find((e) => e.id === "el_b");
  assert.equal(expected.type, "button");
  nodes.get("el_b").setAttribute("type", "submit");
  const r = await executeAction(
    { action: "click", element_id: "el_b" }, document, nodes,
    { expected, taskId: "task-1" },
  );
  assert.equal(r.ok, false);
  assert.match(r.detail, /changed since the walk \(type button → submit\)/);
});

test("audit executor: fingerprint refuses a form-membership change", async () => {
  const { document } = parseHTML(
    '<button id="el_c" type="button">Continue</button><form id="f"></form>',
  );
  const { info, nodes } = walkDomWithNodes(document);
  const expected = info.find((e) => e.id === "el_c");
  assert.equal(expected.inForm, false);
  document.getElementById("f").appendChild(nodes.get("el_c"));
  const r = await executeAction(
    { action: "click", element_id: "el_c" }, document, nodes,
    { expected, taskId: "task-1" },
  );
  assert.equal(r.ok, false);
  assert.match(r.detail, /form membership changed/);
});

test("audit executor: fingerprint passes when tag, name, type and form all match", async () => {
  // No false positives from the new type/inForm comparisons.
  resetExecutedSubmits();
  const { document } = parseHTML(
    '<form><button id="el_ok" type="button">Continue</button></form>',
  );
  const { info, nodes } = walkDomWithNodes(document);
  const expected = info.find((e) => e.id === "el_ok");
  const r = await executeAction(
    { action: "click", element_id: "el_ok" }, document, nodes,
    { expected, taskId: "task-1" },
  );
  assert.equal(r.ok, true, r.detail);
});

test("audit executor: a malformed fingerprint is refused, not thrown", async () => {
  const { document } = parseHTML('<button id="el_e" type="button">Hi</button>');
  const { nodes } = walkDomWithNodes(document);
  const r = await executeAction(
    { action: "click", element_id: "el_e" }, document, nodes,
    { expected: { id: "el_e" } },
  );
  assert.equal(r.ok, false);
  assert.match(r.detail, /malformed fingerprint/);
});

// --- Executor: detached nodes + malformed input ------------------

test("audit executor: an element removed from the page is refused (no partial action)", async () => {
  const { document } = parseHTML(
    '<button id="el_d" type="button">Hi</button><input id="el_t" type="text">',
  );
  const { nodes } = walkDomWithNodes(document);
  let clicked = false;
  nodes.get("el_d").addEventListener("click", () => { clicked = true; });
  nodes.get("el_d").remove();
  nodes.get("el_t").remove();
  const click = await executeAction({ action: "click", element_id: "el_d" }, document, nodes);
  assert.equal(click.ok, false);
  assert.match(click.detail, /no longer in the page/);
  assert.equal(clicked, false, "detached node's listener must not fire");
  const type = await executeAction(
    { action: "type", element_id: "el_t", text: "x" }, document, nodes,
  );
  assert.equal(type.ok, false);
  assert.match(type.detail, /no longer in the page/);
});

test("audit executor: navigate with an unparseable URL returns a failure, not a throw", async () => {
  const { document } = parseHTML("<html><body></body></html>");
  const { nodes } = walkDomWithNodes(document);
  const r = await executeAction(
    { action: "navigate", url: "https://" }, document, nodes,
    { allowedSites: ["example.com"] },
  );
  assert.equal(r.ok, false);
  assert.match(r.detail, /valid http\(s\) URL/);
});

// --- Content script: error-path URL -------------------------------

test("audit content: an execute error response strips the page URL query", async () => {
  // The executor strips query strings everywhere (a URL can carry
  // a reset token or an email), but the content script's catch
  // block returned location.href raw — straight to the server.
  const { document, window } = parseHTML('<button id="el_x" type="button">Hi</button>');
  globalThis.document = document;
  globalThis.window = window;
  globalThis.location = { href: "https://internshala.com/reset?token=SECRET123&email=a@b.com" };
  document.getElementById("el_x").click = () => { throw new Error("boom from page"); };
  const resp = await dispatch(contentListener, {
    type: "execute",
    action: { action: "click", element_id: "el_x" },
  });
  assert.equal(resp.ok, false);
  assert.equal(resp.detail, "boom from page");
  assert.equal(resp.url, "https://internshala.com/reset");
  assert.equal(resp.url.includes("SECRET123"), false);
});

// --- Background: observe targets the right tab -------------------

test("audit background: observe activates a non-active target tab before capturing", async () => {
  // captureVisibleTab screenshots the ACTIVE tab, but the walk
  // targets msg.tabId: without activation the vision pipeline
  // describes a different page than the DOM walk.
  observeStubs({ active: false });
  const resp = await dispatch(bgMain, { type: "eclipse.observe", tabId: 42 });
  assert.deepEqual(resp, { blackoutBoxes: [] });
  const order = calls.map((c) => c[0]);
  const at = (name) => order.indexOf(name);
  assert.ok(at("tabs.get") !== -1, "tabs.get called");
  assert.ok(at("tabs.update") !== -1, "tabs.update called");
  assert.ok(at("tabs.get") < at("tabs.update"), "tab checked before activation");
  assert.ok(at("tabs.update") < at("tabs.sendMessage"), "activated before the walk");
  assert.ok(at("tabs.update") < at("captureVisibleTab"), "activated before the capture");
  const update = calls.find((c) => c[0] === "tabs.update");
  assert.equal(update[1], 42);
  assert.deepEqual(update[2], { active: true });
});

test("audit background: observe does not re-activate an already-active tab", async () => {
  observeStubs({ active: true });
  const resp = await dispatch(bgMain, { type: "eclipse.observe", tabId: 42 });
  assert.deepEqual(resp, { blackoutBoxes: [] });
  assert.equal(calls.some((c) => c[0] === "tabs.update"), false);
});

test("audit background: an unknown taskKind defaults to mixed (strictest)", async () => {
  // The gate treats an unknown taskKind as dom-only, so vision
  // would silently stop being required.
  observeStubs({ active: true });
  await dispatch(bgMain, { type: "eclipse.observe", tabId: 42, taskKind: "bogus" });
  const sent = calls.find((c) => c[0] === "runtime.sendMessage" && c[1].type === "observe");
  assert.equal(sent[1].taskKind, "mixed");
  calls.length = 0;
  await dispatch(bgMain, { type: "eclipse.observe", tabId: 42, taskKind: "dom" });
  const sentDom = calls.find((c) => c[0] === "runtime.sendMessage" && c[1].type === "observe");
  assert.equal(sentDom[1].taskKind, "dom");
});

// --- Offscreen: always answer, forget old tasks, stay in lane ----

test("audit offscreen: a malformed observe gets an error response, not a hang", async () => {
  // The handler had no try/catch: a throw (here the redactor's
  // .map on undefined elements) left sendResponse uncalled and
  // the background's await hung forever.
  const before = unhandledRejections.length;
  const resp = await dispatch(offscreenListener, {
    type: "observe",
    url: "https://example.com/",
    viewport: { width: 800, height: 600 },
    elements: undefined,
    screenshot: null,
    taskKind: "dom",
    egressTarget: "http://127.0.0.1:8000",
  });
  assert.equal(typeof resp.error, "string");
  assert.ok(resp.error.length > 0);
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(unhandledRejections.length, before, "the throw must be contained, not escape");
});

test("audit offscreen: placeholder registries are evicted (no unbounded PII retention)", async () => {
  // Registries hold raw PII values and were never dropped. After
  // more tasks than the cap, the oldest task's registry is gone:
  // its placeholders re-number from 1.
  const el = (email) => ([{
    id: "el_email", tag: "input", role: "textbox", name: "Email", text: "",
    value: email, sensitive: false,
    rect: { x: 0, y: 0, width: 10, height: 10 }, visible: true, interactable: true,
  }]);
  async function observeTask(taskId, email) {
    const resp = await dispatch(offscreenListener, {
      type: "observe", url: "https://example.com/",
      viewport: { width: 800, height: 600 },
      elements: el(email), screenshot: null, taskKind: "dom",
      egressTarget: "http://127.0.0.1:8000", taskId,
    });
    assert.ok(resp.redactedPayload, `no payload for ${taskId}: ${JSON.stringify(resp).slice(0, 120)}`);
    return JSON.parse(resp.redactedPayload).elements[0].value;
  }
  assert.equal(await observeTask("task-A", "alice@example.com"), "[EMAIL_1]");
  for (let i = 0; i < 11; i++) {
    await observeTask(`task-filler-${i}`, `filler${i}@example.com`);
  }
  assert.equal(
    await observeTask("task-A", "bob@example.com"),
    "[EMAIL_1]",
    "task-A registry must have been evicted (old code: [EMAIL_2])",
  );
});

test("audit offscreen: extension-wide messages are left to the background (no response race)", async () => {
  // The offscreen listener answered EVERY runtime message with an
  // "unknown message type" error — immediately, while the
  // background's eclipse.observe answer is async, so the sender
  // got the error and the real result was dropped.
  observeStubs({ active: true });
  let offscreenResponded = false;
  const ret = offscreenListener(
    { type: "eclipse.observe", tabId: 42 }, { id: "test" },
    () => { offscreenResponded = true; },
  );
  assert.equal(ret, false, "offscreen must decline messages that are not its own");
  await new Promise((r) => setTimeout(r, 50));
  assert.equal(offscreenResponded, false);
  const resp = await dispatchAll(
    { type: "eclipse.observe", tabId: 42 },
    [bgMain, offscreenListener],
  );
  assert.deepEqual(resp, { blackoutBoxes: [] });
});
