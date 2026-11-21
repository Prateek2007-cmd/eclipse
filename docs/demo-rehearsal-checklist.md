# Demo rehearsal checklist

Run this the day before the review, on the demo laptop, with the
real gateway key in the server environment. Never debug live.

## The day before

- [ ] Fresh clone of `akashgoudsidduluri/eclipse` into a clean
      directory. Follow the README (≤10 steps). It must work.
- [ ] `python3 scripts/download_models.py` — all sha256s verify.
- [ ] `python3 scripts/download_rag_model.py` — 3 files present.
- [ ] `python3 evals/sih-benchmark/generate_images.py` (fixtures)
      — the 3 PNGs are not in git; regenerate after cloning.
- [ ] Full gate: Python tests, Node tests, harness 60/60,
      `npm run typecheck`, `npm run build`. All green.
- [ ] Benchmark run: `python3 evals/sih-benchmark/benchmark.py`
      then `score.py --latest`. Record the numbers + run date.
      These are the only numbers in the deck.
- [ ] WebGPU: open webgpureport.org on the demo laptop. Confirm
      the adapter is high-performance, not fallback.
- [ ] Florence-2: load the extension, run one grounding call on
      the login fixture. Confirm it answers on WebGPU.
- [ ] Planner live run: start the server with `ANTSEED_API_KEY`
      set, run the job-hunt task on the fixtures. Record the real
      step latency (expect 4–8s/step on free cloud models).
- [ ] One live-site run (Internshala job listing) — or confirm
      the fixture recording is ready as the fallback.

## Recording (do this before the review)

- [ ] Pre-record every live beat: the job-hunt flow, the
      raw-vs-redacted proof view, the watch firing, the briefing.
- [ ] Each recording is ≤90s, captioned, no real PII on screen
      (fixtures only).
- [ ] Recordings live in `docs/demo-recordings/` with a README
      naming what each clip shows.

## The review itself

- [ ] Laptop charged, charger packed. Chrome + Brave closed to
      the demo tabs only.
- [ ] Server running on localhost:8000, token copied to the
      workspace tab. Extension loaded unpacked, models present.
- [ ] Phone hotspot as backup network (the gateway is remote).
- [ ] Rule: one retry live, then play the recording and say so.
      Never debug live. Never claim a recording is live.
- [ ] CAPTCHA / 3-D Secure / passkey / OTP: human-only. Say so
      on stage when the handoff card appears.

## Safety lines (say these out loud)

- "Raw PII never leaves this device. The redaction happens
  on-device, before the cloud sees anything."
- "The audit hash chain proves tampering after the fact. It
  does not prevent it."
- "These latency numbers are from a benchmark run on
  <date>, with a mock planner. The live planner number is
  <X>s/step."
- "If this step fails, I will play the recording and say so."
