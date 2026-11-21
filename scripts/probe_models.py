#!/usr/bin/env python3
"""Live model probe (Sprint 0 repeatable).

Probes the verified free top 5 on the user's OWN Antseed gateway
(https://akash-antseed.duckdns.org/v1) and prints model / calls left /
latency / context for each.

Uses the registered custom.akash-antseed credential through the
surrogate exchange in dynamic_credentials.py — the same pattern as
antseed.py. The real key never appears in this file, in arguments, in
environment variables, or in logs. Never use Prateek's gateway for
this probe (the orchestrator-friends gateway-credentials file is
Prateek's gateway; hitting it returned 403).

- Calls left: the gateway does not report quota, so it is read from
  the local team usage log if present and reported as "~N left" under
  the 200/day rule, with the caveat stated.
- Context: the gateway /v1/models returns ids only, so context is
  reported as "not reported by gateway; planner cap 8K".
- gpt-oss-120b is probed too; it has returned empty content 3 times
  in a row, so it is expected to fail and is not the planner.

Usage:
    python3 scripts/probe_models.py
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
from dynamic_credentials import add_surrogate_to_request, read_json_response

BASE = "https://akash-antseed.duckdns.org/v1"
CONNECTOR = "custom.akash-antseed"
ALLOWED_HOSTS = ["akash-antseed.duckdns.org"]

FREE_MODELS = [
    "gpt-oss-120b",
    "google-gemma-4-31b-it",
    "nvidia-nemotron-3-nano-30b-a3b",
    "google-gemma-4-26b-a4b-it",
    "mistral-small-3-2-24b-instruct",
]
USAGE_FILE = Path.home() / "workspace" / "antseed-team" / "usage.json"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        raise RuntimeError("redirect refused, verify the endpoint")


def call(method: str, path: str, body: dict | None = None,
         timeout: int = 30) -> tuple[int, dict, float]:
    """One authenticated call to the user's own gateway."""
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Content-Type", "application/json")
    add_surrogate_to_request(request, CONNECTOR, allowed_hosts=ALLOWED_HOSTS)
    t0 = time.monotonic()
    with urllib.request.build_opener(NoRedirect()).open(
        request, timeout=timeout
    ) as response:
        return response.status, read_json_response(response), \
            time.monotonic() - t0


def _calls_left_today() -> str:
    """Calls left under the 200/day rule, from the local usage log."""
    if not USAGE_FILE.is_file():
        return "unknown (no usage log; gateway does not report quota)"
    try:
        usage = json.loads(USAGE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return "unknown (usage log unreadable)"
    today = datetime.now().strftime("%Y-%m-%d")
    used = 0
    for day, models in usage.get("daily", {}).items():
        if day == today:
            used = sum(models.values())
    return f"~{max(0, 200 - used - len(FREE_MODELS))} left of 200/day " \
           f"(team log: {used} used today; this probe uses " \
           f"{len(FREE_MODELS)} calls)"


def _probe(model: str) -> dict:
    try:
        _status, body, latency = call("POST", "/chat/completions", {
            "model": model,
            "messages": [{"role": "user",
                          "content": "Reply with exactly: OK"}],
            "max_tokens": 5,
            "temperature": 0,
        })
        content = body.get("choices", [{}])[0].get("message", {}).get("content")
        if content and content.strip():
            return {"model": model, "status": "OK",
                    "latency_s": round(latency, 2),
                    "content": content.strip()[:40]}
        return {"model": model, "status": "EMPTY",
                "latency_s": round(latency, 2),
                "content": "(empty content)"}
    except urllib.error.HTTPError as exc:
        return {"model": model, "status": f"HTTP {exc.code}",
                "latency_s": 0.0, "content": ""}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"model": model, "status": f"ERROR: {exc}",
                "latency_s": 0.0, "content": ""}


def main(argv: list[str] | None = None) -> int:
    # Gateway up?
    try:
        _status, payload, _latency = call("GET", "/models", timeout=15)
        models = payload.get("data", [])
        print(f"gateway up: {BASE} ({len(models)} models)")
    except urllib.error.HTTPError as exc:
        print(f"gateway DOWN: HTTP {exc.code}", file=sys.stderr)
        return 1
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"gateway DOWN: {exc}", file=sys.stderr)
        return 1

    print(f"calls left: {_calls_left_today()}")
    print("context: not reported by gateway; planner cap 8K")
    print()
    results = []
    for model in FREE_MODELS:
        result = _probe(model)
        results.append(result)
        print(f"{result['model']:<38} {result['status']:<10} "
              f"{result['latency_s']:>5.2f}s  {result['content']}")
    ok = sum(1 for r in results if r["status"] == "OK")
    print()
    print(f"result: {ok}/{len(FREE_MODELS)} answering; "
          "live planner is the first OK model in this list")
    return 0 if ok >= 1 else 2


if __name__ == "__main__":
    sys.exit(main())
