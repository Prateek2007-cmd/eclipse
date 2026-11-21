"""Vault (Sprint 5, plan §6.7): the encrypted store for secrets.

Two kinds of secret, with different lifetimes:

- Opted-in login passwords: stored encrypted in the Fernet file
  (`data/vault.fernet`, 0600) — only when the user opts in, per
  site. Default is per-task entry (no storage).
- Card number and CVV: entered per task, NEVER stored. They live
  only in the per-task grant (in memory) and are wiped when the
  grant expires or the task ends. OTP: never stored, never in the
  vault at all.

The vault file is Fernet-encrypted (AES-128-CBC + HMAC, via the
`cryptography` library). The key is derived from the user's
password with PBKDF2-HMAC-SHA256 (600,000 iterations, a random
16-byte salt stored in the file header). The password is never
stored, never logged.

A per-task grant is created by POST /vault/unlock (password +
task_id). It expires per task or after 15 minutes, whichever is
first. The vault locks on 10 minutes of idle. Revoke-all is one
click (POST /vault/revoke-all).

Fill is local only: /vault/fill returns success/failure, never
the value. The fill map itself is attached to the execute
response (server/tasks/loop.py) — the local channel to the
extension — only when the vault is unlocked for that task AND
an approval grant exists (approval BEFORE vault fill, §5.4).
"""
