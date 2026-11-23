import { Component, lazy, Suspense, useEffect, useRef, useState, type ReactNode } from "react";
import { api, clearToken, getToken, setToken, type QuotaModel } from "./api";
import { AgentView } from "./views/AgentView";
import { MissionsView } from "./views/MissionsView";
import { JobsView } from "./views/JobsView";
import { MailView } from "./views/MailView";
import { WatchesView } from "./views/WatchesView";
import { ConnectorsView } from "./views/ConnectorsView";
import { VaultView } from "./views/VaultView";
import { MemoryView } from "./views/MemoryView";
import { ProofView } from "./views/ProofView";
import { AuditView } from "./views/AuditView";
import "./tokens.css";
import "./app.css";

const BoundaryScene = lazy(() =>
  import("./components/BoundaryScene").then((m) => ({ default: m.BoundaryScene })),
);

type ViewId =
  | "agent"
  | "missions"
  | "jobs"
  | "mail"
  | "watches"
  | "connectors"
  | "vault"
  | "memory"
  | "proof"
  | "audit";

const VIEWS: { id: ViewId; label: string; short: string }[] = [
  { id: "agent", label: "Agent", short: "Ag" },
  { id: "missions", label: "Missions", short: "Mi" },
  { id: "jobs", label: "Jobs", short: "Jo" },
  { id: "mail", label: "Mail", short: "Ma" },
  { id: "watches", label: "Watches & Briefing", short: "Wa" },
  { id: "connectors", label: "Connectors", short: "Co" },
  { id: "vault", label: "Vault", short: "Va" },
  { id: "memory", label: "Memory & RAG", short: "Me" },
  { id: "proof", label: "Proof", short: "Pr" },
  { id: "audit", label: "Audit & Trace", short: "Au" },
];

/** Error boundary (review B1): one view crashing must never
 * blank the whole app again. A failed view shows its own
 * error state; the rest of the workspace keeps working. */
class ViewErrorBoundary extends Component<
  { name: string; children: ReactNode },
  { failed: boolean; message: string }
> {
  state = { failed: false, message: "" };

  static getDerivedStateFromError(err: Error) {
    return { failed: true, message: err.message };
  }

  render() {
    if (this.state.failed) {
      return (
        <section aria-label={this.props.name} className="pane">
          <h2>{this.props.name}</h2>
          <p role="alert" className="danger">
            This view crashed: {this.state.message}. The rest of the workspace
            is still working — fix the view or reload the page.
          </p>
        </section>
      );
    }
    return this.props.children;
  }
}

function NavList({
  view,
  collapsed,
  onNavigate,
}: {
  view: ViewId;
  collapsed: boolean;
  onNavigate: (v: ViewId) => void;
}) {
  return (
    <ul className="nav-list">
      {VIEWS.map((v) => (
        <li key={v.id}>
          <button
            type="button"
            className={v.id === view ? "nav-current" : ""}
            aria-current={v.id === view ? "page" : undefined}
            aria-label={v.label}
            title={v.label}
            onClick={() => onNavigate(v.id)}
          >
            {collapsed ? (
              <span className="nav-short" aria-hidden="true">
                {v.short}
              </span>
            ) : (
              v.label
            )}
          </button>
        </li>
      ))}
    </ul>
  );
}

export default function App() {
  const [token, setTokenState] = useState<string | null>(() => getToken());
  const [tokenInput, setTokenInput] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [tokenRejected, setTokenRejected] = useState(false);
  const [view, setView] = useState<ViewId>("agent");
  const [navCollapsed, setNavCollapsed] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showScene, setShowScene] = useState(false);
  const [scenePaused, setScenePaused] = useState(false);
  const [serverUp, setServerUp] = useState<boolean | null>(null);
  const [quota, setQuota] = useState<QuotaModel[]>([]);
  const drawerRef = useRef<HTMLDialogElement | null>(null);

  const saveToken = () => {
    const t = tokenInput.trim();
    if (!t) return;
    setToken(t);
    setTokenState(t);
    setTokenInput("");
    setTokenRejected(false);
  };

  const forgetToken = () => {
    clearToken();
    setTokenState(null);
    setQuota([]);
    setServerUp(null);
  };

  // Sprint 9 fix (review M18): a 401 anywhere returns the user
  // to the token gate with a plain message — the views no longer
  // strand the user behind 401 banners. The api client fires
  // `eclipse-unauthorized` on a 401; this listener acts on it.
  useEffect(() => {
    const onUnauthorized = () => {
      clearToken();
      setTokenState(null);
      setTokenRejected(true);
    };
    window.addEventListener("eclipse-unauthorized", onUnauthorized);
    return () => window.removeEventListener("eclipse-unauthorized", onUnauthorized);
  }, []);

  // Sprint 9 fix (review M13): the top bar says "Connected" only
  // when the server answers /health — token presence alone is
  // not a connection.
  useEffect(() => {
    if (!token) {
      setServerUp(null);
      setQuota([]);
      return;
    }
    let alive = true;
    const check = async () => {
      try {
        const [health, q] = await Promise.all([api.health(), api.quota()]);
        if (!alive) return;
        setServerUp(health.status === "ok");
        setQuota(q.models);
      } catch {
        if (alive) setServerUp(false);
      }
    };
    void check();
    const timer = setInterval(check, 30000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [token]);

  // The mobile nav is a native modal <dialog> (guide: dialog /
  // navigation-drawer): the browser provides the top layer,
  // Esc handling and focus return.
  useEffect(() => {
    const dialog = drawerRef.current;
    if (!dialog) return;
    if (drawerOpen && !dialog.open) {
      dialog.showModal();
    } else if (!drawerOpen && dialog.open) {
      dialog.close();
    }
  }, [drawerOpen]);

  useEffect(() => {
    const dialog = drawerRef.current;
    if (!dialog) return;
    const onClose = () => setDrawerOpen(false);
    dialog.addEventListener("close", onClose);
    return () => dialog.removeEventListener("close", onClose);
  }, []);

  const navigate = (v: ViewId) => {
    setView(v);
    setDrawerOpen(false);
  };

  const currentLabel = VIEWS.find((v) => v.id === view)?.label ?? "Agent";

  return (
    <div className="app">
      <a href="#main" className="skip-link">
        Skip to main content
      </a>
      <header className="topbar">
        {token && (
          <button
            type="button"
            className="menu-button"
            onClick={() => setDrawerOpen(true)}
            aria-label="Open navigation menu"
            aria-expanded={drawerOpen}
            aria-controls="nav-drawer"
          >
            ☰
          </button>
        )}
        <h1>Eclipse</h1>
        <p className="tagline">The browser agent that sees for you — and eclipses what no one else should see.</p>
        {token && (
          <div className="topbar-hud" role="status" aria-live="polite">
            <span className={serverUp ? "success" : serverUp === false ? "danger" : "muted"}>
              {serverUp ? "● Connected" : serverUp === false ? "● Server unreachable" : "● Checking…"}
            </span>
            {quota.length > 0 && (
              <span className="quota-hud" aria-label="Quota used today">
                {quota.map((m) => (
                  <span
                    key={m.name}
                    className={m.limit > 0 && m.used_today / m.limit >= 0.8 ? "warning" : "muted"}
                    title={`${m.name}: ${m.used_today} of ${m.limit} calls today`}
                  >
                    {m.name} {m.used_today}/{m.limit}
                  </span>
                ))}
              </span>
            )}
          </div>
        )}
        <div className="topbar-actions">
          {token && (
            <button type="button" onClick={() => setShowScene((v) => !v)} aria-pressed={showScene}>
              {showScene ? "Hide the Privacy Boundary" : "Show the Privacy Boundary"}
            </button>
          )}
          {token && (
            <button type="button" onClick={forgetToken}>
              Forget token
            </button>
          )}
        </div>
      </header>

      {!token ? (
        <main id="main" className="token-gate-wrap">
          <div className="token-gate">
            <h2>Connect to the local server</h2>
            {tokenRejected && (
              <p role="alert" className="danger">
                That token was rejected — check <span className="mono">data/.eclipse-token</span> on the server and paste it again.
              </p>
            )}
            <p>
              The workspace talks to the Eclipse server on localhost:8000.
              Paste the server token (from <span className="mono">data/.eclipse-token</span>).
              It is kept in this tab only and never written to a file — close
              the tab and it is gone.
            </p>
            <form
              onSubmit={(e) => {
                e.preventDefault();
                saveToken();
              }}
            >
              <label htmlFor="server-token">Server token</label>
              <div className="token-row">
                <input
                  id="server-token"
                  type={showToken ? "text" : "password"}
                  value={tokenInput}
                  onChange={(e) => setTokenInput(e.target.value)}
                  autoComplete="off"
                />
                <button
                  type="button"
                  onClick={() => setShowToken((v) => !v)}
                  aria-pressed={showToken}
                >
                  {showToken ? "Hide" : "Show"}
                </button>
              </div>
              <button type="submit" className="primary" disabled={!tokenInput.trim()}>
                Connect
              </button>
            </form>
            <p className="muted">
              Local only — the server binds 127.0.0.1, and raw PII never leaves
              this device.
            </p>
          </div>
        </main>
      ) : (
        <div className="shell">
          <nav
            aria-label="Workspace"
            className={`sidebar ${navCollapsed ? "sidebar-collapsed" : ""}`}
          >
            <NavList view={view} collapsed={navCollapsed} onNavigate={navigate} />
            <button
              type="button"
              className="collapse-button"
              onClick={() => setNavCollapsed((v) => !v)}
              aria-label={navCollapsed ? "Expand navigation" : "Collapse navigation"}
              aria-pressed={navCollapsed}
            >
              {navCollapsed ? "»" : "« Collapse"}
            </button>
          </nav>

          <main id="main" className="content">
            {showScene && (
              <section aria-label="Privacy Boundary" className="pane scene-pane">
                <div className="section-header">
                  <h2>The Privacy Boundary</h2>
                  <button
                    type="button"
                    onClick={() => setScenePaused((v) => !v)}
                    aria-pressed={scenePaused}
                  >
                    {scenePaused ? "Resume motion" : "Pause motion"}
                  </button>
                </div>
                <Suspense fallback={<p className="muted">Loading the scene…</p>}>
                  <BoundaryScene paused={scenePaused} />
                </Suspense>
                <p className="muted">
                  Green dots are redacted tokens — they pass through the wall to the
                  planner. Red dots are raw personal data — they bounce off.
                </p>
              </section>
            )}
            <ViewErrorBoundary key={view} name={currentLabel}>
              {view === "agent" && (
                <AgentView token={token} selectedId={selectedId} onSelect={setSelectedId} />
              )}
              {view === "missions" && (
                <MissionsView
                  token={token}
                  onSelectTask={(id) => {
                    setSelectedId(id);
                    setView("agent");
                  }}
                  onGoToJobs={() => setView("jobs")}
                />
              )}
              {view === "jobs" && <JobsView token={token} />}
              {view === "mail" && <MailView token={token} />}
              {view === "watches" && <WatchesView token={token} />}
              {view === "connectors" && <ConnectorsView token={token} />}
              {view === "vault" && <VaultView token={token} />}
              {view === "memory" && <MemoryView token={token} />}
              {view === "proof" && <ProofView token={token} />}
              {view === "audit" && <AuditView token={token} />}
            </ViewErrorBoundary>
          </main>
        </div>
      )}

      <dialog
        id="nav-drawer"
        ref={drawerRef}
        aria-label="Navigation"
        className="nav-drawer"
        closedby="any"
      >
        <div className="drawer-head">
          <h2>Eclipse</h2>
          <button type="button" onClick={() => setDrawerOpen(false)} aria-label="Close menu">
            ✕
          </button>
        </div>
        <NavList view={view} collapsed={false} onNavigate={navigate} />
      </dialog>

      <footer className="statusbar">
        <p role="status" aria-live="polite">
          {token
            ? serverUp === false
              ? "Server unreachable — showing the last data fetched."
              : selectedId && view === "agent"
                ? `Task ${selectedId} selected.`
                : `${currentLabel} view.`
            : "Not connected."}
        </p>
        <p className="muted">Local only — the server binds 127.0.0.1, and raw PII never leaves this device.</p>
      </footer>
    </div>
  );
}
