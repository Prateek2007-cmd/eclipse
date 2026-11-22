import { useState } from "react";
import { api, type VaultStatus } from "../api";
import { fmtDateTime, usePoll, useTaskList } from "../hooks";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState, ErrorBanner, SectionHeader, StatusChip } from "../components/ui";

/** Vault view: status, create, unlock/lock, fill checks,
 * grants, and storing opted-in login passwords.
 *
 * Nothing secret is ever displayed here: the server returns
 * success/failure only, never a value, and this view never
 * logs a password, a card number, or the server token. Card
 * details live only in the in-memory grant for one task —
 * never in the vault file, never in SQLite, and never stored
 * by this form. */
export function VaultView({ token }: { token: string | null }) {
  const [status, setStatus] = useState<VaultStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const { tasks } = useTaskList(token);

  const [createPassword, setCreatePassword] = useState("");
  const [createConfirm, setCreateConfirm] = useState("");

  const [unlockTaskId, setUnlockTaskId] = useState("");
  const [unlockPassword, setUnlockPassword] = useState("");
  const [cardNumber, setCardNumber] = useState("");
  const [cardExpiry, setCardExpiry] = useState("");
  const [cardCvv, setCardCvv] = useState("");

  const [fillTaskId, setFillTaskId] = useState("");
  const [fillOrigin, setFillOrigin] = useState("");
  const [fillField, setFillField] = useState("");
  const [fillResult, setFillResult] = useState<string | null>(null);

  const [storeTaskId, setStoreTaskId] = useState("");
  const [storeOrigin, setStoreOrigin] = useState("");
  const [storeField, setStoreField] = useState("");
  const [storeValue, setStoreValue] = useState("");

  const [confirmRevokeAll, setConfirmRevokeAll] = useState(false);
  const [confirmLock, setConfirmLock] = useState(false);

  const load = async () => {
    try {
      const data = await api.vaultStatus();
      setStatus(data);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  usePoll(load, [token], 15000);

  const createVault = async () => {
    if (createPassword !== createConfirm) {
      setError("The two passwords do not match.");
      return;
    }
    setPending("create");
    setError(null);
    setNotice(null);
    try {
      await api.createVault(createPassword);
      setNotice("Vault created. Unlock it for a task below.");
      setCreatePassword("");
      setCreateConfirm("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const unlock = async () => {
    setPending("unlock");
    setError(null);
    setNotice(null);
    try {
      const result = await api.unlockVault({
        password: unlockPassword,
        task_id: unlockTaskId,
        card_number: cardNumber.trim() || undefined,
        card_expiry: cardExpiry.trim() || undefined,
        card_cvv: cardCvv.trim() || undefined,
      });
      setNotice(
        `Unlocked for the task — grant ${result.grant_id} expires ${fmtDateTime(result.expires_at)}.` +
          (result.card_last4 ? ` Card ending ${result.card_last4} is held in memory for this task only.` : ""),
      );
      // Clear the secrets from the form the moment they are used.
      setUnlockPassword("");
      setCardNumber("");
      setCardExpiry("");
      setCardCvv("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const lock = async () => {
    setConfirmLock(false);
    setPending("lock");
    setError(null);
    try {
      const result = await api.lockVault();
      setNotice(`Locked — ${result.revoked} grant${result.revoked === 1 ? "" : "s"} revoked.`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const revokeAll = async () => {
    setConfirmRevokeAll(false);
    setPending("revoke-all");
    setError(null);
    try {
      const result = await api.revokeAllVault();
      setNotice(`Revoked ${result.revoked} grant${result.revoked === 1 ? "" : "s"}.`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const fill = async () => {
    setPending("fill");
    setError(null);
    setFillResult(null);
    try {
      const result = await api.fillCheck(fillTaskId, fillOrigin.trim(), fillField.trim());
      setFillResult(
        result.ok
          ? "A fill is possible for this field."
          : `No fill: ${result.reason ?? "unknown reason"}`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const store = async () => {
    setPending("store");
    setError(null);
    setNotice(null);
    try {
      await api.storePassword({
        task_id: storeTaskId,
        origin: storeOrigin.trim(),
        field: storeField.trim(),
        value: storeValue,
      });
      setNotice(`Stored the ${storeField.trim()} password for ${storeOrigin.trim()}.`);
      setStoreValue("");
      setStoreField("");
      setStoreOrigin("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="view vault-view">
      <div className="view-head">
        <h2>Vault</h2>
        <p className="muted">
          One encrypted file, unlocked in memory per task. A fill needs the
          vault unlocked for the task AND an approval grant — approval
          before vault fill, always.
        </p>
      </div>
      {error && <ErrorBanner error={error} onRetry={() => void load()} />}
      {notice && (
        <p role="status" className="success">
          {notice}
        </p>
      )}

      <section aria-label="Vault status" className="pane">
        <SectionHeader title="Status" />
        {loading ? (
          <p className="muted">Loading vault status…</p>
        ) : status ? (
          <div>
            <p>
              <StatusChip tone={status.unlocked ? "ok" : "plain"}>
                {status.unlocked ? "Unlocked" : "Locked"}
              </StatusChip>{" "}
              <StatusChip tone={status.has_grants ? "info" : "plain"}>
                {status.has_grants ? `${status.grants.length} live grant${status.grants.length === 1 ? "" : "s"}` : "No live grants"}
              </StatusChip>
            </p>
            <p className="muted mono">{status.vault_file}</p>
            {status.grants.length > 0 && (
              <table>
                <caption>Live grants — ids, tasks and expiry, never a secret value</caption>
                <thead>
                  <tr>
                    <th scope="col">Grant</th>
                    <th scope="col">Task</th>
                    <th scope="col">Expires</th>
                    <th scope="col">Card</th>
                  </tr>
                </thead>
                <tbody>
                  {status.grants.map((g) => (
                    <tr key={g.grant_id}>
                      <td className="mono">{g.grant_id}</td>
                      <td className="mono">{g.task_id}</td>
                      <td className="mono">{fmtDateTime(g.expires_at)}</td>
                      <td>{g.has_card ? `ends ${g.card_last4 ?? "—"}` : "—"}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            <div className="task-actions">
              <button
                type="button"
                onClick={() => setConfirmLock(true)}
                disabled={pending !== null || !status.unlocked}
              >
                Lock vault
              </button>
              <button
                type="button"
                onClick={() => setConfirmRevokeAll(true)}
                disabled={pending !== null || !status.has_grants}
              >
                Revoke all grants
              </button>
            </div>
          </div>
        ) : (
          <EmptyState>Vault status is unavailable.</EmptyState>
        )}
      </section>

      <div className="split">
        <section aria-label="Create the vault" className="pane">
          <SectionHeader title="Create vault" />
          <p className="muted">
            Only needed once. A mistyped first password must not silently
            become the password, so the first unlock never creates the
            vault — create it here, deliberately.
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void createVault();
            }}
          >
            <label htmlFor="vault-create-password">Vault password (min 8 characters)</label>
            <input
              id="vault-create-password"
              type="password"
              autoComplete="new-password"
              value={createPassword}
              onChange={(e) => setCreatePassword(e.target.value)}
              required
              minLength={8}
            />
            <label htmlFor="vault-create-confirm">Repeat the password</label>
            <input
              id="vault-create-confirm"
              type="password"
              autoComplete="new-password"
              value={createConfirm}
              onChange={(e) => setCreateConfirm(e.target.value)}
              required
              minLength={8}
            />
            <button type="submit" className="primary" disabled={pending !== null}>
              {pending === "create" ? "Creating…" : "Create vault"}
            </button>
          </form>
        </section>

        <section aria-label="Unlock the vault" className="pane">
          <SectionHeader title="Unlock for a task" />
          {tasks.length === 0 ? (
            <EmptyState>No tasks yet — a grant belongs to a task.</EmptyState>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void unlock();
              }}
            >
              <label htmlFor="vault-unlock-task">Task</label>
              <select
                id="vault-unlock-task"
                value={unlockTaskId}
                onChange={(e) => setUnlockTaskId(e.target.value)}
                required
              >
                <option value="">Pick a task…</option>
                {tasks.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.goal} ({t.status})
                  </option>
                ))}
              </select>
              <label htmlFor="vault-unlock-password">Vault password</label>
              <input
                id="vault-unlock-password"
                type="password"
                autoComplete="current-password"
                value={unlockPassword}
                onChange={(e) => setUnlockPassword(e.target.value)}
                required
              />
              <p className="muted">
                Card details are optional, per task, and never stored: they
                live only in this task's in-memory grant (15 minutes) and
                are wiped on expiry.
              </p>
              <div className="form-grid">
                <div>
                  <label htmlFor="vault-card-number">Card number (optional)</label>
                  <input
                    id="vault-card-number"
                    type="password"
                    autoComplete="off"
                    inputMode="numeric"
                    value={cardNumber}
                    onChange={(e) => setCardNumber(e.target.value)}
                  />
                </div>
                <div>
                  <label htmlFor="vault-card-expiry">Expiry (optional)</label>
                  <input
                    id="vault-card-expiry"
                    autoComplete="off"
                    placeholder="MM/YY"
                    value={cardExpiry}
                    onChange={(e) => setCardExpiry(e.target.value)}
                  />
                </div>
                <div>
                  <label htmlFor="vault-card-cvv">CVV (optional)</label>
                  <input
                    id="vault-card-cvv"
                    type="password"
                    autoComplete="off"
                    inputMode="numeric"
                    value={cardCvv}
                    onChange={(e) => setCardCvv(e.target.value)}
                  />
                </div>
              </div>
              <button type="submit" className="primary" disabled={pending !== null || !unlockTaskId}>
                {pending === "unlock" ? "Unlocking…" : "Unlock"}
              </button>
            </form>
          )}
        </section>
      </div>

      <div className="split">
        <section aria-label="Check a fill" className="pane">
          <SectionHeader title="Can this field be filled?" />
          <p className="muted">
            Answers success or failure only — never the value. The value
            itself travels only to the extension, inside an execute
            response, when the vault is unlocked and an approval grant
            exists.
          </p>
          {tasks.length === 0 ? (
            <EmptyState>No tasks yet.</EmptyState>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void fill();
              }}
            >
              <label htmlFor="vault-fill-task">Task</label>
              <select
                id="vault-fill-task"
                value={fillTaskId}
                onChange={(e) => setFillTaskId(e.target.value)}
                required
              >
                <option value="">Pick a task…</option>
                {tasks.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.goal} ({t.status})
                  </option>
                ))}
              </select>
              <label htmlFor="vault-fill-origin">Site (origin)</label>
              <input
                id="vault-fill-origin"
                value={fillOrigin}
                onChange={(e) => setFillOrigin(e.target.value)}
                placeholder="https://example.com"
                required
              />
              <label htmlFor="vault-fill-field">Field</label>
              <input
                id="vault-fill-field"
                value={fillField}
                onChange={(e) => setFillField(e.target.value)}
                placeholder="password, email, card_number…"
                required
              />
              <button type="submit" disabled={pending !== null || !fillTaskId}>
                {pending === "fill" ? "Checking…" : "Check"}
              </button>
              {fillResult && (
                <p role="status" className="secondary">
                  {fillResult}
                </p>
              )}
            </form>
          )}
        </section>

        <section aria-label="Store a password" className="pane">
          <SectionHeader title="Store a login password" />
          <p className="muted">
            Only for a site you opted in to. The vault must be unlocked
            for the task, with a live grant. Card numbers and CVVs are
            refused — they are never stored, by design.
          </p>
          {tasks.length === 0 ? (
            <EmptyState>No tasks yet.</EmptyState>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void store();
              }}
            >
              <label htmlFor="vault-store-task">Task</label>
              <select
                id="vault-store-task"
                value={storeTaskId}
                onChange={(e) => setStoreTaskId(e.target.value)}
                required
              >
                <option value="">Pick a task…</option>
                {tasks.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.goal} ({t.status})
                  </option>
                ))}
              </select>
              <label htmlFor="vault-store-origin">Site (origin)</label>
              <input
                id="vault-store-origin"
                value={storeOrigin}
                onChange={(e) => setStoreOrigin(e.target.value)}
                placeholder="https://example.com"
                required
              />
              <label htmlFor="vault-store-field">Field</label>
              <input
                id="vault-store-field"
                value={storeField}
                onChange={(e) => setStoreField(e.target.value)}
                placeholder="password"
                required
              />
              <label htmlFor="vault-store-value">Value</label>
              <input
                id="vault-store-value"
                type="password"
                autoComplete="new-password"
                value={storeValue}
                onChange={(e) => setStoreValue(e.target.value)}
                required
              />
              <button type="submit" className="primary" disabled={pending !== null || !storeTaskId}>
                {pending === "store" ? "Storing…" : "Store password"}
              </button>
            </form>
          )}
        </section>
      </div>

      <ConfirmDialog
        open={confirmLock}
        title="Lock the vault?"
        confirmLabel="Lock vault"
        danger
        onConfirm={() => void lock()}
        onCancel={() => setConfirmLock(false)}
      >
        <p>
          The key leaves memory and every grant is revoked. Tasks that need
          a fill will ask for approval again.
        </p>
      </ConfirmDialog>
      <ConfirmDialog
        open={confirmRevokeAll}
        title="Revoke every vault grant?"
        confirmLabel="Revoke all grants"
        danger
        onConfirm={() => void revokeAll()}
        onCancel={() => setConfirmRevokeAll(false)}
      >
        <p>
          Every live grant is deleted at once, across all tasks. The vault
          stays unlocked, but no task can fill until it is unlocked again.
        </p>
      </ConfirmDialog>
    </div>
  );
}
