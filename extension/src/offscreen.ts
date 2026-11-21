/** Offscreen document entry (Sprint 2).
 *
 * Runs on-device inference in the offscreen document (WebGPU).
 * Message protocol (from the service worker):
 *   { type: "observe", url, viewport, elements, screenshot }
 *     → run the vision cascade, redact, second-scan, gate, and
 *       return { redactedPayload, blackoutBoxes, hits, gate }.
 *       The screenshot is painted black over the blackout boxes
 *       before it is returned as proof.
 *   { type: "ground", imageSource, phrase }
 *     → Florence-2 once, on demand; empty boxes when unavailable.
 *
 * Models load lazily on first use. Nothing here touches the network
 * except model files on first load.
 */
import * as ort from "onnxruntime-web";
import type { Observation } from "./vision/types.js";
import type { RgbaImage } from "./vision/yolo.js";
import { createYoloSession } from "./vision/yolo.js";
import { createOcrSession } from "./vision/ocr.js";
import { createBlazeFaceSession } from "./vision/blazeface.js";
import { observe, groundOnDemand } from "./vision/observe.js";
import { redactObservation } from "./vision/redactor.js";
import { secondScan } from "./vision/second-scan.js";
import { evaluateGate } from "./vision/gate.js";
import { checkEgress } from "./vision/egress.js";
import { PlaceholderRegistry } from "./vision/pii.js";
import { FLORENCE_MAX_CALLS_PER_STEP } from "./vision/florence2.js";
import type { Box } from "./vision/types.js";

// onnxruntime-web wasm files live in dist/ort-wasm/ (copied by the build).
ort.env.wasm.wasmPaths = chrome.runtime.getURL("dist/ort-wasm/");

const MODELS = {
  yolo: "models/yolo26n.onnx",
  det: "models/ch_PP-OCRv4_det_infer.onnx",
  rec: "models/ch_PP-OCRv4_rec_infer.onnx",
  recChars: "models/ppocr_rec_chars.txt",
  blazeface: "models/blazeface.onnx",
};

let sessions: {
  yolo: Awaited<ReturnType<typeof createYoloSession>> | null;
  ocr: Awaited<ReturnType<typeof createOcrSession>> | null;
  blazeface: Awaited<ReturnType<typeof createBlazeFaceSession>> | null;
} | null = null;

/** One placeholder registry per task: [EMAIL_1] must mean the same
 *  value on every step of one task. A fresh registry per observe
 *  message would re-number the same value mid-task. The task id
 *  comes from the caller (Sprint 3's planner); until then every
 *  observe shares the "default" task. */
const registries = new Map<string, PlaceholderRegistry>();

/** Florence-2 calls used by the current step. A step starts with
 *  an "observe" message, so the counter resets there; a "ground"
 *  after that observe belongs to that step. */
let florenceCallsThisStep = 0;

async function loadSessions() {
  if (sessions) return sessions;
  // A failed model load is logged and yields a null session — the
  // cascade reports it as "failed" and the gate blocks image tasks.
  // Never swallow it silently: a missing yolo26n.onnx must not
  // degrade to DOM+OCR-only without a trace.
  const [yolo, ocr, blazeface] = await Promise.all([
    createYoloSession(ort as never, chrome.runtime.getURL(MODELS.yolo)).catch((err) => {
      console.error("[eclipse] yolo model failed to load:", err);
      return null;
    }),
    (async () => {
      try {
        const chars = await fetch(chrome.runtime.getURL(MODELS.recChars)).then((r) => r.text());
        return await createOcrSession(
          ort as never,
          chrome.runtime.getURL(MODELS.det),
          chrome.runtime.getURL(MODELS.rec),
          chars,
        ).catch((err) => {
          console.error("[eclipse] ocr model failed to load:", err);
          return null;
        });
      } catch (err) {
        // The char-list fetch itself failed: OCR is unavailable.
        // (Before this fix the throw escaped loadSessions and the
        // observe message never got a response — a hang, not a leak.)
        console.error("[eclipse] ocr char list failed to load:", err);
        return null;
      }
    })(),
    createBlazeFaceSession(ort as never, chrome.runtime.getURL(MODELS.blazeface)).catch((err) => {
      console.error("[eclipse] blazeface model failed to load:", err);
      return null;
    }),
  ]);
  sessions = { yolo, ocr, blazeface };
  return sessions;
}

/** Paint the blackout boxes destructively onto the screenshot. */
export function paintBlackout(image: RgbaImage, boxes: Box[]): RgbaImage {
  const data = new Uint8ClampedArray(image.data);
  for (const box of boxes) {
    const x0 = Math.max(0, Math.floor(box.x * image.width));
    const y0 = Math.max(0, Math.floor(box.y * image.height));
    const x1 = Math.min(image.width, Math.ceil((box.x + box.w) * image.width));
    const y1 = Math.min(image.height, Math.ceil((box.y + box.h) * image.height));
    for (let y = y0; y < y1; y++) {
      for (let x = x0; x < x1; x++) {
        const i = (y * image.width + x) * 4;
        data[i] = 0;
        data[i + 1] = 0;
        data[i + 2] = 0;
        data[i + 3] = 255;
      }
    }
  }
  return { data, width: image.width, height: image.height };
}

interface ObserveMessage {
  type: "observe";
  url: string;
  viewport: { width: number; height: number };
  elements: Observation["elements"];
  screenshot: RgbaImage | null;
  taskKind: "dom" | "image" | "mixed";
  egressTarget: string;
  /** Task id (Sprint 3's planner passes it); the placeholder
   *  registry is scoped to it. Defaults to "default". */
  taskId?: string;
}

interface GroundMessage {
  type: "ground";
  imageSource: string;
  phrase: string;
}

chrome.runtime.onMessage.addListener((msg: ObserveMessage | GroundMessage, _sender, sendResponse) => {
  (async () => {
    if (msg.type === "ground") {
      // Florence-2 budget: at most FLORENCE_MAX_CALLS_PER_STEP per
      // step. The counter resets on each "observe" (a new step).
      if (florenceCallsThisStep >= FLORENCE_MAX_CALLS_PER_STEP) {
        sendResponse({
          boxes: [],
          used: false,
          reason: `Florence-2 budget exhausted for this step (max ${FLORENCE_MAX_CALLS_PER_STEP})`,
        });
        return;
      }
      florenceCallsThisStep += 1;
      const result = await groundOnDemand(msg.imageSource, msg.phrase);
      sendResponse({ boxes: result.boxes, used: result.used, reason: result.reason });
      return;
    }
    if (msg.type === "observe") {
      // A new step starts: reset the Florence-2 budget.
      florenceCallsThisStep = 0;
      const s = await loadSessions();
      const obs = await observe({
        url: msg.url,
        viewport: msg.viewport,
        elements: msg.elements,
        screenshot: msg.screenshot,
        sessions: s,
      });
      const taskId = msg.taskId ?? "default";
      let registry = registries.get(taskId);
      if (!registry) {
        registry = new PlaceholderRegistry();
        registries.set(taskId, registry);
      }
      const redacted = redactObservation(obs, registry);
      // The second scan also checks that none of the raw values the
      // redactor recorded is still present in the payload.
      const rawValues = redacted.hits
        .filter((h) => h.type !== "face" && !h.value.startsWith("withheld:"))
        .map((h) => h.value);
      const scan = secondScan(redacted.redactedPayload, rawValues);
      const egress = checkEgress(msg.egressTarget);
      const gate = evaluateGate({
        taskKind: msg.taskKind,
        domWalkerOk: obs.detectorStatus.dom === "ok",
        yoloOk: obs.detectorStatus.yolo !== "failed",
        ocrOk: obs.detectorStatus.ocr !== "failed",
        blazefaceOk: obs.detectorStatus.blazeface !== "failed",
        florenceOk: obs.detectorStatus.florence2 !== "failed",
        secondScan: scan,
        egressAllowed: egress.allowed,
        egressReason: egress.reason,
      });
      const proofScreenshot = msg.screenshot
        ? paintBlackout(msg.screenshot, redacted.blackoutBoxes)
        : null;
      sendResponse({
        redactedPayload: gate.allow ? redacted.redactedPayload : null,
        blackoutBoxes: redacted.blackoutBoxes,
        hits: redacted.hits.map((h) => ({ type: h.type, placeholder: h.placeholder })),
        gate,
        proofScreenshot,
        detectorStatus: obs.detectorStatus,
      });
      return;
    }
    sendResponse({ error: `unknown message type: ${(msg as { type: string }).type}` });
  })();
  return true; // async response
});
