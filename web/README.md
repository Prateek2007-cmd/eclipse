# Web workspace UI

Built in Sprint 9 (2026-11-21). Served by the local server at
`/workspace` (static mount in `server/main.py`).

## Workspace — `web/workspace/`

Vite + React 19 + TypeScript, 3 panes (Task + Plan, Proof, Trace +
Audit), live HUD, proof views, watches + briefing board, and the
lazy-loaded Privacy Boundary 3D scene. 60/30/10 dark:
#070B12 / #12212B / #FF6B35.

**Deviation from the plan (§5): no Tailwind v4 and no shadcn.**
The design system is plain CSS driven by one tokens file
(`src/tokens.css`) — the §5.8 step-1 rule (tokens first, no
hardcoded colours elsewhere) is honoured; the library swap was a
deliberate time/dependency call. The server token lives in the
tab only (sessionStorage) and is never written to a file.

Build: `cd web/workspace && npm run build` (tsc + vite).
Dev: `npm run dev` (port 5173, /api proxy to 127.0.0.1:8000).

**Known gaps:** execute/verify are not wired in the workspace —
execution runs in the extension; the workspace is a monitoring +
control surface. The workspace has not been screenshot-reviewed
in a real browser (the cloud browser cannot reach this VM's
localhost and outbound tunnels are blocked by the sandbox
network policy).

## Proof page — `web/proof.html`

The standalone proof page (Sprint 6): SIH metrics, what-was-sent,
egress audit, redaction proof, latency waterfall. Served at
`/proof`. Single self-contained static page, no build step.

## Landing page — `web/landing.html`

Static HTML landing page (Sprint 9, Antideploy-style): hero with
the tagline, How it works (3 steps), Privacy in code
(redaction before/after), SIH metrics (real benchmark numbers
only), FAQ. No build step; open the file or serve it statically.
