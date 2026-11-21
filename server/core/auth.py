"""Localhost token auth. Every request carries X-Eclipse-Token.

The token is generated on server start and stored in a local file with
0600 perms, never logged.
"""
from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, Header, HTTPException

from . import config

TokenDep = Annotated[str | None, Header(alias="X-Eclipse-Token")]


def require_token(token: TokenDep = None) -> None:
    """Dependency: reject any request without the current server token."""
    if token is None or not hmac.compare_digest(token, config.get_token()):
        raise HTTPException(
            status_code=401, detail="missing or invalid X-Eclipse-Token"
        )
