"""Google Calendar connector (Sprint 7, plan §7.2).

Scopes (minimal, held by the platform connector):
calendar.readonly, calendar.events (create only).

- Read: list events, free/busy check for interview slots.
- Create: interview or deadline events, only after user approval,
  with title, time, link, and a plain description. Never delete
  or move events the user did not create through Eclipse.
"""
from __future__ import annotations

import json

from .base import ConnectorError, GwsCli

PRIMARY = "primary"


def _require_dict(data, what: str) -> dict:
    """The CLI must return a JSON object; a raw string or a list
    is a malformed response, not an empty result."""
    if not isinstance(data, dict):
        raise ConnectorError(
            "calendar", f"unexpected {what} output from the CLI", "error"
        )
    return data


class CalendarConnector:
    """Calendar via hatch_gws_cli. `cli` is injectable for tests."""

    def __init__(self, cli: GwsCli | None = None):
        self.cli = cli or GwsCli()

    def status(self) -> dict:
        try:
            data = self.cli.status("calendar")
        except ConnectorError as exc:
            if exc.kind == "auth":
                return {"name": "calendar", "state": "needs_reconnect", "error": str(exc)}
            return {"name": "calendar", "state": "unavailable", "error": str(exc)}
        if not isinstance(data, dict):
            return {"name": "calendar", "state": "unavailable", "error": "unexpected status output"}
        if data.get("status") == "connected":
            return {"name": "calendar", "state": "connected"}
        return {
            "name": "calendar",
            "state": "needs_reconnect",
            "connect_url": data.get("connect_url"),
        }

    def freebusy(self, time_min: str, time_max: str) -> dict:
        """Free/busy for the window (RFC3339). Returns the raw
        freebusy result: {calendars: {primary: {busy: [...]}}}."""
        data = self.cli.run_read(
            "calendar", "freebusy", "query",
            "--json",
            json.dumps({
                "timeMin": time_min,
                "timeMax": time_max,
                "items": [{"id": "primary"}],
            }),
        )
        return _require_dict(data, "freebusy")

    def list_events(self, time_min: str, time_max: str) -> list[dict]:
        """Events in the window, single instances, time order."""
        data = self.cli.run_read(
            "calendar", "events", "list",
            "--params",
            json.dumps({
                "calendarId": "primary",
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": True,
                "orderBy": "startTime",
            }),
        )
        data = _require_dict(data, "events list")
        items = data.get("items", [])
        return [
            {
                "id": e.get("id"),
                "summary": e.get("summary"),
                "start": e.get("start"),
                "end": e.get("end"),
                "description": e.get("description"),
                "location": e.get("location"),
            }
            for e in items
        ]

    def create_event(
        self,
        summary: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
    ) -> dict:
        """Create a timed event on the primary calendar.

        `start`/`end` are RFC3339 with an offset. The caller owns
        the user approval — this only performs the create. Returns
        {id, summary, start, end, htmlLink} with start/end as
        plain RFC3339 strings. Raises ConnectorError when the CLI
        returns no event id — the caller must not record a
        success it never got.
        """
        body = {
            "summary": summary,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location
        data = self.cli.run(
            "calendar", "events", "insert",
            "--params", json.dumps({"calendarId": "primary"}),
            "--json", json.dumps(body),
        )
        data = _require_dict(data, "events insert")
        event_id = data.get("id")
        if not event_id:
            raise ConnectorError(
                "calendar", "the calendar call returned no event id", "error"
            )

        def _when(value, fallback: str) -> str:
            if isinstance(value, dict):
                return value.get("dateTime") or value.get("date") or fallback
            return value or fallback

        return {
            "id": event_id,
            "summary": data.get("summary", summary),
            "start": _when(data.get("start"), start),
            "end": _when(data.get("end"), end),
            "htmlLink": data.get("htmlLink"),
        }
