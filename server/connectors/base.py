"""Shared transport for the Google Workspace connectors (Sprint 7).

The server reaches Gmail, Calendar and Drive through the
platform's `hatch_gws_cli` binary, which holds the OAuth grant.
The transport is a subprocess call with a timeout; its output is
parsed as JSON when possible, kept as text otherwise. The
transport function is injectable so tests never touch the real
mailbox or calendar.

Timeout rationale (30s, not the planner's 8s): the CLI does a
full HTTPS round-trip to Google plus its own startup, and a send
can legitimately take a few seconds; 8s would fail healthy calls.
The timeout only bounds a hung call — it is not a latency
target.
"""
from __future__ import annotations

import json
import logging
import subprocess
import time

logger = logging.getLogger("eclipse.connectors")

GWS_CLI = "/opt/hatch/bin/hatch_gws_cli"
DEFAULT_TIMEOUT_S = 30


class ConnectorError(Exception):
    """A connector call failed (auth, quota, API down, timeout)."""

    def __init__(self, connector: str, message: str, kind: str = "error"):
        super().__init__(message)
        self.connector = connector
        self.kind = kind  # auth | quota | unavailable | error


def _classify_error(message: str) -> str:
    """Classify a CLI failure from its text. Auth failures mean
    the grant is expired/revoked (reconnect card); quota failures
    mean wait-and-retry-once, then skip."""
    lowered = message.lower()
    if any(
        word in lowered
        for word in (
            "not connected",
            "unauthenticated",
            "invalid_grant",
            "invalid_token",
            "token expired",
            "token_expired",
            "unauthorized",
            "forbidden",
            "permission_denied",
            "insufficientpermissions",
            "insufficient authentication",
            "account may not be linked",
            "reauth",
            "401",
            "403",
        )
    ):
        return "auth"
    if any(
        word in lowered
        for word in (
            "quota",
            "ratelimitexceeded",
            "rate limited",
            "rate_limited",
            "too many requests",
            "429",
        )
    ):
        return "quota"
    return "error"


def _real_transport(argv: list[str], timeout: int = DEFAULT_TIMEOUT_S) -> dict:
    """Run hatch_gws_cli and return {ok, data, raw}."""
    try:
        proc = subprocess.run(
            [GWS_CLI, *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        logger.warning("hatch_gws_cli timed out after %ss: %s", timeout, argv[:2])
        raise ConnectorError("gws", f"hatch_gws_cli timed out after {timeout}s", "unavailable")
    except FileNotFoundError:
        raise ConnectorError("gws", "hatch_gws_cli not found on this machine", "unavailable")
    raw = (proc.stdout or "").strip()
    if proc.returncode != 0:
        # Log the full output server-side only; the message
        # carried to the caller is a short generic one (M1 —
        # raw CLI text must not land in SQLite or API details).
        logger.warning(
            "hatch_gws_cli failed (%s): %s",
            argv[:2],
            (raw or proc.stderr.strip())[:500],
        )
        message = raw or proc.stderr.strip() or f"exit {proc.returncode}"
        raise ConnectorError("gws", message, _classify_error(message))
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        data = {"raw": raw}
    return {"ok": True, "data": data, "raw": raw}


def _looks_like_auth_error(message: str) -> bool:
    return _classify_error(message) == "auth"


class GwsCli:
    """Thin wrapper around hatch_gws_cli.

    `transport` is (argv, timeout) -> {ok, data, raw}; inject a
    fake in tests. Every call returns the parsed data dict (or
    {"raw": ...} for non-JSON output).
    """

    def __init__(self, transport=None, timeout: int = DEFAULT_TIMEOUT_S):
        self._transport = transport or _real_transport
        self.timeout = timeout

    def run(self, *argv: str) -> dict:
        """Run the CLI and return the parsed data dict."""
        result = self._transport(list(argv), self.timeout)
        data = result.get("data", {})
        if isinstance(data, dict) and data.get("kind") == "connector_rate_limited":
            raise ConnectorError("gws", "rate limited by the provider", "quota")
        return data

    def run_read(self, *argv: str) -> dict:
        """A read call with the plan §7.4 quota rule: on a quota
        failure, wait once and retry once, then give up."""
        try:
            return self.run(*argv)
        except ConnectorError as exc:
            if exc.kind != "quota":
                raise
            time.sleep(1)
            return self.run(*argv)

    def status(self, provider: str) -> dict:
        """The connector's status dict (connected / not connected)."""
        try:
            return self.run(provider, "status")
        except ConnectorError:
            raise
        except Exception as exc:  # noqa: BLE001 - CLI shape varies
            raise ConnectorError(provider, str(exc), "unavailable")


def public_error(exc: ConnectorError) -> str:
    """The short generic detail safe to return to a caller or
    store in SQLite. The full CLI output stays in the server
    log only (M1)."""
    if exc.kind == "auth":
        return "the Google grant expired or was revoked — reconnect the connector"
    if exc.kind == "quota":
        return "Google rate limit reached — try again shortly"
    if exc.kind == "unavailable":
        return "the Google connector is unavailable right now"
    return "the Google connector call failed"
