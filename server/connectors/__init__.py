"""Connectors (Sprint 7, plan §7): Gmail → Calendar → Drive.

Max 3 connectors, each independent — one failing never kills a
task. Build order: Gmail first, Calendar second, Drive third.

OAuth design (§7.4), stated honestly: Eclipse does NOT hold the
Google OAuth tokens. The tokens live in the platform connector
store, which Eclipse cannot read — the server reaches Gmail,
Calendar and Drive by shelling out to the platform's
`hatch_gws_cli`, which holds the grant. Eclipse's own vault
(Fernet file) holds only Eclipse-managed secrets, never Google
tokens. So:
- Refresh is the platform's job; when the CLI reports the
  account is not connected, Eclipse marks the connector
  "needs_reconnect" and surfaces the platform's connect URL.
- Disconnect in Eclipse only stops Eclipse using the connector;
  revoking the grant itself happens in the platform Settings
  (the disconnect URL is surfaced, never faked).
- Per-connector on/off lives in Eclipse's SQLite (registry.py).
- Failure modes: auth expired → reconnect card; quota hit → wait
  and retry once, then skip; API down → skip and tell the user.
  Never retry a send.
"""
