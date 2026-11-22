/** Audit regression tests (vision + vault area): URL redaction,
 *  hidden inputs under a real browser's computed style, shadow DOM
 *  and iframe walking, and PII-pattern coverage (UPI, passport,
 *  US phones, DOB variants, phone lookbehind). All PII here is
 *  synthetic test data.
 *
 *  Run: npm test (pretest compiles src/vision/*.ts to tests-dist/).
 */
import test from "node:test";
import assert from "node:assert/strict";
import { parseHTML } from "linkedom";

import { walkDom } from "../tests-dist/vision/dom-walker.js";
import { redactObservation, redactText, redactUrl } from "../tests-dist/vision/redactor.js";
import { findPatternPii, PlaceholderRegistry, classifyField } from "../tests-dist/vision/pii.js";
import { secondScan } from "../tests-dist/vision/second-scan.js";
import { evaluateGate } from "../tests-dist/vision/gate.js";
import { checkEgress } from "../tests-dist/vision/egress.js";

function observeFromElements(elements, url = "http://localhost/test") {
  return {
    url,
    viewport: { width: 1280, height: 800 },
    elements,
    detections: [],
    ocr: [],
    faces: [],
    florenceUsed: false,
    detectorStatus: { dom: "ok", yolo: "ok", ocr: "ok", blazeface: "ok", florence2: "ok" },
    observedAt: Date.now(),
  };
}

test("audit: URL query params are redacted, not passed raw to the planner", () => {
  // The URL used to travel raw: a name in a query param sails past
  // the second scan (names have no pattern) straight to the planner.
  const red = redactObservation(
    observeFromElements(
      [],
      "https://shop.example.com/checkout?email=aarav.test@example.com&name=Aarav%20Test&phone=9999988888&upi=aarav@okhdfcbank&tab=orders&lang=en",
    ),
  );
  const payload = red.redactedText;
  const url = JSON.parse(payload).url;
  assert.equal(
    url,
    "https://shop.example.com/checkout?email=[EMAIL_1]&name=[NAME_1]&phone=[PHONE_1]&upi=[UPI_1]&tab=orders&lang=en",
  );
  for (const raw of ["aarav.test@example.com", "Aarav", "9999988888", "aarav@okhdfcbank"]) {
    assert.ok(!payload.includes(raw), `raw value must not leak: ${raw}`);
  }
  // The URL hits are recorded like any other hit (the second scan's
  // raw-values check covers them).
  assert.ok(red.hits.some((h) => h.type === "email" && h.value === "aarav.test@example.com"));
  assert.ok(red.hits.some((h) => h.type === "name" && h.value === "Aarav Test"));
  assert.equal(secondScan(payload).clean, true);
});

test("audit: URL fragment and path PII are redacted; a clean URL is untouched", () => {
  const frag = redactUrl(
    "https://example.com/reset#email=aarav.test@example.com",
    new PlaceholderRegistry(),
  );
  assert.equal(frag.text, "https://example.com/reset#email=[EMAIL_1]");
  assert.ok(!frag.text.includes("aarav.test@example.com"));

  const path = redactUrl(
    "https://example.com/users/aarav.test@example.com/profile",
    new PlaceholderRegistry(),
  );
  assert.equal(path.text, "https://example.com/users/[EMAIL_1]/profile");

  const card = redactUrl(
    "https://example.com/pay?card_number=4111%201111%201111%201111",
    new PlaceholderRegistry(),
  );
  assert.equal(card.text, "https://example.com/pay?card_number=[CARD_LAST4]");

  const clean = redactUrl("https://example.com/jobs?tab=orders&page=2", new PlaceholderRegistry());
  assert.equal(clean.text, "https://example.com/jobs?tab=orders&page=2");
  assert.equal(clean.hits.length, 0);

  // An unparseable URL falls back to pattern redaction, not a throw.
  const weird = redactUrl("not a url at all: aarav.test@example.com", new PlaceholderRegistry());
  assert.ok(weird.text.includes("[EMAIL_1]"));
  assert.ok(!weird.text.includes("aarav.test@example.com"));
});

test("audit: hidden inputs are walked when the browser hides them via computed style", () => {
  // In a real browser the UA stylesheet gives type=hidden inputs
  // display:none, so getComputedStyle reports them hidden — and the
  // old walker skipped them before the hidden-input carve-out was
  // ever reached (linkedom has no getComputedStyle, so the fixture
  // test never caught it).
  const { document } = parseHTML(`<html><body><form>
    <input id="el_hidden_prev_email" name="previous_email" type="hidden" value="old.user@example.com">
    <input id="el_vis" name="email" type="email" value="test.user@example.com">
    <div id="el_gone" style="display: none">gone</div>
  </form></body></html>`);
  Object.defineProperty(document, "defaultView", {
    value: {
      getComputedStyle: (el) => ({
        display: el.getAttribute("type") === "hidden" ? "none" : "block",
        visibility: "visible",
      }),
    },
    configurable: true,
  });
  const els = walkDom(document);
  const prev = els.find((e) => e.id === "el_hidden_prev_email");
  assert.ok(prev, "hidden previous-email input is walked even when computed style hides it");
  assert.equal(prev.value, "old.user@example.com");
  assert.equal(prev.visible, false);
  assert.equal(prev.interactable, false);
  // A genuinely hidden element is still skipped.
  assert.equal(els.find((e) => e.id === "el_gone"), undefined);
  // And the hidden email is redacted end to end.
  const red = redactObservation(observeFromElements(els));
  assert.ok(!red.redactedText.includes("old.user@example.com"));
  assert.ok(red.redactedText.includes("[EMAIL_2]"));
});

test("audit: shadow DOM elements are walked and their PII redacted", () => {
  const { document } = parseHTML(
    `<html><body><div id="host"></div><button id="el_top">Top</button></body></html>`,
  );
  const host = document.getElementById("host");
  const shadow = host.attachShadow({ mode: "open" });
  shadow.innerHTML =
    `<button id="el_shadow_btn">Shadow button</button>` +
    `<input id="el_shadow_email" type="email" aria-label="Email" value="test.user@example.com">`;
  const els = walkDom(document);
  const btn = els.find((e) => e.id === "el_shadow_btn");
  assert.ok(btn, "button inside an open shadow root is walked");
  assert.equal(btn.name, "Shadow button");
  const email = els.find((e) => e.id === "el_shadow_email");
  assert.ok(email, "input inside an open shadow root is walked");
  assert.equal(email.value, "test.user@example.com");
  const red = redactObservation(observeFromElements(els));
  assert.ok(red.redactedText.includes("[EMAIL_1]"), "shadow email redacted");
  assert.ok(!red.redactedText.includes("test.user@example.com"));
});

test("audit: same-origin iframe elements are walked", () => {
  const { document } = parseHTML(
    `<html><body><iframe id="el_frame"></iframe><button id="el_top">Top</button></body></html>`,
  );
  const { document: inner } = parseHTML(
    `<html><body><input id="el_frame_email" type="email" aria-label="Email" value="test.user@example.com"></body></html>`,
  );
  const frame = document.getElementById("el_frame");
  Object.defineProperty(frame, "contentDocument", { value: inner, configurable: true });
  const els = walkDom(document);
  const email = els.find((e) => e.id === "el_frame_email");
  assert.ok(email, "input inside a same-origin iframe is walked");
  assert.equal(email.value, "test.user@example.com");
  const red = redactObservation(observeFromElements(els));
  assert.ok(!red.redactedText.includes("test.user@example.com"));
});

test("audit: UPI ids are detected (the email pattern misses them — no dot-TLD)", () => {
  const one = findPatternPii("pay aarav@okhdfcbank for the order");
  assert.deepEqual(one.map((f) => [f.type, f.value]), [["upi", "aarav@okhdfcbank"]]);
  // A phone-like UPI prefix is one UPI hit, not a phone hit.
  const two = findPatternPii("send to 9999988888@paytm");
  assert.deepEqual(two.map((f) => [f.type, f.value]), [["upi", "9999988888@paytm"]]);
  assert.deepEqual(
    findPatternPii("my id is test.user@ybl").map((f) => f.type),
    ["upi"],
  );
  // A bare "word@word" with an unknown handle is NOT matched — it
  // false-positives on prose.
  assert.deepEqual(findPatternPii("meet me@noon tomorrow"), []);
  // A UPI field label classifies, and the label rule redacts a UPI
  // id wholesale even when its handle is not in the pattern list.
  assert.equal(classifyField("UPI ID"), "upi");
  assert.equal(
    redactText("UPI ID: aarav@somebank", new PlaceholderRegistry()).text,
    "UPI ID: [UPI_1]",
  );
  const red = redactText("Pay to aarav@okhdfcbank", new PlaceholderRegistry());
  assert.equal(red.text, "Pay to [UPI_1]");
});

test("audit: Indian passports are detected, including OCR-lowercased", () => {
  assert.deepEqual(
    findPatternPii("passport A1234567").map((f) => [f.type, f.value]),
    [["passport", "A1234567"]],
  );
  assert.deepEqual(
    findPatternPii("passport a1234567").map((f) => [f.type, f.value]),
    [["passport", "a1234567"]],
  );
  // A PAN is still a PAN, not a passport.
  assert.deepEqual(
    findPatternPii("PAN ABCDE1234F").map((f) => f.type),
    ["pan"],
  );
  assert.equal(classifyField("Passport number"), "passport");
});

test("audit: US phone numbers are detected; a card stays a card", () => {
  assert.deepEqual(
    findPatternPii("call +1 (415) 555-0132").map((f) => [f.type, f.value]),
    [["phone", "+1 (415) 555-0132"]],
  );
  assert.deepEqual(
    findPatternPii("call 415-555-0132").map((f) => [f.type, f.value]),
    [["phone", "415-555-0132"]],
  );
  assert.deepEqual(
    findPatternPii("call 4155550132").map((f) => [f.type, f.value]),
    [["phone", "4155550132"]],
  );
  // A 16-digit card is one card hit — the US-phone pattern also
  // fires inside it, but the overlap dedup keeps the longer card.
  assert.deepEqual(
    findPatternPii("card 4111 1111 1111 1111").map((f) => [f.type, f.value]),
    [["card_number", "4111 1111 1111 1111"]],
  );
  // The Indian format still works.
  assert.deepEqual(
    findPatternPii("call +91 99999 88888").map((f) => [f.type, f.value]),
    [["phone", "+91 99999 88888"]],
  );
});

test("audit: DOB variants are detected; a bare ISO date is not a DOB", () => {
  assert.deepEqual(
    findPatternPii("born Jan 15, 1990").map((f) => [f.type, f.value]),
    [["dob", "Jan 15, 1990"]],
  );
  assert.deepEqual(
    findPatternPii("born 15 January 1990").map((f) => [f.type, f.value]),
    [["dob", "15 January 1990"]],
  );
  assert.deepEqual(
    findPatternPii("DOB 15-01-1990").map((f) => [f.type, f.value]),
    [["dob", "15-01-1990"]],
  );
  assert.deepEqual(
    findPatternPii("DOB 15/01/1990").map((f) => [f.type, f.value]),
    [["dob", "15/01/1990"]],
  );
  // The safe paragraph's workshop date must stay (over-redaction).
  assert.deepEqual(findPatternPii("workshop on 2026-12-01"), []);
});

test("audit: the phone pattern never matches a substring of a longer digit run", () => {
  // "98765432101" (11 digits) used to redact as "9[PHONE_1]" — a
  // partial match inside a longer run. Now: no match at all.
  assert.deepEqual(findPatternPii("ref 98765432101"), []);
  // An 11-digit run starting with 1 is a whole US phone, matched
  // in full — never partially.
  assert.deepEqual(
    findPatternPii("order 19876543210").map((f) => [f.type, f.value]),
    [["phone", "19876543210"]],
  );
});

test("audit: the second scan covers the new PII types too", () => {
  const upi = secondScan('{"text":"pay aarav@okhdfcbank"}');
  assert.equal(upi.clean, false);
  assert.equal(upi.remaining[0].type, "upi");
  const passport = secondScan('{"text":"passport A1234567"}');
  assert.equal(passport.clean, false);
  assert.equal(passport.remaining[0].type, "passport");
  const usPhone = secondScan('{"text":"call 415-555-0132"}');
  assert.equal(usPhone.clean, false);
  assert.equal(usPhone.remaining[0].type, "phone");
  assert.equal(secondScan('{"text":"pay [UPI_1]"}').clean, true);
});

test("audit: egress rejects lookalike origins, not just unknown hosts", () => {
  assert.equal(checkEgress("https://127.0.0.1:8000.evil.com/x").allowed, false);
  assert.equal(checkEgress("http://localhost:8000.evil.com/x").allowed, false);
  assert.equal(checkEgress("https://akash-antseed.duckdns.org.evil.com/v1").allowed, false);
  assert.equal(checkEgress("https://evil.com/?u=http://127.0.0.1:8000").allowed, false);
  assert.equal(checkEgress("http://127.0.0.1:8000@evil.com/").allowed, false);
  // The real targets still pass.
  assert.equal(checkEgress("http://127.0.0.1:8000/plan").allowed, true);
  assert.equal(checkEgress("https://api.groq.com/openai/v1/chat/completions").allowed, true);
});

test("audit: elements inside a display:none ancestor are not walked", () => {
  // isHidden used to check only the element itself — but a button
  // inside a display:none container keeps its own computed display,
  // so it was walked as visible and the executor would click what
  // the user cannot see.
  const { document } = parseHTML(`<html><body>
    <div id="wrap_none" style="display: none"><button id="el_in_none">Hidden</button></div>
    <div id="wrap_hidden" hidden><button id="el_in_hidden_attr">Hidden</button></div>
    <div id="wrap_vis"><button id="el_vis">Visible</button></div>
  </body></html>`);
  const els = walkDom(document);
  assert.equal(els.find((e) => e.id === "el_in_none"), undefined);
  assert.equal(els.find((e) => e.id === "el_in_hidden_attr"), undefined);
  assert.ok(els.find((e) => e.id === "el_vis"), "visible button still walked");

  // The same via computed style (a class-based display:none).
  const { document: doc2 } = parseHTML(`<html><body>
    <div id="wrap"><button id="el_btn2">Click</button></div>
  </body></html>`);
  Object.defineProperty(doc2, "defaultView", {
    value: {
      getComputedStyle: (el) => ({
        display: el.id === "wrap" ? "none" : "block",
        visibility: "visible",
      }),
    },
    configurable: true,
  });
  assert.equal(walkDom(doc2).find((e) => e.id === "el_btn2"), undefined);

  // Visibility is inherited but OVERRIDABLE: a child with
  // visibility:visible inside a visibility:hidden parent IS
  // rendered, so ancestors are not checked for visibility.
  const { document: doc3 } = parseHTML(`<html><body>
    <div id="vis_wrap"><button id="el_vis_child">Override</button><button id="el_vis_child2">Hidden</button></div>
  </body></html>`);
  Object.defineProperty(doc3, "defaultView", {
    value: {
      getComputedStyle: (el) => ({
        display: "block",
        visibility:
          el.id === "el_vis_child" ? "visible" : el.id === "vis_wrap" || el.id === "el_vis_child2" ? "hidden" : "visible",
      }),
    },
    configurable: true,
  });
  const els3 = walkDom(doc3);
  assert.ok(els3.find((e) => e.id === "el_vis_child"), "visibility override is rendered");
  assert.equal(els3.find((e) => e.id === "el_vis_child2"), undefined);

  // The ancestor walk crosses a shadow boundary: a hidden host
  // hides its shadow children.
  const { document: doc4 } = parseHTML(
    `<html><body><div id="host" style="display: none"></div></body></html>`,
  );
  const host = doc4.getElementById("host");
  host.attachShadow({ mode: "open" }).innerHTML =
    `<button id="el_shadow_hidden">Shadow</button>`;
  assert.equal(walkDom(doc4).find((e) => e.id === "el_shadow_hidden"), undefined);
});

test("audit: inForm covers form-attribute association, not just ancestry", () => {
  // closest("form") alone misses a submit button outside its
  // <form> with form="f1" — it still submits it, and the server's
  // consequential-action check reads this flag.
  const { document } = parseHTML(`<html><body>
    <form id="f1" action="/go"></form>
    <button id="el_outside" form="f1" type="submit">Submit</button>
    <input id="el_outside_input" form="f1" type="text" value="x">
    <form id="f2"><button id="el_inside" type="button">In form</button></form>
    <button id="el_nofrm" form="does-not-exist" type="button">No form</button>
    <div id="el_div_frm" form="f1">not a control</div>
  </body></html>`);
  const els = walkDom(document);
  assert.equal(els.find((e) => e.id === "el_outside").inForm, true);
  assert.equal(els.find((e) => e.id === "el_outside_input").inForm, true);
  assert.equal(els.find((e) => e.id === "el_inside").inForm, true);
  assert.equal(els.find((e) => e.id === "el_nofrm").inForm, false);
  assert.equal(els.find((e) => e.id === "el_div_frm").inForm, false);
});

test("audit: the gate defaults an unknown taskKind to the strictest kind", () => {
  // The old `=== "image" || === "mixed"` treated any other kind
  // as dom-only, so an unknown kind with failed OCR sailed
  // through. Unknown kinds now require vision like image/mixed.
  const base = {
    domWalkerOk: true,
    yoloOk: true,
    ocrOk: false,
    blazefaceOk: true,
    florenceOk: true,
    secondScan: { clean: true, remaining: [] },
    egressAllowed: true,
  };
  assert.equal(
    evaluateGate({ ...base, taskKind: "video" }).allow,
    false,
    "unknown kind + OCR failed blocks",
  );
  assert.equal(
    evaluateGate({ ...base, taskKind: "VIDEO" }).allow,
    false,
    "unknown kind (case typo) + OCR failed blocks",
  );
  assert.equal(
    evaluateGate({ ...base, taskKind: "video", ocrOk: true, blazefaceOk: false }).allow,
    false,
    "unknown kind + face detection failed blocks",
  );
  assert.equal(
    evaluateGate({ ...base, taskKind: "video", ocrOk: true }).allow,
    true,
    "unknown kind with vision passing allows",
  );
  assert.equal(
    evaluateGate({ ...base, taskKind: "dom" }).allow,
    true,
    "dom tasks still do not need OCR",
  );
});
