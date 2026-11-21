#!/usr/bin/env python3
"""Sprint 6 benchmark: a real loop run over the 12 fixtures that
produces the 5 SIH metrics for real (no more nulls).

What it measures, and what it does NOT:
- Grounding: % of steps where the action the loop planned resolves
  to the ground-truth target element and verifies. The planner here
  is a MOCK that returns the ground-truth action — so this measures
  the loop mechanics (validation, execution, verification), NOT a
  live model's grounding ability. A live planner's first-try
  grounding needs a real gateway run; this file says so in the
  output. Do not quote the grounding number as a model result.
- PII recall/precision: server/planner/scan.py (same patterns as the
  extension's pii.ts) run over each fixture, scored against the
  ground-truth PII locations. Real measurement of the redactor
  patterns.
- Redaction: the second PII scan on the redacted payload — must be
  0 recoverable tokens.
- Resources: real MB on disk for the always-loaded models
  (extension/models) and the server embeddings model.
- Latency: per-step timings of the LOCAL stages (redact, plan with
  a mock planner, execute, verify). The plan time is NOT live
  gateway latency — the mock returns instantly. The honest
  expectation for a live run is 4-8s/step on free cloud models;
  this benchmark measures the loop overhead only.

Pass --live to run the loop with the REAL planner instead of the
mock (fix list item 1): the server's router calls gemma-4-31b on
the user's own Antseed gateway, authenticated with the vault-held
connector credential via the surrogate. Grounding and latency
then ARE live-model measurements. A live run uses ~25 planner
calls against the 200/day free quota.

Usage:
    python3 evals/sih-benchmark/benchmark.py [--out results/benchmark-...json]
    python3 evals/sih-benchmark/benchmark.py --live [--out results/benchmark-live-...json]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent.parent
sys.path.insert(0, str(ROOT))

from server.core import config, db  # noqa: E402
from server.planner import router as planner_router  # noqa: E402
from server.planner import scan  # noqa: E402
from server.tasks import loop  # noqa: E402

RESULTS_DIR = BASE / "results"
FIXTURES_DIR = BASE / "fixtures"
EXTENSION_MODELS = ROOT / "extension" / "models"
RAG_MODELS = ROOT / "server" / "rag" / "models"

FIXTURE_FILES = {
    "login": "login.html",
    "job-listing": "job-listing.html",
    "application-form": "application-form.html",
    "checkout": "checkout.html",
    "inbox": "inbox.html",
    "result-portal": "result-portal.html",
    "scanned-doc": "scanned-doc.html",
    "hidden-fields": "hidden-fields.html",
    "faces": "faces.html",
    "pii-text": "pii-text.html",
    "captcha": "captcha.html",
    "broken-selector": "broken-selector.html",
}


def _load_ground_truth() -> dict:
    with open(BASE / "ground_truth.json", encoding="utf-8") as f:
        return json.load(f)


class _FixtureParser(HTMLParser):
    """Map element id -> {tag, text, value, attrs} for a fixture."""

    def __init__(self) -> None:
        super().__init__()
        self.elements: dict[str, dict] = {}
        self._current: dict | None = None
        self._text_parts: list[str] = []
        self.all_text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        el_id = attrs_d.get("id")
        if el_id:
            self._current = {
                "id": el_id,
                "tag": tag,
                "attrs": attrs_d,
                "value": attrs_d.get("value", ""),
                "text": "",
            }
            self.elements[el_id] = self._current
            self._text_parts = []
        elif tag in ("input", "textarea", "select"):
            self._current = None

    def handle_data(self, data):
        if data.strip():
            self.all_text.append(data.strip())
        if self._current is not None and data.strip():
            self._text_parts.append(data.strip())

    def handle_endtag(self, tag):
        if self._current is not None and tag == self._current["tag"]:
            self._current["text"] = " ".join(self._text_parts)
            self._current = None


def _parse_fixture(name: str) -> _FixtureParser:
    parser = _FixtureParser()
    parser.feed((FIXTURES_DIR / FIXTURE_FILES[name]).read_text(encoding="utf-8"))
    return parser


def _pii_metrics(gt: dict) -> dict:
    """PII recall/precision of scan.py vs the ground truth.

    For each ground-truth PII location, the value is read from the
    fixture element itself (text or value attribute); recall is the
    share of those values the redactor actually detects. Precision
    is the share of detected values that are real PII locations.
    Runtime-value entries (OTP) are skipped — the field is empty
    in the fixture by design.
    """
    per_fixture: dict[str, dict] = {}
    tp = fp = fn = 0
    worst_fixture = None
    worst_recall = 1.0
    for name, fixture_gt in gt["fixtures"].items():
        expected = [e for e in fixture_gt.get("pii", []) if not e.get("runtime_value")]
        if not expected:
            continue
        parsed = _parse_fixture(name)
        full_text = " ".join(parsed.all_text)
        # Also scan the raw values of the elements (a card number
        # lives in a value attribute, not in visible text).
        for el in parsed.elements.values():
            if el["value"]:
                full_text += " " + el["value"]
        clean, hits = scan.redact(full_text)
        # A hit counts for a location when its value is a substring
        # of that element's text/value AND its type matches the
        # ground-truth type. (The element text is e.g. "Email:
        # aarav.test@example.com" — the hit is the bare address.)
        tp_f = fp_f = fn_f = 0
        missed_values: list[str] = []
        label_based_missed: list[str] = []
        for entry in expected:
            if entry["location"].startswith("img:"):
                # Vision-detected (faces / OCR on a scanned image),
                # not text-regex — measured by the vision tests,
                # not by this scan.
                continue
            el = parsed.elements.get(entry["location"])
            if el is None:
                fn_f += 1
                missed_values.append(entry["location"])
                continue
            element_text = (el["value"] or el["text"] or "").strip()
            if not element_text:
                continue
            found = any(
                h.get("type") == entry["type"]
                and h.get("value")
                and h["value"] in element_text
                for h in hits
            )
            if found:
                tp_f += 1
            elif entry["type"] in (
                "name",
                "address",
                "roll",
                "cvv",
                "otp",
                "password",
            ):
                # NOT regex-detectable: names/addresses/rolls are
                # caught by label (extension redactLabelValue);
                # CVV/OTP/password are caught by field type or label.
                # Reported separately, not counted as regex misses —
                # the headline recall covers regex types only.
                label_based_missed.append(entry["location"])
            else:
                fn_f += 1
                missed_values.append(entry["location"])
        # Precision: a hit is a true positive when its value appears
        # inside some expected element's text with the matching type.
        # Image locations (faces / scanned docs) are vision-detected
        # and have no text element — they never count here.
        for h in hits:
            if not h.get("value"):
                continue
            matched = any(
                not e["location"].startswith("img:")
                and h["value"] in ((parsed.elements.get(e["location"]) or {}).get("value") or (parsed.elements.get(e["location"]) or {}).get("text") or "")
                and h.get("type") == e["type"]
                for e in expected
            )
            if not matched:
                fp_f += 1
        tp += tp_f
        fp += fp_f
        fn += fn_f
        recall_f = tp_f / max(1, tp_f + fn_f)
        per_fixture[name] = {
            "expected_regex": tp_f + fn_f,
            "found": tp_f,
            "false_positives": fp_f,
            "missed": fn_f,
            "recall": round(recall_f, 3),
            "missed_values": missed_values,
            "label_based_not_regex": label_based_missed,
        }
        # Only fixtures with at least one regex-expected entry can
        # be "worst": a fixture with no regex PII (faces, scanned-doc
        # — vision-detected) computes 0/max(1,0) = 0.0 and would win
        # the worst slot on an empty denominator.
        if tp_f + fn_f > 0 and recall_f < worst_recall:
            worst_recall = recall_f
            worst_fixture = name
    recall = tp / max(1, tp + fn)
    precision = tp / max(1, tp + fp)
    return {
        "recall": round(recall, 3),
        "precision": round(precision, 3),
        "true_positives": tp,
        "false_positives": fp,
        "false_negatives": fn,
        "per_fixture": per_fixture,
        "worst_fixture": worst_fixture,
        "worst_recall": round(worst_recall, 3),
    }


def _redaction_proof(gt: dict) -> dict:
    """Second PII scan on every redacted payload: 0 tokens."""
    leaked_total = 0
    per_fixture: dict[str, list[str]] = {}
    for name, fixture_gt in gt["fixtures"].items():
        if not fixture_gt.get("pii"):
            continue
        parsed = _parse_fixture(name)
        full_text = " ".join(parsed.all_text)
        for el in parsed.elements.values():
            if el["value"]:
                full_text += " " + el["value"]
        clean, _ = scan.redact(full_text)
        second = scan.scan(clean)
        leaked = [h["value"] for h in second]
        per_fixture[name] = leaked
        leaked_total += len(leaked)
    return {
        "recoverable_tokens": leaked_total,
        "per_fixture": per_fixture,
        "passed": leaked_total == 0,
    }


def _resource_metrics() -> dict:
    """Real MB on disk for the models the loop actually loads."""
    extension_bytes = 0
    extension_files: dict[str, int] = {}
    if EXTENSION_MODELS.exists():
        for f in sorted(EXTENSION_MODELS.iterdir()):
            if f.is_file() and f.suffix == ".onnx":
                extension_bytes += f.stat().st_size
                extension_files[f.name] = f.stat().st_size
    rag_bytes = 0
    rag_files: dict[str, int] = {}
    if RAG_MODELS.exists():
        for f in sorted(RAG_MODELS.iterdir()):
            if f.is_file():
                rag_bytes += f.stat().st_size
                rag_files[f.name] = f.stat().st_size
    return {
        "extension_models_mb": round(extension_bytes / 1_000_000, 1),
        "extension_models": extension_files,
        "server_embeddings_mb": round(rag_bytes / 1_000_000, 1),
        "server_embeddings": rag_files,
        "extension_budget_mb": 300,
        "within_budget": extension_bytes <= 300_000_000,
    }


def _mock_plan_fn(actions: list[dict]):
    """Return a call_fn that hands the loop the ground-truth plan."""
    plan = [{"description": a["expected_end_state"]} for a in actions]

    def call_fn(prompt, local_only=False):
        return json.dumps(plan), "mock:ground-truth", 0

    return call_fn


def _mock_step_fn(actions: list[dict]):
    """Return a call_fn that hands the loop the ground-truth action
    for the step being executed.

    Keyed by the loop's own progress: the prompt's LAST STEP
    RESULTS section lists completed steps ("step N: ..."), so the
    next action is actions[done]. NOT a blind counter — if a step
    ever retries, the same done-count appears twice and the mock
    fails loudly instead of silently returning the next step's
    action and measuring the wrong thing.
    """
    import re

    seen_done: set[int] = set()

    def call_fn(prompt, local_only=False):
        done = 0
        section = prompt.split("LAST STEP RESULTS:")[-1] if "LAST STEP RESULTS:" in prompt else ""
        for m in re.finditer(r"^step (\d+):", section, re.MULTILINE):
            done = max(done, int(m.group(1)))
        if done in seen_done:
            raise AssertionError(
                f"benchmark mock: step {done + 1} is being retried — "
                "the mock cannot re-plan a step (would return the "
                "wrong action)"
            )
        seen_done.add(done)
        if done >= len(actions):
            raise AssertionError(
                f"benchmark mock: {done} steps done but only "
                f"{len(actions)} ground-truth actions exist"
            )
        a = actions[done]
        action = {"action": a["action"], "element_id": a["element_id"]}
        if a["action"] == "type":
            # Type the placeholder the redactor would have issued.
            action["text"] = a.get("value_hint", "[VALUE]")
        return json.dumps(action), "mock:ground-truth", 0

    return call_fn


def _loop_metrics(gt: dict, live: bool = False) -> dict:
    """Run the real server loop over the fixtures and time every
    step. Mock planner by default; --live uses the real planner
    (gemma-4-31b on the user's own gateway) instead."""
    tmp = tempfile.mkdtemp(prefix="eclipse-benchmark-")
    old_data_dir = config.DATA_DIR
    config.DATA_DIR = Path(tmp)
    try:
        return _loop_metrics_inner(gt, live=live)
    finally:
        config.DATA_DIR = old_data_dir


def _loop_metrics_inner(gt: dict, live: bool = False) -> dict:
    db.migrate()

    step_times: list[float] = []
    per_fixture: dict[str, dict] = {}
    grounded = 0
    handoffs = 0
    total = 0
    worst_fixture = None
    worst_time = 0.0
    for name, fixture_gt in gt["fixtures"].items():
        actions = [
            a
            for a in fixture_gt.get("target_actions", [])
            if a["action"] in ("click", "type", "select")
        ]
        if not actions:
            continue
        url = f"http://localhost/{FIXTURE_FILES[name]}"
        elements = [
            {
                "id": a["element_id"],
                "tag": "button" if a["action"] == "click" else "input",
                "role": "button" if a["action"] == "click" else "textbox",
                "name": a["element_id"],
                "text": a.get("expected_end_state", ""),
                "value": "",
                "visible": True,
                "interactable": True,
            }
            for a in actions
        ]
        observation = {"url": url, "elements": elements}
        conn = db.connect()
        task_id = loop.create_task(conn, f"benchmark {name}", "")
        if live:
            # Fix list item 1: the real planner. call_planner matches
            # the call_fn(prompt, local_only=False) -> (raw, model, ms)
            # shape the loop expects.
            plan_fn = planner_router.call_planner
            step_fn = planner_router.call_planner
        else:
            plan_fn = _mock_plan_fn(actions)
            step_fn = _mock_step_fn(actions)
        try:
            plan = loop.plan_task(conn, task_id, call_fn=plan_fn)
        except Exception as exc:  # noqa: BLE001 - benchmark reports it
            per_fixture[name] = {"error": f"plan failed: {exc}"}
            conn.close()
            continue
        steps_ok = 0
        step_errors: dict[str, str] = {}
        t0 = time.perf_counter()
        for i, action in enumerate(actions, start=1):
            t_step = time.perf_counter()
            try:
                result = loop.execute_step(
                    conn, task_id, i, observation, call_fn=step_fn
                )
                # Build the post-action observation: a type fills the
                # field, a click navigates away — the clicked element
                # is gone from the new page (verify_step strips query
                # and fragment from URLs, so a ?done=1 suffix alone
                # does NOT verify a click).
                new_elements = [dict(e) for e in elements]
                if action["action"] == "type":
                    for e in new_elements:
                        if e["id"] == action["element_id"]:
                            e["value"] = "[VALUE]"
                else:
                    new_elements = [
                        e for e in new_elements if e["id"] != action["element_id"]
                    ]
                new_observation = {
                    "url": url,
                    "elements": new_elements,
                }
                verify = loop.verify_step(
                    conn,
                    task_id,
                    i,
                    new_observation,
                    result.get("expected_end_state", ""),
                    previous_url=url,
                    previous_observation=observation,
                )
                step_times.append(time.perf_counter() - t_step)
                total += 1
                if verify.get("verified"):
                    grounded += 1
                    steps_ok += 1
            except Exception as exc:  # noqa: BLE001 - benchmark reports it
                step_times.append(time.perf_counter() - t_step)
                total += 1
                msg = str(exc)
                if "human-only" in msg:
                    # The loop correctly refused: OTP / passkey /
                    # 3-D Secure / pay steps are human-only by
                    # design. Counted as a correct handoff, not a
                    # grounding failure.
                    handoffs += 1
                    step_errors[f"step_{i}"] = f"handoff (correct): {msg}"
                else:
                    step_errors[f"step_{i}"] = msg
        elapsed = time.perf_counter() - t0
        conn.close()
        per_fixture[name] = {
            "steps": len(actions),
            "verified": steps_ok,
            "elapsed_s": round(elapsed, 3),
            "errors": step_errors,
        }
        if elapsed > worst_time:
            worst_time = elapsed
            worst_fixture = name
    p50 = statistics.median(step_times) if step_times else 0.0
    executable = max(1, total - handoffs)
    return {
        "grounding_first_try": round(grounded / executable, 3),
        "grounded_steps": grounded,
        "total_steps": total,
        "human_only_handoffs": handoffs,
        "step_time_s": {
            "p50": round(p50, 3),
            "max": round(max(step_times), 3) if step_times else 0.0,
            "mean": round(statistics.mean(step_times), 3) if step_times else 0.0,
        },
        "latency_target_s": 6,
        "p50_within_target": p50 <= 6,
        "worst_fixture": worst_fixture,
        "worst_fixture_s": round(worst_time, 3),
        "per_fixture": per_fixture,
        "planner": (
            "live google-gemma-4-31b-it (own Antseed gateway, "
            "surrogate auth) — real model grounding and latency"
            if live
            else "mock (ground-truth actions) — NOT live gateway latency"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Eclipse SIH benchmark (Sprint 6)")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument(
        "--live",
        action="store_true",
        help="run the loop with the real planner (gemma-4-31b on the "
        "user's own gateway) instead of the mock",
    )
    args = parser.parse_args()

    gt = _load_ground_truth()
    started = time.time()
    pii = _pii_metrics(gt)
    redaction = _redaction_proof(gt)
    resources = _resource_metrics()
    loop_metrics = _loop_metrics(gt, live=args.live)
    elapsed = time.time() - started

    if args.live:
        honesty = (
            "Grounding and latency were measured with the LIVE planner "
            "(google-gemma-4-31b-it on the user's own Antseed gateway, "
            "authenticated with the vault-held connector credential "
            "via the surrogate). They are real model measurements, "
            "not mock numbers."
        )
    else:
        honesty = (
            "Grounding and latency are measured with a MOCK planner that "
            "returns the ground-truth action. They measure the loop, not "
            "a live model. A live gateway run is still needed for a real "
            "planner grounding number and real step latency (honest "
            "expectation 4-8s/step on free cloud models)."
        )
    doc = {
        "benchmark": "eclipse-sih",
        "sprint": 6,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(started)),
        "fixtures": len(gt["fixtures"]),
        "elapsed_s": round(elapsed, 1),
        "metrics": {
            "visual_grounding_first_try": loop_metrics["grounding_first_try"],
            "pii_recall": pii["recall"],
            "pii_recall_scope": "regex-detectable types",
            "pii_precision": pii["precision"],
            "pii_precision_scope": "regex-detectable types",
            "redaction_recoverable_tokens": redaction["recoverable_tokens"],
            "resource_mb": resources["extension_models_mb"],
            "latency_p50_s": loop_metrics["step_time_s"]["p50"],
        },
        "detail": {
            "pii": pii,
            "redaction": redaction,
            "resources": resources,
            "loop": loop_metrics,
        },
        "honesty": honesty,
    }
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = args.out or RESULTS_DIR / (
        f"benchmark-{'live-' if args.live else ''}"
        f"{time.strftime('%Y%m%d-%H%M%S', time.localtime(started))}.json"
    )
    with open(out, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
    print(f"benchmark: {out}")
    print(f"  fixtures: {doc['fixtures']}")
    planner_label = "live planner" if args.live else "mock planner"
    print(f"  grounding ({planner_label}): {loop_metrics['grounding_first_try']:.1%}")
    print(f"  PII recall {pii['recall']:.1%} / precision {pii['precision']:.1%}")
    print(f"  redaction recoverable tokens: {redaction['recoverable_tokens']}")
    print(f"  extension models: {resources['extension_models_mb']} MB (budget 300)")
    print(f"  latency p50: {loop_metrics['step_time_s']['p50']}s (target ≤6s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
