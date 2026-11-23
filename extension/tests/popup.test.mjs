/** Popup token-handoff tests (PR #5, W3 — the BLOCKER fix).
 *
 * The real popup.html is parsed with linkedom, the chrome +
 * fetch globals are faked, and popup.ts is imported against
 * them. Asserts: Save writes chrome.storage.local["eclipseToken"],
 * clears the input, never shows the value, and the status copy
 * no longer claims the token appears by opening the workspace.
 *
 * Run: npm test (pretest compiles src/popup.ts to tests-dist/).
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { parseHTML } from "linkedom";

const EXT = join(dirname(fileURLToPath(import.meta.url)), "..");
const html = readFileSync(join(EXT, "popup.html"), "utf8");
const { document, window } = parseHTML(html);

globalThis.document = document;
globalThis.HTMLButtonElement = window.HTMLButtonElement;
globalThis.HTMLInputElement = window.HTMLInputElement;

// Fake chrome.storage.local.
const store = new Map();
globalThis.chrome = {
  storage: {
    local: {
      get: async (key) => {
        if (typeof key === "string") {
          return store.has(key) ? { [key]: store.get(key) } : {};
        }
        return {};
      },
      set: async (obj) => {
        for (const [k, v] of Object.entries(obj)) store.set(k, v);
      },
    },
  },
  tabs: { create: async () => ({}) },
};

// Fake server: /health answers 200 only when the request carries
// the fake token, 401 otherwise (like the real token gate).
globalThis.fetch = async (_url, init = {}) => {
  const headers = init.headers ?? {};
  const ok = headers["X-Eclipse-Token"] === "fake-token-123";
  return {
    status: ok ? 200 : 401,
    json: async () =>
      ok
        ? { status: "ok", service: "eclipse", version: "0.1.0" }
        : { detail: "missing or invalid X-Eclipse-Token" },
  };
};

// The fakes must exist before this import: popup.ts wires its
// listeners and runs one status refresh at module top level.
await import("../tests-dist/popup.js");

function $(id) {
  return document.getElementById(id);
}

function click(el) {
  el.dispatchEvent(new window.Event("click", { bubbles: true }));
}

async function flush() {
  await new Promise((resolve) => setTimeout(resolve, 10));
}

async function reset() {
  store.clear();
  $("token-input").value = "";
  $("token-save-text").textContent = "";
  click($("refresh"));
  await flush();
}

test("popup has a token input and Save button", () => {
  assert.notEqual($("token-input"), null);
  assert.equal($("token-input").type, "password");
  assert.notEqual($("save-token"), null);
  assert.equal($("save-token").textContent, "Save");
});

test("Save writes chrome.storage.local eclipseToken and clears the input", async () => {
  await reset();
  $("token-input").value = "fake-token-123";
  click($("save-token"));
  await flush();
  assert.equal(store.get("eclipseToken"), "fake-token-123");
  assert.equal($("token-input").value, "");
  assert.match($("token-save-text").textContent, /Saved/);
  // The value is never displayed anywhere in the popup.
  assert.ok(!document.body.textContent.includes("fake-token-123"));
});

test("Save with an empty input stores nothing", async () => {
  await reset();
  $("token-input").value = "   ";
  click($("save-token"));
  await flush();
  assert.equal(store.has("eclipseToken"), false);
  assert.match($("token-save-text").textContent, /Paste a token first/);
});

test("Enter in the token input saves too", async () => {
  await reset();
  $("token-input").value = "fake-token-123";
  const event = new window.Event("keydown", { bubbles: true });
  event.key = "Enter";
  $("token-input").dispatchEvent(event);
  await flush();
  assert.equal(store.get("eclipseToken"), "fake-token-123");
});

test("with no token stored, the copy asks for a paste (not 'open the workspace')", async () => {
  await reset();
  assert.match($("token-text").textContent, /Not saved/);
  assert.match($("token-text").textContent, /paste the token below/);
  assert.ok(!$("token-text").textContent.includes("open the workspace once"));
});

test("with a token stored, the status shows Saved and the server verifies it", async () => {
  await reset();
  store.set("eclipseToken", "fake-token-123");
  click($("refresh"));
  await flush();
  assert.equal($("token-text").textContent, "Saved");
  assert.match($("server-text").textContent, /token accepted/);
});

test("a rejected token says to paste it again below", async () => {
  await reset();
  store.set("eclipseToken", "wrong-token");
  click($("refresh"));
  await flush();
  assert.match($("server-text").textContent, /rejected/);
  assert.match($("server-text").textContent, /paste the token again below/);
});
