import { useEffect, useState } from "react";
import { api, type BriefingCard, type Watch } from "../api";

/** Watches + morning briefing (plan §8, §5.3 WatchCard/BriefingCard). */
export function WatchPanel({ token }: { token: string | null }) {
  const [watches, setWatches] = useState<Watch[]>([]);
  const [briefing, setBriefing] = useState<BriefingCard[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [building, setBuilding] = useState(false);
  const [muting, setMuting] = useState<string | null>(null);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    const load = async () => {
      try {
        const w = await api.watches();
        if (!alive) return;
        setWatches(w.watches);
        setError(null);
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (alive) setLoading(false);
      }
    };
    // Sprint 9 fix (review M5): load the latest stored briefing
    // on mount, so a returning user sees it instead of "No
    // briefing built yet". A 404 means none exists — not an error.
    const loadBriefing = async () => {
      try {
        const b = await api.briefingLatest();
        if (alive) setBriefing(b.cards);
      } catch {
        if (alive) setBriefing([]);
      }
    };
    void load();
    void loadBriefing();
    const timer = setInterval(load, 15000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [token]);

  const loadBriefing = async () => {
    setBuilding(true);
    try {
      const b = await api.buildBriefing();
      setBriefing(b.cards);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBuilding(false);
    }
  };

  const muteWatch = async (id: string) => {
    setMuting(id);
    try {
      await api.muteWatch(id, 60);
      const w = await api.watches();
      setWatches(w.watches);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setMuting(null);
    }
  };

  const deleteWatch = async (id: string) => {
    setMuting(id);
    try {
      await api.deleteWatch(id);
      const w = await api.watches();
      setWatches(w.watches);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setMuting(null);
    }
  };

  return (
    <section aria-label="Watches and briefing" className="pane watch-pane">
      <h2>Watches &amp; Briefing</h2>
      {error && (
        <p role="alert" className="danger">
          {error}
        </p>
      )}
      {loading && <p className="muted">Loading watches…</p>}
      {watches.length === 0 ? (
        !loading && (
          <p className="muted">
            No watches yet. Watches only run while Chrome is open — they check
            pages in open tabs on a chrome.alarms schedule, never from the server.
          </p>
        )
      ) : (
        <ul className="watch-list">
          {watches.map((w) => (
            <li key={w.id} className={w.status === "broken" ? "watch-broken" : ""}>
              <div>{w.url}</div>
              <div className="secondary">
                {w.condition} · every {w.interval_min} min ·{" "}
                {w.last_value ?? "no baseline yet"}
              </div>
              <div className="muted mono">
                {w.status}
                {w.last_checked ? ` · checked ${new Date(w.last_checked * 1000).toLocaleTimeString()}` : ""}
              </div>
              <div className="task-actions">
                <button
                  type="button"
                  onClick={() => void muteWatch(w.id)}
                  disabled={muting !== null || w.status === "muted"}
                >
                  {muting === w.id ? "Muting…" : w.status === "muted" ? "Muted" : "Mute"}
                </button>
                <button
                  type="button"
                  onClick={() => void deleteWatch(w.id)}
                  disabled={muting !== null}
                >
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
      <h3>Morning briefing</h3>
      <button type="button" onClick={() => void loadBriefing()} disabled={building}>
        {building ? "Building…" : "Build briefing now"}
      </button>
      {briefing.length === 0 ? (
        <p className="muted">No briefing built yet. A briefing never sends mail — it shows, you act.</p>
      ) : (
        <ul className="briefing-list">
          {briefing.map((c, i) => (
            <li key={`${c.kind}-${i}`}>
              <strong>{c.title}</strong>
              <p>{c.body}</p>
              {c.action && c.action_label && (
                // Sprint 9 fix (review M5): offer cards carry a
                // Gmail id, not a URL — render those as plain text,
                // never an <a> with no href.
                c.action.startsWith("http") ? (
                  <a href={c.action}>{c.action_label}</a>
                ) : (
                  <span className="muted">{c.action_label}</span>
                )
              )}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
