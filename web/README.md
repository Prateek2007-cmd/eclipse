# Web workspace UI

Built in Sprint 9 (2026-11-21). Served by the local server at
`/workspace` (static mount in `server/main.py`).

## Workspace — `web/workspace/`

Vite + React 19 + TypeScript, rebuilt 2026-11-22 as a proper
agent console: a collapsible sidebar (mobile drawer) with nine
views — Agent, Jobs, Mail, Watches & Briefing, Connectors,
Vault, Memory & RAG, Proof, Audit & Trace — a top bar with
connection status (from `/health`, not token presence) and a
quota HUD, and the lazy-loaded Privacy Boundary 3D scene.
60/30/10 dark: #070B12 / #12212B / #FF6B35.

**Deviation from the plan (§5): no Tailwind v4 and no shadcn.**
The design system is plain CSS driven by one tokens file
(`src/tokens.css`) — the §5.8 step-1 rule (tokens first, no
hardcoded colours elsewhere) is honoured; the library swap was a
deliberate time/dependency call. The server token lives in the
tab only (sessionStorage) and is never written to a file.
Confirmations use the native `<dialog>` element; the API client
(`src/api.ts`) covers every endpoint in `server/api/`, typed
from the route handlers — the server is the contract.

Build: `cd web/workspace && npm ci && npm run build` (tsc + vite).
Dev: `npm run dev` (port 5173, proxies /tasks, /proof, /audit,
/vault, /monitor, /jobs, /mail, /rag, /memory, /connectors,
/health, /metrics, /quota to 127.0.0.1:8000).

**Known gaps:** execute/verify are typed in `api.ts` but not
wired to any UI button — they need a live browser observation
({url, elements}) that only the extension produces, so a button
would send a fabricated observation. Execution runs in the
extension; the workspace is a monitoring + control surface.
Screenshot-reviewed in a real browser on 2026-11-21 ~17:1x IST
(Playwright Chromium headless on this VM): all 4 panes render,
metrics show real numbers, the plan timeline shows real step
descriptions, the 3D scene renders via WebGL. Screenshots:
`docs/screenshots/` (PNGs, regenerate after cloning). The
2026-11-22 rebuild has NOT been screenshot-reviewed yet.

## Proof page — `web/proof.html`

The standalone proof page (Sprint 6): SIH metrics, what-was-sent,
egress audit, redaction proof, latency waterfall. Served at
`/proof`. Single self-contained static page, no build step.

## Landing page — `web/landing.html`

Static HTML landing page (Sprint 9, Antideploy-style): hero with
the tagline, How it works (3 steps), Privacy in code
(redaction before/after), SIH metrics (real benchmark numbers
only), FAQ. No build step; open the file or serve it statically.
