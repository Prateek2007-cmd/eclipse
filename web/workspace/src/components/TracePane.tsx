import { useEffect, useState } from "react";
import { api, type AuditEntry } from "../api";

/** Right pane (plan §5.2, §5.6): trace + audit. */
export function TracePane({ token }: { token: string | null }) {
  const [audit, setAudit] = useState<AuditEntry[]>([]);
  const [verified, setVerified] = useState<boolean | null>(null);
  const [quota, setQuota] = useState<{ name: string; used_today: number; limit: number }[]>([]);
  const [vault, setVault] = useState<{ unlocked: boolean; has_grants: boolean } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastOk, setLastOk] = useState<number | null>(null);

  useEffect(() => {
    if (!token) return;
    let alive = true;
    const load = async () => {
      try {
        const [a, q, v] = await Promise.all([
          api.audit(),
          api.quota(),
          api.vaultStatus(),
        ]);
        if (!alive) return;
        setAudit(a.entries);
        setQuota(q.models);
        setVault(v);
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
  }, [token]);

  const verify = async () => {
    try {
      const result = await api.verifyAudit();
      setVerified(result.ok);
    } catch {
      setVerified(false);
    }
  };

  return (
    <section aria-label="Trace and audit" className="pane trace-pane">
      <h2>Trace &amp; Audit</h2>
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
      {loading && <p className="muted">Loading trace…</p>}

      <h3>Quota</h3>
      {quota.length === 0 ? (
        !loading && <p className="muted">No quota data.</p>
      ) : (
        <ul>
          {quota.map((m) => (
            <li key={m.name}>
              <span className="mono">{m.name}</span>: {m.used_today}/{m.limit} calls today
              {m.used_today / m.limit >= 0.8 && (
                <span className="warning"> — over 80%</span>
              )}
            </li>
          ))}
        </ul>
      )}

      <h3>Vault</h3>
      <p>
        {vault === null
          ? loading
            ? "Loading…"
            : "Unknown."
          : vault.unlocked
            ? "Unlocked for this task."
            : "Locked."}{" "}
        {vault?.has_grants ? "Grants live." : "No live grants."}
      </p>

      <h3>Audit log</h3>
      <button type="button" onClick={() => void verify()}>
        Verify hash chain
      </button>
      {verified !== null && (
        <p role="status" className={verified ? "success" : "danger"}>
          {verified
            ? "Hash chain verifies — no tampering detected."
            : "Hash chain FAILED — the log may have been tampered with."}
        </p>
      )}
      {audit.length === 0 ? (
        !loading && <p className="muted">No audit entries yet.</p>
      ) : (
        <table>
          <caption>Audit entries, newest first</caption>
          <thead>
            <tr>
              <th scope="col">Time</th>
              <th scope="col">Action</th>
              <th scope="col">Hash</th>
            </tr>
          </thead>
          <tbody>
            {[...audit].reverse().slice(0, 50).map((e) => (
              <tr key={e.id}>
                <td className="mono">{new Date(e.created_at).toLocaleTimeString()}</td>
                <td>{e.action}</td>
                <td className="mono">{e.payload_hash.slice(0, 12)}…</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p className="muted">
        The hash chain proves tampering after the fact. It does not prevent it.
      </p>
    </section>
  );
}
