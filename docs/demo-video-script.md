# Demo video script (2–3 minutes)

The job-hunt flow end to end + the Diff proof view.
Fixtures only — no real PII on screen. Pre-record every beat;
the live demo follows the same script with the recording as the
fallback (one retry live, then play the recording and say so).

## 0:00–0:15 — The problem (voiceover, title card)

> "Every browser agent sends what it sees to the cloud. Your
> name, your email, your card — gone before anyone asks. Eclipse
> does it the other way round."

Shot: title card, tagline. Cut to the workspace.

## 0:15–0:45 — The task (screen capture, workspace)

> "Find SDE internships in Hyderabad."

Shot: the goal typed into the task box. The plan appears — one
row per step. The agent walks the job listing (12 roles) and
shortlists 5 matches, each with a reason: stipend, location,
deadline, skills. No warm-up, the real flow.

## 0:45–1:20 — The privacy proof (screen capture, Proof pane)

> "Here is what the agent saw — and here is what the cloud got."

Shot: the Diff view — each redacted payload with its placeholder
tokens highlighted (`[NAME_1]`, `[EMAIL_1]`, `[CARD_LAST4]`), and
the list of PII types removed. The egress audit shows 0 hits with
the grep pattern listed. The redaction proof shows 0 recoverable
tokens. There is no raw side to show: raw PII is redacted
on-device and never stored, so the raw text does not exist
anywhere to display.

## 1:20–1:50 — The application + approval (screen capture)

> "Apply to one role. The agent fills the form locally — the card
> never leaves the device — and waits for approval."

Shot: the form filling, the ApprovalCard (what will happen,
destination, "no charge", card last 4), Approve tapped, receipt
recorded with confirmation text.

## 1:50–2:20 — Watching (screen capture, watches + briefing)

> "And it keeps watching while you are away."

Shot: the result portal watch firing (Not selected → Selected),
then the morning briefing with 10 cards. Note on screen: watches
run only while Chrome is open.

## 2:20–2:40 — The numbers (title cards)

- Grounding 100% (18/18, mock planner) · PII 100%/100% ·
  0 recoverable tokens · 26.7 MB models · gate 235/235, 62/62, 60/60.
- Honesty card: "Grounding and latency measured with a mock
  planner. Live planner latency unmeasured; expectation 4–8 s/step."

## 2:40–3:00 — Close (voiceover, Privacy Boundary scene)

> "The browser agent that sees for you — and eclipses what no
> one else should see."

Shot: the 3D Privacy Boundary — green dots through the wall, red
dots bouncing off. End card: repo URL.

## Shot list (recordings, `docs/demo-recordings/`)

1. `job-hunt.mp4` — 0:15–1:50, the full flow.
2. `proof-view.mp4` — 0:45–1:20, the split screen + egress audit.
3. `watch-firing.mp4` — 1:50–2:20, watch + briefing.
4. `boundary-scene.mp4` — 2:40–3:00, the 3D scene.

Each clip ≤90 s, captioned, fixtures only.
