# ECLIPSE

**The browser agent that sees for you — and eclipses what no one else should see.**

Privacy-first browser agent for SIH 2026 PS 26171, "On-device Visual
Perception for Light-weight Browser Agents" (ISRO). The same build is the
Theme-Based Project (TBP): SIH is the core prototype, TBP adds connectors,
live monitoring, more flows, and the academic deliverables.

Fresh build, fresh repo. Not PrivAgent.

## What it does

Eclipse finishes real browser tasks on real sites, with privacy first:

1. **Job hunt** — share a resume once, Eclipse searches Internshala,
   LinkedIn and Unstop, shortlists with match reasons, applies after your
   approval, and reports a receipt per role.
2. **Cold mail** — drafts one short personal mail per recruiter, sends
   only after your approval.
3. **Secure checkout and login** — card and login filled locally from the
   vault. Full card, CVV and OTP never leave the device, never reach the
   planner, never enter logs.
4. **Scanned documents** — reads PNG scans with on-device vision only.
   DOM scores zero by design.
5. **Live monitoring** — watches result portals and the inbox, alerts on
   change, morning briefing.

Privacy pipeline, every step, no exceptions: observe locally → detect PII
→ redact (destructive blackout, never blur) → fail-closed GATE → second
PII scan → plan (cloud, redacted only) → guarded execute → verify by
re-observing → checkpoint + log.

## Setup (10 steps or fewer)

1. `git clone https://github.com/akashgoudsidduluri/eclipse.git && cd eclipse`
   (Setup works after the first push lands — until then, work from the
   local checkout.)
2. `python3 -m venv .venv && source .venv/bin/activate`
3. `pip install -r requirements.txt`
4. `.venv/bin/uvicorn server.main:app --host 127.0.0.1 --port 8000`
5. Read the token: `cat data/.eclipse-token` (file is 0600, never logged)
6. Check health: `curl -H "X-Eclipse-Token: <token>" http://127.0.0.1:8000/health`
7. Run the benchmark harness: `python3 evals/sih-benchmark/run.py --runs 1`
8. Score it: `python3 evals/sih-benchmark/score.py --latest`
9. Run the tests: `.venv/bin/python -m unittest discover -s tests -v`
10. Load the extension unpacked in Chrome (`extension/`, `npm run build` first).
11. Open the proof page: `http://127.0.0.1:8000/proof` (paste the token from step 5).

## Architecture — 3 tiers, local first

- **Tier 1, Extension** (Chrome MV3, `extension/`): perceive + redact +
  execute. Offscreen document runs on-device inference (DOM walker, YOLO,
  OCR, BlazeFace, Florence-2 lazy, WebGPU only). Redactor, vault and gate
  run here. IndexedDB holds model weights only.
- **Tier 2, Local server** (`server/`, FastAPI, localhost:8000, bind
  127.0.0.1 only, token auth): orchestrate + guard + vault + memory +
  RAG. One SQLite file (`data/eclipse.db`, WAL). The server is the ONLY
  SQLite writer. Vault file separate, Fernet-encrypted, never in SQLite.
- **Tier 3, Cloud planner** (redacted input only, open-weights only):
  own Antseed gateway (verified free models, gemma-4-31b live planner),
  then OrcaRouter free, then Groq gpt-oss-120b, then Ollama local
  qwen2.5:7b. Never silently fall back from local mode to cloud.

## Status — honest grades

| Sprint | Scope | Grade |
|---|---|---|
| 0 | Start blocks: model probe, connector check, repo, resume, laptop spec, WebGPU | done, verified live 2026-11-21 |
| 1 | Fixtures + harness + server skeleton | done — harness 60/60, unittests 12/12, metrics null (no loop yet) |
| 2 | Vision + redaction + gate | done — Node 22/22, Python 20/20, harness 60/60; OCR 6/6 lines, BlazeFace 2+1 faces (ground truth); Florence-2 implemented but not live-verified (no WebGPU in sandbox) |
| 3 | Planner + executor + verify | done — Python 66/66, Node 39/39, harness 60/60; review BLOCK fixed (server PII scan, verify baseline diff, handoff resume, default-submit guard); planner is implemented and unit-tested with a mock planner but NOT live-verified against the real gateway (needs ANTSEED_API_KEY in the server env) |
| 4 | Job-hunt flow end to end | done — Python 99/99, Node 48/48, harness 60/60; review pass BLOCK (backend 2 blockers + 11 minors, frontend 1 blocker + 6 minors) all fixed + regression tests; matcher reproduces the fixture ground truth (5/12); approval-before-submit + already-applied gate + receipts recorded by the loop; fingerprint now live in the execute path |
| 5 | Vault + cold mail + checkout | done — Python 137/137, Node 52/52, harness 60/60; Fernet vault file (PBKDF2, 0600, create ≠ unlock, 10-min key idle lock), per-task grants (15-min expiry, 10-min idle lock, origin-scoped, card wiped on expiry), fill map in the execute response (approval BEFORE vault fill, placeholder↔element binding, [CARD_LAST4] fills the full number); cold-mail drafts from the real shortlist + approval (one approval sends one draft) + partial unique-index dedup + send log; checkout flow on the fixture (card fill → approve → verify); extension WebCrypto vault module (AES-256-GCM, unit-tested, not yet wired into the fill path); review pass: backend BLOCK + frontend BLOCK, all findings fixed + regression tests |
| 6 | RAG + benchmark + proof views | done — Python 157/157, Node 52/52, harness 60/60; sqlite-vec RAG (all-MiniLM-L6-v2, 384-dim, cosine ≥0.6, PII text refused, forget works); benchmark run 2026-11-21: grounding 100% (mock planner — loop, not a live model), PII recall 100% / precision 100% (regex types), redaction 0 recoverable tokens, extension models 26.7 MB (budget 300), latency p50 0.001s (mock planner — loop overhead only); proof views live at /proof (metrics, what-was-sent, egress audit, redaction proof, latency waterfall) + web/proof.html; scan.py overlap fix (card vs Aadhaar) + ground-truth fix (hidden-fields current email); latency gate passes — no §6.10 fixes needed (live planner latency still unmeasured, honest expectation 4–8s/step) |
| 7 | Connectors (Gmail → Calendar → Drive) | not started |
| 8 | Live monitoring | not started |
| 9 | Demo + TBP deliverables | not started |

Grades: implemented-and-verified / implemented-but-not-live-verified /
partial / mocked / not implemented. No number on any slide until
`score.py` produces it from a real benchmark run.

## SIH metrics — measured 2026-11-21 (benchmark run)

From `evals/sih-benchmark/benchmark.py` on the 12 fixtures
(`results/benchmark-20261121-082839.json`, `score.py --latest`):

- Visual grounding, first try: **100%** (18/18 executable steps; 2 human-only handoffs — OTP + pay — counted separately). Target ≥85%. **Mock planner** — this measures the loop, not a live model.
- PII recall / precision: **100% / 100%** (regex-detectable types only). Target ≥95% / ≥90%. Names, addresses, CVV, OTP and passwords are caught by label/field, not regex — listed separately in the benchmark detail.
- Redaction: **0 recoverable tokens** after redaction. Target 0.
- Resource use: extension models **26.7 MB** (budget 300 MB). Target ≤500 MB RAM / ≤300 MB download.
- Latency p50: **0.001s/step** (mock planner — loop overhead only). Target ≤6s/step. A live gateway run is still needed for real step latency (honest expectation 4–8s/step on free cloud models).

Targets (for reference): visual accuracy ≥85%, PII recall ≥95% / precision ≥90%, redaction 0 tokens, RAM ≤500MB / models ≤300MB, latency ≤6s/step, demo task ≤90s.

## Rules

- No code until Start Blocks are green. Build in sprint order, do not
  reorder. After each task: test gate, then review-and-fix, then next.
- Never say done until verified. Never fabricate numbers.
- CAPTCHA, 3-D Secure, passkey and second-device OTP are human-only.
  Never build a CAPTCHA solver.
- Approvals per action: sends, purchases, pushes and deletes each need
  their own go-ahead. (Standing rule change 2026-11-21 12:39 IST: the
  user superseded this for the build — push and build without asking,
  complete the whole project. The per-action rule still describes the
  product's approval behaviour for sends/purchases, not the build.)
- No secrets in chat, files, logs, screenshots, or the repo.

## Docs

- Full plan: `~/workspace/privagent-rebuild-research/11-ECLIPSE-FINALISED-PLAN-SIH-TBP.md`
  (also the source of truth while the repo docs grow).
- `docs/` holds the sqlite-vec runbook, the ONNX + Transformers.js
  runbook, and the demo rehearsal checklist (as sprints land).
