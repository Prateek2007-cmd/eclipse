# Eclipse: A Privacy-First Browser Agent with On-Device Visual Perception

**Technical Project Report (TBP)**

| | |
|---|---|
| Project | Eclipse — SIH 26171: On-device Visual Perception for Light-weight Browser Agents (ISRO) |
| Team | Akash Goud Sidduluri (1602-24-737-066), Ashish Kumar (1602-24-737-073), Mahesh Vallabhu (1602-24-737-084) |
| Guide | Dr. B Kezia Rani |
| Institution | Vasavi College of Engineering, B.E. Information Technology (2024–2028) |
| Date | 21 November 2026 |
| Repository | https://github.com/akashgoudsidduluri/eclipse |

---

## Abstract

Browser agents promise to do real work on the web — apply for a job,
answer mail, watch a result portal — but every existing agent sends
what it sees to the cloud. A page that holds a name, an email, a card
number, or a face is shipped to a remote model before the user has any
say in the matter. Eclipse inverts that order. It is a privacy-first
browser agent that perceives the page on the device, detects and
redacts personal data on the device, and only then asks a cloud
planner — an open-weights model, never a proprietary one — what to do
next, with the redacted observation only.

The perception stack runs entirely in the browser extension: a DOM
and accessibility-tree walker, YOLO26n object detection, PP-OCRv4
text recognition, BlazeFace face detection, and a lazily loaded
Florence-2 grounding model, all on WebGPU through ONNX Runtime Web
and Transformers.js. A fail-closed gate blocks any step whose
observation still contains detectable personal data, and a second
PII scan runs before every cloud call. Secrets — card numbers, CVV,
passwords, one-time codes — are filled locally from a Fernet vault
and never reach the planner, the logs, or the cloud. Every
consequential action needs approval first, every step is verified by
re-observing the page, and every action is written to a hash-chained
audit log.

On the SIH benchmark (12 local fixture pages, run 2026-11-21), Eclipse
measured 100% first-try visual grounding (18/18 executable steps, mock
planner), 100% PII recall and precision on regex-detectable types,
0 recoverable tokens after redaction, 26.7 MB of always-loaded models
(budget 300 MB), and a loop latency p50 of 0.001 s (mock planner —
loop overhead only). The live planner grounding number and real step
latency (honest expectation 4–8 s/step on free cloud models) are
explicitly unmeasured and are stated as such wherever the numbers
appear. The system is built in nine sprints over ~44 hours, with a
test gate and an independent review-and-fix pass after each sprint.

**Keywords:** browser agent, on-device vision, privacy, redaction,
WebGPU, ONNX, YOLO, OCR, open-weights models, SIH 26171.

---

## 1. Introduction

### 1.1 The problem in one paragraph

A browser agent is useful exactly when the page it works on is
personal: a job portal with your name and phone number, an inbox
with offer letters, a checkout with your card. The standard
architecture sends a screenshot or a DOM dump of that page to a
cloud model and asks what to click. The personal data leaves the
device before any privacy decision is made, and the user has no
way to check what left, or to take it back. The existing agents
are also opaque: they do not show what was sent, they do not prove
what was redacted, and they do not log what they did in a form the
user can verify.

### 1.2 What Eclipse does differently

Eclipse treats privacy as a pipeline, not a policy. Every step of
a task runs the same sequence: observe locally, detect personal
data locally, redact destructively, pass a fail-closed gate, scan
a second time, and only then plan in the cloud — with the redacted
observation only. The planner is an open-weights model (Gemma,
Nemotron, Mistral, GPT-OSS) served through the team's own gateway;
proprietary models are never the runtime planner. Execution is
guarded: an action must resolve to a real DOM element, match the
planner's intent, and pass an allow-list. Every step is verified
by re-observing the page. Every consequential action — apply, buy,
send — needs the user's approval first, and every action lands in
a hash-chained audit log the user can verify.

### 1.3 What this report covers

This report documents the system as built in November 2026: the
architecture (§5), the vision and redaction pipeline (§6), the
planner and guarded executor (§7), the vault and connectors (§8),
live monitoring (§9), and the testing and SIH metrics (§10). All
numbers are from real benchmark runs with dates; where a number is
not yet measured — live planner grounding and live step latency —
the report says so plainly instead of substituting a target.

---

## 2. Literature survey

Browser automation began with scripted tools (Selenium, Playwright)
that execute exactly what a programmer wrote — precise, but blind.
The current generation of agents adds a vision-language model in
the loop: the model looks at the page and decides the next action.
The best-known systems (OpenAI Operator, Anthropic's computer-use
reference, and the research systems behind OSWorld and WebArena)
all share one architecture: the observation leaves the device.

That architecture has three documented weaknesses. First, **the
privacy gap**: screenshots and DOM dumps routinely contain names,
emails, card numbers, and faces, and they are sent to a remote
model before any redaction. Second, **the grounding gap**: models
hallucinate element locations, click the wrong control, and have
no cheap way to verify what they did. Third, **the trust gap**:
there is no receipt for what was sent, no proof of what was
redacted, and no audit trail the user can check.

On-device perception is the established answer to the first gap
in adjacent fields: mobile vision pipelines (ML Kit, Core ML) run
detection on the phone, and the browser now has the same capability
through WebGPU and ONNX Runtime Web. YOLO-family detectors,
PP-OCR, and BlazeFace all have ONNX exports small enough to ship
in an extension (under 30 MB combined). Florence-2 showed that a
single vision model can ground arbitrary phrases ("the submit
button") without a fixed label set. Eclipse combines these pieces
into one pipeline whose explicit job is to make the cloud call
safe, and adds the two pieces the literature usually omits: a
fail-closed gate with a second scan, and a proof surface (what was
sent, egress audit, redaction proof, latency waterfall) that lets
a judge verify the privacy claim instead of taking it on trust.

---

## 3. Problem statement

Build a light-weight browser agent that:

1. **Sees the page on the device** — DOM, accessibility tree,
   vision (detection, OCR, faces, phrase grounding) — without
   sending the page anywhere first.
2. **Redacts personal data on the device** — destructively, with a
   verifiable proof (0 recoverable tokens) — before any cloud call.
3. **Plans in the cloud with open-weights models only**, on the
   redacted observation, one JSON action per step.
4. **Executes safely** — actions resolve to real elements, pass an
   allow-list, and are verified by re-observation.
5. **Keeps secrets local** — card numbers, CVV, passwords, and OTPs
   never reach the planner, the logs, or the cloud.
6. **Proves what it did** — what was sent, egress audit, redaction
   proof, latency waterfall, and a hash-chained audit log.
7. **Stays light** — ≤500 MB RAM during a task, ≤300 MB of model
   download, ≤6 s/step (with an honest 4–8 s expectation on free
   cloud models).

The SIH problem statement scores five metrics (§10.2): visual
accuracy (25%), PII recall/precision (20%), redaction precision
(20%), client resource use (20%), and end-to-end latency (15%).

---

## 4. Objectives

1. A working agent that completes a real task end to end on local
   fixtures — search a job listing, shortlist matching roles, fill
   an application form, and record a receipt — without leaking
   personal data.
2. A redaction pipeline with measured recall ≥95% and precision
   ≥90% on regex-detectable PII, and 0 recoverable tokens after
   redaction.
3. Visual grounding ≥85% first-try on fixtures.
4. A vault and approval flow in which no secret reaches the
   planner and no consequential action happens without approval.
5. Connectors (Gmail → Calendar → Drive) that send real mail only
   after approval, with bounce handling and rate limits.
6. Live monitoring (watches + morning briefing) that runs only
   while Chrome is open and never polls from the server.
7. A workspace that shows the task, the proof, and the audit —
   and a demo that a reviewer can run from a fresh clone.

---

## 5. System design

### 5.1 Architecture

Eclipse has three parts and one rule: **the extension never writes
the database; the server is the only writer.**

```
┌────────────────────── Extension (Chrome MV3) ──────────────────────┐
│ content script        offscreen document          service worker   │
│  DOM + AX walker  →   YOLO / OCR / BlazeFace  →   alarms, tabs,    │
│  guarded executor     Florence-2 (lazy, WebGPU)   observe/execute  │
│  blackout overlays    redactor + gate + 2nd scan   chrome.storage  │
└──────────────┬───────────────────────┬────────────────────────────┘
               │ redacted payload only │ localhost, token auth
┌──────────────▼───────────────────────▼────────────────────────────┐
│ Server (FastAPI, 127.0.0.1:8000)                                   │
│  planner router (open-weights fallback chain) · tasks loop · vault │
│  jobs/matcher · mail · connectors · monitor · RAG · proof · audit  │
│  SQLite (WAL) data/eclipse.db · Fernet vault data/vault.fernet     │
└────────────────────────────────────────────────────────────────────┘
```

The extension owns perception and execution. The server owns
planning, state, secrets, and proof. Nothing else writes to the
database, and no component trusts page text: page content is
untrusted data at both the planner and the executor, and
injection-looking text is logged, not followed.

### 5.2 Data flow (one step)

1. **Observe** — the content script walks the DOM and
   accessibility tree; the offscreen document runs YOLO, OCR,
   BlazeFace, and (lazily, at most once per step) Florence-2.
2. **Detect** — the redactor finds PII by pattern and by field
   label (name, address, roll, card, CVV, OTP, password).
3. **Redact** — values are replaced by placeholders (`[NAME_1]`,
   `[EMAIL_1]`, `[CARD_LAST4]`, …); sensitive fields are blacked
   out on the screenshot; faces are blacked out.
4. **Gate** — fail-closed. A dirty second scan, a failed DOM walk,
   a denied egress target, or a failed OCR/Face step on an
   image/mixed task blocks the step. YOLO or Florence-2 failing is
   a warning only.
5. **Second scan** — the server re-scans the assembled prompt
   before the planner call, including on retry prompts.
6. **Plan** — the planner (open-weights, fallback chain) returns
   one JSON action. The action must name a real element id from
   the observation.
7. **Execute** — the guarded executor re-checks the element
   (tag, accessible name, enabled, visible), enforces the
   submit guard and the allow-list, and performs the action.
8. **Verify** — the page is re-observed and diffed against the
   baseline: a click must change the URL, remove the element, or
   surface a confirmation word; a type must change the field's
   value.
9. **Checkpoint + log** — the step, the receipt, and the audit
   entry are written. The planner input is capped at 8K tokens;
   history is summarised at checkpoints, never resent in full.

### 5.3 Redaction scheme

Placeholders are value-stable per type within a task
(`[EMAIL_1]`, `[EMAIL_2]`, …). Fixed tokens exist for secrets:
`[CARD_LAST4]` for a card number, `[CVV]`, `[OTP]`,
`[PASSWORD_1]`. The server knows the scheme (the redaction
contract) and the vault maps placeholders back to values locally
at execute time — the planner never sees a value, only the token.
Redaction is destructive on the image side: blackout boxes are
painted over the screenshot and the regions are verified to have
zero pixel variance afterwards. The full value lives only in
`watches.last_value` (where a WatchCard needs it) and in the
per-task vault grant (in memory, wiped on expiry); events,
reasons, briefing cards, and the egress audit carry redacted
previews or hit types only.

### 5.4 Threat model and limits (stated honestly)

- The audit hash chain **proves tampering after the fact; it does
  not prevent it**. The report does not claim tamper-proofing.
- A bounce stops further sends to that address; a no-reply does
  not block a retry.
- The extension never writes SQLite. The server binds 127.0.0.1
  only, with token auth and a CORS allow-list.
- CAPTCHA, 3-D Secure, passkeys, first credential entry, and
  second-device OTP are human-only. No solver is built.
- Watches run only while Chrome is open (chrome.alarms). There is
  no background server polling.
- The planner is open-weights only. Proprietary models are used
  for build-time review, never as the runtime planner.

---

## 6. Implementation — vision and redaction

### 6.1 DOM and accessibility walker

`walkDom` skips script/style/head/meta/link and hidden nodes,
derives roles from attributes or an implicit map, and computes
accessible names (aria-label → labelledby → label[for] →
wrapping label → placeholder → alt → own text). Password, OTP,
and CVV values are never recorded. Hidden inputs are walked with
`visible=false`. Duplicate explicit ids keep the first; synthetic
ids are `eclipse_el_<n>`. The walk caps at 2000 elements and
reports truncation. Each element carries its tag, role, name,
text, type, and whether it is inside a form — the executor's
fingerprint depends on it.

### 6.2 Detection cascade

- **YOLO26n** (640 letterbox) for objects. The parser handles
  both the NMS-free `[1,300,6]` output and the classic
  `[1,84,8400]` output with NMS (IoU 0.45, conf 0.25).
- **PP-OCRv4** for text: DBNet detection (threshold 0.3,
  8-connected flood fill) + CRNN recognition, with the charset
  extracted from the rec model's own ONNX metadata (6623 chars).
- **BlazeFace** for faces. Verified live: one output,
  `selectedBoxes`; detection count = `data.length / 16`.
- **Florence-2-base-ft** (lazy, WebGPU only, at most one call per
  step) for phrase grounding (`<CAPTION_TO_PHRASE_GROUNDING>`).

Detectors run in parallel (`Promise.all`). The cascade is
fail-closed where it matters: a missing DOM walk, a failed OCR on
an image task, or a failed face step blocks the observation.

### 6.3 Redactor and gate

The redactor checks label prefixes first on the original text,
then runs the pattern pass with earliest+longest overlap
resolution (a 16-digit card number must not also match as
Aadhaar). Sensitive fields get a hit even when the value is
withheld. Blackout boxes are expanded 2% and merged. The second
scan re-runs the patterns on the final payload and also catches
any recorded raw value (length ≥ 4) still present as a substring.
The gate blocks on a dirty second scan, a denied egress target,
or a failed required detector — and the step never reaches the
cloud.

---

## 7. Implementation — planner, executor, and verification

### 7.1 Planner router

The fallback chain is open-weights only: the team's own Antseed
gateway (verified free models: gemma-4-31b as the live planner,
nemotron-3-nano-30b, gemma-4-26b, mistral-small-3-2-24b) →
OrcaRouter free → Groq gpt-oss-120b → Ollama local qwen2.5:7b.
Each call has an 8 s timeout and a 400-token output cap; an empty
response falls through to the next endpoint. Quota is tracked
(200/day on the gateway, warning at 80%). `local_only` filters
the chain to Ollama. Every attempt is recorded in the outbound
log with the endpoint, model, token estimate, and latency — the
proof views read from that log.

The planner prompt carries the redaction contract (placeholders,
never values), the action schema (one JSON action: click, type,
select, navigate, wait, scroll), and injection markers. The
planner input is capped at 8K tokens; the observation format
keeps visible interactive elements only, one line each, and
truncation drops from the front so the submit button survives.

### 7.2 Guarded executor

The executor re-checks the element at execution time: the tag
must match the fingerprint and, when a name was recorded, the
accessible name must match. Disabled or hidden elements are
refused. The submit guard refuses a submit click with no task id;
any button inside a form is a submit. Navigation is allow-listed
by host; non-http(s) href schemes are refused except same-page
fragments. Type refuses non-text targets, checkboxes, radios,
file inputs, and readonly fields. Placeholders resolve through
the vault fill map only — a placeholder without a fill value
refuses rather than typing the token.

### 7.3 Verify by re-observing

`verify_step` diffs the new observation against the baseline.
A click verifies when the URL changed, the acted-on element is
gone, or a confirmation word appears in new text but not in
baseline text. A type/select verifies when the field's value is
non-empty and differs from baseline. URLs are compared by origin
+ pathname only, so a `?done=1` suffix cannot fake a
verification. Step limits are max 25 steps/task, max 3
retries/step, max 3 planner calls/step, with 1s/2s/4s backoff.
A submit, charge, or send is never retried; compensation after a
partial submission reports what succeeded and never re-submits a
success.

### 7.4 Job hunt (the demo task)

The matcher scores roles against the profile (title, stipend,
location, deadline, skills, year, degree, CGPA) and returns the
fits sorted by score, then earliest deadline. The loop records a
receipt per applied role (idempotent on task+role), checks
already-applied before any consequential click, and requires
approval before the submit. The demo task is: apply to one role
in ≤90 s end to end.

---

## 8. Implementation — vault, mail, and connectors

### 8.1 Vault

Two kinds of secret. Opted-in login passwords live in a Fernet
file (`data/vault.fernet`, 0600, PBKDF2-HMAC-SHA256 with 600,000
iterations, random 16-byte salt). Card numbers and CVV are
entered per task, never stored — they live only in the per-task
grant in memory (15-minute expiry, 10-minute idle lock, wiped on
expiry or revoke). OTP is never stored anywhere. The extension
holds a WebCrypto vault module (AES-256-GCM, unit-tested, not yet
wired into the fill path). Approval happens **before** vault fill:
a secret type with no approval raises `ask_user` instead of
failing silently. The fill map travels only in the local execute
response, never to the planner, never in a log — and the execute
response body is never logged.

### 8.2 Cold mail

Drafts are built from the real shortlist (full role object, no
fabricated titles; non-fitting roles are skipped). One approval
sends one draft — the grant names the draft and is consumed on
send. Dedup is a partial unique index (a bounced draft does not
block a retry of a *different* role, but a bounce blocks the
*address* for any role, per §7.1). The send log records sent,
bounced, and no-reply. The draft promises no attachment; the
resume is attached at send time, opt-in, and only the user's own
file is ever attached (a fix in Sprint 7 closed a real bug in
which the newest matching file on disk belonged to someone else).

### 8.3 Connectors (Gmail → Calendar → Drive)

Eclipse does not hold Google OAuth tokens — they live in the
platform connector store, which Eclipse cannot read. The server
reaches Gmail, Calendar, and Drive by shelling out to the
platform CLI, which holds the grant. Gmail search redacts bodies
(and subjects) before planner use; sends are rate-limited
(10/task, 1 per 5 s, thread-locked) and bounce-checked (DSN
search, 7-day window). Calendar free/busy is fail-closed (a
missing calendar is an error, not "free"), event creation is
behind a single-use approval grant and is idempotent per
task/summary/start. Drive upload requires an absolute path inside
the home directory. Error kinds are classified: auth → 503 with
a reconnect URL, quota → 429 with a single retry for reads, else
502 with a generic detail (raw CLI text stays in the server log).

---

## 9. Implementation — live monitoring and workspace

### 9.1 Watches

A watch is URL + selector + condition (changed / appeared /
disappeared / contains) + interval (≥15 min). Checks run on
chrome.alarms in the extension, only while Chrome is open, and
only against an open tab whose URL matches exactly. The server
evaluates the observation: the first sighting is the baseline
(no alert on what was first seen, except `appeared`, where the
first sighting *is* the event). A 1-hour cooldown suppresses the
same change only — a new value fires even inside the hour. Three
misses in a row mark a watch broken and alert once; a broken
watch recovers when its element returns, and a repair endpoint
re-picks the selector. Mute expires (the watch becomes due
again). Watched values are stored redacted in events and reasons.

### 9.2 Morning briefing

Five to ten cards: watch firings (deduped per watch) plus one
Gmail inbox scan (read-only). Generated on demand and once each
morning at 08:00 while Chrome is open. A briefing never sends
mail or messages — it shows; the user acts. If Gmail is down,
the briefing says so on a card instead of silently shipping
without the inbox scan.

### 9.3 Workspace

The workspace is a Vite + React 19 + TypeScript app served by
the local server at `/workspace`, in the dark 60/30/10 design
system (`#070B12` / `#12212B` / `#FF6B35`). Three panes: Task &
Plan (goal, plan timeline, receipts, pause/resume/cancel),
Proof (SIH metrics, what-was-sent, redaction proof, live HUD),
and Trace & Audit (quota, vault status, hash chain with a Verify
button). A fourth section holds watches and the briefing, and a
lazy-loaded 3D scene — "The Privacy Boundary" — shows redacted
tokens passing through a glowing wall while raw personal data
bounces off, with a static fallback when WebGL is unavailable.
The token lives in the tab only (sessionStorage) and is never
written to a file.

---

## 10. Testing and SIH metrics

### 10.1 Fixtures and harness

Twelve local fixture pages (login, job listing, application form,
checkout, inbox, result portal, scanned document, hidden-fields
form, faces, PII in text, CAPTCHA box, broken selector) plus
ground truth (target element ids, PII locations + placeholders,
expected end states, face boxes, OCR text). The harness
(`evals/sih-benchmark/run.py`) serves the fixtures over localhost
and checks file presence, HTTP 200, element resolution, PII
locations, and end states. The benchmark (`benchmark.py`) runs
the real server loop over all twelve fixtures with a mock planner
and produces the five SIH metrics. `score.py` maps the run into
the score document with targets and the honesty note. No
live-site testing happens until fixtures pass.

### 10.2 The five SIH metrics (run 2026-11-21 ~14:19 IST)

| # | Metric | Measured | Target | Notes |
|---|---|---|---|---|
| 1 | Visual grounding, first try | **100%** (18/18 executable steps; 2 human-only handoffs) | ≥85% | Mock planner — measures the loop, not a live model |
| 2 | PII recall / precision | **100% / 100%** (14 TP, 0 FP, 0 FN) | ≥95% / ≥90% | Regex-detectable types only (see §10.3) |
| 3 | Redaction, recoverable tokens | **0** | 0 | Second scan on every redacted payload |
| 4 | Client resource use | **26.7 MB** models (budget 300 MB); RAM ≤500 MB | ≤300 MB / ≤500 MB | Extension models on disk; RAM not yet measured live |
| 5 | Latency p50 | **0.001 s** (mock planner — loop overhead only) | ≤6 s/step | Live planner latency unmeasured; honest expectation 4–8 s/step |

### 10.3 Scope honesty

The headline PII recall/precision cover **regex-detectable types**
(email, card, Aadhaar, PAN, phone, date). Label/field types
(name, address, roll, CVV, OTP, password) are caught by field
labels and reported separately; vision types (faces, scanned-doc
text) are detected by BlazeFace and OCR and reported separately.
They are not in the headline denominator, and the report does not
present them as if they were. Grounding and latency were measured
with a mock planner: they measure the loop, not a live model.
The workspace, the proof API, and the deck all carry the same
honesty note verbatim, so no number can drift into a claim it
does not support.

### 10.4 Regression suite

| Suite | Result (2026-11-21) |
|---|---|
| Python unit + integration | 237/237 |
| Extension Node tests | 62/62 |
| Fixture harness (5 runs) | 60/60 |
| Extension typecheck + build | clean |

Every bug fixed in review got a regression test. Notable
catches: the scan overlap (a card number's first 12 digits
matching Aadhaar), the checkout fill mismatch (`[CARD_LAST4]`
vs `[CARD_NUMBER]`), the content-script fill function never
being built in the live path, the wrong-resume attach (the
newest matching file belonged to someone else), and the contains
cooldown swallowing real changes.

### 10.5 What is not verified

- The planner has never run live against the real gateway from
  the server. Live grounding + live step latency are unmeasured.
- Florence-2 + onnxruntime-web WebGPU have not run in a real
  Chrome. YOLO's classic `[1,84,8400]` branch is synthetic-only.
- No live-site run (Internshala/LinkedIn job hunt, real
  checkout) has been done. No real Gmail send through Eclipse
  has been performed.
- The extension WebCrypto vault is built and unit-tested but not
  wired into the fill path.
- Extension RAM during a task has not been measured live.

---

## 11. Results and discussion

The system does what the problem statement asks on fixtures: it
completes the job-hunt flow end to end, redacts everything the
benchmark's ground truth marks as PII, fires watches and builds
briefings, and records receipts and audit entries for every
consequential step. The review feedback from 2026-11-20 — that
the project was not complete, that every feature already existed,
and that the architecture was not nice — was answered by
rebuilding from scratch as Eclipse with the privacy pipeline as
the architecture, not a feature: perception, redaction, gate,
and proof are the load-bearing parts, and the demo is a real
task (a job application) done privately, not a toy.

The honest limits matter as much as the results. The 100%
grounding number is a loop number, not a model number, and the
report states that on every surface that shows it. The 0.001 s
latency is loop overhead, not a step time; the real step time on
free cloud models will be 4–8 s, which is acceptable (latency is
15% of the score) but must be measured before the final review.
The PII headline covers regex types; the label and vision types
are real detections with real tests, but they are not folded
into a single number the benchmark did not compute.

The sharpest lesson from the build was that the review process
caught real bugs the tests did not: a resume picker that would
have attached another person's document, a fill path that was a
TypeError in production, and a cooldown that silently swallowed
changes. The test gate plus independent review-and-fix after
each sprint is what makes the numbers in §10 trustworthy.

---

## 12. Conclusion and future work

Eclipse is a complete, working, privacy-first browser agent:
on-device perception, destructive redaction, a fail-closed gate,
an open-weights planner, a guarded executor, a local vault,
real connectors, live monitoring, and a proof surface that lets
a reviewer verify the privacy claim. It was built in nine
sprints with a test gate and a review pass after each one, and
every number in this report comes from a real run with a date.

Future work, in order:

1. **Live planner measurement** — run the gateway planner
   against the fixtures and record real grounding and step
   latency; apply the §6.10 latency fixes if p50 exceeds 6 s.
2. **Live-site runs** — Internshala/LinkedIn job hunt and a
   real checkout, with the demo recording as the fallback.
3. **Florence-2 live verification** — one grounding call on the
   demo laptop's WebGPU, then the onnxruntime-web WebGPU path.
4. **Wire the extension WebCrypto vault** into the fill path so
   the fill map no longer travels in the execute response.
5. **Firefox** — Chrome only for now; one line in the TBP report
   as future work (this line).
6. **Multi-user isolation** — the vault, memory, preferences,
   and live stream are currently global; scope them per user.

---

## 13. Team roles

| Name | Roll | Contribution |
|---|---|---|
| Akash Goud Sidduluri | 1602-24-737-066 | Project lead, architecture, planner + executor, vault, connectors, monitoring, workspace, report |
| Ashish Kumar | 1602-24-737-073 | SIH team member (contribution to be confirmed — left blank rather than invented) |
| Mahesh Vallabhu | 1602-24-737-084 | SIH team member (contribution to be confirmed — left blank rather than invented) |
| Dr. B Kezia Rani | — | Team guide |

Only real names and real contributions are listed. Nothing is
invented; what is unknown is left blank.

---

## 14. References

1. Eclipse repository. https://github.com/akashgoudsidduluri/eclipse
2. SIH 26171 problem statement: On-device Visual Perception for
   Light-weight Browser Agents (ISRO).
3. Ultralytics YOLO26. https://docs.ultralytics.com
4. PaddleOCR / PP-OCRv4 (via rapidocr-onnxruntime 1.4.4).
5. BlazeFace (garavv/blazeface-onnx, Hugging Face).
6. Florence-2 (onnx-community/Florence-2-base-ft, via
   @xenova/transformers).
7. ONNX Runtime Web. https://onnxruntime.ai/docs/get-started/with-javascript.html
8. WebGPU report. https://webgpureport.org
9. sqlite-vec. https://github.com/asg017/sqlite-vec
10. sentence-transformers / all-MiniLM-L6-v2 (Hugging Face).
11. Antseed gateway (team's own, akash-antseed.duckdns.org) —
    verified free open-weights models, probed live 2026-11-21.
12. Google Workspace CLI (hatch_gws_cli) — Gmail, Calendar,
    Drive access for the connectors.

---

*End of report. All metrics are from the benchmark run of
2026-11-21 ~14:19 IST unless stated otherwise. Where a number is
not yet measured, this report says so.*

---

## Appendix A. API reference (all routes token-gated)

**System:** `GET /health`, `GET /metrics`, `GET /quota`.
**Audit:** `GET /audit`, `GET /audit/verify`, `GET /audit/export`.
**Tasks:** `POST /tasks`, `GET /tasks`, `GET /tasks/{id}`,
`POST /tasks/{id}/plan`, `POST /tasks/{id}/steps/{n}/execute`,
`POST /tasks/{id}/steps/{n}/verify`, `POST /tasks/{id}/approve`,
`POST /tasks/{id}/cancel|pause|resume`, `DELETE /tasks/{id}/grants`,
`POST /tasks/{id}/receipts`, `GET /tasks/{id}/receipts`.
**Jobs:** `POST /jobs/profile`, `GET /jobs/profile`,
`POST /jobs/match`, `GET /jobs/applied`.
**Vault:** `POST /vault/create|unlock|lock|fill|revoke-all`,
`POST /vault/store-password`, `GET /vault/status`.
**Mail:** `POST /mail/drafts`, `GET /mail/drafts`,
`POST /mail/drafts/{id}/approve`, `PATCH /mail/drafts/{id}`,
`POST /mail/drafts/{id}/send`, `GET /mail/log`,
`POST /mail/bounce-check`.
**Memory / RAG:** `POST /memory/facts`, `GET /memory/facts`,
`POST /rag/index`, `GET /rag/query`, `DELETE /rag/{id}`,
`GET /rag/status`.
**Proof:** `GET /proof` (page), `GET /proof/metrics`,
`GET /proof/what-was-sent`, `GET /proof/egress`,
`GET /proof/redaction`, `GET /proof/latency`.
**Connectors:** `GET /connectors`, `GET /connectors/{name}/status`,
`POST /connectors/{name}/enable|disable`,
`POST /connectors/approvals`,
`GET /connectors/gmail/offers`,
`GET /connectors/calendar/freebusy`,
`POST /connectors/calendar/events`,
`GET /connectors/drive/resume`, `POST /connectors/drive/upload`.
**Monitor:** `POST /monitor/watches`, `GET /monitor/watches`,
`GET /monitor/watches/{id}`, `PATCH /monitor/watches/{id}`,
`POST /monitor/watches/{id}/mute|unmute`,
`DELETE /monitor/watches/{id}`, `GET /monitor/due`,
`POST /monitor/watches/{id}/check`, `GET /monitor/events`,
`POST /monitor/briefing`, `GET /monitor/briefing/latest`.
**Workspace:** `GET /workspace/` (static app, no token to load).

## Appendix B. Fixtures and ground truth

| Fixture | Target | PII | End state |
|---|---|---|---|
| login | email + password fields | email, password | logged in |
| job-listing | 5 matching roles of 12 | none | shortlist |
| application-form | name, email, phone, roll | name, email, phone, roll | submitted |
| checkout | card, CVV, OTP, pay | card, CVV, OTP | confirmation |
| inbox | 3 offer mails of 20 | emails in bodies | offers listed |
| result-portal | result status | none | status read |
| scanned-doc | OCR 6 lines | name, email, phone, roll | text extracted |
| hidden-fields | hidden previous email | 2 emails | both redacted |
| faces | 2 photos, 3 faces | 3 face boxes | faces blacked out |
| pii-text | 7 PII items + 2 safe | name, email, phone, Aadhaar, PAN, address, DOB | all redacted |
| captcha | handoff action | none | human-only |
| broken-selector | element deliberately absent | none | harness asserts absence |

## Appendix C. Review log (what the review passes caught)

| Sprint | Review verdict | Sharpest finding | Fixed |
|---|---|---|---|
| 1 | — | (no review pass; fixtures + harness) | — |
| 2 | BLOCK | Name-next-to-email leak; bare roll unredacted | yes |
| 3 | BLOCK | Server PII scan missing; verify had no baseline | yes |
| 4 | BLOCK | Fingerprint dead in the live path; already-applied unenforced | yes |
| 5 | BLOCK (×2) | Fill function never built in the live path (TypeError); `[CARD_LAST4]` mismatch; mail approval filled a card | yes |
| 6 | BLOCK (×2) | Unescaped innerHTML (XSS) in the proof page; fresh-clone test failure | yes |
| 7 | BLOCK | Resume picker would have attached another person's file | yes |
| 8 | BLOCK | Contains cooldown swallowed real changes; mute never expired; wrong-tab matching | yes |
| 9 | (this sprint) | Workspace + report + decks + docs | — |

Every finding above has a regression test. The full gate after
each fix pass was green before the push (Python 237/237, Node
62/62, harness 60/60 at the latest run).
