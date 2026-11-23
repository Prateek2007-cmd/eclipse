import { useEffect, useState, type CSSProperties } from "react";
import {
  api,
  type EgressResult,
  type LatencyResult,
  type ProofMetrics,
  type RedactionResult,
  type SentEntry,
} from "../api";
import {
  Disclosure,
  EmptyState,
  ErrorBanner,
  RelativeTime,
  SectionHeader,
  Spinner,
  StatusChip,
  SummaryChips,
} from "../components/ui";
import "./ProofView.css";

/** One metric as a stat row: a big measured number, a short
 * label, and what it compares to. `within` is null when the
 * metric wasn't measured — no chip, no invented verdict. */
type StatRow = {
  label: string;
  value: string;
  compare: string;
  detail?: string;
  within: boolean | null;
};

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
  /** A fraction as a percent — one decimal when it isn't
   * whole (72.2%), none when it is (100%). */
  const pct = (v: number | null | undefined) =>
    v === null || v === undefined ? "—" : `${Number((v * 100).toFixed(1))}%`;

  /** Targets come from the API (server/api/proof_api.py
   * returns the full key set even with no run); the fallbacks
   * are the same values, for a server that omits a key. */
  const t = metrics?.targets ?? {};
  const targetGrounding = t.visual_grounding_first_try ?? 0.85;
  const targetRecall = t.pii_recall ?? 0.95;
  const targetPrecision = t.pii_precision ?? 0.9;
  const targetTokens = t.redaction_recoverable_tokens ?? 0;
  const targetMb = t.resource_mb ?? 300;
  const targetLatency = t.latency_p50_s ?? 6;

  /** The six SIH metrics as stat rows — only what the API
   * returned. The grounding detail ("13 of 18 fixtures") is
   * arithmetic on the returned fraction and fixture count. */
  const statRows: StatRow[] = m
    ? [
        {
          label: "Grounding (mock planner)",
          value: pct(m.grounding),
          compare: `Target ≥${pct(targetGrounding)}`,
          detail:
            m.grounding !== null && metrics?.fixtures
              ? `${Math.round(m.grounding * metrics.fixtures)} of ${metrics.fixtures} fixtures`
              : undefined,
          within: m.grounding === null ? null : m.grounding >= targetGrounding,
        },
        {
          label: "PII recall",
          value: pct(m.pii_recall),
          compare: `Target ≥${pct(targetRecall)}`,
          within: m.pii_recall === null ? null : m.pii_recall >= targetRecall,
        },
        {
          label: "PII precision",
          value: pct(m.pii_precision),
          compare: `Target ≥${pct(targetPrecision)}`,
          within: m.pii_precision === null ? null : m.pii_precision >= targetPrecision,
        },
        {
          label: "Recoverable tokens",
          value: m.redaction_tokens === null ? "—" : String(m.redaction_tokens),
          compare: `Target ${targetTokens}`,
          within: m.redaction_tokens === null ? null : m.redaction_tokens <= targetTokens,
        },
        {
          label: "Extension models",
          value: m.resource_mb === null ? "—" : `${m.resource_mb} MB`,
          compare: `Target ≤${targetMb} MB`,
          within: m.resource_mb === null ? null : m.resource_mb <= targetMb,
        },
        {
          label: "Latency p50 (mock planner)",
          value: m.latency_p50_s === null ? "—" : `${m.latency_p50_s}s`,
          compare: `Target ≤${targetLatency}s`,
          within: m.latency_p50_s === null ? null : m.latency_p50_s <= targetLatency,
        },
      ]
    : [];

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
      {loading && <Spinner label="Loading proof" />}

      <section aria-label="SIH metrics" className="pane">
        <SectionHeader title="SIH metrics" />
        {m ? (
          <div>
            <p className="muted proof-runline">
              Latest benchmark run
              {metrics?.run_date ? ` — ${metrics.run_date}` : " — no run yet"}
              {metrics?.fixtures ? `, ${metrics.fixtures} fixtures` : ""}
            </p>
            <ul className="stat-rows">
              {statRows.map((row) => (
                <li key={row.label} className="stat-row">
                  <div className="stat-main">
                    <span className="stat-value num">{row.value}</span>
                    <span className="stat-label">{row.label}</span>
                  </div>
                  <div className="stat-compare">
                    <span className="muted">
                      {row.compare}
                      {row.detail ? ` · ${row.detail}` : ""}
                    </span>
                    {row.within === null ? (
                      <span className="muted">Not measured</span>
                    ) : (
                      <StatusChip tone={row.within ? "ok" : "bad"}>
                        {row.within ? "Within target" : "Over target"}
                      </StatusChip>
                    )}
                  </div>
                </li>
              ))}
            </ul>
            {metrics?.honesty && <p className="muted proof-note">{metrics.honesty}</p>}
            {metrics?.worst_page && (
              <p className="muted proof-note">Worst page: {metrics.worst_page}</p>
            )}
            {metrics?.worst_pii_fixture && (
              <p className="muted proof-note">Worst PII fixture: {metrics.worst_pii_fixture}</p>
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

        <div className="hud proof-hud" aria-label="Live HUD">
          <span>
            Egress hits:{" "}
            <strong className={`num ${egress?.passed ? "success" : "danger"}`}>
              {egress?.hit_count ?? "—"}
            </strong>
          </span>
          <span>
            Redaction:{" "}
            <strong className={`num ${redaction?.passed ? "success" : "danger"}`}>
              {redaction?.recoverable_tokens ?? "—"} tokens
            </strong>
          </span>
          <span>
            Latency p50: <strong className="num">{latency?.p50_s ?? "—"}s</strong>
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
                  <li key={entry.id} className="sent-entry">
                    <div className="sent-meta">
                      <RelativeTime ts={entry.at} />
                      <span className="mono sent-endpoint">{entry.endpoint}</span>
                      <span className="mono">{entry.model}</span>
                      <span className="num">
                        {entry.tokens_est} tokens · {entry.ms}ms
                      </span>
                    </div>
                    <pre>{entry.prompt_redacted}</pre>
                    {entry.pii_types.length > 0 && (
                      <div className="sent-pii">
                        <span className="muted">Redacted before sending:</span>
                        <SummaryChips
                          items={entry.pii_types.map((t) => ({ label: "", value: t }))}
                        />
                      </div>
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
                  <li key={entry.id} className="sent-entry">
                    <div className="sent-meta">
                      <RelativeTime ts={entry.at} />
                      <span className="mono sent-endpoint">{entry.endpoint}</span>
                      <span className="mono">{entry.model}</span>
                    </div>
                    <pre>{renderDiff(entry.prompt_redacted)}</pre>
                    {entry.pii_types.length > 0 && (
                      <div className="sent-pii">
                        <span className="muted">Removed:</span>
                        <SummaryChips
                          items={entry.pii_types.map((t) => ({ label: "", value: t }))}
                        />
                      </div>
                    )}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}

        {view === "redaction" && (
          <div className="redaction-tab">
            <div className="proof-line">
              <StatusChip
                tone={redaction?.passed === true ? "ok" : redaction?.passed === false ? "bad" : "plain"}
              >
                {redaction?.passed === true
                  ? "Redaction passed"
                  : redaction?.passed === false
                    ? "Redaction failed"
                    : "No check yet"}
              </StatusChip>
              <SummaryChips
                items={[
                  {
                    label: "Recoverable tokens",
                    value: String(redaction?.recoverable_tokens ?? "—"),
                  },
                  { label: "Calls checked", value: String(egress?.checked ?? "—") },
                  { label: "Egress hits", value: String(egress?.hit_count ?? "—") },
                ]}
              />
            </div>
            <Disclosure summary="How this is checked">
              <p className="muted">
                After redaction, a second PII scan of every payload finds{" "}
                {redaction?.recoverable_tokens ?? "—"} recoverable tokens.
                The server never stores raw observations — raw PII is
                redacted on the device before anything reaches the cloud,
                and the redacted payload is all that is kept. That is why
                there is no raw text to show here.
              </p>
              {redaction?.note && <p className="muted">{redaction.note}</p>}
              {redaction?.reason && <p className="muted">{redaction.reason}</p>}
            </Disclosure>
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
                    <li key={fixture} className="label-fixture">
                      <span className="mono">{fixture}</span>
                      <SummaryChips
                        items={types.map((t) => ({ label: "", value: t }))}
                      />
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <h3>Egress audit</h3>
            {egress ? (
              <div className="egress-block">
                <p className="egress-line">
                  <StatusChip tone={egress.passed ? "ok" : "bad"}>
                    {egress.passed ? "Passed" : "Failed"}
                  </StatusChip>
                  <span className={egress.passed ? undefined : "danger"}>
                    {egress.passed
                      ? `${egress.checked} outbound calls checked, ${egress.hit_count} hits.`
                      : `${egress.hit_count} secret${egress.hit_count === 1 ? "" : "s"} left the device. Stop the run and block the payload.`}
                  </span>
                </p>
                <p className="muted">{egress.note}</p>
                {egress.hits.length > 0 && (
                  <div className="table-scroll" tabIndex={0} role="region" aria-label="Egress hits">
                    <table
                      style={
                        {
                          "--label-2": '"Entry: "',
                          "--label-3": '"Endpoint: "',
                        } as CSSProperties
                      }
                    >
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
                            <th scope="row">{hit.type}</th>
                            <td className="mono">{hit.entry_id}</td>
                            <td className="mono">{hit.endpoint}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <div className="sent-pii">
                  <span className="muted">Patterns checked:</span>
                  {egress.patterns.length > 0 ? (
                    <SummaryChips
                      items={egress.patterns.map((p) => ({ label: "", value: p }))}
                    />
                  ) : (
                    <span className="muted">—</span>
                  )}
                </div>
              </div>
            ) : (
              <Spinner label="Loading egress audit" />
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
                <div className="proof-line">
                  <SummaryChips
                    items={[
                      { label: "Step time p50", value: `${latency.p50_s ?? "—"}s` },
                      ...(latency.mean_s !== null
                        ? [{ label: "Mean", value: `${latency.mean_s}s` }]
                        : []),
                      ...(latency.max_s !== null
                        ? [{ label: "Max", value: `${latency.max_s}s` }]
                        : []),
                      ...(latency.target !== null
                        ? [{ label: "Target", value: `≤${latency.target}s` }]
                        : []),
                    ]}
                  />
                  {latency.target !== null && latency.p50_within_target !== null && (
                    <StatusChip tone={latency.p50_within_target ? "ok" : "bad"}>
                      {latency.p50_within_target ? "Within target" : "Over target"}
                    </StatusChip>
                  )}
                </div>
                {latency.planner && <p className="muted">Planner: {latency.planner}</p>}
                {latency.note && <p className="muted">{latency.note}</p>}
                <h3>Per fixture (slowest first)</h3>
                <ul className="waterfall">
                  {latencyEntries.map(([name, f]) => (
                    <li key={name}>
                      <div className="waterfall-row">
                        <span className="mono">{name}</span>
                        <span className="mono num">
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
