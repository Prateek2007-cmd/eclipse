import { useState } from "react";
import "./MailView.css";
import { api, type Draft, type MailLogEntry, type RoleInput } from "../api";
import { newApprovalId, usePoll, useTaskList } from "../hooks";
import { ConfirmDialog } from "../components/ConfirmDialog";
import {
  ActivityLine,
  ApprovalBanner,
  EmptyState,
  ErrorBanner,
  HumanError,
  RelativeTime,
  SectionHeader,
  ShowMore,
  StatusChip,
  humanStatus,
  statusTone,
} from "../components/ui";

/** How many drafts / log entries show before "Show more". */
const DRAFTS_PAGE_SIZE = 8;
const LOG_PAGE_SIZE = 8;

/** An approve/send/edit failure, shown in the draft detail
 * pane as a HumanError with a retry that re-opens the
 * confirm dialog (approve/send) or re-runs the save (edit).
 * Load/create/bounce failures stay in the top ErrorBanner. */
type DetailError = {
  title: string;
  detail: string;
  retry: "approve" | "send" | "edit";
};

/** Mail view: drafts, the approve → send flow, the send log,
 * and bounce checks.
 *
 * The one-approval-one-send rule (§5.4): approving a draft
 * creates a grant that names that draft; the send consumes it.
 * One approval cannot send every draft in the task, and a
 * job-submit approval cannot send a mail. */
export function MailView({ token }: { token: string | null }) {
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [log, setLog] = useState<MailLogEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [lastOk, setLastOk] = useState<number | null>(null);
  const [statusFilter, setStatusFilter] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [draftsShown, setDraftsShown] = useState(DRAFTS_PAGE_SIZE);
  const [logShown, setLogShown] = useState(LOG_PAGE_SIZE);
  const [editBody, setEditBody] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [detailError, setDetailError] = useState<DetailError | null>(null);
  const [sendResult, setSendResult] = useState<string | null>(null);
  const [attachResume, setAttachResume] = useState(false);
  const [confirmSend, setConfirmSend] = useState(false);
  const [confirmApprove, setConfirmApprove] = useState(false);

  // Create-drafts form: one recipient + the role from the
  // shortlist. The server re-matches the role against the
  // stored profile and refuses roles that do not fit.
  const { tasks } = useTaskList(token);
  const [newTaskId, setNewTaskId] = useState("");
  const [newEmail, setNewEmail] = useState("");
  const [newRoleId, setNewRoleId] = useState("");
  const [newRoleTitle, setNewRoleTitle] = useState("");
  const [newRoleCompany, setNewRoleCompany] = useState("");
  const [newRoleUrl, setNewRoleUrl] = useState("");
  const [createResult, setCreateResult] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const [bounceTaskId, setBounceTaskId] = useState("");
  const [bounceResult, setBounceResult] = useState<string | null>(null);
  const [checkingBounce, setCheckingBounce] = useState(false);

  const load = async () => {
    try {
      const [d, l] = await Promise.all([
        api.listDrafts(undefined, statusFilter || undefined),
        api.mailLog(),
      ]);
      setDrafts(d.drafts);
      setLog(l.log);
      setError(null);
      setLastOk(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  usePoll(load, [token, statusFilter], 15000);

  const selected = drafts.find((d) => d.id === selectedId) ?? null;

  const createDrafts = async () => {
    setCreating(true);
    setError(null);
    setCreateResult(null);
    try {
      const role: RoleInput = {
        id: newRoleId.trim(),
        title: newRoleTitle.trim(),
        company: newRoleCompany.trim(),
        url: newRoleUrl.trim(),
      };
      const data = await api.createDrafts(newTaskId, [
        { email: newEmail.trim(), role },
      ]);
      const created = data.created.length;
      const skipped = data.skipped
        .map((s) => `${s.email}: ${Array.isArray(s.reason) ? s.reason.join("; ") : s.reason}`)
        .join(" · ");
      setCreateResult(
        created > 0
          ? `Created ${created} draft${created === 1 ? "" : "s"}.`
          : `Nothing created. ${skipped}`,
      );
      setNewEmail("");
      setNewRoleId("");
      setNewRoleTitle("");
      setNewRoleCompany("");
      setNewRoleUrl("");
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  const approve = async () => {
    if (!selected) return;
    setConfirmApprove(false);
    setPending("approve");
    setDetailError(null);
    try {
      await api.approveDraft(selected.id, newApprovalId());
      await load();
    } catch (err) {
      setDetailError({
        title: "Couldn't approve this draft",
        detail: err instanceof Error ? err.message : String(err),
        retry: "approve",
      });
    } finally {
      setPending(null);
    }
  };

  const saveEdit = async () => {
    if (!selected || editBody === null) return;
    setPending("edit");
    setDetailError(null);
    try {
      await api.editDraft(selected.id, editBody);
      setEditBody(null);
      await load();
    } catch (err) {
      setDetailError({
        title: "Couldn't save the edit",
        detail: err instanceof Error ? err.message : String(err),
        retry: "edit",
      });
    } finally {
      setPending(null);
    }
  };

  const send = async () => {
    if (!selected) return;
    setConfirmSend(false);
    setPending("send");
    setDetailError(null);
    setSendResult(null);
    try {
      const result = await api.sendDraft(selected.id, attachResume);
      setSendResult(
        `Sent — ${result.detail}${result.attached_resume ? ` (attached ${result.attached_resume})` : ""}`,
      );
      await load();
    } catch (err) {
      setDetailError({
        title: "Couldn't send this mail",
        detail: err instanceof Error ? err.message : String(err),
        retry: "send",
      });
    } finally {
      setPending(null);
    }
  };

  const retryDetailError = () => {
    if (!detailError) return;
    if (detailError.retry === "approve") setConfirmApprove(true);
    else if (detailError.retry === "send") setConfirmSend(true);
    else void saveEdit();
  };

  const bounceCheck = async () => {
    if (!bounceTaskId) return;
    setCheckingBounce(true);
    setError(null);
    setBounceResult(null);
    try {
      const data = await api.bounceCheck(bounceTaskId);
      const bounced = data.checked.filter((c) => c.bounced);
      setBounceResult(
        data.checked.length === 0
          ? "No sent mails to check for this task."
          : bounced.length === 0
            ? `Checked ${data.checked.length} sent mail${data.checked.length === 1 ? "" : "s"} — no bounces.`
            : `Checked ${data.checked.length} — ${bounced.length} bounced: ${bounced.map((b) => b.recipient).join(", ")}`,
      );
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCheckingBounce(false);
    }
  };

  return (
    <div className="view mail-view">
      <div className="view-head">
        <h2>Mail</h2>
        <p className="muted">
          Drafts are written locally, approved by you, and sent one at a
          time through your connected Gmail. One approval sends one draft.
        </p>
      </div>
      {error && <ErrorBanner error={error} lastOk={lastOk} onRetry={() => void load()} />}

      <div className="split">
        <section aria-label="Drafts" className="pane">
          <SectionHeader
            title="Drafts"
            action={
              <select
                aria-label="Filter drafts by status"
                value={statusFilter}
                onChange={(e) => {
                  setStatusFilter(e.target.value);
                  setDraftsShown(DRAFTS_PAGE_SIZE);
                }}
              >
                <option value="">All statuses</option>
                <option value="draft">Draft</option>
                <option value="approved">Approved</option>
                <option value="sent">Sent</option>
                <option value="bounced">Bounced</option>
              </select>
            }
          />
          {loading && (
            <div className="skeleton-stack" aria-hidden="true">
              <div className="skeleton skeleton-block" style={{ height: 64 }} />
              <div className="skeleton skeleton-block" style={{ height: 64 }} />
            </div>
          )}
          {!loading && drafts.length === 0 && (
            <EmptyState>
              No drafts yet. Create one below — one per recruiter, from a
              role on the shortlist.
            </EmptyState>
          )}
          <ul className="task-list draft-list">
            {drafts.slice(0, draftsShown).map((d) => (
              <li key={d.id}>
                <button
                  type="button"
                  className={d.id === selectedId ? "selected" : ""}
                  onClick={() => {
                    setSelectedId(d.id);
                    setEditBody(null);
                    setDetailError(null);
                    setSendResult(null);
                  }}
                  aria-current={d.id === selectedId ? true : undefined}
                >
                  <span className="draft-row-main">
                    <span className="draft-recipient">{d.recipient}</span>
                    <span className="muted draft-subject">{d.subject}</span>
                    <span className="muted draft-meta">
                      <RelativeTime ts={d.created_at} />
                      {d.id === selectedId ? (
                        <span className="selected-mark"> · Selected</span>
                      ) : null}
                    </span>
                  </span>
                  <StatusChip tone={statusTone(d.status)}>{humanStatus(d.status)}</StatusChip>
                </button>
              </li>
            ))}
          </ul>
          <ShowMore
            total={drafts.length}
            shown={draftsShown}
            onShowMore={() => setDraftsShown(drafts.length)}
            label="drafts"
          />
        </section>

        <section aria-label="Draft detail" className="pane">
          {!selected ? (
            <EmptyState>Pick a draft to read it, edit it, approve it, and send it.</EmptyState>
          ) : (
            <div className="draft-detail">
              <SectionHeader
                title={selected.subject}
                action={<StatusChip tone={statusTone(selected.status)}>{humanStatus(selected.status)}</StatusChip>}
              />
              <p className="muted draft-meta-line">
                To {selected.recipient} · role {selected.role_id} · created{" "}
                <RelativeTime ts={selected.created_at} />
              </p>
              {detailError && (
                <HumanError
                  title={detailError.title}
                  detail={detailError.detail}
                  onRetry={retryDetailError}
                />
              )}
              {editBody === null ? (
                <div className="mail-body-pane">{selected.body}</div>
              ) : (
                <div>
                  <label htmlFor="draft-edit">Edit the draft body</label>
                  <textarea
                    id="draft-edit"
                    value={editBody}
                    onChange={(e) => setEditBody(e.target.value)}
                    rows={10}
                  />
                  <p className="muted">
                    Editing clears approval — approve again before sending.
                  </p>
                  <div className="task-actions">
                    <button
                      type="button"
                      className="primary"
                      onClick={() => void saveEdit()}
                      disabled={pending !== null}
                    >
                      {pending === "edit" ? "Saving…" : "Save edit"}
                    </button>
                    <button type="button" onClick={() => setEditBody(null)}>
                      Cancel edit
                    </button>
                  </div>
                </div>
              )}
              {sendResult && (
                <p role="status" className="success">
                  {sendResult}
                </p>
              )}
              {selected.status === "draft" && (
                <>
                  <ApprovalBanner
                    count={1}
                    text="This draft needs your approval before it can be sent. Approve only after reading it — the approval is single-use and expires in 10 minutes."
                  >
                    <button
                      type="button"
                      onClick={() => {
                        setEditBody(selected.body);
                        setDetailError(null);
                      }}
                      disabled={pending !== null}
                    >
                      Edit
                    </button>
                    <button
                      type="button"
                      className="primary"
                      onClick={() => setConfirmApprove(true)}
                      disabled={pending !== null}
                    >
                      {pending === "approve" ? "Approving…" : "Approve"}
                    </button>
                  </ApprovalBanner>
                  <p className="muted mail-edit-note">
                    Editing clears approval — you will need to approve again
                    before sending.
                  </p>
                </>
              )}
              {selected.status === "approved" && (
                <>
                  <div className="task-actions">
                    <label className="check-row">
                      <input
                        type="checkbox"
                        checked={attachResume}
                        onChange={(e) => setAttachResume(e.target.checked)}
                      />
                      Attach resume PDF
                    </label>
                    <button
                      type="button"
                      className="primary"
                      onClick={() => setConfirmSend(true)}
                      disabled={pending !== null}
                    >
                      {pending === "send" ? "Sending…" : "Send"}
                    </button>
                  </div>
                  <p className="muted">
                    Approved — this approval sends this draft once, then it
                    is used up.
                  </p>
                </>
              )}
              {selected.status === "sent" && (
                <p className="muted">
                  Sent. The approval for this draft was consumed by the
                  send — sending again needs a fresh approval.
                </p>
              )}
            </div>
          )}
        </section>
      </div>

      <section aria-label="Create drafts" className="pane">
        <SectionHeader title="Create a draft" />
        {tasks.length === 0 ? (
          <EmptyState>No tasks yet — a draft belongs to a task.</EmptyState>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void createDrafts();
            }}
          >
            <label htmlFor="draft-task">Task</label>
            <select
              id="draft-task"
              value={newTaskId}
              onChange={(e) => setNewTaskId(e.target.value)}
              required
            >
              <option value="">Pick a task…</option>
              {tasks.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.goal} ({t.status})
                </option>
              ))}
            </select>
            <div className="form-grid">
              <div>
                <label htmlFor="draft-email">Recruiter email</label>
                <input
                  id="draft-email"
                  type="email"
                  value={newEmail}
                  onChange={(e) => setNewEmail(e.target.value)}
                  required
                />
              </div>
              <div>
                <label htmlFor="draft-role-id">Role id (from the shortlist)</label>
                <input
                  id="draft-role-id"
                  value={newRoleId}
                  onChange={(e) => setNewRoleId(e.target.value)}
                  required
                />
              </div>
              <div>
                <label htmlFor="draft-role-title">Role title</label>
                <input
                  id="draft-role-title"
                  value={newRoleTitle}
                  onChange={(e) => setNewRoleTitle(e.target.value)}
                  required
                />
              </div>
              <div>
                <label htmlFor="draft-role-company">Company</label>
                <input
                  id="draft-role-company"
                  value={newRoleCompany}
                  onChange={(e) => setNewRoleCompany(e.target.value)}
                />
              </div>
              <div>
                <label htmlFor="draft-role-url">Role URL</label>
                <input
                  id="draft-role-url"
                  value={newRoleUrl}
                  onChange={(e) => setNewRoleUrl(e.target.value)}
                />
              </div>
            </div>
            <p className="muted">
              The server re-matches the role against your profile: a role
              that does not fit, or a recruiter already mailed for it, is
              skipped and reported — never drafted.
            </p>
            <button type="submit" className="primary" disabled={creating || !newTaskId}>
              {creating ? "Creating…" : "Create draft"}
            </button>
            {createResult && (
              <p role="status" className="secondary">
                {createResult}
              </p>
            )}
          </form>
        )}
      </section>

      <div className="split">
        <section aria-label="Send log" className="pane">
          <SectionHeader title="Send log" />
          {log.length === 0 ? (
            <EmptyState>No mails sent yet. Every send is logged here, per recruiter.</EmptyState>
          ) : (
            <>
              <ul className="mail-log-list">
                {log.slice(0, logShown).map((entry) => (
                  <li key={entry.id}>
                    <ActivityLine
                      tone={statusTone(entry.status)}
                      title={logTitle(entry)}
                      detail={
                        <p className="muted">
                          Role {entry.role_id}
                          {entry.detail ? ` · ${entry.detail}` : ""}
                        </p>
                      }
                      when={entry.created_at}
                    />
                  </li>
                ))}
              </ul>
              <ShowMore
                total={log.length}
                shown={logShown}
                onShowMore={() => setLogShown(log.length)}
                label="entries"
              />
            </>
          )}
        </section>

        <section aria-label="Bounce check" className="pane">
          <SectionHeader title="Bounce check" />
          <p className="muted">
            A bounce arrives minutes after the send, so this is a separate
            check: it scans the inbox for delivery failures and updates the
            log. A bounced address is blocked from further sends.
          </p>
          {tasks.length === 0 ? (
            <EmptyState>No tasks yet.</EmptyState>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void bounceCheck();
              }}
            >
              <label htmlFor="bounce-task">Task</label>
              <select
                id="bounce-task"
                value={bounceTaskId}
                onChange={(e) => setBounceTaskId(e.target.value)}
                required
              >
                <option value="">Pick a task…</option>
                {tasks.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.goal} ({t.status})
                  </option>
                ))}
              </select>
              <button type="submit" disabled={checkingBounce || !bounceTaskId}>
                {checkingBounce ? "Checking…" : "Check for bounces"}
              </button>
              {bounceResult && (
                <p role="status" className="secondary">
                  {bounceResult}
                </p>
              )}
            </form>
          )}
        </section>
      </div>

      <ConfirmDialog
        open={confirmApprove}
        title="Approve this draft?"
        confirmLabel="Approve draft"
        onConfirm={() => void approve()}
        onCancel={() => setConfirmApprove(false)}
      >
        <p>
          You are saying you read this draft and it may be sent to{" "}
          <strong>{selected?.recipient}</strong>. The approval is single-use
          and expires in 10 minutes.
        </p>
      </ConfirmDialog>
      <ConfirmDialog
        open={confirmSend}
        title="Send this mail?"
        confirmLabel="Send mail"
        onConfirm={() => void send()}
        onCancel={() => setConfirmSend(false)}
      >
        <p>
          This sends the draft to <strong>{selected?.recipient}</strong>{" "}
          through your connected Gmail
          {attachResume ? " with the resume PDF attached" : ", without an attachment"}.
          It consumes the approval — a second send needs a fresh approval.
        </p>
      </ConfirmDialog>
    </div>
  );
}

/** One send-log line in plain words: what happened, and to
 * whom. Unknown statuses fall back to the raw status so
 * nothing is ever hidden. */
function logTitle(entry: MailLogEntry): string {
  if (entry.status === "sent") return `Sent to ${entry.recipient}`;
  if (entry.status === "bounced") return `Bounced — ${entry.recipient}`;
  return `${entry.status} — ${entry.recipient}`;
}
