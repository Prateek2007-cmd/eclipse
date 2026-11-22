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
import re

from .base import ConnectorError, GwsCli

# "resume"/"cv" as a whole word in the filename stem.
_WORD_RE = re.compile(r"(?:^|[^a-z0-9])(resume|cv)(?:$|[^a-z0-9])")


def _resume_rank(name: str) -> int:
    """How resume-like a filename is. Drive's `name contains
    'resume'` also matches 'Presumed Innocence' and
    'resume-template', and newest-first alone would pick those
    over resume.pdf — so the name ranks first, the date only
    breaks ties within a rank: 0 = the stem is exactly
    `resume`/`cv`, 1 = the word appears (or the stem starts or
    ends with it, e.g. `JohnSmithResume.pdf`), 2 = a substring
    match only."""
    stem = (name or "").rsplit(".", 1)[0].lower()
    if stem in ("resume", "cv"):
        return 0
    if (
        _WORD_RE.search(stem)
        or stem.startswith(("resume", "cv"))
        or stem.endswith(("resume", "cv"))
    ):
        return 1
    return 2


def _best_match(files: list[dict]) -> dict | None:
    """The best-ranked file, newest within a rank (stable sort:
    newest first, then by rank)."""
    if not files:
        return None
    files.sort(key=lambda f: f.get("modifiedTime") or "", reverse=True)
    files.sort(key=lambda f: _resume_rank(f.get("name") or ""))
    return files[0]


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
        """The user's resume file in Drive: the best name match,
        newest within a rank. A substring-only match ('Presumed
        Innocence') loses to a real CV file; it is returned only
        when nothing better exists."""
        files = self.list_files(
            "name contains 'resume' and trashed=false", page_size=10
        )
        best = _best_match(files)
        if best is not None and _resume_rank(best.get("name") or "") < 2:
            return best
        cv_best = _best_match(
            self.list_files(
                "name contains 'CV' and trashed=false", page_size=10
            )
        )
        if cv_best is not None:
            return cv_best
        return best

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
