import { Component, lazy, Suspense, useEffect, useState, type ReactNode } from "react";
import { api, clearToken, getToken, setToken } from "./api";
import { TaskPane } from "./components/TaskPane";
import { ProofPane } from "./components/ProofPane";
import { TracePane } from "./components/TracePane";
import { WatchPanel } from "./components/WatchPanel";
import "./tokens.css";
import "./app.css";

const BoundaryScene = lazy(() =>
  import("./components/BoundaryScene").then((m) => ({ default: m.BoundaryScene })),
);

/** Error boundary (review B1): one pane crashing must never
 * blank the whole app again. A failed pane shows its own
 * error state; the rest of the workspace keeps working. */
class PaneErrorBoundary extends Component<
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
            This pane crashed: {this.state.message}. The rest of the workspace
            is still working — fix the pane or reload the page.
          </p>
        </section>
      );
    }
    return this.props.children;
  }
}

export default function App() {
  const [token, setTokenState] = useState<string | null>(() => getToken());
  const [tokenInput, setTokenInput] = useState("");
  const [showToken, setShowToken] = useState(false);
  const [tokenRejected, setTokenRejected] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showScene, setShowScene] = useState(false);
  const [scenePaused, setScenePaused] = useState(false);
  const [serverUp, setServerUp] = useState<boolean | null>(null);

  const saveToken = () => {
    const t = tokenInput.trim();
    if (!t) return;
    setToken(t);
    setTokenState(t);
    setTokenInput("");
    setTokenRejected(false);
  };

  // Sprint 9 fix (review M18): a 401 anywhere returns the user
  // to the token gate with a plain message — the panes no longer
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

  // Sprint 9 fix (review M13): the footer says "Connected" only
  // when the server answers /health — token presence alone is
  // not a connection.
  useEffect(() => {
    if (!token) {
      setServerUp(null);
      return;
    }
    let alive = true;
    const check = async () => {
      try {
        await api.health();
        if (alive) setServerUp(true);
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

  return (
    <div className="app">
      <a href="#main" className="skip-link">
        Skip to main content
      </a>
      <header className="topbar">
        <h1>Eclipse</h1>
        <p className="tagline">The browser agent that sees for you — and eclipses what no one else should see.</p>
        <div className="topbar-actions">
          <button type="button" onClick={() => setShowScene((v) => !v)} aria-pressed={showScene}>
            {showScene ? "Hide the Privacy Boundary" : "Show the Privacy Boundary"}
          </button>
          {token && (
            <button
              type="button"
              onClick={() => {
                clearToken();
                setTokenState(null);
              }}
            >
              Forget token
            </button>
          )}
        </div>
      </header>

      {!token ? (
        <main id="main" className="token-gate">
          <h2>Connect to the local server</h2>
          {tokenRejected && (
            <p role="alert" className="danger">
              That token was rejected — check <span className="mono">data/.eclipse-token</span> on the server and paste it again.
            </p>
          )}
          <p>
            The workspace talks to the Eclipse server on localhost:8000. Paste the
            server token (from <span className="mono">data/.eclipse-token</span>).
            It is kept in this tab only and never written to a file.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              saveToken();
            }}
          >
            <label htmlFor="server-token">Server token</label>
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
              {showToken ? "Hide token" : "Show token"}
            </button>
            <button type="submit" className="primary" disabled={!tokenInput.trim()}>
              Connect
            </button>
          </form>
        </main>
      ) : (
        <main id="main" className="workspace">
          <PaneErrorBoundary name="Task & Plan">
            <TaskPane token={token} selectedId={selectedId} onSelect={setSelectedId} />
          </PaneErrorBoundary>
          <PaneErrorBoundary name="Proof">
            <ProofPane token={token} />
          </PaneErrorBoundary>
          <PaneErrorBoundary name="Trace & Audit">
            <TracePane token={token} />
          </PaneErrorBoundary>
          <PaneErrorBoundary name="Watches & Briefing">
            <WatchPanel token={token} />
          </PaneErrorBoundary>
          {showScene && (
            <section aria-label="Privacy Boundary" className="pane scene-pane">
              <h2>The Privacy Boundary</h2>
              <button
                type="button"
                onClick={() => setScenePaused((v) => !v)}
                aria-pressed={scenePaused}
              >
                {scenePaused ? "Resume motion" : "Pause motion"}
              </button>
              <Suspense fallback={<p className="muted">Loading the scene…</p>}>
                <BoundaryScene paused={scenePaused} />
              </Suspense>
              <p className="muted">
                Green dots are redacted tokens — they pass through the wall to the
                planner. Red dots are raw personal data — they bounce off.
              </p>
            </section>
          )}
        </main>
      )}

      <footer className="statusbar">
        <p role="status" aria-live="polite">
          {token
            ? serverUp === false
              ? "Server unreachable — showing the last data fetched."
              : selectedId
                ? `Task ${selectedId} selected. Steps, proof and audit update live.`
                : "Connected. Pick a task or start one."
            : "Not connected."}
        </p>
        <p className="muted">Local only — the server binds 127.0.0.1, and raw PII never leaves this device.</p>
      </footer>
    </div>
  );
}
