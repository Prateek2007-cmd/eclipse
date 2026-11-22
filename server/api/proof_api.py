"""Proof views API (Sprint 6, plan §5.6).

- GET /proof/metrics — the 5 SIH metrics from the latest benchmark
  run, with run date, fixture count, worst page, worst PII type.
  No numbers from a deck: when no benchmark has run, the metrics
  are null with the reason.
- GET /proof/what-was-sent — every outbound planner payload,
  redacted, with timestamp, endpoint, token count. Searchable by
  endpoint (?q=). Copy copies redacted only — the stored text IS
  the redacted text.
- GET /proof/egress — live grep of outbound requests for secrets,
  with the grep pattern listed. A hit stops the run.
- GET /proof/redaction — redaction proof from the latest benchmark
  (second PII scan on every redacted payload = 0 recoverable
  tokens).
- GET /proof/latency — latency waterfall from the latest benchmark
  (per-step timings, worst step highlighted).
- GET /proof — the proof page itself (static HTML in web/).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse

from server.core.auth import require_token
from server.proof import egress_audit, entries

router = APIRouter(prefix="/proof", tags=["proof"])

BENCHMARK_DIR = Path(__file__).resolve().parents[2] / "evals" / "sih-benchmark" / "results"
WEB_DIR = Path(__file__).resolve().parents[2] / "web"

# The outbound log lives in this server process only (in-memory,
# cap 200 — see server/proof). Planner calls made by other
# processes, e.g. the benchmark harness, are not in it: a 0 here
# means "no call recorded by this server since it started", not
# "no call was ever made".
OUTBOUND_SCOPE = (
    "Calls recorded by this server process since it started "
    "(in-memory log, cap 200). Planner calls made by other "
    "processes — e.g. the benchmark harness — are not included; "
    "a live benchmark run records its own calls in its results file."
)


def _latest_benchmark() -> dict | None:
    files = sorted(
        BENCHMARK_DIR.glob("benchmark-*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        return None
    with open(files[-1], encoding="utf-8") as f:
        return json.load(f)


@router.get("", include_in_schema=False)
def proof_page():
    """The proof page (static HTML).

    Intentionally public: the page holds no data — every data
    endpoint it calls is token-gated. (Review M6, Sprint 9.)"""
    page = WEB_DIR / "proof.html"
    if not page.exists():
        return {"error": "proof page not built yet"}
    return FileResponse(page, media_type="text/html")


@router.get("/metrics")
def metrics(_: str = Depends(require_token)):
    """The 5 SIH metrics from the latest benchmark run."""
    bench = _latest_benchmark()
    if bench is None:
        # Sprint 9 fix (review M10): always return the full key
        # set so clients can read the fields they type as
        # required — null, not missing.
        return {
            "metrics": None,
            "targets": {
                "visual_grounding_first_try": 0.85,
                "pii_recall": 0.95,
                "pii_precision": 0.90,
                "redaction_recoverable_tokens": 0,
                "resource_mb": 300,
                "latency_p50_s": 6,
            },
            "honesty": "",
            "run_date": None,
            "generated_at": None,
            "fixtures": None,
            "worst_page": None,
            "worst_pii_fixture": None,
            "reason": "no benchmark run yet — run evals/sih-benchmark/benchmark.py",
        }
    detail = bench.get("detail", {})
    pii = detail.get("pii", {})
    loop_detail = detail.get("loop", {})
    return {
        "generated_at": bench.get("generated_at"),
        "fixtures": bench.get("fixtures"),
        "elapsed_s": bench.get("elapsed_s"),
        "metrics": bench.get("metrics"),
        "targets": {
            "visual_grounding_first_try": 0.85,
            "pii_recall": 0.95,
            "pii_precision": 0.90,
            "redaction_recoverable_tokens": 0,
            "resource_mb": 300,
            "latency_p50_s": 6,
        },
        "pii_scope": "regex-detectable types",
        "worst_page": loop_detail.get("worst_fixture"),
        "worst_page_s": loop_detail.get("worst_fixture_s"),
        "worst_pii_fixture": pii.get("worst_fixture"),
        "worst_pii_recall": pii.get("worst_recall"),
        "honesty": bench.get("honesty"),
    }


@router.get("/what-was-sent")
def what_was_sent(
    q: str = Query(default="", description="filter by endpoint or model substring"),
    limit: int = Query(default=50, ge=1, le=200),
    _: str = Depends(require_token),
):
    """Every outbound planner payload, redacted."""
    rows = entries(limit)
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in r["endpoint"].lower() or ql in r["model"].lower()]
    return {"count": len(rows), "entries": rows, "scope": OUTBOUND_SCOPE}


@router.get("/egress")
def egress(_: str = Depends(require_token)):
    """Live grep of outbound requests for secrets."""
    audit = egress_audit()
    return {
        "passed": audit["passed"],
        "hit_count": audit["hit_count"],
        "hits": audit["hits"],
        "checked": audit["checked"],
        "patterns": audit["patterns"],
        "scope": OUTBOUND_SCOPE,
        "note": (
            "A hit means a secret left the device. Stop the run and "
            "block the payload."
            if audit["hits"]
            else (
                "0 calls checked — no planner call has been recorded "
                "by this server since it started."
                if audit["checked"] == 0
                else "0 hits — no secret has left the device in the checked calls."
            )
        ),
    }


@router.get("/redaction")
def redaction(_: str = Depends(require_token)):
    """Redaction proof from the latest benchmark run."""
    bench = _latest_benchmark()
    if bench is None:
        # Sprint 9 fix (review M10): full key set, nulls not missing.
        return {
            "generated_at": None,
            "recoverable_tokens": None,
            "passed": None,
            "pii_recall": None,
            "pii_precision": None,
            "per_fixture": None,
            "label_based_not_regex": {},
            "reason": "no benchmark run yet — run evals/sih-benchmark/benchmark.py",
        }
    detail = bench.get("detail", {})
    red = detail.get("redaction", {})
    pii = detail.get("pii", {})
    recoverable = red.get("recoverable_tokens")
    # The note must state the measured number, not a hardcoded 0:
    # the old text claimed "found 0 recoverable tokens" even when
    # the run had found some (passed: false).
    if recoverable is None:
        note = "No redaction data in the latest benchmark run."
    elif recoverable == 0:
        note = (
            "The second PII scan on every redacted payload found 0 "
            "recoverable tokens."
        )
    else:
        note = (
            f"The second PII scan on every redacted payload found "
            f"{recoverable} recoverable tokens — redaction FAILED "
            f"its target of 0."
        )
    note += (
        " Names, addresses, CVV, OTP and passwords are caught by "
        "label/field, not by regex — they are listed under "
        "label_based_not_regex."
    )
    return {
        "generated_at": bench.get("generated_at"),
        "recoverable_tokens": recoverable,
        "passed": red.get("passed"),
        "pii_recall": pii.get("recall"),
        "pii_precision": pii.get("precision"),
        "per_fixture": red.get("per_fixture"),
        "label_based_not_regex": {
            k: v.get("label_based_not_regex", [])
            for k, v in pii.get("per_fixture", {}).items()
            if v.get("label_based_not_regex")
        },
        "note": note,
    }


@router.get("/latency")
def latency(_: str = Depends(require_token)):
    """Latency waterfall from the latest benchmark run."""
    bench = _latest_benchmark()
    if bench is None:
        return {
            "generated_at": None,
            "step_time_s": None,
            "latency_target_s": None,
            "p50_within_target": None,
            "worst_fixture": None,
            "worst_fixture_s": None,
            "per_fixture": None,
            "planner": None,
            "reason": "no benchmark run yet — run evals/sih-benchmark/benchmark.py",
        }
    loop_detail = bench.get("detail", {}).get("loop", {})
    planner = str(loop_detail.get("planner", ""))
    # The note must match the planner that actually ran: the old
    # text always claimed mock timings, which mislabels a live run.
    if planner.startswith("live"):
        note = (
            "Timings are real step latency from a live gateway run "
            "(planner: " + planner + ")."
        )
    else:
        note = (
            "Timings are loop overhead with a mock planner. A live "
            "gateway run is still needed for real step latency "
            "(honest expectation 4-8s/step on free cloud models)."
        )
    return {
        "generated_at": bench.get("generated_at"),
        "step_time_s": loop_detail.get("step_time_s"),
        "latency_target_s": loop_detail.get("latency_target_s"),
        "p50_within_target": loop_detail.get("p50_within_target"),
        "worst_fixture": loop_detail.get("worst_fixture"),
        "worst_fixture_s": loop_detail.get("worst_fixture_s"),
        "per_fixture": loop_detail.get("per_fixture"),
        "planner": loop_detail.get("planner"),
        "note": note,
    }
