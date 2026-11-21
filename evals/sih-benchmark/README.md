# SIH benchmark — 12 fixtures, 5 metrics

## What it is

A local benchmark for the SIH build, run on 12 fixture pages before any
live site is touched. No live-site testing until fixtures pass.

## Fixtures (fixtures/, ground truth in ground_truth.json)

| # | Fixture | Tests |
|---|---|---|
| 1 | login.html | login form fill + submit |
| 2 | job-listing.html | 12 roles, 5 match — shortlist, do not click the other 7 |
| 3 | application-form.html | form fill + resume upload + submit |
| 4 | checkout.html | card + CVV + OTP, payment approval flow |
| 5 | inbox.html | 20 mails, 3 offers |
| 6 | result-portal.html | watch target: status "Not selected" |
| 7 | scanned-doc.html | scanned PNG, no DOM text — vision only |
| 8 | hidden-fields.html | hidden fields are never user data |
| 9 | faces.html | faces in images, blur before anything leaves |
| 10 | pii-text.html | 7 PII items in plain text |
| 11 | captcha.html | CAPTCHA — human handoff, never solve |
| 12 | broken-selector.html | watched element gone — watch must say "broken" |

All PII in fixtures is synthetic test data (example.com, 99999 numbers,
test card 4111 1111 1111 1111). Never use real PII in a fixture.

## Run

```bash
python3 evals/sih-benchmark/run.py --runs 5
python3 evals/sih-benchmark/score.py --latest
```

## Metrics (targets, not results)

- Visual accuracy (25%): ≥85% correct element grounding.
- PII recall/precision (20%): recall ≥95%, precision ≥90%.
- Redaction precision (20%): 0 recoverable tokens.
- Resource use (20%): RAM ≤500MB, models ≤300MB.
- Latency (15%): ≤6s per step, demo task ≤90s.

Sprint 1: the harness runs structural checks only (files exist, served
over HTTP, ground-truth element ids and PII locations resolve). The 5
SIH metrics are recorded as not measured, with the reason, until the
real loop (Sprints 2-4) runs on the fixtures. No number on any slide
until `score.py` produces it from a real benchmark run.
