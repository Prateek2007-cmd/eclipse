import { useEffect, useState } from "react";
import { api, type ProofMetrics, type SentEntry } from "../api";

/** Middle pane (plan §5.2, §5.6): the proof view.
 *
 * Honest split: the server never stores raw observations —
 * raw PII never leaves the device and is never written to
 * disk — so there is no "raw" text to show. The views are:
 * what was sent (redacted, with timestamps and token counts),
 * the Diff view (redacted payloads with the placeholder tokens
 * highlighted, plus which PII types were removed), and the
 * redaction proof (recall/precision, 0 recoverable tokens,
 * egress audit). */
export function ProofPane({ token }: { token: string | null }) {
  const [metrics, setMetrics] = useState<ProofMetrics>(null);
  const [sent, setSent] = useState<SentEntry[]>([]);
  const [egress, setEgress] = useState<{ hit_count: number; passed: boolean } | null>(null);
  const [redaction, setRedaction] = useState<{ recoverable_tokens: number | null; passed: boolean | null } | null>(null);
  const [latency, setLatency] = useState<{ p50_s: number | null; target: number | null; p50_within_target: boolean | null } | null>(null);
  const [view, setView] = useState<"sent" | "diff" | "redaction">("sent");
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastOk, setLastOk] = useState<number | null>(null);

  // Sprint 9 fix (review M11): the search is debounced 200ms
  // and lives in its own effect — a keystroke no longer re-runs
  // all five endpoints or resets the poll timer.
  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), 200);
    return () => clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    const load = async () => {
      try {
        const [m, s, e, r, l] = await Promise.all([
          api.proofMetrics(),
          api.whatWasSent(debouncedQuery),
          api.egress(),
          api.redaction(),
          api.latency(),
        ]);
        if (!alive) return;
        setMetrics(m);
        setSent(s.entries);
        setEgress(e);
        setRedaction(r);
        setLatency(l);
        setError(null);
        setLastOk(Date.now());
      } catch (err) {
        if (alive) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (alive) setLoading(false);
      }
    };
    void load();
    const timer = setInterval(load, 5000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [token, debouncedQuery]);

  const m = metrics?.metrics;
  const pct = (v: number | null) =>
    v === null || v === undefined ? "—" : `${Math.round(v * 100)}%`;

  /** Highlight placeholder tokens ([NAME_1], [EMAIL_1], …) in a
   * redacted payload so the Diff view shows exactly what was
   * removed and what took its place. */
  const renderDiff = (text: string) => {
    const parts = text.split(/(\[[A-Z0-9_]+\])/g);
    return parts.map((p, i) =>
      /^\[[A-Z0-9_]+\]$/.test(p) ? (
        <mark key={i} className="placeholder-token">
          {p}
        </mark>
      ) : (
        <span key={i}>{p}</span>
      ),
    );
  };

  return (
    <section aria-label="Proof" className="pane proof-pane">
      <h2>Proof</h2>
      {error && (
        <p role="alert" className="danger">
          {error}{" "}
          {lastOk && (
            <span className="muted">
              Showing data from {new Date(lastOk).toLocaleTimeString()} — it may be stale.
            </span>
          )}
        </p>
      )}
      {loading && <p className="muted">Loading proof…</p>}

      <h3>SIH metrics</h3>
      {m ? (
        <div>
          <table>
            <caption>
              Latest benchmark run
              {metrics?.run_date ? ` — ${metrics.run_date}` : " — no run yet"}
              {metrics?.fixtures ? `, ${metrics.fixtures} fixtures` : ""}
            </caption>
            <thead>
              <tr>
                <th scope="col">Metric</th>
                <th scope="col">Measured</th>
                <th scope="col">Target</th>
              </tr>
            </thead>
            <tbody>
              <tr>
                <th scope="row">Grounding (mock planner)</th>
                <td className="mono">{pct(m.grounding)}</td>
                <td className="mono">≥85%</td>
              </tr>
              <tr>
                <th scope="row">PII recall</th>
                <td className="mono">{pct(m.pii_recall)}</td>
                <td className="mono">≥95%</td>
              </tr>
              <tr>
                <th scope="row">PII precision</th>
                <td className="mono">{pct(m.pii_precision)}</td>
                <td className="mono">≥90%</td>
              </tr>
              <tr>
                <th scope="row">Recoverable tokens</th>
                <td className="mono">{m.redaction_tokens ?? "—"}</td>
                <td className="mono">0</td>
              </tr>
              <tr>
                <th scope="row">Extension models</th>
                <td className="mono">{m.resource_mb ?? "—"} MB</td>
                <td className="mono">≤300 MB</td>
              </tr>
              <tr>
                <th scope="row">Latency p50 (mock planner)</th>
                <td className="mono">{m.latency_p50_s ?? "—"}s</td>
                <td className="mono">≤6s</td>
              </tr>
            </tbody>
          </table>
          {metrics?.honesty && <p className="muted">{metrics.honesty}</p>}
          {metrics?.worst_page && (
            <p className="muted">Worst page: {metrics.worst_page}</p>
          )}
          {metrics?.worst_pii_fixture && (
            <p className="muted">Worst PII fixture: {metrics.worst_pii_fixture}</p>
          )}
        </div>
      ) : (
        !loading && <p className="muted">No benchmark run yet — run evals/sih-benchmark/benchmark.py.</p>
      )}

      <div className="hud" aria-label="Live HUD">
        <span>
          Egress hits: <strong className={egress?.passed ? "success" : "danger"}>{egress?.hit_count ?? "—"}</strong>
        </span>
        <span>
          Redaction: <strong className={redaction?.passed ? "success" : "danger"}>{redaction?.recoverable_tokens ?? "—"} tokens</strong>
        </span>
        <span>
          Latency p50: <strong>{latency?.p50_s ?? "—"}s</strong>
        </span>
      </div>

      <div role="group" aria-label="Proof view">
        <button type="button" onClick={() => setView("sent")} aria-pressed={view === "sent"}>
          What was sent
        </button>
        <button type="button" onClick={() => setView("diff")} aria-pressed={view === "diff"}>
          Diff
        </button>
        <button type="button" onClick={() => setView("redaction")} aria-pressed={view === "redaction"}>
          Redaction proof
        </button>
      </div>

      {view === "sent" ? (
        <div>
          <label htmlFor="sent-search">Search what was sent</label>
          <input
            id="sent-search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="endpoint or model"
          />
          {sent.length === 0 ? (
            <p className="muted">Nothing sent yet. Every outbound payload appears here, redacted.</p>
          ) : (
            <ul className="sent-list">
              {sent.map((entry) => (
                <li key={entry.id}>
                  <div className="mono secondary">
                    {new Date(entry.at * 1000).toLocaleTimeString()} · {entry.endpoint} ·{" "}
                    {entry.model} · {entry.tokens_est} tokens · {entry.ms}ms
                  </div>
                  <pre>{entry.prompt_redacted}</pre>
                  {entry.pii_types.length > 0 && (
                    <p className="muted">PII types in the original (redacted before sending): {entry.pii_types.join(", ")}</p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : view === "diff" ? (
        <div>
          <p className="muted">
            The Diff view shows each redacted payload with its placeholder
            tokens highlighted — what was removed, and what took its place.
            There is no raw side to compare against: raw PII is redacted
            on the device and never stored, so the raw text does not exist
            anywhere to show.
          </p>
          {sent.length === 0 ? (
            <p className="muted">Nothing sent yet.</p>
          ) : (
            <ul className="sent-list">
              {sent.map((entry) => (
                <li key={entry.id}>
                  <div className="mono secondary">
                    {new Date(entry.at * 1000).toLocaleTimeString()} · {entry.endpoint} · {entry.model}
                  </div>
                  <pre>{renderDiff(entry.prompt_redacted)}</pre>
                  {entry.pii_types.length > 0 && (
                    <p className="muted">Removed: {entry.pii_types.join(", ")}</p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <div>
          <p>
            Redaction proof: after redaction, a second PII scan of every payload
            finds <strong className={redaction?.passed ? "success" : "danger"}>{redaction?.recoverable_tokens ?? "—"} recoverable tokens</strong>.
          </p>
          <p className="muted">
            The server never stores raw observations — raw PII is redacted on the
            device before anything reaches the cloud, and the redacted payload is
            all that is kept. That is why there is no raw text to show here.
          </p>
        </div>
      )}
    </section>
  );
}
