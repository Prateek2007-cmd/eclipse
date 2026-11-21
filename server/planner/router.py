"""Planner router (Sprint 3): open-weights-only fallback chain.

Chain (plan §9, open-weights only — never Gemini/GPT as the runtime
planner):
  1. Own Antseed gateway (verified free, gemma-4-31b live planner)
  2. OrcaRouter free (only when ORCA_ROUTER_BASE_URL is set — its
     exact host is unconfirmed, so it is skipped, not guessed)
  3. Groq gpt-oss-120b
  4. Ollama local qwen2.5:7b
Never silently fall back from local mode to cloud: the chain is
cloud-first by design for the demo, and a task whose constraints say
"local only" stops at Ollama (see `local_only`).

Each call has an 8s timeout (plan §6.10 fix 7). On timeout or an
empty response, the next model is tried at once. The model that
answered is logged and its quota is incremented (Antseed free models
only — the quota table tracks those; Groq/Ollama are untracked).
Warn at 80% of the daily limit; stop cloud calls at 100%.

Antseed auth (fix list item 1, 2026-11-21): the gateway key lives
in the Secure Vault as the registered `custom.akash-antseed`
connector credential — it is NEVER in an env var, a file, or a
log. When ANTSEED_API_KEY is not set in the environment, the
Antseed endpoint authenticates with the connector's surrogate:
`dynamic_credentials.add_surrogate_to_request` attaches an
`hsurr:` bearer token and authd swaps in the real key on the
approved request to the registered host (the same mechanism the
`antseed.py` CLI uses). The raw key is never visible to the
server process.
"""
from __future__ import annotations

import functools
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from ..core import db
from ..proof import record as _record_outbound

logger = logging.getLogger("eclipse.planner")

# 15s, not 8s: the second live benchmark run (2026-11-21) showed
# plan calls timing out at ~8.8s once the token cap rose to 800 —
# a multi-step plan generates far more tokens than a single action
# and needs the extra time. Step calls still typically answer in
# ~2.5s; the 15s is a ceiling, not a target.
PLANNER_TIMEOUT_S = 15.0
# 800, not 400: the first live benchmark run (2026-11-21) showed
# gemma-4-31b over-generating 8-step plans that hit the 400-token
# cap mid-JSON and failed to parse (2 of 5 plan calls). A step call
# returns one short JSON action and uses far fewer tokens; the cap
# only bites plan calls. 800 covers ~15 steps of plan output.
PLANNER_MAX_TOKENS = 800
PLANNER_TEMPERATURE = 0.2


class PlannerError(RuntimeError):
    """All planner endpoints failed (or none is configured)."""


@dataclass
class Endpoint:
    """One planner endpoint in the fallback chain."""

    name: str
    base_url: str
    model: str
    api_key: str | None = None
    # The quota-table model id (Antseed free models only). None means
    # untracked (Groq, Ollama, OrcaRouter).
    quota_model: str | None = None
    # Surrogate auth (fix list item 1): when True, authenticate with
    # the registered connector credential instead of api_key. The raw
    # key is never in the server process.
    use_surrogate: bool = False
    surrogate_connector: str | None = None
    surrogate_hosts: tuple[str, ...] = ()


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def default_chain() -> list[Endpoint]:
    """Build the fallback chain from the environment."""
    chain: list[Endpoint] = []
    antseed_key = _env("ANTSEED_API_KEY")
    antseed_base = (
        _env("ANTSEED_BASE_URL", "https://akash-antseed.duckdns.org/v1") or ""
    )
    antseed_model = _env("ANTSEED_MODEL", "google-gemma-4-31b-it") or ""
    antseed_host = urllib.parse.urlparse(antseed_base).hostname or ""
    if antseed_key:
        chain.append(
            Endpoint(
                name="antseed",
                base_url=antseed_base,
                model=antseed_model,
                api_key=antseed_key,
                quota_model=antseed_model,
            )
        )
    else:
        # No env key: authenticate with the vault-held connector
        # credential via the surrogate (fix list item 1). The
        # endpoint is always in the chain — a missing env var must
        # not silently drop the live planner. ANTSEED_USE_SURROGATE=0
        # disables this (planner-less server: plan calls fail closed
        # with 502, as test_plan_fails_closed_without_a_planner
        # verifies).
        if _env("ANTSEED_USE_SURROGATE", "1") != "0":
            chain.append(
                Endpoint(
                    name="antseed",
                    base_url=antseed_base,
                    model=antseed_model,
                    api_key=None,
                    quota_model=antseed_model,
                    use_surrogate=True,
                    surrogate_connector="custom.akash-antseed",
                    surrogate_hosts=(antseed_host,) if antseed_host else (),
                )
            )
    orca_base = _env("ORCA_ROUTER_BASE_URL")
    orca_key = _env("ORCA_ROUTER_API_KEY")
    orca_model = _env("ORCA_ROUTER_MODEL")
    if orca_base and orca_key and orca_model:
        chain.append(
            Endpoint(
                name="orcarouter",
                base_url=orca_base,
                model=orca_model,
                api_key=orca_key,
            )
        )
    groq_key = _env("GROQ_API_KEY")
    if groq_key:
        chain.append(
            Endpoint(
                name="groq",
                base_url=_env("GROQ_BASE_URL", "https://api.groq.com/openai/v1") or "",
                model=_env("GROQ_MODEL", "gpt-oss-120b") or "",
                api_key=groq_key,
                # gpt-oss-120b has a quota row in FREE_MODELS, so its
                # calls are tracked like the Antseed free models.
                quota_model=_env("GROQ_MODEL", "gpt-oss-120b"),
            )
        )
    chain.append(
        Endpoint(
            name="ollama",
            base_url=_env("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1") or "",
            model=_env("OLLAMA_MODEL", "qwen2.5:7b") or "",
            api_key=None,
        )
    )
    return chain


def _attach_surrogate(request, connector: str, hosts: tuple[str, ...]) -> None:
    """Attach the vault-held connector credential to a request.

    Imports the bundled helper lazily so this module stays
    importable where the helper is absent; the call fails loudly
    only when the surrogate path is actually used. The helper
    returns an `hsurr:` surrogate — authd swaps in the real key on
    the approved request. The raw key never enters this process.
    """
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import add_surrogate_to_request

    add_surrogate_to_request(request, connector, allowed_hosts=list(hosts))


def _real_transport(
    url: str,
    headers: dict[str, str],
    payload: dict,
    timeout: float,
    surrogate: tuple[str, tuple[str, ...]] | None = None,
) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    if surrogate is not None:
        _attach_surrogate(req, surrogate[0], surrogate[1])
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _rollover_if_needed() -> None:
    """Reset quota rows whose day has passed. Rollover used to run
    only in db.migrate() at server startup, so a server running
    across midnight never reset used_today — Antseed models stayed
    exhausted until restart. Now it runs on every quota read/write.
    """
    import time as _time

    from ..core import db as _db

    conn = _db.connect()
    try:
        now = int(_time.time())
        conn.execute(
            "UPDATE quota SET used_today = 0, reset_at = ? WHERE reset_at <= ?",
            (db._next_midnight_utc_epoch(), now),
        )
        conn.commit()
    finally:
        conn.close()


def _quota_remaining(model: str) -> int | None:
    """Calls left today for a quota-tracked model; None if untracked."""
    _rollover_if_needed()
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT used_today, limit_day FROM quota WHERE model = ?", (model,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        # A configured model with no quota row is NOT silently
        # untracked — log it loudly so the misconfiguration is seen.
        logger.warning(
            "planner model %s has no quota row; quota enforcement is off for it",
            model,
        )
        return None
    return max(0, int(row["limit_day"]) - int(row["used_today"]))


def _quota_bump(model: str) -> None:
    conn = db.connect()
    try:
        conn.execute(
            "UPDATE quota SET used_today = used_today + 1 WHERE model = ?", (model,)
        )
        conn.commit()
        row = conn.execute(
            "SELECT used_today, limit_day FROM quota WHERE model = ?", (model,)
        ).fetchone()
        if row and row["limit_day"]:
            frac = int(row["used_today"]) / int(row["limit_day"])
            if frac >= 0.8:
                logger.warning(
                    "planner quota %s at %.0f%% (%d/%d today)",
                    model, frac * 100, row["used_today"], row["limit_day"],
                )
    finally:
        conn.close()


def call_planner(
    prompt: str,
    chain: list[Endpoint] | None = None,
    max_tokens: int = PLANNER_MAX_TOKENS,
    temperature: float = PLANNER_TEMPERATURE,
    local_only: bool = False,
    transport=None,
) -> tuple[str, str, float]:
    """Call the planner, trying each endpoint in order.

    Returns (response_text, model_name, ms). Raises PlannerError when
    every endpoint fails or none is configured. `transport` is an
    injectable (url, headers, payload, timeout) -> response dict, for
    tests; the default is a real urllib POST.
    """
    transport = transport or _real_transport
    endpoints = chain if chain is not None else default_chain()
    if local_only:
        endpoints = [e for e in endpoints if e.name == "ollama"]
    errors: list[str] = []
    for ep in endpoints:
        # Quota: skip a tracked model that is exhausted today.
        if ep.quota_model:
            left = _quota_remaining(ep.quota_model)
            if left is not None and left <= 0:
                errors.append(f"{ep.name}: quota exhausted today")
                continue
        url = ep.base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {ep.api_key}"} if ep.api_key else {}
        payload = {
            "model": ep.model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        # An injected transport (tests) is used as-is for every
        # endpoint. Otherwise a surrogate endpoint gets the
        # surrogate transport; every other endpoint gets the plain
        # urllib transport.
        ep_transport = transport
        if transport is _real_transport and ep.use_surrogate:
            if not ep.surrogate_connector or not ep.surrogate_hosts:
                errors.append(f"{ep.name}: surrogate misconfigured")
                continue
            ep_transport = functools.partial(
                _real_transport,
                surrogate=(ep.surrogate_connector, ep.surrogate_hosts),
            )
        started = time.time()
        try:
            data = ep_transport(url, headers, payload, PLANNER_TIMEOUT_S)
            text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            text = text.strip()
            if not text:
                errors.append(f"{ep.name}: empty response")
                continue
            ms = (time.time() - started) * 1000
            if ep.quota_model:
                _quota_bump(ep.quota_model)
            logger.info("planner answered via %s (%s) in %.0f ms", ep.name, ep.model, ms)
            _record_outbound(url, ep.model, prompt, ms, ok=True)
            return text, f"{ep.name}:{ep.model}", ms
        except Exception as exc:  # noqa: BLE001 - try the next model
            ms = (time.time() - started) * 1000
            errors.append(f"{ep.name}: {exc} ({ms:.0f} ms)")
            _record_outbound(url, ep.model, prompt, ms, ok=False)
            continue
    raise PlannerError("planner failed on every endpoint: " + "; ".join(errors))
