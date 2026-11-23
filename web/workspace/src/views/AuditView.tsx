import { useState, type CSSProperties } from "react";
import { api, type AuditEntry, type AuditVerifyResult, type SystemMetrics } from "../api";
import { fmtAuditDateTime, usePoll, useTaskList } from "../hooks";
import {
  EmptyState,
  ErrorBanner,
  HumanError,
  RelativeTime,
  SectionHeader,
  ShowMore,
  Spinner,
  StatusChip,
} from "../components/ui";
import "./AuditView.css";

/** Rows shown before "Show more" — the API has no
 * pagination, so the cap is client-side. */
const PAGE = 25;

/** Audit & Trace view: the hash-chained audit log, its
 * verification, the JSON export, and server metrics.
 *
 * The hash chain proves tampering after the fact. It does not
 * prevent it. Audit created_at is Unix MILLISECONDS (the only
 * ms timestamps the server returns — everything else is
 * seconds), so entries render through fmtAuditDateTime, not
 * fmtDateTime: the seconds formatter multiplied them a second
 * time and showed every entry in year 58863. The export
 * filename below uses exported_at raw for the same reason.
 * RelativeTime takes seconds, so the ms value is divided by
 * 1000 before it is passed in. */
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
  const [shown, setShown] = useState(PAGE);
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

  const newestFirst = [...audit].reverse();

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
            <dd className="mono num">{metrics.tasks}</dd>
            <dt>Audit entries</dt>
            <dd className="mono num">{metrics.audit_entries}</dd>
            <dt>Tokens (in / out)</dt>
            <dd className="mono num">
              {metrics.tokens.in} / {metrics.tokens.out}
            </dd>
          </dl>
        ) : (
          <Spinner label="Loading server metrics" />
        )}
        {metrics?.note && <p className="muted">{metrics.note}</p>}
      </section>

      <section aria-label="Hash chain" className="pane">
        <SectionHeader title="Hash chain" />
        <div className="audit-actions">
          <button type="button" onClick={() => void verify()} disabled={verifying}>
            {verifying ? "Verifying…" : "Verify hash chain"}
          </button>
          <button type="button" onClick={() => void exportLog()} disabled={exporting}>
            {exporting ? "Exporting…" : "Export JSON"}
          </button>
          {verifying && <Spinner label="Verifying" />}
          {exporting && <Spinner label="Exporting" />}
        </div>
        {verified && verified.ok && (
          <p role="status" className="audit-verify-ok">
            <StatusChip tone="ok">Chain verified</StatusChip>
            <span>
              {verified.checked} entries checked, no tampering detected.
            </span>
          </p>
        )}
        {verified && !verified.ok && verified.first_bad_id !== null && (
          <HumanError
            title={`Hash chain failed at entry ${verified.first_bad_id} — ${verified.checked} checked before it`}
            detail={verified.reason ?? "The log may have been tampered with."}
            onRetry={() => void verify()}
          />
        )}
        {verified && !verified.ok && verified.first_bad_id === null && (
          <HumanError
            title="Verification failed"
            detail={verified.reason ?? "Unknown error"}
            onRetry={() => void verify()}
          />
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
              onChange={(e) => {
                setTaskFilter(e.target.value);
                setShown(PAGE);
              }}
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
        {loading && <Spinner label="Loading audit log" />}
        {!loading && audit.length === 0 && (
          <EmptyState>
            No audit entries yet. The first consequential action — a task,
            an approval, a send — writes the first entry.
          </EmptyState>
        )}
        {audit.length > 0 && (
          <div className="audit-log-wrap">
            <div className="table-scroll" tabIndex={0} role="region" aria-label="Audit log">
              <table
                style={
                  {
                    "--label-2": '"Time: "',
                    "--label-3": '"Action: "',
                    "--label-4": '"Task: "',
                    "--label-5": '"Hash: "',
                  } as CSSProperties
                }
              >
                <caption>Audit entries, newest first</caption>
                <thead>
                  <tr>
                    <th scope="col" className="num">#</th>
                    <th scope="col">Time</th>
                    <th scope="col">Action</th>
                    <th scope="col">Task</th>
                    <th scope="col">Hash</th>
                  </tr>
                </thead>
                <tbody>
                  {newestFirst.slice(0, shown).map((e) => (
                    <tr key={e.id}>
                      <th scope="row" className="mono num">{e.id}</th>
                      <td className="audit-time">
                        <RelativeTime ts={Math.floor(e.created_at / 1000)} />
                        <span className="muted audit-absolute">
                          {fmtAuditDateTime(e.created_at)}
                        </span>
                      </td>
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
            <ShowMore
              total={newestFirst.length}
              shown={shown}
              onShowMore={() => setShown((n) => n + PAGE)}
              label="entries"
            />
          </div>
        )}
      </section>
    </div>
  );
}
