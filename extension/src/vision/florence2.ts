/** Florence-2 — lazy grounding model (Sprint 2).
 *
 * Florence-2-base INT8 (230M, ~265–442 MB) is the *slow, expensive*
 * step: it is the only model that does language → box natively
 * ("click the blue submit button" → box). Rules from the research:
 *  - Load lazily, only when DOM + YOLO + OCR cannot answer.
 *  - Call at most once per agent step, never per frame.
 *  - WebGPU only. Never run it on the WASM/CPU path in the demo —
 *    on CPU it is 5–20 s per query. If WebGPU is unavailable, fall
 *    back to DOM + YOLO + OCR and say so.
 *
 * The transformers.js import is dynamic so the ~10 MB library and the
 * model weights are never loaded until the first grounding call.
 */
import type { Box } from "./types.js";

export const FLORENCE_MODEL_ID = "onnx-community/Florence-2-base-ft";
export const FLORENCE_MAX_CALLS_PER_STEP = 1;

export interface FlorenceResult {
  available: boolean;
  reason?: string;
  boxes: { box: Box; label: string; confidence: number }[];
  /** Milliseconds the call took, when measured. */
  ms?: number;
}

interface TransformersLike {
  AutoProcessor: {
    from_pretrained(id: string): Promise<{
      (image: unknown, task: string, options?: Record<string, unknown>): Promise<Record<string, unknown>>;
      batch_decode(outputs: unknown, opts: Record<string, unknown>): string[];
      post_process_generation(
        text: string,
        task: string,
        imageSize: { width: number; height: number },
      ): { boxes?: number[][]; labels?: string[]; scores?: number[] };
    }>;
  };
  Florence2ForConditionalGeneration: {
    from_pretrained(
      id: string,
      opts: Record<string, unknown>,
    ): Promise<{
      generate(inputs: Record<string, unknown>): Promise<unknown>;
      config: Record<string, unknown>;
      dispose?: () => Promise<void>;
    }>;
  };
  RawImage: {
    read(source: string | Blob | Uint8Array): Promise<{ width: number; height: number; data: unknown }>;
  };
}

let cached: {
  processor: Awaited<ReturnType<TransformersLike["AutoProcessor"]["from_pretrained"]>>;
  model: Awaited<
    ReturnType<TransformersLike["Florence2ForConditionalGeneration"]["from_pretrained"]>
  >;
} | null = null;

/** Is WebGPU available in this context? (Proven YES on the demo
 *  laptop, 2026-11-21 — high-performance adapter, AMD RDNA-3.) */
export function hasWebGpu(): boolean {
  const nav = globalThis.navigator as Navigator & { gpu?: unknown };
  return typeof nav?.gpu !== "undefined";
}

async function loadTransformers(): Promise<TransformersLike> {
  // Dynamic import: transformers.js is only pulled in when needed.
  return (await import("@xenova/transformers")) as unknown as TransformersLike;
}

/** Ground a phrase to boxes: "the blue submit button" → boxes.
 *  `imageSource` is an object URL / data URL / Blob of the screenshot.
 *  Returns available:false with a reason when WebGPU or the model is
 *  unavailable — the caller falls back to DOM + YOLO + OCR. */
export async function groundPhrase(
  imageSource: string | Blob | Uint8Array,
  phrase: string,
): Promise<FlorenceResult> {
  const started = Date.now();
  if (!hasWebGpu()) {
    return {
      available: false,
      reason: "WebGPU unavailable — Florence-2 refused (CPU path is 5–20 s/query). Falling back to DOM + YOLO + OCR.",
      boxes: [],
    };
  }
  try {
    const tf = await loadTransformers();
    if (!cached) {
      const processor = await tf.AutoProcessor.from_pretrained(FLORENCE_MODEL_ID);
      const model = await tf.Florence2ForConditionalGeneration.from_pretrained(FLORENCE_MODEL_ID, {
        dtype: "q8",
        device: "webgpu",
      });
      cached = { processor, model };
    }
    const image = await tf.RawImage.read(imageSource as never);
    const task = "<CAPTION_TO_PHRASE_GROUNDING>";
    const inputs = await cached.processor(image, task, { text: phrase });
    const generated = await cached.model.generate({
      ...inputs,
      max_new_tokens: 100,
    });
    const decoded = cached.processor.batch_decode(generated, { skip_special_tokens: false });
    const post = cached.processor.post_process_generation(decoded[0], task, {
      width: image.width,
      height: image.height,
    });
    const boxes = (post.boxes ?? []).map((b, i) => ({
      box: {
        x: b[0] / image.width,
        y: b[1] / image.height,
        w: (b[2] - b[0]) / image.width,
        h: (b[3] - b[1]) / image.height,
      },
      label: (post.labels ?? [])[i] ?? phrase,
      confidence: (post.scores ?? [])[i] ?? 0,
    }));
    return { available: true, boxes, ms: Date.now() - started };
  } catch (err) {
    return {
      available: false,
      reason: `Florence-2 failed: ${err instanceof Error ? err.message : String(err)}`,
      boxes: [],
      ms: Date.now() - started,
    };
  }
}

/** Release the cached model (memory budget: extension RAM ≤500 MB).
 *  Disposing actually frees the ~265 MB+ of weights; merely nulling
 *  the reference (the old code) left them resident. */
export async function unloadFlorence(): Promise<void> {
  if (!cached) return;
  const model = cached.model;
  cached = null;
  if (typeof model.dispose === "function") {
    try {
      await model.dispose();
    } catch {
      // Already released; nothing to do.
    }
  }
}
