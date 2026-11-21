"""Eclipse server configuration: paths, token, CORS allow-list.

Token: generated on server start, stored in memory and in a local file
with 0600 perms, never logged. The ECLIPSE_TOKEN env override exists
only so tests can use a fixed token.
"""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = REPO_ROOT / "data"

DATA_DIR = Path(os.environ.get("ECLIPSE_DATA_DIR", DEFAULT_DATA_DIR))
TOKEN_FILE = DATA_DIR / ".eclipse-token"

# CORS allow-list: only the extension origin and the localhost workspace.
# The extension origin is set once the extension id is known (Sprint 3).
ALLOWED_ORIGINS = [
    "http://127.0.0.1:5173",
    "http://localhost:5173",
    "http://127.0.0.1:8000",
    "http://localhost:8000",
]
_extension_origin = os.environ.get("ECLIPSE_EXTENSION_ORIGIN")
if _extension_origin:
    ALLOWED_ORIGINS.append(_extension_origin)

_token: str | None = None


def get_token() -> str:
    """Return the server token, generating and persisting it once.

    The file is created with 0600 from the first byte (os.open with
    mode 0o600), never 0644-then-chmod. An existing file with lax
    perms is tightened on read.
    """
    global _token
    if _token is not None:
        return _token
    env_token = os.environ.get("ECLIPSE_TOKEN")
    if env_token:
        _token = env_token
        return _token
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if TOKEN_FILE.exists():
        os.chmod(TOKEN_FILE, stat.S_IRUSR | stat.S_IWUSR)  # 0600
        _token = TOKEN_FILE.read_text().strip()
        if _token:
            return _token
    _token = secrets.token_urlsafe(32)
    fd = os.open(TOKEN_FILE, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, _token.encode())
    finally:
        os.close(fd)
    return _token
