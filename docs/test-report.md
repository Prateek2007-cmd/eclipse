# Test report

Generated from real runs on 2026-11-21. Every number below was
produced by a command in this repo, on the dates shown. No
number here comes from a deck or from memory.

## Gate (latest full run, 2026-11-21 ~15:1x IST)

| Suite | Result |
|---|---|
| Python unit + integration (`unittest discover -s tests`) | 235/235 |
| Extension Node tests (`npm test`) | 62/62 |
| Fixture harness (`run.py --runs 5`) | 60/60 |
| Extension typecheck (`npm run typecheck`) | clean |
| Extension build (`npm run build`) | clean |

## SIH benchmark (run 2026-11-21 ~14:19 IST, `results/benchmark-20261121-084924.json`)

| Metric | Measured | Target |
|---|---|---|
| Visual grounding, first try | 100% (18/18 executable steps; 2 human-only handoffs) | ≥85% |
| PII recall (regex types) | 100% (14 TP, 0 FP, 0 FN) | ≥95% |
| PII precision (regex types) | 100% | ≥90% |
| Redaction, recoverable tokens | 0 | 0 |
| Extension models on disk | 26.7 MB | ≤300 MB |
| Latency p50 | 0.001s (mock planner — loop overhead only) | ≤6s/step |

Honesty note (also in the benchmark JSON, score.py output, the
proof API and the workspace): grounding and latency were
measured with a **mock planner**. They measure the loop, not a
live model. A live gateway run is still needed for a real
planner grounding number and real step latency (honest
expectation 4–8s/step on free cloud models).

PII scope: the headline recall/precision cover **regex-detectable
types** (email, card, Aadhaar, PAN, phone, date). Label/field
types (name, address, roll, CVV, OTP, password) and vision types
(faces, scanned-doc text) are detected by other means and
reported separately — they are not in the headline denominator.

## What the tests cover

- **Redactor + gate** (Sprint 2): PII patterns, label-prefix
  rules, overlap resolution (card vs Aadhaar), span-based
  redaction, second PII scan, fail-closed gate, egress allow-list.
- **Planner + executor** (Sprint 3): action validation, grounding
  (element must be in the observation), step limits, retry rules,
  verify-by-re-observing with a baseline diff, handoff/resume.
- **Job hunt** (Sprint 4): matcher (fixture ground truth 5/12),
  already-applied gate, receipts, approval-before-submit.
- **Vault + mail + checkout** (Sprint 5): Fernet vault, per-task
  grants, fill binding (placeholder↔element, origin-scoped),
  approval-before-fill, draft/send gates, partial-unique dedup.
- **RAG + proof** (Sprint 6): embeddings, cosine ≥0.6 threshold,
  PII refusal, forget, outbound log (types only), egress audit.
- **Connectors** (Sprint 7): Gmail/Calendar/Drive via the platform
  CLI, approval gates, idempotency, bounce blocking, error kinds.
- **Monitoring** (Sprint 8): watch conditions, baseline, cooldown,
  3-miss break + recovery, mute expiry, repair, briefing cap.

## What is NOT verified (do not claim otherwise)

- The planner has never run live against the real gateway from
  the server (needs `ANTSEED_API_KEY` in the server env). Live
  grounding + live step latency are unmeasured.
- Florence-2 + onnxruntime-web WebGPU have not run in a real
  Chrome. YOLO's classic `[1,84,8400]` branch is synthetic-only.
- A live-site run (Internshala/LinkedIn job hunt, real checkout)
  has not been done. A real Gmail send through Eclipse has not
  been performed (needs a user-approved draft + the platform's
  send card).
- The extension WebCrypto vault is built and unit-tested but not
  wired into the fill path.
- The workspace (Sprint 9) is built and served, and was NOT
  screenshot-reviewed in a real browser — the cloud browser
  cannot reach this VM's localhost and outbound tunnels are
  blocked by the sandbox network policy. The Playwright
  browser install (needs approval) is the path to it. Its 3D
  scene renders only where WebGL is available, with a static
  fallback otherwise.
