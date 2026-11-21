#!/usr/bin/env python3
"""Eclipse SIH benchmark harness (Sprint 1).

Serves the 12 fixtures over localhost, checks each one against
ground_truth.json, and writes results JSON.

Sprint 1 scope: structural checks only. The planner, executor, vision
and redactor do not exist yet (Sprints 2-4), so the 5 SIH metrics are
recorded as NOT MEASURED with the reason — never invented. The harness
proves itself by running cleanly on the fixtures; scores come later.

Usage:
    python3 evals/sih-benchmark/run.py [--runs N] [--out DIR]
"""
from __future__ import annotations

import argparse
import functools
import json
import sys
import threading
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = Path(__file__).resolve().parent
FIXTURES_DIR = BASE / "fixtures"
GROUND_TRUTH = BASE / "ground_truth.json"
DEFAULT_OUT = BASE / "results"

# The 5 SIH metrics (section 6.7 of the finalised plan). Targets only;
# values stay null until a real benchmark run produces them.
METRIC_TARGETS = {
    "visual_accuracy": {"target": ">=85% first-try grounding", "weight": 0.25},
    "pii_recall_precision": {"target": "recall >=95%, precision >=90%", "weight": 0.20},
    "redaction_precision": {"target": "0 recoverable tokens", "weight": 0.20},
    "resource_use": {"target": "RAM <=500MB, models <=300MB", "weight": 0.20},
    "latency": {"target": "<=6s/step, demo task <=90s", "weight": 0.15},
}


class _QuietHandler(SimpleHTTPRequestHandler):
    """Serve fixtures without logging every GET to stdout."""

    def log_message(self, *args):
        pass


class _PageParser(HTMLParser):
    """Collect element ids and their content from a fixture.

    For each id, `values[id]` is the input's `value` attribute when
    present, otherwise the element's visible text. An empty value
    means the element carries no data — a PII label pointing at an
    empty element is a broken fixture, not a pass.
    """

    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.values: dict[str, str] = {}
        self._open: list[str | None] = []

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        element_id = attr.get("id")
        self._open.append(element_id)
        if element_id:
            self.ids.add(element_id)
            # Inputs carry their data in `value`, not in text.
            if "value" in attr:
                self.values[element_id] = attr["value"]
            else:
                self.values.setdefault(element_id, "")

    def handle_endtag(self, tag):
        if self._open:
            self._open.pop()

    def handle_data(self, data):
        text = data.strip()
        if not text:
            return
        for element_id in reversed(self._open):
            if element_id is not None:
                self.values[element_id] = (
                    self.values.get(element_id, "") + " " + text
                ).strip()
                return


def _serve_fixtures():
    """Start a localhost server for the fixtures dir. Returns (server, port)."""
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES_DIR))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1]


def _check_fixture(name: str, spec: dict, port: int) -> dict:
    """Run the structural checks for one fixture."""
    result: dict = {"name": name, "file": spec["file"], "checks": {},
                    "passed": True}
    checks = result["checks"]
    path = FIXTURES_DIR / spec["file"]

    checks["file_exists"] = path.is_file()
    if not checks["file_exists"]:
        result["passed"] = False
        return result

    # Fixture must be served over HTTP (the real loop fetches pages).
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/{spec['file']}", timeout=5
        ) as resp:
            checks["http_200"] = resp.status == 200
            html = resp.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        checks["http_200"] = False
        html = ""
    if not checks["http_200"]:
        result["passed"] = False
        return result

    parser = _PageParser()
    parser.feed(html)

    # Target-action element ids. The broken-selector fixture is the
    # exception: its whole point is that el_result_status is ABSENT,
    # so the check asserts absence instead of presence.
    if name == "broken-selector":
        checks["target_elements_present"] = (
            "el_result_status" not in parser.ids
        )
        checks["target_elements_missing"] = (
            [] if checks["target_elements_present"]
            else ["el_result_status (must be absent)"]
        )
    else:
        missing_actions = [
            a["element_id"] for a in spec.get("target_actions", [])
            if a["element_id"] not in parser.ids
        ]
        checks["target_elements_present"] = not missing_actions
        checks["target_elements_missing"] = missing_actions

    # Every PII location must exist AND carry data — except values
    # that only exist at runtime (OTP from a user handoff). An element
    # that exists but is empty would pass a presence-only check while
    # the fixture holds no PII at all.
    missing_pii = []
    empty_pii = []
    for item in spec.get("pii", []):
        location = item["location"]
        if location.startswith("img:"):
            if not (FIXTURES_DIR / location[4:]).is_file():
                missing_pii.append(location)
        elif location not in parser.ids:
            missing_pii.append(location)
        elif not item.get("runtime_value") and not parser.values.get(
            location, ""
        ).strip():
            empty_pii.append(location)
    checks["pii_locations_present"] = not missing_pii
    checks["pii_locations_missing"] = missing_pii
    checks["pii_values_non_empty"] = not empty_pii
    checks["pii_values_empty"] = empty_pii

    # Fixtures that need image data must declare it.
    if name == "faces":
        checks["face_boxes_defined"] = bool(spec.get("face_boxes"))
    if name == "scanned-doc":
        checks["ocr_text_defined"] = bool(spec.get("ocr_text"))

    # Expected end states must be written (a fixture without one cannot
    # be scored later).
    missing_states = [
        a["element_id"] for a in spec.get("target_actions", [])
        if not a.get("expected_end_state")
    ]
    checks["end_states_defined"] = not missing_states
    checks["end_states_missing"] = missing_states

    checks["pii_count"] = len(spec.get("pii", []))
    checks["action_count"] = len(spec.get("target_actions", []))
    required = ("target_elements_present", "pii_locations_present",
                "pii_values_non_empty", "end_states_defined")
    if name == "faces":
        required += ("face_boxes_defined",)
    if name == "scanned-doc":
        required += ("ocr_text_defined",)
    result["passed"] = all(checks[k] for k in required)
    return result


def run(runs: int, out_dir: Path) -> tuple[dict, bool]:
    """Run the harness. Returns (results, all_passed)."""
    with open(GROUND_TRUTH, encoding="utf-8") as f:
        ground_truth = json.load(f)

    server, port = _serve_fixtures()
    results: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sprint": 1,
        "runs": runs,
        "run_results": [],
        "fixtures": {},
        "metrics": {
            name: {
                "value": None,
                "target": spec["target"],
                "weight": spec["weight"],
                "status": "not_measured",
                "reason": "planner + executor built (Sprint 3); "
                          "no score until a real loop runs on fixtures "
                          "(Sprint 6 benchmark)",
            }
            for name, spec in METRIC_TARGETS.items()
        },
    }
    total_passed = 0
    total_fixtures = 0
    try:
        for run_index in range(runs):
            run_fixtures = {}
            for name, spec in ground_truth["fixtures"].items():
                check = _check_fixture(name, spec, port)
                run_fixtures[name] = check
                total_fixtures += 1
                total_passed += 1 if check["passed"] else 0
            results["run_results"].append({
                "run": run_index + 1,
                "fixtures": run_fixtures,
                "passed": sum(
                    1 for c in run_fixtures.values() if c["passed"]
                ),
                "total": len(run_fixtures),
            })
        # "fixtures" keeps the last run, for readers that expect it.
        results["fixtures"] = results["run_results"][-1]["fixtures"]
    finally:
        server.shutdown()
        server.server_close()

    results["fixture_pass_rate"] = {
        "passed": total_passed,
        "total": total_fixtures,
        "status": "measured",
    }
    all_passed = total_passed == total_fixtures

    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{time.time_ns() % 1_000_000:06d}"
    out_path = out_dir / f"results-{stamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    results["out_path"] = str(out_path)
    return results, all_passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Eclipse SIH benchmark harness")
    parser.add_argument("--runs", type=int, default=5,
                        help="repeat the fixture checks N times (default 5)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT,
                        help="results directory")
    args = parser.parse_args(argv)

    results, all_passed = run(args.runs, args.out)
    rate = results["fixture_pass_rate"]
    print(f"fixtures: {rate['passed']}/{rate['total']} passed "
          f"({args.runs} run(s))")
    for name, check in results["fixtures"].items():
        status = "PASS" if check["passed"] else "FAIL"
        print(f"  [{status}] {name}: "
              f"{check['checks'].get('action_count', 0)} actions, "
              f"{check['checks'].get('pii_count', 0)} PII labels")
        if not check["passed"]:
            print(f"        {check['checks']}")
    print("SIH metrics: not measured (no real loop run yet — Sprint 6 benchmark)")
    print(f"results: {results['out_path']}")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
