import { useState } from "react";
import { api, type AuditEntry, type AuditVerifyResult, type SystemMetrics } from "../api";
import { fmtDateTime, usePoll, useTaskList } from "../hooks";
import { EmptyState, ErrorBanner, SectionHeader } from "../components/ui";

/** Audit & Trace view: the hash-chained audit log, its
 * verification, the JSON export, and server metrics.
 *
 * The hash chain proves tampering after the fact. It does not
 * prevent it. (Also fixed here: the old pane rendered
 * created_at — Unix seconds — as milliseconds, showing every
 * entry at 1970.) */
export function AuditView({ token }: { token: string | null }) {
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [metrics, setMetrics] = useState<SystemMetrics | null>(null);
  const [verified, setVerified] = useState<AuditVerifyResult | null>(null);
  const [verifying, setVerifying] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const [taskFilter, setTaskFilter] = useState("");
  const { tasks } = useTaskList(token);

  const load = async () => {
    try {
      const [a, m] = await Promise.all([
        api.audit(taskFilter || undefined),
        api.systemMetrics(),
      ]);
      setAudit(a.entries);
      setMetrics(m);
      setError(null);
      setLastOk(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  usePoll(load, [token, taskFilter], 10000);

  const verify = async () => {
    setVerifying(true);
    try {
      const result = await api.verifyAudit();
      setVerified(result);
    } catch (err) {
      setVerified({
        ok: false,
        checked: 0,
        first_bad_id: null,
        reason: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setVerifying(false);
    }
  };

  const exportLog = async () => {
    setExporting(true);
    setError(null);
    try {
      const data = await api.exportAudit();
      const blob = new Blob([JSON.stringify(data, null, 2)], {
        type: "application/json",
      });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `eclipse-audit-${new Date(data.exported_at).toISOString().slice(0, 10)}.json`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setExporting(false);
    }
  };

  return (
    <div className="view audit-view">
      <div className="view-head">
        <h2>Audit &amp; Trace</h2>
        <p className="muted">
          Every consequential action lands in the hash-chained log. Verify
          the chain to prove nothing was rewritten after the fact.
        </p>
      </div>
      {error && <ErrorBanner error={error} lastOk={lastOk} onRetry={() => void load()} />}

      <section aria-label="Server" className="pane">
        <SectionHeader title="Server" />
        {metrics ? (
          <dl className="kv">
            <dt>Tasks</dt>
            <dd className="mono">{metrics.tasks}</dd>
            <dt>Audit entries</dt>
            <dd className="mono">{metrics.audit_entries}</dd>
            <dt>Tokens (in / out)</dt>
            <dd className="mono">
              {metrics.tokens.in} / {metrics.tokens.out}
            </dd>
          </dl>
        ) : (
          <p className="muted">Loading server metrics…</p>
        )}
        {metrics?.note && <p className="muted">{metrics.note}</p>}
      </section>

      <section aria-label="Hash chain" className="pane">
        <SectionHeader
          title="Hash chain"
          action={
            <div className="task-actions">
              <button type="button" onClick={() => void verify()} disabled={verifying}>
                {verifying ? "Verifying…" : "Verify hash chain"}
              </button>
              <button type="button" onClick={() => void exportLog()} disabled={exporting}>
                {exporting ? "Exporting…" : "Export JSON"}
              </button>
            </div>
          }
        />
        {verified && (
          <p role="status" className={verified.ok ? "success" : "danger"}>
            {verified.ok
              ? `Hash chain verifies — ${verified.checked} entries checked, no tampering detected.`
              : verified.first_bad_id !== null
                ? `Hash chain FAILED at entry ${verified.first_bad_id} (${verified.checked} checked before it): ${verified.reason ?? "the log may have been tampered with."}`
                : `Verification failed: ${verified.reason ?? "unknown error"}`}
          </p>
        )}
        <p className="muted">
          Export downloads the full log as JSON — a local backup, on
          demand. Nothing is uploaded anywhere.
        </p>
      </section>

      <section aria-label="Audit log" className="pane">
        <SectionHeader
          title={`Audit log (${audit.length})`}
          action={
            <select
              aria-label="Filter audit entries by task"
              value={taskFilter}
              onChange={(e) => setTaskFilter(e.target.value)}
            >
              <option value="">All tasks</option>
              {tasks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.goal}
                </option>
              ))}
            </select>
          }
        />
        {loading && <p className="muted">Loading audit log…</p>}
        {!loading && audit.length === 0 && (
          <EmptyState>
            No audit entries yet. The first consequential action — a task,
            an approval, a send — writes the first entry.
          </EmptyState>
        )}
        {audit.length > 0 && (
          <div className="table-scroll">
            <table>
              <caption>Audit entries, newest first</caption>
              <thead>
                <tr>
                  <th scope="col">#</th>
                  <th scope="col">Time</th>
                  <th scope="col">Action</th>
                  <th scope="col">Task</th>
                  <th scope="col">Hash</th>
                </tr>
              </thead>
              <tbody>
                {[...audit].reverse().slice(0, 100).map((e) => (
                  <tr key={e.id}>
                    <td className="mono">{e.id}</td>
                    <td className="mono">{fmtDateTime(e.created_at)}</td>
                    <td>{e.action}</td>
                    <td className="mono">{e.task_id ?? "—"}</td>
                    <td className="mono" title={e.hash}>
                      {e.payload_hash.slice(0, 12)}…
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {audit.length > 100 && (
          <p className="muted">Showing the newest 100 of {audit.length} — export for the full log.</p>
        )}
      </section>
    </div>
  );
}
