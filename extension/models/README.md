# Extension models — downloaded, never committed

Model files live in `extension/models/` and are **gitignored**. They are
fetched on first use (or by `scripts/download_models.py`, which pins
their sha256s) from their official sources.

Budget (plan §6.7): the always-loaded models total ≤300 MB — about
26 MB on disk. Florence-2 is **excluded** from that budget: at
265–442 MB it is loaded lazily, WebGPU only, and disposed after use
(extension RAM ≤500 MB, see `unloadFlorence`).

| Model | File | Size | Source / licence |
|---|---|---|---|
| YOLO26n | `yolo26n.onnx` | ~9.9 MB | Ultralytics (AGPL-3.0 — SIH prototype use, licence stated) |
| PP-OCRv4 det | `ch_PP-OCRv4_det_infer.onnx` | ~4.7 MB | PaddleOCR (Apache-2.0) via RapidOCR |
| PP-OCRv4 rec | `ch_PP-OCRv4_rec_infer.onnx` | ~10.9 MB | PaddleOCR (Apache-2.0) via RapidOCR |
| PP-OCR cls | `ch_ppocr_mobile_v2.0_cls_infer.onnx` | ~0.6 MB | PaddleOCR (Apache-2.0) via RapidOCR |
| BlazeFace | `blazeface.onnx` | ~0.5 MB | MediaPipe (Apache-2.0), community ONNX export |
| Florence-2-base INT8 | `florence2/` (4 graphs) | ~265–442 MB | Microsoft (MIT), onnx-community — lazy, WebGPU only |

Florence-2 is loaded only when DOM + YOLO + OCR cannot answer, and
never on the WASM/CPU path in the demo. If WebGPU is unavailable, the
agent falls back to DOM + YOLO + OCR and says so.
