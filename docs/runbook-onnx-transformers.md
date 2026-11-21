# Runbook: ONNX + Transformers.js (on-device vision)

The extension runs four vision models in the browser with
`onnxruntime-web` (WebGPU when available, WASM fallback) and
Florence-2 with `@xenova/transformers`. No model runs on the
server for vision; the server only embeds text for RAG.

## Models (all pinned, sha256-verified on every download)

| Model | File | Size | Source |
|---|---|---|---|
| YOLO26n (detect) | `yolo26n.onnx` | 9.9 MB | Ultralytics v8.4.0 (AGPL-3.0) |
| PP-OCRv4 det | `ch_PP-OCRv4_det_infer.onnx` | 4.7 MB | rapidocr-onnxruntime 1.4.4 |
| PP-OCRv4 rec | `ch_PP-OCRv4_rec_infer.onnx` | 10.9 MB | rapidocr-onnxruntime 1.4.4 |
| PP-OCR cls | `ch_ppocr_mobile_v2.0_cls_infer.onnx` | 0.6 MB | rapidocr-onnxruntime 1.4.4 (downloaded, not wired) |
| BlazeFace | `blazeface.onnx` | 0.5 MB | HF garavv/blazeface-onnx (Apache-2.0) |
| Florence-2-base-ft | (Transformers.js cache) | ~265–442 MB | onnx-community (lazy, WebGPU only) |

Always-loaded models total **~26.7 MB** (budget 300 MB).
Florence-2 is excluded from that budget and bounded by the
≤500 MB RAM budget instead.

Download / verify: `python3 scripts/download_models.py`
(sha256s in the script; a mismatch fails the run).

## WebGPU check (run this on the demo laptop first)

1. Open https://webgpureport.org in Chrome.
2. Expect: a high-performance adapter, `isFallbackAdapter: false`,
   `requestAdapter` + `requestDevice` successful.
3. On the demo laptop (2026-11-21): AMD RDNA-3, vendor amd,
   features include `timestamp-query`, `shader-f16`, `subgroups`,
   `texture-compression-bc`; maxBufferSize 2 GB. **WebGPU: YES.**

## CPU fallback

- `florence2.ts` refuses the CPU path (`hasWebGpu()` check) —
  Florence-2 runs on WebGPU only. Without WebGPU the cascade is
  DOM + YOLO + OCR only, and the log says so.
- `onnxruntime-web` falls back to WASM automatically when no
  WebGPU adapter is available. YOLO / OCR / BlazeFace still run,
  slower.
- If neither works, the gate fails closed: the observation is
  blocked, not sent unredacted.

## BlazeFace gotcha (verified live 2026-11-21)

The model takes inputs `image` (1,3,128,128) float32 [0..1] +
`conf_threshold` + `max_detections` (int64) + `iou_threshold`
and returns **one** output, `selectedBoxes`. The upstream
README's two-output claim is wrong. Detection count =
`data.length / 16`. Confidence is reported as the threshold
(0.5) — a lower bound, not a measured score.

## OCR gotcha

The rec model has 6625 output classes = 6623 chars + blank +
space. `ppocr_rec_chars.txt` (6623 chars) was extracted from the
rec model's own ONNX metadata — do not substitute another
charset file. The det input is downscaled to long side 960
(downscale only); rec crops are resized to height 48, width a
multiple of 32, max 1536.

## YOLO gotcha

`parseYoloOutput` handles both the NMS-free `[1,300,6]` output
and the classic `[1,84,8400]` output + NMS (IoU 0.45, conf
0.25). The classic branch is synthetic-tensor tested only —
it has not run against a real page.

## What is NOT live-verified

- Florence-2 end to end in a real Chrome (no WebGPU in the
  sandbox; model files not downloaded there).
- The onnxruntime-web WebGPU path in a real Chrome.
- OCR heuristics on dense real pages.

Do not claim these as verified. The demo rehearsal checklist
says how to verify them on the demo laptop before the review.
