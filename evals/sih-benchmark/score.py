#!/usr/bin/env python3
"""Score a benchmark results file.

Reads the newest results JSON (or --file), prints the fixture pass
rate and the 5 SIH metrics, and writes score.json next to it.

Sprint 6: the 5 SIH metrics now come from the real benchmark run
(benchmark.py), which measures the loop on the 12 fixtures. score.py
prefers the newest benchmark-*.json; a harness results-*.json still
scores, but its metrics are null with the reason — never invented.
A score file with nulls is honest; a made-up 87% would be a lie.

Usage:
    python3 evals/sih-benchmark/score.py --latest
    python3 evals/sih-benchmark/score.py --file results/benchmark-....json
    python3 evals/sih-benchmark/score.py --file results/results-....json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent
RESULTS_DIR = BASE / "results"


def _latest_results() -> Path:
    """Newest benchmark run wins (by modification time — a custom
    --out name must not outrank a newer run); a harness run is the
    fallback."""
    bench = sorted(
        RESULTS_DIR.glob("benchmark-*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if bench:
        return bench[-1]
    files = sorted(
        RESULTS_DIR.glob("results-*.json"),
        key=lambda p: p.stat().st_mtime,
    )
    if not files:
        raise FileNotFoundError(
            f"no results files in {RESULTS_DIR}; run benchmark.py or run.py first"
        )
    return files[-1]


def score(results_path: Path) -> dict:
    """Build the score document from a results file."""
    with open(results_path, encoding="utf-8") as f:
        results = json.load(f)

    if results_path.name.startswith("benchmark-"):
        # The benchmark file already carries the 5 real metrics.
        return _score_benchmark(results_path, results)
    rate = results.get("fixture_pass_rate", {})
    score_doc = {
        "scored_at": results.get("generated_at"),
        "source": str(results_path),
        "sprint": results.get("sprint"),
        "fixture_pass_rate": rate,
        "metrics": results.get("metrics", {}),
    }
    # Never overwrite the input: for a file not named results-*.json
    # the replace below is a no-op, so fall back to a -score name.
    out_name = results_path.name.replace("results-", "score-")
    if out_name == results_path.name:
        out_name = results_path.stem + "-score.json"
    out_path = results_path.with_name(out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(score_doc, f, indent=2)
    score_doc["out_path"] = str(out_path)
    return score_doc


def _metric(value, target, **extra) -> dict:
    """One scored metric. A missing value is not_measured, never
    'measured' with a null — the old code stamped every metric
    'measured' even when the benchmark file had no value for it."""
    doc = {
        "value": value,
        "target": target,
        "status": "measured" if value is not None else "not_measured",
    }
    doc.update(extra)
    return doc


def _score_benchmark(results_path: Path, results: dict) -> dict:
    """Score a benchmark-*.json: the metrics are real measurements."""
    m = results.get("metrics", {})
    detail = results.get("detail", {})
    loop_detail = detail.get("loop", {})
    pii = detail.get("pii", {})
    # The grounding/latency notes must match the planner that
    # actually ran: a live run labelled 'mock planner' (the old
    # hardcoded notes) misreports real model measurements as loop
    # overhead. benchmark.py writes planner strings starting with
    # 'live ' for --live runs and 'mock ' otherwise.
    live = str(loop_detail.get("planner", "")).startswith("live")
    grounding_note = (
        "live planner — real model grounding"
        if live
        else "mock planner — loop grounding, not a live model"
    )
    latency_note = (
        "live gateway latency"
        if live
        else "loop overhead with a mock planner — not live gateway latency"
    )
    score_doc = {
        "scored_at": results.get("generated_at"),
        "source": str(results_path),
        "sprint": results.get("sprint"),
        "fixtures": results.get("fixtures"),
        "metrics": {
            "visual_grounding_first_try": _metric(
                m.get("visual_grounding_first_try"),
                0.85,
                note=grounding_note,
                human_only_handoffs=loop_detail.get("human_only_handoffs"),
            ),
            "pii_recall": _metric(
                m.get("pii_recall"),
                0.95,
                scope="regex-detectable types",
                worst_fixture=pii.get("worst_fixture"),
            ),
            "pii_precision": _metric(
                m.get("pii_precision"),
                0.90,
                scope="regex-detectable types",
            ),
            "redaction_recoverable_tokens": _metric(
                m.get("redaction_recoverable_tokens"),
                0,
            ),
            "resource_mb": _metric(
                m.get("resource_mb"),
                300,
                note="extension model download size on disk",
            ),
            "latency_p50_s": _metric(
                m.get("latency_p50_s"),
                6,
                note=latency_note,
                worst_fixture=loop_detail.get("worst_fixture"),
            ),
        },
        "honesty": results.get("honesty"),
    }
    out_name = results_path.name.replace("benchmark-", "score-")
    out_path = results_path.with_name(out_name)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(score_doc, f, indent=2)
    score_doc["out_path"] = str(out_path)
    return score_doc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Score Eclipse benchmark results")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--latest", action="store_true",
                       help="score the newest results file")
    group.add_argument("--file", type=Path, help="score this results file")
    args = parser.parse_args(argv)

    try:
        results_path = _latest_results() if args.latest else args.file
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not results_path.is_file():
        print(f"error: no such file: {results_path}", file=sys.stderr)
        return 2

    doc = score(results_path)
    rate = doc.get("fixture_pass_rate")
    if rate:
        print(f"fixtures: {rate.get('passed')}/{rate.get('total')} passed")
    else:
        print(f"fixtures: {doc.get('fixtures')} in the benchmark run")
    print("SIH metrics:")
    measured = True
    for name, metric in doc["metrics"].items():
        value = metric.get("value")
        status = metric.get("status", "not_measured")
        if value is None:
            measured = False
            print(f"  {name}: not measured ({status}) — "
                  f"target {metric.get('target')}")
        else:
            print(f"  {name}: {value} (target {metric.get('target')})")
    if not measured:
        print("No SIH numbers yet — a real loop has not run on the fixtures.")
    elif doc.get("honesty"):
        print(f"Note: {doc['honesty']}")
    print(f"score: {doc['out_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
