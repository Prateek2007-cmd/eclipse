#!/usr/bin/env python3
"""Score a benchmark results file (Sprint 1).

Reads the newest results JSON (or --file), prints the fixture pass
rate and the 5 SIH metrics, and writes score.json next to it.

Sprint 1: the 5 SIH metrics are NOT MEASURED — the planner, executor,
vision and redactor do not exist yet. score.py reports them as null
with the reason, never invents a number. A score file with nulls is
honest; a made-up 87% would be a lie.

Usage:
    python3 evals/sih-benchmark/score.py --latest
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
    files = sorted(RESULTS_DIR.glob("results-*.json"))
    if not files:
        raise FileNotFoundError(
            f"no results files in {RESULTS_DIR}; run run.py first"
        )
    return files[-1]


def score(results_path: Path) -> dict:
    """Build the score document from a results file."""
    with open(results_path, encoding="utf-8") as f:
        results = json.load(f)

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
    rate = doc["fixture_pass_rate"]
    print(f"fixtures: {rate.get('passed')}/{rate.get('total')} passed")
    print("SIH metrics:")
    for name, metric in doc["metrics"].items():
        value = metric.get("value")
        status = metric.get("status", "not_measured")
        if value is None:
            print(f"  {name}: not measured ({status}) — "
                  f"target {metric.get('target')}")
        else:
            print(f"  {name}: {value} (target {metric.get('target')})")
    print("No SIH numbers yet — a real loop has not run on the fixtures.")
    print(f"score: {doc['out_path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
