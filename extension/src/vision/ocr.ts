/** OCR — PP-OCRv4 detection + PP-OCRv4 recognition, ONNX (Sprint 2).
 *
 * RapidOCR / PP-OCRv4 ONNX in WASM: 300–1,500 ms full-screen,
 * 100–300 ms on a cropped region. The detector (DBNet) finds text
 * boxes; the recogniser (CRNN + CTC) reads them. The classifier
 * (180° rotation) is skipped — screenshots are upright.
 *
 * The `ort` namespace is injected (onnxruntime-web in the browser,
 * onnxruntime-node in tests). The recognition dictionary is the
 * 6623-char list embedded in the rec model's own ONNX metadata
 * (`models/ppocr_rec_chars.txt`, extracted by
 * scripts/download_models.py) — RapidOCR reads the same metadata at
 * runtime, so this order is exactly what the model expects. The
 * smoke test on the scanned form proves it reads real words.
 */
import type { OcrWord } from "./types.js";
import type { OrtLike, RgbaImage } from "./yolo.js";

/** Build the CTC decode list from the model's char file: RapidOCR
 *  inserts "blank" at index 0 and a space at the end. */
export function buildDecodeList(charsText: string): string[] {
  const list = charsText.split("\n");
  if (list.length && list[list.length - 1] === "") list.pop();
  return ["blank", ...list, " "];
}

export const OCR_DET_THRESHOLD = 0.3;
export const OCR_REC_HEIGHT = 48;

/** CTC greedy decode: skip blank (index 0) and repeated chars. */
export function ctcDecode(
  logits: Float32Array | number[],
  timeSteps: number,
  numClasses: number,
  decodeList: string[],
): string {
  let out = "";
  let prev = -1;
  for (let t = 0; t < timeSteps; t++) {
    let best = 0;
    let bestVal = -Infinity;
    for (let c = 0; c < numClasses; c++) {
      const v = logits[t * numClasses + c];
      if (v > bestVal) {
        bestVal = v;
        best = c;
      }
    }
    if (best !== 0 && best !== prev) {
      const ch = decodeList[best];
      if (ch && ch !== "blank") out += ch;
    }
    prev = best;
  }
  return out;
}

function resizeRgbaBilinear(
  image: RgbaImage,
  newW: number,
  newH: number,
): { data: Float32Array; width: number; height: number } {
  // Bilinear, not nearest-neighbour: nearest-neighbour downscaling
  // destroys thin text strokes (9 reads as 8, U as d). Normalised
  // with mean 0.5 / std 0.5 (the rec model's convention; the det
  // model officially uses ImageNet mean/std, but an A/B on the
  // scanned-form fixture gave identical boxes, so one path is kept).
  const out = new Float32Array(3 * newW * newH);
  const xScale = image.width / newW;
  const yScale = image.height / newH;
  for (let y = 0; y < newH; y++) {
    for (let x = 0; x < newW; x++) {
      const srcX = (x + 0.5) * xScale - 0.5;
      const srcY = (y + 0.5) * yScale - 0.5;
      const x0 = Math.max(0, Math.floor(srcX));
      const y0 = Math.max(0, Math.floor(srcY));
      const x1 = Math.min(image.width - 1, x0 + 1);
      const y1 = Math.min(image.height - 1, y0 + 1);
      const fx = Math.max(0, Math.min(1, srcX - x0));
      const fy = Math.max(0, Math.min(1, srcY - y0));
      const dstIdx = y * newW + x;
      for (let c = 0; c < 3; c++) {
        const v00 = image.data[(y0 * image.width + x0) * 4 + c];
        const v10 = image.data[(y0 * image.width + x1) * 4 + c];
        const v01 = image.data[(y1 * image.width + x0) * 4 + c];
        const v11 = image.data[(y1 * image.width + x1) * 4 + c];
        const v =
          v00 * (1 - fx) * (1 - fy) +
          v10 * fx * (1 - fy) +
          v01 * (1 - fx) * fy +
          v11 * fx * fy;
        out[c * newW * newH + dstIdx] = (v / 255 - 0.5) / 0.5;
      }
    }
  }
  return { data: out, width: newW, height: newH };
}

/** Threshold the DBNet probability map and return axis-aligned boxes
 *  (relative coords) via 8-connected flood fill. */
export function detPostProcess(
  probMap: Float32Array | number[],
  mapW: number,
  mapH: number,
  threshold = OCR_DET_THRESHOLD,
): { x: number; y: number; w: number; h: number }[] {
  const visited = new Uint8Array(mapW * mapH);
  const boxes: { x: number; y: number; w: number; h: number }[] = [];
  const stack: number[] = [];
  for (let i = 0; i < mapW * mapH; i++) {
    if (visited[i] || probMap[i] < threshold) continue;
    // New component.
    let minX = mapW;
    let minY = mapH;
    let maxX = -1;
    let maxY = -1;
    let count = 0;
    stack.length = 0;
    stack.push(i);
    visited[i] = 1;
    while (stack.length) {
      const cur = stack.pop()!;
      const cx = cur % mapW;
      const cy = Math.floor(cur / mapW);
      count += 1;
      if (cx < minX) minX = cx;
      if (cx > maxX) maxX = cx;
      if (cy < minY) minY = cy;
      if (cy > maxY) maxY = cy;
      for (let dy = -1; dy <= 1; dy++) {
        for (let dx = -1; dx <= 1; dx++) {
          if (dx === 0 && dy === 0) continue;
          const nx = cx + dx;
          const ny = cy + dy;
          if (nx < 0 || nx >= mapW || ny < 0 || ny >= mapH) continue;
          const ni = ny * mapW + nx;
          if (!visited[ni] && probMap[ni] >= threshold) {
            visited[ni] = 1;
            stack.push(ni);
          }
        }
      }
    }
    // Ignore specks smaller than ~2 map pixels.
    if (count < 2) continue;
    // Expand the box: the DBNet map is 1/4 resolution, so the
    // thresholded component is only the glyph CORE — the box is
    // shorter than the letters (ascenders/descenders are cut). Grow
    // vertically by half the box height on each side (total 2x) and
    // horizontally by 10%, then clamp. RapidOCR's unclip_ratio 1.6
    // does the same job on the polygon; this is the bbox version.
    const w = maxX - minX + 1;
    const h = maxY - minY + 1;
    const growX = Math.max(1, Math.round(w * 0.1));
    const growY = Math.max(2, Math.round(h * 0.5));
    const x0 = Math.max(0, minX - growX);
    const y0 = Math.max(0, minY - growY);
    const x1 = Math.min(mapW - 1, maxX + growX);
    const y1 = Math.min(mapH - 1, maxY + growY);
    boxes.push({
      x: x0 / mapW,
      y: y0 / mapH,
      w: (x1 - x0 + 1) / mapW,
      h: (y1 - y0 + 1) / mapH,
    });
  }
  // Reading order: top to bottom, then left to right.
  boxes.sort((a, b) => a.y - b.y || a.x - b.x);
  return boxes;
}

export interface OcrSession {
  /** Read all text in the image, in reading order. */
  read(image: RgbaImage): Promise<OcrWord[]>;
  dispose(): Promise<void>;
}

/** Merge OCR words that sit on the same text line into one word
 *  (text joined with a space, box = union). Exported for tests. */
export function groupIntoLines(words: OcrWord[]): OcrWord[] {
  if (words.length < 2) return words;
  const sorted = [...words].sort(
    (a, b) => a.box.y + a.box.h / 2 - (b.box.y + b.box.h / 2) || a.box.x - b.box.x,
  );
  const lines: OcrWord[][] = [];
  for (const w of sorted) {
    const cy = w.box.y + w.box.h / 2;
    const line = lines.find((l) => {
      const first = l[0];
      const lcy = first.box.y + first.box.h / 2;
      const lh = Math.max(first.box.h, w.box.h);
      return Math.abs(cy - lcy) <= lh * 0.5;
    });
    if (line) line.push(w);
    else lines.push([w]);
  }
  return lines.map((line) => {
    const ordered = [...line].sort((a, b) => a.box.x - b.box.x);
    const x = Math.min(...ordered.map((w) => w.box.x));
    const y = Math.min(...ordered.map((w) => w.box.y));
    const x2 = Math.max(...ordered.map((w) => w.box.x + w.box.w));
    const y2 = Math.max(...ordered.map((w) => w.box.y + w.box.h));
    const conf =
      ordered.reduce((s, w) => s + w.confidence, 0) / ordered.length;
    return {
      text: ordered.map((w) => w.text).join(" "),
      box: { x, y, w: x2 - x, h: y2 - y },
      confidence: conf,
    };
  });
}

export async function createOcrSession(
  ort: OrtLike,
  detModelPath: string,
  recModelPath: string,
  /** Contents of models/ppocr_rec_chars.txt (the model's own dict). */
  recCharsText: string,
  executionProviders: string[] = ["webgpu", "wasm"],
): Promise<OcrSession> {
  const decodeList = buildDecodeList(recCharsText);
  const det = await ort.InferenceSession.create(detModelPath, { executionProviders });
  const rec = await ort.InferenceSession.create(recModelPath, { executionProviders });

  async function read(image: RgbaImage): Promise<OcrWord[]> {
    // --- Detection: downscale so the long side is at most 960,
    // multiple of 32. PP-OCR only *downscales* to 960 — small images
    // are kept at native resolution (upscaling them wastes time and
    // adds no detail).
    const scale = Math.min(1, 960 / Math.max(image.width, image.height));
    const detW = Math.max(32, Math.round((image.width * scale) / 32) * 32);
    const detH = Math.max(32, Math.round((image.height * scale) / 32) * 32);
    const detInput = resizeRgbaBilinear(image, detW, detH);
    const detTensor = new ort.Tensor("float32", detInput.data, [1, 3, detH, detW]);
    const detOut = await det.run({ [det.inputNames[0]]: detTensor });
    const detFirst = detOut[det.outputNames[0]];
    const mapW = detFirst.dims[3];
    const mapH = detFirst.dims[2];
    const boxes = detPostProcess(detFirst.data as Float32Array, mapW, mapH);

    // --- Recognition: crop each box, resize to height 48, CTC decode. ---
    const words: OcrWord[] = [];
    for (const box of boxes) {
      const x0 = Math.max(0, Math.floor(box.x * image.width));
      const y0 = Math.max(0, Math.floor(box.y * image.height));
      const x1 = Math.min(image.width, Math.ceil((box.x + box.w) * image.width));
      const y1 = Math.min(image.height, Math.ceil((box.y + box.h) * image.height));
      if (x1 - x0 < 4 || y1 - y0 < 4) continue;
      const cropW = x1 - x0;
      const cropH = y1 - y0;
      // Do NOT clamp the width to 320: a long line (ratio 20-30)
      // squeezed to 48x320 is unreadable — the CRNN takes variable
      // width, so grow with the aspect ratio (multiple of 32, max
      // 1536). RapidOCR does the same via max_wh_ratio.
      const recW = Math.min(
        1536,
        Math.max(32, Math.ceil(((cropW / cropH) * OCR_REC_HEIGHT) / 32) * 32),
      );
      // Crop then resize.
      const crop: RgbaImage = {
        data: new Uint8Array(cropW * cropH * 4),
        width: cropW,
        height: cropH,
      };
      for (let y = 0; y < cropH; y++) {
        for (let x = 0; x < cropW; x++) {
          const s = ((y0 + y) * image.width + (x0 + x)) * 4;
          const d = (y * cropW + x) * 4;
          crop.data[d] = image.data[s];
          crop.data[d + 1] = image.data[s + 1];
          crop.data[d + 2] = image.data[s + 2];
          crop.data[d + 3] = 255;
        }
      }
      const recInput = resizeRgbaBilinear(crop, recW, OCR_REC_HEIGHT);
      const recTensor = new ort.Tensor("float32", recInput.data, [1, 3, OCR_REC_HEIGHT, recW]);
      const recOut = await rec.run({ [rec.inputNames[0]]: recTensor });
      const recFirst = recOut[rec.outputNames[0]];
      const timeSteps = recFirst.dims[1];
      const numClasses = recFirst.dims[2];
      const text = ctcDecode(recFirst.data as Float32Array, timeSteps, numClasses, decodeList);
      if (!text.trim()) continue;
      // Confidence: mean of the winning class probs. The rec output
      // is already softmax (tensor name softmax_11.tmp_0; per-step
      // class sums are 1.0), so the winning value IS the probability
      // — do not apply a sigmoid to it (the old code compressed
      // every confidence into ~[0.5, 0.73]).
      let confSum = 0;
      let confN = 0;
      const logits = recFirst.data as Float32Array;
      for (let t = 0; t < timeSteps; t++) {
        let best = 0;
        let bestVal = -Infinity;
        for (let c = 0; c < numClasses; c++) {
          const v = logits[t * numClasses + c];
          if (v > bestVal) {
            bestVal = v;
            best = c;
          }
        }
        if (best !== 0) {
          confSum += bestVal;
          confN += 1;
        }
      }
      words.push({
        text,
        box,
        confidence: confN ? confSum / confN : 0,
      });
    }
    // Group words into lines: the detector returns one box per
    // 8-connected component, and a "Label: value" line can split
    // into two boxes ("Roll:" / "TEST-24-0841") — the label rule in
    // the redactor can only fire on a whole line. Words whose
    // y-centres are within half a line height belong to one line.
    return groupIntoLines(words);
  }

  return {
    read,
    async dispose(): Promise<void> {
      for (const s of [det, rec]) {
        const sess = s as unknown as { release?: () => Promise<void> };
        if (typeof sess.release === "function") await sess.release();
      }
    },
  };
}
