import { useEffect, useState } from "react";
import {
  api,
  type EgressResult,
  type LatencyResult,
  type ProofMetrics,
  type RedactionResult,
  type SentEntry,
} from "../api";
import { EmptyState, ErrorBanner, SectionHeader } from "../components/ui";

/** Proof view (plan §5.2, §5.6).
 *
 * Honest split: the server never stores raw observations —
 * raw PII never leaves the device and is never written to
 * disk — so there is no "raw" text to show. The views are:
 * what was sent (redacted, with timestamps and token counts),
 * the Diff view (redacted payloads with the placeholder tokens
 * highlighted, plus which PII types were removed), the
 * redaction proof (recall/precision, 0 recoverable tokens,
 * egress audit), and the latency waterfall from the latest
 * benchmark run. */
export function ProofView({ token }: { token: string | null }) {
  const [metrics, setMetrics] = useState<ProofMetrics | null>(null);
  const [sent, setSent] = useState<SentEntry[]>([]);
  const [egress, setEgress] = useState<EgressResult | null>(null);
  const [redaction, setRedaction] = useState<RedactionResult | null>(null);
  const [latency, setLatency] = useState<LatencyResult | null>(null);
  const [view, setView] = useState<"sent" | "diff" | "redaction" | "latency">("sent");
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

  const latencyEntries = latency?.per_fixture
    ? Object.entries(latency.per_fixture).sort((a, b) => b[1].elapsed_s - a[1].elapsed_s)
    : [];
  const maxElapsed = latencyEntries.length ? latencyEntries[0][1].elapsed_s : 0;

  return (
    <div className="view proof-view">
      <div className="view-head">
        <h2>Proof</h2>
        <p className="muted">
          Every number here comes from a benchmark run or a live server
          check. When no benchmark has run, the metrics are empty — never
          invented.
        </p>
      </div>
      {error && <ErrorBanner error={error} lastOk={lastOk} />}
      {loading && <p className="muted">Loading proof…</p>}

      <section aria-label="SIH metrics" className="pane">
        <SectionHeader title="SIH metrics" />
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
          !loading && (
            <EmptyState>
              {metrics?.reason ??
                "No benchmark run yet — run evals/sih-benchmark/benchmark.py."}
            </EmptyState>
          )
        )}

        <div className="hud" aria-label="Live HUD">
          <span>
            Egress hits:{" "}
            <strong className={egress?.passed ? "success" : "danger"}>
              {egress?.hit_count ?? "—"}
            </strong>
          </span>
          <span>
            Redaction:{" "}
            <strong className={redaction?.passed ? "success" : "danger"}>
              {redaction?.recoverable_tokens ?? "—"} tokens
            </strong>
          </span>
          <span>
            Latency p50: <strong>{latency?.p50_s ?? "—"}s</strong>
          </span>
        </div>
      </section>

      <section aria-label="Proof detail" className="pane">
        <div
          role="group"
          aria-label="Proof view"
          className="view-tabs"
        >
          <button type="button" onClick={() => setView("sent")} aria-pressed={view === "sent"}>
            What was sent
          </button>
          <button type="button" onClick={() => setView("diff")} aria-pressed={view === "diff"}>
            Diff
          </button>
          <button
            type="button"
            onClick={() => setView("redaction")}
            aria-pressed={view === "redaction"}
          >
            Redaction proof
          </button>
          <button
            type="button"
            onClick={() => setView("latency")}
            aria-pressed={view === "latency"}
          >
            Latency
          </button>
        </div>

        {view === "sent" && (
          <div>
            <label htmlFor="sent-search">Search what was sent</label>
            <input
              id="sent-search"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="endpoint or model"
            />
            {sent.length === 0 ? (
              <EmptyState>
                Nothing sent yet. Every outbound payload appears here, redacted.
              </EmptyState>
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
                      <p className="muted">
                        PII types in the original (redacted before sending):{" "}
                        {entry.pii_types.join(", ")}
                      </p>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {view === "diff" && (
          <div>
            <p className="muted">
              The Diff view shows each redacted payload with its placeholder
              tokens highlighted — what was removed, and what took its place.
              There is no raw side to compare against: raw PII is redacted
              on the device and never stored, so the raw text does not exist
              anywhere to show.
            </p>
            {sent.length === 0 ? (
              <EmptyState>Nothing sent yet.</EmptyState>
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
        )}

        {view === "redaction" && (
          <div>
            <p>
              Redaction proof: after redaction, a second PII scan of every
              payload finds{" "}
              <strong className={redaction?.passed ? "success" : "danger"}>
                {redaction?.recoverable_tokens ?? "—"} recoverable tokens
              </strong>
              .
            </p>
            <p className="muted">
              The server never stores raw observations — raw PII is redacted
              on the device before anything reaches the cloud, and the
              redacted payload is all that is kept. That is why there is no
              raw text to show here.
            </p>
            {redaction?.note && <p className="muted">{redaction.note}</p>}
            {redaction?.reason && <p className="muted">{redaction.reason}</p>}
            {redaction && Object.keys(redaction.label_based_not_regex).length > 0 && (
              <div>
                <h3>Caught by label, not by regex</h3>
                <p className="muted">
                  Names, addresses, CVV, OTP and passwords are caught by
                  field label, not by a pattern. These fixtures had such
                  values:
                </p>
                <ul className="plain-list">
                  {Object.entries(redaction.label_based_not_regex).map(([fixture, types]) => (
                    <li key={fixture}>
                      <span className="mono">{fixture}</span> — {types.join(", ")}
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <h3>Egress audit</h3>
            {egress ? (
              <div>
                <p>
                  {egress.passed ? (
                    <span className="success">
                      Passed — {egress.checked} outbound calls checked, {egress.hit_count} hits.
                    </span>
                  ) : (
                    <span className="danger">
                      FAILED — {egress.hit_count} secret{egress.hit_count === 1 ? "" : "s"} left
                      the device. Stop the run and block the payload.
                    </span>
                  )}
                </p>
                <p className="muted">{egress.note}</p>
                {egress.hits.length > 0 && (
                  <table>
                    <caption>Hits carry type, entry and endpoint only — never the value</caption>
                    <thead>
                      <tr>
                        <th scope="col">Type</th>
                        <th scope="col">Entry</th>
                        <th scope="col">Endpoint</th>
                      </tr>
                    </thead>
                    <tbody>
                      {egress.hits.map((hit, i) => (
                        <tr key={`${hit.entry_id}-${i}`}>
                          <td>{hit.type}</td>
                          <td className="mono">{hit.entry_id}</td>
                          <td className="mono">{hit.endpoint}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                )}
                <p className="muted">
                  Patterns checked: {egress.patterns.join(", ") || "—"}
                </p>
              </div>
            ) : (
              <p className="muted">Loading egress audit…</p>
            )}
          </div>
        )}

        {view === "latency" && (
          <div>
            {latency?.per_fixture === null || latency?.per_fixture === undefined ? (
              <EmptyState>
                {latency?.reason ??
                  "No benchmark run yet — run evals/sih-benchmark/benchmark.py."}
              </EmptyState>
            ) : (
              <div>
                <p>
                  Step time p50{" "}
                  <strong>{latency.p50_s ?? "—"}s</strong>
                  {latency.mean_s !== null && (
                    <>
                      {" "}· mean <strong>{latency.mean_s}s</strong>
                    </>
                  )}
                  {latency.max_s !== null && (
                    <>
                      {" "}· max <strong>{latency.max_s}s</strong>
                    </>
                  )}
                  {latency.target !== null && (
                    <>
                      {" "}· target ≤{latency.target}s —{" "}
                      {latency.p50_within_target ? (
                        <span className="success">within target</span>
                      ) : (
                        <span className="danger">over target</span>
                      )}
                    </>
                  )}
                </p>
                {latency.planner && <p className="muted">Planner: {latency.planner}</p>}
                {latency.note && <p className="muted">{latency.note}</p>}
                <h3>Per fixture (slowest first)</h3>
                <ul className="waterfall">
                  {latencyEntries.map(([name, f]) => (
                    <li key={name}>
                      <div className="waterfall-row">
                        <span className="mono">{name}</span>
                        <span className="mono">
                          {f.elapsed_s}s · {f.steps} steps · {f.verified} verified
                        </span>
                      </div>
                      <div
                        className="waterfall-bar"
                        style={{
                          width: `${maxElapsed > 0 ? (f.elapsed_s / maxElapsed) * 100 : 0}%`,
                        }}
                        aria-hidden="true"
                      />
                      {Object.keys(f.errors).length > 0 && (
                        <ul className="plain-list">
                          {Object.entries(f.errors).map(([step, msg]) => (
                            <li key={step} className="muted">
                              {step}: {msg}
                            </li>
                          ))}
                        </ul>
                      )}
                    </li>
                  ))}
                </ul>
                {latency.worst_fixture && (
                  <p className="muted">
                    Worst fixture: {latency.worst_fixture}
                    {latency.worst_fixture_s !== null ? ` (${latency.worst_fixture_s}s)` : ""}
                  </p>
                )}
              </div>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
