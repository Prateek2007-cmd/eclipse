# Eclipse extension (Chrome MV3)

Sprint 2: vision + redaction + gate. The side panel, task UI and
orchestration land in Sprints 3 and 6.

## Layout

- `manifest.json` — MV3 manifest, localhost server + own gateway hosts.
- `offscreen.html` + `src/offscreen.ts` — the offscreen document that
  runs on-device inference (WebGPU).
- `src/content.ts` — content script: DOM/AX walker, blackout overlays.
- `src/background.ts` — service worker: offscreen lifecycle, messages.
- `src/vision/` — walker, YOLO, OCR, BlazeFace, Florence-2 (lazy),
  redactor, fail-closed gate, second PII scan, egress allow-list.
- `models/` — downloaded model files, gitignored (see `models/README.md`).

## Build

```bash
npm install
npm run build        # esbuild -> dist/
npm run typecheck    # tsc --noEmit
npm test             # node --test tests/*.test.mjs
```

Load unpacked in Chrome from this directory (Sprint 3 wires the
service worker to the server).
