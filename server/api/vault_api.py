"""Vault API (Sprint 5, plan §6.1/§6.7).

- POST /vault/create {password} → create the vault file with
  this password (fails when a vault already exists). The first
  unlock no longer creates the vault — a mistyped first password
  must not silently become the password.
- POST /vault/unlock {password, task_id, card?} → unlock the
  vault file and create a per-task grant (15-minute expiry).
  The optional card (number, expiry, cvv) lives only in the
  grant, in memory — never in the vault file, never in SQLite.
- POST /vault/lock {task_id?} → drop the key, revoke the task's
  grants (or all grants when no task_id is given).
- POST /vault/fill {task_id, origin, field} → whether a fill is
  possible. Returns success/failure ONLY, never the value.
- POST /vault/revoke-all → revoke every grant in one click.
- GET /vault/status?task_id= → unlocked (the Fernet key is in
  memory), has_grants, grants, expiry — never a secret value.

The vault file is one Fernet file per data dir (server/vault/
store.py). The fill map itself is attached to the execute
response (server/tasks/loop.py) — the local channel to the
extension — only when the vault is unlocked for that task AND
an approval grant exists (approval BEFORE vault fill, §5.4).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..core import db
from ..core.auth import require_token
from ..tasks import loop
from ..vault import grants as grant_store
from ..vault.store import VAULT_FILE, Vault, VaultError

router = APIRouter()

# The process-wide vault (one Fernet file, unlocked in memory).
VAULT = Vault()


class CreateIn(BaseModel):
    password: str = Field(..., min_length=8, max_length=500)


class UnlockIn(BaseModel):
    password: str = Field(..., min_length=1, max_length=500)
    task_id: str = Field(..., min_length=1, max_length=100)
    # Per-task card details (entered per task, never stored).
    card_number: str | None = Field(default=None, max_length=25)
    card_expiry: str | None = Field(default=None, max_length=10)
    card_cvv: str | None = Field(default=None, max_length=6)


class LockIn(BaseModel):
    task_id: str | None = Field(default=None, max_length=100)


class FillIn(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=100)
    origin: str = Field(..., min_length=1, max_length=300)
    field: str = Field(..., min_length=1, max_length=100)


class StorePasswordIn(BaseModel):
    task_id: str = Field(..., min_length=1, max_length=100)
    origin: str = Field(..., min_length=1, max_length=300)
    field: str = Field(..., min_length=1, max_length=100)
    value: str = Field(..., min_length=1, max_length=500)


@router.post("/vault/create")
def create(body: CreateIn, _token: str = Depends(require_token)):
    """Create the vault file with this password. Fails when a
    vault already exists (unlock it instead)."""
    try:
        VAULT.create(body.password)
    except VaultError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "vault_file": VAULT_FILE}


@router.post("/vault/unlock")
def unlock(body: UnlockIn, _token: str = Depends(require_token)):
    """Unlock the vault file with the password and create a
    per-task grant. A wrong password is a 401; a missing vault
    is a 404 (create one first); a missing task is a 404."""
    conn = db.connect()
    try:
        if loop.get_task(conn, body.task_id) is None:
            raise HTTPException(status_code=404, detail=f"no task {body.task_id}")
    finally:
        conn.close()
    try:
        VAULT.unlock(body.password)
    except VaultError as exc:
        status = 404 if "no vault yet" in str(exc) else 401
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    card = None
    if body.card_number:
        card = {
            "number": body.card_number,
            "expiry": body.card_expiry,
            "cvv": body.card_cvv,
        }
    grant = grant_store.GRANTS.create(body.task_id, card)
    return {
        "task_id": body.task_id,
        "grant_id": grant.id,
        "expires_at": grant.expires_at,
        "expires_in_s": grant_store.GRANT_TTL_S,
        "card_last4": grant.card_last4,
    }


@router.post("/vault/lock")
def lock(body: LockIn, _token: str = Depends(require_token)):
    """Drop the key from memory and revoke the task's grants (or
    all grants when no task_id is given)."""
    VAULT.lock()
    if body.task_id:
        revoked = grant_store.GRANTS.revoke_task(body.task_id)
    else:
        revoked = grant_store.GRANTS.revoke_all()
    return {"locked": True, "revoked": revoked}


@router.post("/vault/fill")
def fill(body: FillIn, _token: str = Depends(require_token)):
    """Whether a fill is possible for (task, origin, field).
    Returns success/failure ONLY — never the value. The value
    itself travels only in the execute response, to the extension,
    when the vault is unlocked for the task and an approval grant
    exists."""
    if not VAULT.unlocked:
        return {"ok": False, "reason": "vault is locked"}
    grant = grant_store.GRANTS.get(body.task_id)
    if grant is None:
        return {"ok": False, "reason": "no valid grant for this task"}
    grant.touch()
    field = body.field.lower()
    if field in ("card_number", "card", "cvv"):
        if grant.card is None:
            return {"ok": False, "reason": "no card entered for this task"}
        return {"ok": True, "has_card": True, "card_last4": grant.card_last4}
    if VAULT.get_password(body.origin, body.field) is not None:
        return {"ok": True, "has_password": True}
    return {"ok": False, "reason": f"no stored value for {body.field} at {body.origin}"}


@router.post("/vault/revoke-all")
def revoke_all(_token: str = Depends(require_token)):
    """Revoke every grant in one click (§5.4)."""
    revoked = grant_store.GRANTS.revoke_all()
    return {"revoked": revoked}


@router.get("/vault/status")
def status(task_id: str | None = None, _token: str = Depends(require_token)):
    """Unlocked (the Fernet key is in memory), has_grants,
    grants, expiry — never a secret value. "unlocked" and
    "has_grants" are different things: the key can be in memory
    with no live grant (after revoke-all), and a grant can be
    live only while the key is in memory."""
    return {
        "vault_file": VAULT_FILE,
        "unlocked": VAULT.unlocked,
        **grant_store.GRANTS.status(task_id),
    }


# Fields that must never be stored (§6.7: card number and CVV
# are entered per task, never stored).
NEVER_STORED_FIELDS = {"card_number", "card", "cvv"}


@router.post("/vault/store-password")
def store_password(body: StorePasswordIn, _token: str = Depends(require_token)):
    """Store an opted-in login password for (origin, field).
    Only call this when the user opted in for this site — the
    default is per-task entry (no storage). Card numbers and
    CVVs are refused: they are never stored, by design."""
    if body.field.lower() in NEVER_STORED_FIELDS:
        raise HTTPException(
            status_code=400,
            detail=f"field {body.field} is never stored (enter it per task)",
        )
    if not VAULT.unlocked:
        raise HTTPException(status_code=409, detail="vault is locked (unlock first)")
    grant = grant_store.GRANTS.get(body.task_id)
    if grant is None:
        raise HTTPException(status_code=409, detail="no valid grant for this task")
    VAULT.store_password(body.origin, body.field, body.value)
    return {"ok": True, "origin": body.origin, "field": body.field}
