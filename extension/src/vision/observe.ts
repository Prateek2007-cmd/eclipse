/** Observe — the vision cascade (Sprint 2).
 *
 * Order: DOM + accessibility tree first (exact boxes for ~80–90%
 * of web elements), then YOLO (interactable regions), OCR (text +
 * boxes), BlazeFace (faces). Florence-2 is NOT in this cascade —
 * it is lazy, called once per step at most, only when DOM + YOLO +
 * OCR cannot answer a grounding question (see groundOnDemand).
 *
 * Every detector reports ok / failed / skipped. A failure is data
 * for the fail-closed gate, not something to hide.
 */
import type { Detection, Observation, OcrWord } from "./types.js";
import type { Box } from "./types.js";
import type { YoloSession, RgbaImage } from "./yolo.js";
import type { OcrSession } from "./ocr.js";
import type { BlazeFaceSession } from "./blazeface.js";
import { groundPhrase } from "./florence2.js";

export interface VisionSessions {
  yolo: YoloSession | null;
  ocr: OcrSession | null;
  blazeface: BlazeFaceSession | null;
}

export interface ObserveInput {
  url: string;
  viewport: { width: number; height: number };
  /** Elements from the content-script DOM walk. */
  elements: Observation["elements"];
  /** Screenshot pixels; null for DOM-only tasks. */
  screenshot: RgbaImage | null;
  sessions: VisionSessions;
  /** True when Florence-2 was consulted for this step (set by the
   *  offscreen "ground" handler; Sprint 3's planner owns the step
   *  lifecycle and will pass this explicitly). */
  florenceUsed?: boolean;
}

export async function observe(input: ObserveInput): Promise<Observation> {
  const status: Record<string, "ok" | "failed" | "skipped"> = {
    // The walk ran (the content script sent its result); an empty
    // page is a valid result, not a walker failure.
    dom: "ok",
    yolo: "skipped",
    ocr: "skipped",
    blazeface: "skipped",
    florence2: "skipped",
  };
  const errors: Record<string, string> = {};
  let detections: Detection[] = [];
  let ocrWords: OcrWord[] = [];
  let faces: Box[] = [];

  if (input.screenshot) {
    // A screenshot was supplied, so every vision detector is
    // REQUIRED: a missing session (model failed to load) is a
    // failure, not a skip — otherwise an image task with no OCR
    // and no face detection would pass the gate. Detectors run in
    // parallel; the ≤6 s/step budget is tight.
    const jobs: Promise<void>[] = [];
    jobs.push(
      (async () => {
        if (!input.sessions.yolo) {
          status.yolo = "failed";
          errors.yolo = "yolo session unavailable (model failed to load)";
          return;
        }
        try {
          detections = await input.sessions.yolo.run(input.screenshot!);
          status.yolo = "ok";
        } catch (err) {
          status.yolo = "failed";
          errors.yolo = err instanceof Error ? err.message : String(err);
        }
      })(),
    );
    jobs.push(
      (async () => {
        if (!input.sessions.ocr) {
          status.ocr = "failed";
          errors.ocr = "ocr session unavailable (model failed to load)";
          return;
        }
        try {
          ocrWords = await input.sessions.ocr.read(input.screenshot!);
          status.ocr = "ok";
        } catch (err) {
          status.ocr = "failed";
          errors.ocr = err instanceof Error ? err.message : String(err);
        }
      })(),
    );
    jobs.push(
      (async () => {
        if (!input.sessions.blazeface) {
          status.blazeface = "failed";
          errors.blazeface = "blazeface session unavailable (model failed to load)";
          return;
        }
        try {
          const found = await input.sessions.blazeface.detect(input.screenshot!);
          faces = found.map((f) => f.box);
          status.blazeface = "ok";
        } catch (err) {
          status.blazeface = "failed";
          errors.blazeface = err instanceof Error ? err.message : String(err);
        }
      })(),
    );
    await Promise.all(jobs);
  }

  return {
    url: input.url,
    viewport: input.viewport,
    elements: input.elements,
    detections,
    ocr: ocrWords,
    faces,
    florenceUsed: input.florenceUsed ?? false,
    detectorStatus: status,
    detectorErrors: Object.keys(errors).length ? errors : undefined,
    observedAt: Date.now(),
  };
}

/** Ground a phrase with Florence-2, once, on demand. Returns the
 *  boxes found (empty when unavailable) and whether it was used. */
export async function groundOnDemand(
  imageSource: string | Blob | Uint8Array,
  phrase: string,
): Promise<{ boxes: { box: Box; label: string; confidence: number }[]; used: boolean; reason?: string }> {
  const result = await groundPhrase(imageSource, phrase);
  return { boxes: result.boxes, used: result.available, reason: result.reason };
}
