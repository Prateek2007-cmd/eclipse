"""Google Drive connector (Sprint 7, plan §7.3).

Scopes (minimal, held by the platform connector):
drive.readonly, drive.file (files Eclipse creates).

- Read: find the resume file and past application exports.
- Write: upload receipts, exported audit logs, and the morning
  briefing as a file, only when the user asks. Never delete user
  files.
"""
from __future__ import annotations

import json

from .base import ConnectorError, GwsCli


def _require_dict(data, what: str) -> dict:
    if not isinstance(data, dict):
        raise ConnectorError(
            "drive", f"unexpected {what} output from the CLI", "error"
        )
    return data


class DriveConnector:
    """Drive via hatch_gws_cli. `cli` is injectable for tests."""

    def __init__(self, cli: GwsCli | None = None):
        self.cli = cli or GwsCli()

    def status(self) -> dict:
        try:
            data = self.cli.status("drive")
        except ConnectorError as exc:
            if exc.kind == "auth":
                return {"name": "drive", "state": "needs_reconnect", "error": str(exc)}
            return {"name": "drive", "state": "unavailable", "error": str(exc)}
        if not isinstance(data, dict):
            return {"name": "drive", "state": "unavailable", "error": "unexpected status output"}
        if data.get("status") == "connected":
            return {"name": "drive", "state": "connected"}
        return {
            "name": "drive",
            "state": "needs_reconnect",
            "connect_url": data.get("connect_url"),
        }

    def list_files(self, query: str, page_size: int = 20) -> list[dict]:
        """Files matching a Drive query. Returns [{id, name,
        mimeType, modifiedTime, webViewLink}]."""
        data = self.cli.run_read(
            "drive", "files", "list",
            "--params",
            json.dumps({"q": query, "pageSize": page_size}),
        )
        data = _require_dict(data, "files list")
        files = data.get("files", [])
        return [
            {
                "id": f.get("id"),
                "name": f.get("name"),
                "mimeType": f.get("mimeType"),
                "modifiedTime": f.get("modifiedTime"),
                "webViewLink": f.get("webViewLink"),
            }
            for f in files
        ]

    def find_resume(self) -> dict | None:
        """The user's resume file in Drive (newest match)."""
        files = self.list_files(
            "name contains 'resume' and trashed=false", page_size=10
        )
        if not files:
            files = self.list_files(
                "name contains 'CV' and trashed=false", page_size=10
            )
        if not files:
            return None
        files.sort(key=lambda f: f.get("modifiedTime") or "", reverse=True)
        return files[0]

    def upload_file(self, local_path: str, name: str | None = None) -> dict:
        """Upload a local file to Drive (the user asked for it).
        Returns {id, name, webViewLink}. Raises ConnectorError
        when the CLI returns no file id."""
        argv = ["drive", "+upload", "--", local_path]
        if name:
            argv += ["--name", name]
        data = _require_dict(self.cli.run(*argv), "upload")
        file_id = data.get("id")
        if not file_id:
            raise ConnectorError(
                "drive", "the Drive call returned no file id", "error"
            )
        return {
            "id": file_id,
            "name": data.get("name", name),
            "webViewLink": data.get("webViewLink"),
        }
