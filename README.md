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
10. Load the extension unpacked in Chrome — Sprint 3, not built yet.

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
| 4 | Job-hunt flow end to end | not started |
| 5 | Vault + cold mail + checkout | not started |
| 6 | RAG + benchmark + proof views | not started |
| 7 | Connectors (Gmail → Calendar → Drive) | not started |
| 8 | Live monitoring | not started |
| 9 | Demo + TBP deliverables | not started |

Grades: implemented-and-verified / implemented-but-not-live-verified /
partial / mocked / not implemented. No number on any slide until
`score.py` produces it from a real benchmark run.

## SIH metrics (targets, not results)

- Visual-context accuracy (25%): ≥85% correct element grounding.
- PII recall/precision (20%): recall ≥95%, precision ≥90%.
- Redaction precision (20%): 0 recoverable tokens after redaction.
- Client resource use (20%): extension RAM ≤500MB, models ≤300MB.
- End-to-end latency (15%): ≤6s per step, demo task ≤90s.

## Rules

- No code until Start Blocks are green. Build in sprint order, do not
  reorder. After each task: test gate, then review-and-fix, then next.
- Never say done until verified. Never fabricate numbers.
- CAPTCHA, 3-D Secure, passkey and second-device OTP are human-only.
  Never build a CAPTCHA solver.
- Approvals per action: sends, purchases, pushes and deletes each need
  their own go-ahead.
- No secrets in chat, files, logs, screenshots, or the repo.

## Docs

- Full plan: `~/workspace/privagent-rebuild-research/11-ECLIPSE-FINALISED-PLAN-SIH-TBP.md`
  (also the source of truth while the repo docs grow).
- `docs/` holds the sqlite-vec runbook, the ONNX + Transformers.js
  runbook, and the demo rehearsal checklist (as sprints land).
