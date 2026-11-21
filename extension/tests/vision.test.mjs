/** Vision pipeline tests (Sprint 2): walker, redactor, second scan,
 *  gate, egress, PII patterns. Assertions use the Sprint 1 ground
 *  truth (evals/sih-benchmark/ground_truth.json) and the fixture
 *  HTML. All PII in fixtures is synthetic.
 *
 *  Run: npm test (pretest compiles src/vision/*.ts to tests-dist/).
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { parseHTML } from "linkedom";

import { walkDom, walkDomWithNodes, resolveElement } from "../tests-dist/vision/dom-walker.js";
import { redactObservation, redactText, mergeBoxes } from "../tests-dist/vision/redactor.js";
import { groupIntoLines } from "../tests-dist/vision/ocr.js";
import { observe } from "../tests-dist/vision/observe.js";
import { secondScan } from "../tests-dist/vision/second-scan.js";
import { evaluateGate } from "../tests-dist/vision/gate.js";
import { checkEgress } from "../tests-dist/vision/egress.js";
import { findPatternPii, PlaceholderRegistry, classifyField } from "../tests-dist/vision/pii.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const FIXTURES = join(ROOT, "evals", "sih-benchmark", "fixtures");
const GT = JSON.parse(
  readFileSync(join(ROOT, "evals", "sih-benchmark", "ground_truth.json"), "utf8"),
).fixtures;

function loadFixture(name) {
  return readFileSync(join(FIXTURES, name), "utf8");
}

function walkFixture(name) {
  const { document } = parseHTML(loadFixture(name));
  return walkDom(document);
}

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

test("walker: span-wrapped button labels are the button's name", () => {
  // Most real buttons (shadcn included) wrap the label in a <span>;
  // the walker must not report a labelless interactable button.
  const { document } = parseHTML(
    `<html><body><button id="el_apply"><span>Apply now</span></button><div id="el_wrap"><button id="el_inner">Inner</button></div></body></html>`,
  );
  const els = walkDom(document);
  const apply = els.find((e) => e.id === "el_apply");
  assert.ok(apply, "button walked");
  assert.equal(apply.name, "Apply now");
  assert.equal(apply.interactable, true);
  // A container holding a button must NOT claim the button's text
  // as its own name (that is what ownText prevents).
  const wrap = els.find((e) => e.id === "el_wrap");
  assert.equal(wrap.name, "");
});

test("walker: login form — password value never recorded", () => {
  const els = walkFixture("login.html");
  const pw = els.find((e) => e.id === "el_login_password");
  assert.ok(pw, "password input walked");
  assert.equal(pw.sensitive, true);
  assert.equal(pw.value, undefined, "password value must not be recorded");
  const email = els.find((e) => e.id === "el_login_email");
  assert.equal(email.value, "test.user@example.com");
  const submit = els.find((e) => e.id === "el_login_submit");
  assert.equal(submit.role, "button");
  assert.equal(submit.interactable, true);
  const forgot = els.find((e) => e.id === "el_login_forgot");
  assert.equal(forgot.role, "link");
  assert.equal(resolveElement(els, "el_login_submit")?.id, "el_login_submit");
  assert.equal(resolveElement(els, "el_no_such_id"), null);
});

test("walker: hidden inputs are walked but not visible or clickable", () => {
  const els = walkFixture("hidden-fields.html");
  const prev = els.find((e) => e.id === "el_hidden_prev_email");
  assert.ok(prev, "hidden previous-email input is walked");
  assert.equal(prev.visible, false);
  assert.equal(prev.interactable, false);
  assert.equal(prev.value, "old.user@example.com");
  const csrf = els.find((e) => e.id === "el_hidden_csrf");
  assert.equal(csrf.visible, false);
});

test("redactor: pii-text — all 7 ground-truth items redacted, safe text intact", () => {
  const els = walkFixture("pii-text.html");
  const red = redactObservation(observeFromElements(els));
  const payload = red.redactedText;
  for (const want of ["[NAME_1]", "[EMAIL_1]", "[PHONE_1]", "[AADHAAR_1]", "[PAN_1]", "[ADDRESS_1]", "[DOB_1]"]) {
    assert.ok(payload.includes(want), `payload must contain ${want}`);
  }
  for (const raw of ["Aarav Test", "aarav.test@example.com", "99999 88888", "9999 8888 7777", "ABCDE1234F", "12 Test Street", "01 Jan 2005"]) {
    assert.ok(!payload.includes(raw), `raw value must not leak: ${raw}`);
  }
  // Safe paragraphs are NOT redacted (over-redaction is measured).
  assert.ok(payload.includes("office hours are 9 AM to 6 PM"), "safe paragraph intact");
  assert.ok(payload.includes("2026-12-01"), "safe date intact");
  assert.ok(payload.includes("test coordinator"), "intro paragraph intact");
  assert.equal(secondScan(payload).clean, true);
  assert.equal(red.hits.length, 7);
});

test("redactor: login — email replaced, password never in payload", () => {
  const els = walkFixture("login.html");
  const red = redactObservation(observeFromElements(els));
  assert.ok(red.redactedText.includes("[EMAIL_1]"));
  assert.ok(!red.redactedText.includes("test.user@example.com"));
  assert.ok(!red.redactedText.includes("TestPass123!"), "password must never appear");
  // The planner needs a placeholder token to reference the password
  // field by — the walker withholds the value, so the redactor
  // issues one (the vault fills the real value at execution).
  assert.ok(red.redactedText.includes("[PASSWORD_1]"), "password field gets a placeholder");
  assert.equal(secondScan(red.redactedText).clean, true);
});

test("redactor: checkout — card is [CARD_LAST4], CVV never leaves", () => {
  const els = walkFixture("checkout.html");
  const red = redactObservation(observeFromElements(els));
  const payload = red.redactedText;
  assert.ok(payload.includes("[CARD_LAST4]"), "card placeholder is [CARD_LAST4]");
  assert.ok(payload.includes("[NAME_1]"), "card name redacted");
  assert.ok(payload.includes("[CVV]"), "cvv field gets a placeholder");
  assert.ok(!payload.includes("4111 1111 1111 1111"), "raw card number must not leak");
  assert.ok(!payload.includes('"value":"123"'), "raw CVV must not leak");
  // Expiry is not PII per ground truth — it stays (vault fills the card).
  assert.ok(payload.includes("12/29"), "expiry stays");
  assert.equal(secondScan(payload).clean, true);
});

test("redactor: hidden previous email is [EMAIL_2], csrf/tracking stay", () => {
  const els = walkFixture("hidden-fields.html");
  const red = redactObservation(observeFromElements(els));
  const payload = red.redactedText;
  assert.ok(payload.includes("[EMAIL_1]"), "visible email first");
  assert.ok(payload.includes("[EMAIL_2]"), "hidden previous email second");
  assert.ok(!payload.includes("old.user@example.com"));
  assert.ok(payload.includes("csrf-test-token-abc123"), "csrf token is not user PII");
  assert.ok(payload.includes("utm-test-campaign"), "tracking id is not user PII");
  assert.equal(secondScan(payload).clean, true);
});

test("redactor: faces from ground truth are blacked out", () => {
  const faces = GT.faces.face_boxes;
  const obs = observeFromElements([]);
  obs.faces = [
    ...faces["photo-1.png"],
    ...faces["photo-2.png"],
  ];
  const red = redactObservation(obs);
  assert.equal(red.hits.filter((h) => h.type === "face").length, 3);
  // Face placeholders live in the redaction record (hits), not in
  // the payload text — faces are boxes, and the boxes themselves are
  // geometry, not PII. The payload carries the boxes for the proof
  // view; the pixels are blacked out.
  const facePlaceholders = red.hits
    .filter((h) => h.type === "face")
    .map((h) => h.placeholder)
    .sort();
  assert.deepEqual(facePlaceholders, ["[FACE_1]", "[FACE_2]", "[FACE_3]"]);
  // The three face boxes merge into fewer blackout boxes when they
  // overlap (photo-2's big face covers photo-1's region) — what
  // matters is that every face box is covered, not the box count.
  for (const f of obs.faces) {
    const covered = red.blackoutBoxes.some(
      (b) => b.x <= f.x + 0.001 && b.y <= f.y + 0.001 && b.x + b.w >= f.x + f.w - 0.001 && b.y + b.h >= f.y + f.h - 0.001,
    );
    assert.ok(covered, `face box ${JSON.stringify(f)} must be covered`);
  }
});

test("redactor: sensitive values passed at runtime get fixed placeholders", () => {
  const obs = observeFromElements([
    { id: "el_pw", tag: "input", role: "textbox", name: "Password", text: "", value: "hunter2", sensitive: true, rect: { x: 0, y: 0, width: 0, height: 0 }, visible: true, interactable: true },
    { id: "el_otp", tag: "input", role: "textbox", name: "OTP", text: "", value: "482913", sensitive: true, rect: { x: 0, y: 0, width: 0, height: 0 }, visible: true, interactable: true },
    { id: "el_cvv", tag: "input", role: "textbox", name: "CVV", text: "", value: "847", sensitive: true, rect: { x: 0, y: 0, width: 0, height: 0 }, visible: true, interactable: true },
  ]);
  const red = redactObservation(obs);
  assert.ok(red.redactedText.includes("[PASSWORD_1]"));
  assert.ok(red.redactedText.includes("[OTP]"));
  assert.ok(red.redactedText.includes("[CVV]"));
  for (const raw of ["hunter2", "482913", '"value":"847"']) {
    assert.ok(!red.redactedText.includes(raw), `raw secret must not leak: ${raw}`);
  }
});

test("redactor: OCR words with PII are redacted and their boxes blacked out", () => {
  const obs = observeFromElements([]);
  obs.ocr = [
    { text: "Email: test.user@example.com", box: { x: 0.1, y: 0.2, w: 0.5, h: 0.05 }, confidence: 0.98 },
    { text: "College: Test College of Engineering", box: { x: 0.1, y: 0.3, w: 0.5, h: 0.05 }, confidence: 0.97 },
  ];
  const red = redactObservation(obs);
  assert.ok(red.redactedText.includes("[EMAIL_1]"));
  assert.ok(!red.redactedText.includes("test.user@example.com"));
  assert.ok(red.redactedText.includes("Test College of Engineering"), "non-PII OCR text stays");
  assert.equal(red.blackoutBoxes.length, 1, "only the PII word's box is blacked out");
});

test("second scan: clean on redacted, dirty on raw", () => {
  assert.equal(secondScan('{"text":"hello [EMAIL_1]"}').clean, true);
  const dirty = secondScan('{"text":"call aarav.test@example.com"}');
  assert.equal(dirty.clean, false);
  assert.equal(dirty.remaining[0].type, "email");
  // A raw value the redactor recorded must not survive in the
  // payload, even when no pattern matches it (a name has no regex).
  assert.equal(secondScan('{"text":"Aarav Test applied"}', ["Aarav Test"]).clean, false);
  assert.equal(secondScan('{"text":"[NAME_1] applied"}', ["Aarav Test"]).clean, true);
});

test("observe: missing model sessions fail closed on image tasks", async () => {
  // A model that failed to load is a failure, not a skip — otherwise
  // an image task with no OCR and no face detection passes the gate.
  const shot = { data: new Uint8Array(4 * 100 * 100), width: 100, height: 100 };
  const obs = await observe({
    url: "http://localhost/test",
    viewport: { width: 1280, height: 800 },
    elements: [],
    screenshot: shot,
    sessions: { yolo: null, ocr: null, blazeface: null },
  });
  assert.equal(obs.detectorStatus.ocr, "failed");
  assert.equal(obs.detectorStatus.blazeface, "failed");
  assert.equal(obs.detectorStatus.yolo, "failed");
  assert.equal(
    evaluateGate({
      taskKind: "image",
      domWalkerOk: true,
      yoloOk: true,
      ocrOk: obs.detectorStatus.ocr !== "failed",
      blazefaceOk: obs.detectorStatus.blazeface !== "failed",
      florenceOk: true,
      secondScan: { clean: true, remaining: [] },
      egressAllowed: true,
    }).allow,
    false,
    "image task with no OCR must block",
  );
  // DOM-only (no screenshot) stays skipped, not failed — a DOM task
  // does not need vision models.
  const domObs = await observe({
    url: "http://localhost/test",
    viewport: { width: 1280, height: 800 },
    elements: [],
    screenshot: null,
    sessions: { yolo: null, ocr: null, blazeface: null },
  });
  assert.equal(domObs.detectorStatus.ocr, "skipped");
  assert.equal(domObs.detectorStatus.dom, "ok", "an empty page is a valid walk, not a failure");
});

test("gate: fail-closed rules", () => {
  const base = {
    taskKind: "mixed",
    domWalkerOk: true,
    yoloOk: true,
    ocrOk: true,
    blazefaceOk: true,
    florenceOk: true,
    secondScan: { clean: true, remaining: [] },
    egressAllowed: true,
  };
  assert.equal(evaluateGate(base).allow, true);
  assert.equal(evaluateGate({ ...base, domWalkerOk: false }).allow, false);
  assert.equal(evaluateGate({ ...base, secondScan: { clean: false, remaining: [{ type: "email", value: "x@y.z" }] } }).allow, false);
  assert.equal(evaluateGate({ ...base, egressAllowed: false, egressReason: "not on allow-list" }).allow, false);
  assert.equal(evaluateGate({ ...base, ocrOk: false }).allow, false, "image task + OCR failed blocks");
  assert.equal(evaluateGate({ ...base, blazefaceOk: false }).allow, false, "image task + face detection failed blocks");
  assert.equal(evaluateGate({ ...base, taskKind: "dom", ocrOk: false }).allow, true, "DOM task does not need OCR");
  assert.equal(evaluateGate({ ...base, yoloOk: false }).allow, true, "YOLO failure is a warning, not a block");
  assert.equal(evaluateGate({ ...base, florenceOk: false }).allow, true, "Florence-2 failure is a warning, not a block");
});

test("egress: only listed origins pass", () => {
  assert.equal(checkEgress("http://127.0.0.1:8000/health").allowed, true);
  assert.equal(checkEgress("https://akash-antseed.duckdns.org/v1/chat/completions").allowed, true);
  assert.equal(checkEgress("https://api.groq.com/openai/v1/chat/completions").allowed, true);
  assert.equal(checkEgress("http://127.0.0.1:11434/api/chat").allowed, true);
  assert.equal(checkEgress("https://evil.example.com/steal").allowed, false);
  assert.equal(checkEgress("https://prateek-antseed.duckdns.org/v1/models").allowed, false, "Prateek's gateway is never an egress target");
  assert.equal(checkEgress("not a url").allowed, false);
});

test("pii: patterns find structured values; registry is stable per value", () => {
  const found = findPatternPii("mail aarav.test@example.com, call +91 99999 88888, PAN ABCDE1234F, Aadhaar 9999 8888 7777, card 4111 1111 1111 1111, born 01 Jan 2005");
  const types = found.map((f) => f.type).sort();
  assert.deepEqual(types, ["aadhaar", "card_number", "dob", "email", "pan", "phone"]);
  const reg = new PlaceholderRegistry();
  assert.equal(reg.placeholderFor("email", "a@b.com"), "[EMAIL_1]");
  assert.equal(reg.placeholderFor("email", "a@b.com"), "[EMAIL_1]", "same value, same placeholder");
  assert.equal(reg.placeholderFor("email", "c@d.com"), "[EMAIL_2]");
  assert.equal(reg.placeholderFor("card_number", "4111 1111 1111 1111"), "[CARD_LAST4]");
  assert.equal(reg.placeholderFor("cvv", "123"), "[CVV]");
  assert.equal(reg.placeholderFor("otp", "123456"), "[OTP]");
  assert.equal(classifyField("Name on card"), "name");
  assert.equal(classifyField("Card number"), "card_number");
  assert.equal(classifyField("Search"), null);
});

test("redactor: label-prefix rule redacts names and addresses in free text", () => {
  const reg = new PlaceholderRegistry();
  const r1 = redactText("Name: Aarav Test", reg);
  assert.equal(r1.text, "Name: [NAME_1]");
  const r2 = redactText("Address: 12 Test Street, Hyderabad", reg);
  assert.equal(r2.text, "Address: [ADDRESS_1]");
  const r3 = redactText("Total: ₹499", reg);
  assert.equal(r3.text, "Total: ₹499", "unclassified labels are left alone");
  // A label and a pattern in one text node: the label rule must
  // still run — the old code returned after the pattern pass and
  // leaked the name raw.
  const r4 = redactText("Name: Aarav Test, email aarav.test@example.com", new PlaceholderRegistry());
  assert.ok(r4.text.includes("[NAME_1]"), "name redacted even when an email is present");
  assert.ok(r4.text.includes("[EMAIL_1]"), "email redacted");
  assert.ok(!r4.text.includes("Aarav Test"), "raw name must not leak");
  assert.ok(!r4.text.includes("aarav.test@example.com"), "raw email must not leak");
  // Roll numbers have no regex: the label rule is their only path.
  assert.equal(classifyField("Roll"), "roll", "bare 'Roll' classifies");
  const r5 = redactText("Roll: TEST-24-0841", new PlaceholderRegistry());
  assert.equal(r5.text, "Roll: [ROLL_1]");
  // False positives the label rule must NOT make: "File Name" and
  // "Domain Name" are not a person's name, and "Email: contact us"
  // is not an email address.
  assert.equal(redactText("File Name: report.pdf", new PlaceholderRegistry()).text, "File Name: report.pdf");
  assert.equal(redactText("Domain Name: example.com", new PlaceholderRegistry()).text, "Domain Name: example.com");
  assert.equal(redactText("Email: contact us", new PlaceholderRegistry()).text, "Email: contact us");
});

test("ocr: groupIntoLines merges split line fragments", () => {
  // A "Roll: TEST-24-0841" line split into two detector boxes must
  // come back as one line, or the label rule can never fire on it.
  const merged = groupIntoLines([
    { text: "Roll:", box: { x: 0.05, y: 0.45, w: 0.08, h: 0.02 }, confidence: 0.9 },
    { text: "TEST-24-0841", box: { x: 0.14, y: 0.45, w: 0.2, h: 0.02 }, confidence: 0.9 },
    { text: "College: Test College", box: { x: 0.05, y: 0.52, w: 0.4, h: 0.02 }, confidence: 0.9 },
  ]);
  assert.equal(merged.length, 2, "two lines stay two lines");
  assert.equal(merged[0].text, "Roll: TEST-24-0841");
  assert.equal(merged[1].text, "College: Test College");
  const red = redactText(merged[0].text, new PlaceholderRegistry());
  assert.equal(red.text, "Roll: [ROLL_1]");
});

test("redactor: mergeBoxes unions overlaps", () => {
  const merged = mergeBoxes([
    { x: 0.1, y: 0.1, w: 0.2, h: 0.2 },
    { x: 0.2, y: 0.2, w: 0.2, h: 0.2 },
    { x: 0.8, y: 0.8, w: 0.1, h: 0.1 },
  ]);
  assert.equal(merged.length, 2);
});

test("walker: the 2000-element cap reports truncated", () => {
  // A page with more elements than the cap: the walk reports
  // truncated so the planner knows the observation is incomplete.
  const html = "<html><body>" + "<div>item</div>".repeat(2500) + "</body></html>";
  const { document } = parseHTML(html);
  const { info, truncated } = walkDomWithNodes(document, { maxElements: 10 });
  assert.equal(info.length, 10);
  assert.equal(truncated, true);
  const full = walkDomWithNodes(document, { maxElements: 3000 });
  assert.equal(full.truncated, false);
});
