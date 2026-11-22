import { useState } from "react";
import {
  api,
  isHttpError,
  type ConnectorLiveStatus,
  type ConnectorRow,
  type DriveFile,
  type FreebusyResult,
  type GmailOffer,
} from "../api";
import { displayValue, fmtDateTime, usePoll, useTaskList } from "../hooks";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState, ErrorBanner, SectionHeader, StatusChip, statusTone } from "../components/ui";

/** Turn a datetime-local value into an RFC3339 timestamp with
 * a timezone offset (the server requires one). */
function toRfc3339(local: string): string {
  return new Date(local).toISOString();
}

/** Connectors view: Gmail, Calendar, Drive — on/off, live
 * status, offers, free/busy, event creation and uploads.
 *
 * External actions need a single-use approval naming the exact
 * action (§7): approve the plain-words card first, then the
 * action consumes the approval. A failed action leaves the
 * approval usable for a retry. */
export function ConnectorsView({ token }: { token: string | null }) {
  const [rows, setRows] = useState<ConnectorRow[]>([]);
  const [live, setLive] = useState<Record<string, ConnectorLiveStatus>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<string | null>(null);
  const [disableName, setDisableName] = useState<string | null>(null);

  const [offers, setOffers] = useState<GmailOffer[] | null>(null);
  const [offersError, setOffersError] = useState<string | null>(null);

  const [fbStart, setFbStart] = useState("");
  const [fbEnd, setFbEnd] = useState("");
  const [freebusy, setFreebusy] = useState<FreebusyResult | null>(null);

  const [resume, setResume] = useState<DriveFile | null>(null);
  const [resumeMissing, setResumeMissing] = useState(false);

  const { tasks } = useTaskList(token);

  // Calendar event form + its two-step approval.
  const [eventTaskId, setEventTaskId] = useState("");
  const [eventSummary, setEventSummary] = useState("");
  const [eventStart, setEventStart] = useState("");
  const [eventEnd, setEventEnd] = useState("");
  const [eventDescription, setEventDescription] = useState("");
  const [eventLocation, setEventLocation] = useState("");
  const [eventApprovalId, setEventApprovalId] = useState<string | null>(null);
  const [eventApprovalExpires, setEventApprovalExpires] = useState<number | null>(null);
  const [eventResult, setEventResult] = useState<string | null>(null);

  // Drive upload form + its two-step approval.
  const [uploadTaskId, setUploadTaskId] = useState("");
  const [uploadPath, setUploadPath] = useState("");
  const [uploadName, setUploadName] = useState("");
  const [uploadApprovalId, setUploadApprovalId] = useState<string | null>(null);
  const [uploadResult, setUploadResult] = useState<string | null>(null);

  const load = async () => {
    try {
      const data = await api.listConnectors();
      setRows(data.connectors);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  usePoll(load, [token], 30000);

  const checkLive = async (name: string) => {
    setPending(`status:${name}`);
    setError(null);
    try {
      const status = await api.connectorStatus(name);
      setLive((prev) => ({ ...prev, [name]: status }));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const setEnabled = async (name: string, enabled: boolean) => {
    setPending(`${enabled ? "enable" : "disable"}:${name}`);
    setError(null);
    try {
      if (enabled) await api.enableConnector(name);
      else await api.disableConnector(name);
      setDisableName(null);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const loadOffers = async () => {
    setPending("offers");
    setOffersError(null);
    try {
      const data = await api.gmailOffers();
      setOffers(data.offers);
    } catch (err) {
      setOffersError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const checkFreebusy = async () => {
    if (!fbStart || !fbEnd) return;
    setPending("freebusy");
    setError(null);
    try {
      const data = await api.calendarFreebusy(toRfc3339(fbStart), toRfc3339(fbEnd));
      setFreebusy(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const loadResume = async () => {
    setPending("resume");
    setError(null);
    setResumeMissing(false);
    try {
      const data = await api.driveResume();
      setResume(data);
    } catch (err) {
      if (isHttpError(err, 404)) {
        setResume(null);
        setResumeMissing(true);
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setPending(null);
    }
  };

  const approveEvent = async () => {
    setPending("approve-event");
    setError(null);
    setEventResult(null);
    try {
      const approval = await api.createConnectorApproval({
        task_id: eventTaskId,
        kind: "calendar_event",
        summary: eventSummary.trim(),
        start: toRfc3339(eventStart),
      });
      setEventApprovalId(approval.approval_id);
      setEventApprovalExpires(approval.expires_at);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const createEvent = async () => {
    if (!eventApprovalId) return;
    setPending("create-event");
    setError(null);
    try {
      const event = await api.createCalendarEvent({
        task_id: eventTaskId,
        approval_id: eventApprovalId,
        summary: eventSummary.trim(),
        start: toRfc3339(eventStart),
        end: toRfc3339(eventEnd),
        description: eventDescription.trim(),
        location: eventLocation.trim(),
      });
      setEventResult(
        event.duplicate
          ? `That event already exists (id ${event.id ?? "—"}) — no duplicate was created.`
          : `Event created (id ${event.id ?? "—"}).`,
      );
      setEventApprovalId(null);
      setEventApprovalExpires(null);
    } catch (err) {
      // A failed create leaves the approval usable for a retry.
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const approveUpload = async () => {
    setPending("approve-upload");
    setError(null);
    setUploadResult(null);
    try {
      const approval = await api.createConnectorApproval({
        task_id: uploadTaskId,
        kind: "drive_upload",
        path: uploadPath.trim(),
      });
      setUploadApprovalId(approval.approval_id);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const upload = async () => {
    if (!uploadApprovalId) return;
    setPending("upload");
    setError(null);
    try {
      const file = await api.driveUpload({
        task_id: uploadTaskId,
        approval_id: uploadApprovalId,
        path: uploadPath.trim(),
        name: uploadName.trim() || undefined,
      });
      setUploadResult(`Uploaded to Drive (file id ${file.id}).`);
      setUploadApprovalId(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const gmailOn = rows.find((r) => r.name === "gmail")?.enabled;
  const calendarOn = rows.find((r) => r.name === "calendar")?.enabled;
  const driveOn = rows.find((r) => r.name === "drive")?.enabled;

  return (
    <div className="view connectors-view">
      <div className="view-head">
        <h2>Connectors</h2>
        <p className="muted">
          Gmail, Calendar and Drive, through your own grants. Turning a
          connector off stops Eclipse using it — it does not revoke the
          Google grant; that happens in the platform Settings.
        </p>
      </div>
      {error && <ErrorBanner error={error} onRetry={() => void load()} />}

      <section aria-label="Connector status" className="pane">
        <SectionHeader title="Status" />
        {loading && <p className="muted">Loading connectors…</p>}
        {!loading && rows.length === 0 && (
          <EmptyState>
            No connectors are registered yet. The server registers Gmail,
            Calendar and Drive on first run.
          </EmptyState>
        )}
        <ul className="plain-list">
          {rows.map((row) => {
            const status = live[row.name];
            return (
              <li key={row.name}>
                <div className="connector-head">
                  <strong>{row.name}</strong>{" "}
                  <StatusChip tone={row.enabled ? "ok" : "plain"}>
                    {row.enabled ? "on" : "off"}
                  </StatusChip>{" "}
                  {row.state && (
                    <StatusChip tone={statusTone(row.state)}>{row.state}</StatusChip>
                  )}
                </div>
                {row.last_checked && (
                  <p className="muted">
                    Last checked {fmtDateTime(row.last_checked)}
                  </p>
                )}
                {row.last_error && <p className="danger">{row.last_error}</p>}
                {status?.state === "needs_reconnect" && status.connect_url && (
                  <p role="alert" className="warning">
                    The Google grant expired or was revoked.{" "}
                    <a href={status.connect_url} target="_blank" rel="noreferrer">
                      Reconnect
                    </a>
                  </p>
                )}
                {status?.error && <p className="danger">{status.error}</p>}
                <div className="task-actions">
                  <button
                    type="button"
                    onClick={() => void checkLive(row.name)}
                    disabled={pending !== null}
                  >
                    {pending === `status:${row.name}` ? "Checking…" : "Check live status"}
                  </button>
                  {row.enabled ? (
                    <button
                      type="button"
                      onClick={() => setDisableName(row.name)}
                      disabled={pending !== null}
                    >
                      Turn off
                    </button>
                  ) : (
                    <button
                      type="button"
                      onClick={() => void setEnabled(row.name, true)}
                      disabled={pending !== null}
                    >
                      {pending === `enable:${row.name}` ? "Turning on…" : "Turn on"}
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      </section>

      <div className="split">
        <section aria-label="Gmail offers" className="pane">
          <SectionHeader
            title="Gmail offers"
            action={
              <button
                type="button"
                onClick={() => void loadOffers()}
                disabled={pending !== null || !gmailOn}
              >
                {pending === "offers" ? "Scanning…" : "Scan inbox"}
              </button>
            }
          />
          <p className="muted">
            Offer and interview mails, with bodies and subjects redacted
            before they reach this screen.
          </p>
          {!gmailOn && <p className="muted">Turn Gmail on to scan the inbox.</p>}
          {offersError && (
            <p role="alert" className="danger">
              {offersError}
            </p>
          )}
          {offers === null ? (
            <EmptyState>No scan yet.</EmptyState>
          ) : offers.length === 0 ? (
            <EmptyState>No offer or interview mails found.</EmptyState>
          ) : (
            <ul className="plain-list">
              {offers.map((o) => (
                <li key={o.id}>
                  <strong>{o.subject}</strong>
                  <p className="muted">
                    From {o.from} · {o.date}
                  </p>
                  <p>{o.body_redacted}</p>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section aria-label="Calendar free and busy" className="pane">
          <SectionHeader title="Calendar free/busy" />
          {!calendarOn && <p className="muted">Turn Calendar on to check free/busy.</p>}
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void checkFreebusy();
            }}
          >
            <label htmlFor="fb-start">From</label>
            <input
              id="fb-start"
              type="datetime-local"
              value={fbStart}
              onChange={(e) => setFbStart(e.target.value)}
              required
            />
            <label htmlFor="fb-end">To</label>
            <input
              id="fb-end"
              type="datetime-local"
              value={fbEnd}
              onChange={(e) => setFbEnd(e.target.value)}
              required
            />
            <button type="submit" disabled={pending !== null || !calendarOn}>
              {pending === "freebusy" ? "Checking…" : "Check"}
            </button>
          </form>
          {freebusy && (
            <div>
              <p role="status" className={freebusy.free ? "success" : "warning"}>
                {freebusy.free
                  ? "Free — nothing on the calendar in that window."
                  : `Busy — ${freebusy.busy.length} event${freebusy.busy.length === 1 ? "" : "s"} in that window.`}
              </p>
              {freebusy.busy.length > 0 && (
                <ul className="plain-list">
                  {freebusy.busy.map((slot, i) => (
                    <li key={i} className="mono">
                      {slot.start} → {slot.end}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
        </section>
      </div>

      <section aria-label="Create a calendar event" className="pane">
        <SectionHeader title="Create a calendar event" />
        {!calendarOn && <p className="muted">Turn Calendar on to create events.</p>}
        {tasks.length === 0 ? (
          <EmptyState>No tasks yet — an event approval belongs to a task.</EmptyState>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              if (eventApprovalId) void createEvent();
              else void approveEvent();
            }}
          >
            <label htmlFor="event-task">Task</label>
            <select
              id="event-task"
              value={eventTaskId}
              onChange={(e) => setEventTaskId(e.target.value)}
              required
              disabled={eventApprovalId !== null}
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
                <label htmlFor="event-summary">Summary</label>
                <input
                  id="event-summary"
                  value={eventSummary}
                  onChange={(e) => setEventSummary(e.target.value)}
                  required
                  disabled={eventApprovalId !== null}
                />
              </div>
              <div>
                <label htmlFor="event-location">Location</label>
                <input
                  id="event-location"
                  value={eventLocation}
                  onChange={(e) => setEventLocation(e.target.value)}
                  disabled={eventApprovalId !== null}
                />
              </div>
              <div>
                <label htmlFor="event-start">Start</label>
                <input
                  id="event-start"
                  type="datetime-local"
                  value={eventStart}
                  onChange={(e) => setEventStart(e.target.value)}
                  required
                  disabled={eventApprovalId !== null}
                />
              </div>
              <div>
                <label htmlFor="event-end">End</label>
                <input
                  id="event-end"
                  type="datetime-local"
                  value={eventEnd}
                  onChange={(e) => setEventEnd(e.target.value)}
                  required
                  disabled={eventApprovalId !== null}
                />
              </div>
            </div>
            <label htmlFor="event-description">Description</label>
            <textarea
              id="event-description"
              value={eventDescription}
              onChange={(e) => setEventDescription(e.target.value)}
              rows={3}
              disabled={eventApprovalId !== null}
            />
            {eventApprovalId === null ? (
              <div>
                <p className="muted">
                  Step 1 of 2: approve this exact event. The approval is
                  single-use and names this summary and start time.
                </p>
                <button
                  type="submit"
                  className="primary"
                  disabled={pending !== null || !calendarOn}
                >
                  {pending === "approve-event" ? "Approving…" : "Approve this event"}
                </button>
              </div>
            ) : (
              <div className="approval-card" role="group" aria-label="Event approved">
                <h3>Approved — create the event?</h3>
                <p>
                  “{eventSummary}” from {displayValue(toRfc3339(eventStart))}.
                  The approval expires{" "}
                  {eventApprovalExpires ? fmtDateTime(eventApprovalExpires) : "in 10 minutes"}.
                  Creating the same event twice never duplicates it.
                </p>
                <div className="task-actions">
                  <button type="submit" className="primary" disabled={pending !== null}>
                    {pending === "create-event" ? "Creating…" : "Create event"}
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setEventApprovalId(null);
                      setEventApprovalExpires(null);
                    }}
                  >
                    Change details
                  </button>
                </div>
              </div>
            )}
            {eventResult && (
              <p role="status" className="success">
                {eventResult}
              </p>
            )}
          </form>
        )}
      </section>

      <div className="split">
        <section aria-label="Resume in Drive" className="pane">
          <SectionHeader
            title="Resume in Drive"
            action={
              <button
                type="button"
                onClick={() => void loadResume()}
                disabled={pending !== null || !driveOn}
              >
                {pending === "resume" ? "Looking…" : "Find resume"}
              </button>
            }
          />
          <p className="muted">
            The newest file in Drive whose name contains “resume” or “CV”.
            This is a separate lookup from the local resume file the mail
            flow attaches — the two may hold different copies.
          </p>
          {!driveOn && <p className="muted">Turn Drive on to look it up.</p>}
          {resumeMissing && <EmptyState>No resume found in Drive.</EmptyState>}
          {resume && (
            <dl className="kv">
              <dt>Name</dt>
              <dd>{resume.name ?? "—"}</dd>
              <dt>File id</dt>
              <dd className="mono">{resume.id}</dd>
              <dt>Modified</dt>
              <dd>{resume.modifiedTime ?? "—"}</dd>
              {resume.webViewLink && (
                <>
                  <dt>Link</dt>
                  <dd>
                    <a href={resume.webViewLink} target="_blank" rel="noreferrer">
                      Open in Drive
                    </a>
                  </dd>
                </>
              )}
            </dl>
          )}
        </section>

        <section aria-label="Upload to Drive" className="pane">
          <SectionHeader title="Upload to Drive" />
          {!driveOn && <p className="muted">Turn Drive on to upload.</p>}
          {tasks.length === 0 ? (
            <EmptyState>No tasks yet — an upload approval belongs to a task.</EmptyState>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                if (uploadApprovalId) void upload();
                else void approveUpload();
              }}
            >
              <label htmlFor="upload-task">Task</label>
              <select
                id="upload-task"
                value={uploadTaskId}
                onChange={(e) => setUploadTaskId(e.target.value)}
                required
                disabled={uploadApprovalId !== null}
              >
                <option value="">Pick a task…</option>
                {tasks.map((t) => (
                  <option key={t.id} value={t.id}>
                    {t.goal} ({t.status})
                  </option>
                ))}
              </select>
              <label htmlFor="upload-path">Absolute path (inside the home directory)</label>
              <input
                id="upload-path"
                value={uploadPath}
                onChange={(e) => setUploadPath(e.target.value)}
                placeholder="/home/user/…"
                required
                disabled={uploadApprovalId !== null}
              />
              <label htmlFor="upload-name">Name in Drive (optional)</label>
              <input
                id="upload-name"
                value={uploadName}
                onChange={(e) => setUploadName(e.target.value)}
                disabled={uploadApprovalId !== null}
              />
              {uploadApprovalId === null ? (
                <button type="submit" className="primary" disabled={pending !== null || !driveOn}>
                  {pending === "approve-upload" ? "Approving…" : "Approve this upload"}
                </button>
              ) : (
                <div className="approval-card" role="group" aria-label="Upload approved">
                  <h3>Approved — upload the file?</h3>
                  <p>
                    “{uploadPath}”{uploadName ? ` as “${uploadName}”` : ""}. The
                    approval names this path and nothing else.
                  </p>
                  <div className="task-actions">
                    <button type="submit" className="primary" disabled={pending !== null}>
                      {pending === "upload" ? "Uploading…" : "Upload"}
                    </button>
                    <button type="button" onClick={() => setUploadApprovalId(null)}>
                      Change file
                    </button>
                  </div>
                </div>
              )}
              {uploadResult && (
                <p role="status" className="success">
                  {uploadResult}
                </p>
              )}
            </form>
          )}
        </section>
      </div>

      <ConfirmDialog
        open={disableName !== null}
        title={`Turn off ${disableName ?? "this connector"}?`}
        confirmLabel="Turn off"
        danger
        onConfirm={() => {
          const name = disableName;
          if (name) void setEnabled(name, false);
        }}
        onCancel={() => setDisableName(null)}
      >
        <p>
          Eclipse stops using {disableName}. Sends, scans and lookups through
          it will fail until it is turned back on. The Google grant itself
          is not revoked here.
        </p>
      </ConfirmDialog>
    </div>
  );
}
