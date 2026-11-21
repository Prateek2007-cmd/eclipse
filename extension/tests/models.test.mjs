/** Live model smoke tests (Sprint 2): YOLO26n, PP-OCRv4, BlazeFace.
 *
 * These run the real ONNX models (onnxruntime-node, CPU) on the
 * Sprint 1 fixture PNGs. They prove the models load, run, and return
 * well-formed output — and, for OCR, that real words come back.
 * The YOLO parser is also unit-tested with synthetic tensors for
 * both output shapes ([1,300,6] NMS-free and [1,84,8400] + NMS).
 *
 * Run: npm test (slow: model loads take seconds on CPU).
 */
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import * as ort from "onnxruntime-node";
import { PNG } from "pngjs";

import { createYoloSession, parseYoloOutput } from "../tests-dist/vision/yolo.js";
import { createOcrSession } from "../tests-dist/vision/ocr.js";
import { createBlazeFaceSession } from "../tests-dist/vision/blazeface.js";

const HERE = dirname(fileURLToPath(import.meta.url));
const MODELS = join(HERE, "..", "models");
const FIXTURES = join(HERE, "..", "..", "evals", "sih-benchmark", "fixtures");
const GT = JSON.parse(
  readFileSync(join(HERE, "..", "..", "evals", "sih-benchmark", "ground_truth.json"), "utf8"),
).fixtures;

function readPng(name) {
  const png = PNG.sync.read(readFileSync(join(FIXTURES, name)));
  return { data: new Uint8Array(png.data), width: png.width, height: png.height };
}

function norm(s) {
  return s.toLowerCase().replace(/\s+/g, " ").trim();
}

// Compare content, not spacing: PP-OCR drops some spaces (notably
// after ":" and inside "APPLICATION FORM") and lowercases. Strip
// all whitespace on both sides before matching.
function squish(s) {
  return s.replace(/\s+/g, "");
}

test("yolo: parser handles both output shapes", () => {
  const img = { data: new Uint8Array(4 * 100 * 100), width: 100, height: 100 };
  // NMS-free [1,300,6]: one detection, box (100,100)-(200,200) in the
  // 640 frame, score 0.9, class 0 (person).
  const endToEnd = new Float32Array(300 * 6);
  endToEnd[0] = 100; endToEnd[1] = 100; endToEnd[2] = 200; endToEnd[3] = 200;
  endToEnd[4] = 0.9; endToEnd[5] = 0;
  const d1 = parseYoloOutput(endToEnd, [1, 300, 6], img, 6.4, 0, 0);
  assert.equal(d1.length, 1);
  assert.equal(d1[0].label, "person");
  assert.ok(d1[0].confidence > 0.89);
  assert.ok(d1[0].box.x >= 0 && d1[0].box.x <= 1);
  // Classic [1,84,8400]: one anchor with class 0 score 0.8.
  const classic = new Float32Array(84 * 8400);
  classic[0 * 8400 + 0] = 320; // cx
  classic[1 * 8400 + 0] = 320; // cy
  classic[2 * 8400 + 0] = 64; // w
  classic[3 * 8400 + 0] = 64; // h
  classic[4 * 8400 + 0] = 0.8; // class 0 score
  const d2 = parseYoloOutput(classic, [1, 84, 8400], img, 6.4, 0, 0);
  assert.equal(d2.length, 1);
  assert.equal(d2[0].label, "person");
  // Unsupported shape throws (fail loudly, never silently empty).
  assert.throws(() => parseYoloOutput(new Float32Array(10), [1, 10], img, 6.4, 0, 0));
});

test("yolo26n: loads and runs on a fixture photo", async () => {
  const session = await createYoloSession(ort, join(MODELS, "yolo26n.onnx"), ["cpu"]);
  const started = Date.now();
  const detections = await session.run(readPng("photo-1.png"));
  const ms = Date.now() - started;
  console.log(`  yolo26n on photo-1.png: ${detections.length} detections in ${ms} ms`);
  assert.ok(Array.isArray(detections));
  // Correctness, not just well-formedness: the fixture photo must
  // produce at least one detection (a detector returning zero would
  // pass the old assertions).
  assert.ok(detections.length >= 1, "yolo26n must find at least one region on photo-1.png");
  for (const d of detections) {
    assert.ok(d.box.x >= 0 && d.box.x <= 1 && d.box.w > 0 && d.box.w <= 1);
    assert.ok(d.confidence >= 0.25 && d.confidence <= 1);
    assert.ok(typeof d.label === "string" && d.label.length > 0);
  }
  await session.dispose();
});

test("ocr: reads the scanned form (ground-truth lines)", async () => {
  const chars = readFileSync(join(MODELS, "ppocr_rec_chars.txt"), "utf8");
  const session = await createOcrSession(
    ort,
    join(MODELS, "ch_PP-OCRv4_det_infer.onnx"),
    join(MODELS, "ch_PP-OCRv4_rec_infer.onnx"),
    chars,
    ["cpu"],
  );
  const started = Date.now();
  const words = await session.read(readPng("scanned-form.png"));
  const ms = Date.now() - started;
  const text = norm(words.map((w) => w.text).join(" "));
  console.log(`  ocr on scanned-form.png: ${words.length} words in ${ms} ms`);
  console.log(`  ocr text: ${text}`);
  const expected = GT["scanned-doc"].ocr_text.map((line) => squish(norm(line)));
  const matched = expected.filter((line) => squish(text).includes(line));
  console.log(`  ocr matched ${matched.length}/6 ground-truth lines`);
  assert.ok(matched.length >= 4, `expected >=4 of 6 lines, got ${matched.length}: ${text}`);
  // The PII-bearing lines specifically must be read — a generic
  // line count does not prove the values the redactor needs are there.
  const squished = squish(text);
  assert.ok(
    squished.includes(squish(norm("Email: test.user@example.com"))),
    "email line (PII-bearing) must be read",
  );
  assert.ok(
    squished.includes(squish(norm("Phone: 99999 99999"))),
    "phone line (PII-bearing) must be read",
  );
  assert.ok(
    squished.includes(squish(norm("Roll: TEST-24-0841"))),
    "roll line (PII-bearing) must be read",
  );
  await session.dispose();
});

test("blazeface: loads and runs on both fixture photos", async () => {
  const session = await createBlazeFaceSession(ort, join(MODELS, "blazeface.onnx"), ["cpu"]);
  const gtBoxes = GT.faces.face_boxes;
  for (const name of ["photo-1.png", "photo-2.png"]) {
    const started = Date.now();
    const faces = await session.detect(readPng(name));
    const ms = Date.now() - started;
    console.log(`  blazeface on ${name}: ${faces.length} faces in ${ms} ms`);
    assert.ok(Array.isArray(faces));
    for (const f of faces) {
      assert.ok(f.box.x >= 0 && f.box.x <= 1 && f.box.w > 0 && f.box.w <= 1);
      assert.ok(f.confidence > 0 && f.confidence <= 1);
      assert.equal(f.landmarks.length, 6);
    }
    // Correctness vs ground truth: the right count, and a detection
    // whose centre falls inside each GT face box. A detector
    // returning zero faces must not pass.
    const expected = gtBoxes[name];
    assert.equal(faces.length, expected.length, `${name}: expected ${expected.length} faces, got ${faces.length}`);
    for (const gt of expected) {
      const hit = faces.some((f) => {
        const cx = f.box.x + f.box.w / 2;
        const cy = f.box.y + f.box.h / 2;
        return (
          cx >= gt.x - 0.05 && cx <= gt.x + gt.w + 0.05 &&
          cy >= gt.y - 0.05 && cy <= gt.y + gt.h + 0.05
        );
      });
      assert.ok(hit, `${name}: a detection must cover GT face ${JSON.stringify(gt)}`);
    }
  }
  await session.dispose();
});
