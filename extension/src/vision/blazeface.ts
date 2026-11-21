/** BlazeFace — face detection (Sprint 2).
 *
 * Model: garavv/blazeface-onnx (MediaPipe BlazeFace short-range,
 * Apache-2.0), ~0.5 MB, input 128x128 RGB. NMS runs INSIDE the
 * model — it takes conf_threshold / max_detections / iou_threshold
 * as inputs and returns the filtered detections directly, so this
 * wrapper needs no anchor math.
 *
 * Input:  image (1,3,128,128) float32 [0..1]
 *         conf_threshold [1] float32, max_detections [1] int64,
 *         iou_threshold [1] float32
 * Output: a single tensor `selectedBoxes` [1, N, 16] — N detections
 *         after the model's internal NMS (N=0 when nothing is found).
 *         Each row is (top_y, top_x, bot_y, bot_x, 6 landmarks x,y),
 *         all relative (0..1). This export returns NO scores, despite
 *         what the upstream README's Python snippet suggests — the
 *         model's internal conf_threshold already filtered, so
 *         confidence is reported as that threshold (0.5) — a lower
 *         bound, not a measurement. Verified live 2026-11-21.
 *
 * Faces are PII: every detected face becomes a blackout box in the
 * redactor. The landmarks are kept for the proof view only.
 */
import type { Box } from "./types.js";
import type { OrtLike, RgbaImage } from "./yolo.js";

export const BLAZEFACE_INPUT_SIZE = 128;
export const BLAZEFACE_CONF_THRESHOLD = 0.5;
export const BLAZEFACE_IOU_THRESHOLD = 0.3;
export const BLAZEFACE_MAX_DETECTIONS = 25;

export interface FaceDetection {
  box: Box;
  confidence: number;
  /** 6 landmarks: left eye, right eye, nose, mouth, left ear, right ear. */
  landmarks: { x: number; y: number }[];
}

export interface BlazeFaceSession {
  detect(image: RgbaImage): Promise<FaceDetection[]>;
  dispose(): Promise<void>;
}

export async function createBlazeFaceSession(
  ort: OrtLike,
  modelPath: string,
  executionProviders: string[] = ["webgpu", "wasm"],
): Promise<BlazeFaceSession> {
  const session = await ort.InferenceSession.create(modelPath, { executionProviders });

  async function detect(image: RgbaImage): Promise<FaceDetection[]> {
    // Resize to 128x128, /255, NCHW (per the model README).
    const size = BLAZEFACE_INPUT_SIZE;
    const tensor = new Float32Array(3 * size * size);
    for (let y = 0; y < size; y++) {
      for (let x = 0; x < size; x++) {
        const srcX = Math.min(image.width - 1, Math.floor((x * image.width) / size));
        const srcY = Math.min(image.height - 1, Math.floor((y * image.height) / size));
        const srcIdx = (srcY * image.width + srcX) * 4;
        const dstIdx = y * size + x;
        tensor[dstIdx] = image.data[srcIdx] / 255;
        tensor[size * size + dstIdx] = image.data[srcIdx + 1] / 255;
        tensor[2 * size * size + dstIdx] = image.data[srcIdx + 2] / 255;
      }
    }
    const feeds: Record<string, unknown> = {
      [session.inputNames[0]]: new ort.Tensor("float32", tensor, [1, 3, size, size]),
      conf_threshold: new ort.Tensor("float32", Float32Array.of(BLAZEFACE_CONF_THRESHOLD), [1]),
      max_detections: new ort.Tensor("int64", BigInt64Array.of(BigInt(BLAZEFACE_MAX_DETECTIONS)), [1]),
      iou_threshold: new ort.Tensor("float32", Float32Array.of(BLAZEFACE_IOU_THRESHOLD), [1]),
    };
    const outputs = await session.run(feeds);
    const boxes = outputs[session.outputNames[0]];
    const boxData = boxes.data as Float32Array;
    // N from the data length, not from dims: this export's output
    // shape is dynamic and collapses when N=1 ([1,16] instead of
    // [1,1,16]). data.length is always N*16.
    const n = Math.floor(boxData.length / 16);
    const out: FaceDetection[] = [];
    for (let i = 0; i < n; i++) {
      const o = i * 16;
      const topY = boxData[o + 0];
      const topX = boxData[o + 1];
      const botY = boxData[o + 2];
      const botX = boxData[o + 3];
      const w = botX - topX;
      const h = botY - topY;
      if (w < 0.01 || h < 0.01) continue;
      const landmarks: { x: number; y: number }[] = [];
      for (let k = 0; k < 6; k++) {
        landmarks.push({ x: boxData[o + 4 + k * 2], y: boxData[o + 5 + k * 2] });
      }
      out.push({
        box: { x: topX, y: topY, w, h },
        // This export returns no scores; the model's internal
        // conf_threshold (0.5) already filtered the detections, so
        // the confidence is reported as that threshold — a lower
        // bound, not a measurement (the old code reported 1, which
        // overstated it).
        confidence: BLAZEFACE_CONF_THRESHOLD,
        landmarks,
      });
    }
    return out;
  }

  return {
    detect,
    async dispose(): Promise<void> {
      const s = session as unknown as { release?: () => Promise<void> };
      if (typeof s.release === "function") await s.release();
    },
  };
}
