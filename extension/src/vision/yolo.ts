/** YOLO nano detector — the box finder (Sprint 2).
 *
 * Pick: YOLO26n (~5 MB ONNX, NMS-free, 40.9 mAP COCO). Out of the box
 * YOLO knows COCO classes, not UI classes — its value here is as a
 * fast interactable-region detector; DOM is primary for web elements.
 * Licence: Ultralytics YOLO is AGPL-3.0 — fine for an SIH prototype,
 * stated in models/README.md, not hidden.
 *
 * The `ort` namespace is injected so the same code runs on
 * onnxruntime-web (browser offscreen document, WebGPU) and
 * onnxruntime-node (tests). Handles both ONNX output shapes:
 * NMS-free [1, 300, 6] and classic [1, 84, 8400] + NMS.
 */
import type { Detection } from "./types.js";

export const YOLO_INPUT_SIZE = 640;
export const YOLO_CONF_THRESHOLD = 0.25;
export const YOLO_IOU_THRESHOLD = 0.45;

export const COCO_NAMES = [
  "person","bicycle","car","motorcycle","airplane","bus","train","truck","boat",
  "traffic light","fire hydrant","stop sign","parking meter","bench","bird","cat",
  "dog","horse","sheep","cow","elephant","bear","zebra","giraffe","backpack",
  "umbrella","handbag","tie","suitcase","frisbee","skis","snowboard","sports ball",
  "kite","baseball bat","baseball glove","skateboard","surfboard","tennis racket",
  "bottle","wine glass","cup","fork","knife","spoon","bowl","banana","apple",
  "sandwich","orange","broccoli","carrot","hot dog","pizza","donut","cake","chair",
  "couch","potted plant","bed","dining table","toilet","tv","laptop","mouse",
  "remote","keyboard","cell phone","microwave","oven","toaster","sink","refrigerator",
  "book","clock","vase","scissors","teddy bear","hair drier","toothbrush",
];

/** Minimal ort surface this module needs (web + node share it). */
export interface OrtLike {
  InferenceSession: {
    create(path: string, options?: Record<string, unknown>): Promise<{
      inputNames: string[];
      outputNames: string[];
      run(feeds: Record<string, unknown>): Promise<Record<string, { data: Float32Array | number[]; dims: number[] }>>;
    }>;
  };
  Tensor: new (
    type: string,
    data: Float32Array | BigInt64Array,
    dims: number[],
  ) => { data: Float32Array | BigInt64Array; dims: number[] };
}

export interface RgbaImage {
  data: Uint8Array | Uint8ClampedArray;
  width: number;
  height: number;
}

/** Letterbox-resize to 640x640, normalise to 0..1, NCHW float32. */
export function preprocess(image: RgbaImage, size = YOLO_INPUT_SIZE): {
  tensor: Float32Array;
  scale: number;
  padX: number;
  padY: number;
} {
  const { width, height } = image;
  const scale = Math.min(size / width, size / height);
  const newW = Math.round(width * scale);
  const newH = Math.round(height * scale);
  const padX = Math.floor((size - newW) / 2);
  const padY = Math.floor((size - newH) / 2);
  const tensor = new Float32Array(3 * size * size);
  for (let y = 0; y < newH; y++) {
    for (let x = 0; x < newW; x++) {
      const srcX = Math.min(width - 1, Math.floor(x / scale));
      const srcY = Math.min(height - 1, Math.floor(y / scale));
      const srcIdx = (srcY * width + srcX) * 4;
      const dstIdx = (y + padY) * size + (x + padX);
      tensor[dstIdx] = image.data[srcIdx] / 255;
      tensor[size * size + dstIdx] = image.data[srcIdx + 1] / 255;
      tensor[2 * size * size + dstIdx] = image.data[srcIdx + 2] / 255;
    }
  }
  return { tensor, scale, padX, padY };
}

function iou(a: Detection, b: Detection): number {
  const x1 = Math.max(a.box.x, b.box.x);
  const y1 = Math.max(a.box.y, b.box.y);
  const x2 = Math.min(a.box.x + a.box.w, b.box.x + b.box.w);
  const y2 = Math.min(a.box.y + a.box.h, b.box.y + b.box.h);
  const inter = Math.max(0, x2 - x1) * Math.max(0, y2 - y1);
  const union = a.box.w * a.box.h + b.box.w * b.box.h - inter;
  return union <= 0 ? 0 : inter / union;
}

function nms(detections: Detection[], iouThreshold = YOLO_IOU_THRESHOLD): Detection[] {
  const sorted = [...detections].sort((a, b) => b.confidence - a.confidence);
  const kept: Detection[] = [];
  for (const d of sorted) {
    if (kept.every((k) => k.label !== d.label || iou(k, d) < iouThreshold)) {
      kept.push(d);
    }
  }
  return kept;
}

function toRelative(
  cx: number, cy: number, w: number, h: number,
  scale: number, padX: number, padY: number, size: number,
  srcW: number, srcH: number,
): Detection["box"] {
  // Model coords are in the 640 letterboxed frame; undo the letterbox.
  const x = ((cx - w / 2 - padX) / scale) / srcW;
  const y = ((cy - h / 2 - padY) / scale) / srcH;
  return {
    x: Math.max(0, x),
    y: Math.max(0, y),
    w: Math.min(1 - Math.max(0, x), (w / scale) / srcW),
    h: Math.min(1 - Math.max(0, y), (h / scale) / srcH),
  };
}

/** Parse raw YOLO output into detections. `dims` decides the layout. */
export function parseYoloOutput(
  data: Float32Array | number[],
  dims: number[],
  image: RgbaImage,
  scale: number,
  padX: number,
  padY: number,
): Detection[] {
  const out: Detection[] = [];
  const size = YOLO_INPUT_SIZE;
  if (dims.length === 3 && dims[1] === 300 && dims[2] === 6) {
    // NMS-free end-to-end: [x1, y1, x2, y2, score, class] in 640 frame.
    for (let i = 0; i < 300; i++) {
      const o = i * 6;
      const score = data[o + 4];
      if (score < YOLO_CONF_THRESHOLD) continue;
      const cls = Math.round(data[o + 5]);
      const w = data[o + 2] - data[o + 0];
      const h = data[o + 3] - data[o + 1];
      const cx = data[o + 0] + w / 2;
      const cy = data[o + 1] + h / 2;
      out.push({
        box: toRelative(cx, cy, w, h, scale, padX, padY, size, image.width, image.height),
        label: COCO_NAMES[cls] ?? `class_${cls}`,
        confidence: score,
        source: "yolo",
      });
    }
    return out;
  }
  if (dims.length === 3 && dims[2] === 8400) {
    // Classic: [1, 84, 8400] — 4 box + 80 class scores per anchor.
    const numClasses = dims[1] - 4;
    for (let i = 0; i < 8400; i++) {
      const cx = data[0 * 8400 + i];
      const cy = data[1 * 8400 + i];
      const w = data[2 * 8400 + i];
      const h = data[3 * 8400 + i];
      let bestCls = -1;
      let bestScore = 0;
      for (let c = 0; c < numClasses; c++) {
        const s = data[(4 + c) * 8400 + i];
        if (s > bestScore) {
          bestScore = s;
          bestCls = c;
        }
      }
      if (bestScore < YOLO_CONF_THRESHOLD) continue;
      out.push({
        box: toRelative(cx, cy, w, h, scale, padX, padY, size, image.width, image.height),
        label: COCO_NAMES[bestCls] ?? `class_${bestCls}`,
        confidence: bestScore,
        source: "yolo",
      });
    }
    return nms(out);
  }
  throw new Error(`Unsupported YOLO output shape: [${dims.join(", ")}]`);
}

export interface YoloSession {
  run(image: RgbaImage): Promise<Detection[]>;
  dispose(): Promise<void>;
}

export async function createYoloSession(
  ort: OrtLike,
  modelPath: string,
  executionProviders: string[] = ["webgpu", "wasm"],
): Promise<YoloSession> {
  const session = await ort.InferenceSession.create(modelPath, { executionProviders });
  return {
    async run(image: RgbaImage): Promise<Detection[]> {
      const { tensor, scale, padX, padY } = preprocess(image);
      const input = new ort.Tensor("float32", tensor, [1, 3, YOLO_INPUT_SIZE, YOLO_INPUT_SIZE]);
      const feeds: Record<string, unknown> = { [session.inputNames[0]]: input };
      const outputs = await session.run(feeds);
      const first = outputs[session.outputNames[0]];
      return parseYoloOutput(first.data as Float32Array, first.dims, image, scale, padX, padY);
    },
    async dispose(): Promise<void> {
      // onnxruntime-web sessions release via `release()` when present.
      const s = session as unknown as { release?: () => Promise<void> };
      if (typeof s.release === "function") await s.release();
    },
  };
}
