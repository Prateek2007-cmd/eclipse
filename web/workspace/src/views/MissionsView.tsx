import { Fragment, useEffect, useRef, useState } from "react";
import {
  api,
  isHttpError,
  type IntakeResponse,
  type Mission,
  type MissionApproval,
  type MissionBounds,
  type MissionDetail,
  type MissionType,
  type MissionTypeSpec,
  type Profile,
  type ResumeStatus,
} from "../api";
import { displayValue, fmtDateTime, usePoll } from "../hooks";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState, ErrorBanner, SectionHeader, StatusChip, statusTone } from "../components/ui";

/** Missions (PR #5): chat intake → standing mission → monitor
 * → act → 9 AM briefing. The view creates missions from a
 * drafted intake message, shows their live state, and holds
 * the approval gate for their consequential actions. It never
 * executes — browser work runs in the extension, server work
 * in the runner. Missions only run while this laptop and
 * server are on.
 *
 * Modern-web-guidance applied here (guides: forms,
 * ime-safe-enter-submit): labelled controls with help text
 * via aria-describedby, fieldset/legend for the bounds
 * group, IME-safe Enter-to-submit on the composer, and
 * actionable submit-button language. */

const KNOWN_TYPES: { type: MissionType; label: string }[] = [
  { type: "job_hunt", label: "Job hunt" },
  { type: "cold_mail", label: "Cold mail" },
  { type: "price_watch", label: "Price watch" },
];

/** Missing intake fields, phrased as the question the user
 * would answer. Unknown fields fall back to the raw name —
 * never silently dropped. */
const MISSING_FIELD_PROMPTS: Record<string, string> = {
  stipend_min: "Minimum stipend (₹/month)?",
  allowed_sites: "Which sites may it use?",
  recipients: "Who should it mail?",
  role_ids: "Which roles should it mail about?",
  url: "Which page should it watch?",
  selector: "Which element (CSS selector) should it watch?",
  target_price: "At what price should it act?",
  currency: "Which currency?",
  min_score: "Minimum match score (0–100)?",
  sources: "Where should it look — Gmail, pasted roles, or both?",
};

type ActivityTone = "ok" | "warn" | "bad" | "info" | "plain";

function typeLabel(type: string, specs: MissionTypeSpec[]): string {
  return (
    specs.find((s) => s.type === type)?.label ??
    KNOWN_TYPES.find((t) => t.type === type)?.label ??
    type
  );
}

function boundsSummary(m: Mission): string {
  const b = m.bounds ?? ({} as MissionBounds);
  const parts: string[] = [];
  if ((b.max_applies_per_day ?? 0) > 0) {
    parts.push(`≤${b.max_applies_per_day} applies/day`);
  }
  if ((b.max_sends_per_day ?? 0) > 0) {
    parts.push(`≤${b.max_sends_per_day} sends/day`);
  }
  parts.push(`≤${b.max_actions_per_day ?? 0} actions/day`);
  if (b.auto_approve) {
    parts.push("standing approval");
  } else if ((b.require_approval_for ?? []).length > 0) {
    parts.push(`approval needed (${(b.require_approval_for ?? []).join(", ")})`);
  } else {
    parts.push("no approval gate");
  }
  return parts.join(" · ");
}

function lastRunLine(m: Mission): string {
  if (!m.last_run_status) return "Never run";
  const when = m.last_run_at ? fmtDateTime(m.last_run_at) : null;
  switch (m.last_run_status) {
    case "ok":
      return when ? `Last run: ok, ${when}` : "Last run: ok";
    case "waiting_for_extension":
      return "Waiting for Chrome — open Chrome to continue";
    case "stalled":
      return `Stalled — ${m.last_error ?? "waiting for Chrome too long"}`;
    case "failed":
      return `Last run failed — ${m.last_error ?? "see the activity log"}`;
    default:
      return when
        ? `Last run: ${m.last_run_status}, ${when}`
        : `Last run: ${m.last_run_status}`;
  }
}

function activityTone(kind: string): ActivityTone {
  switch (kind) {
    case "applied":
    case "sent":
    case "approved":
      return "ok";
    case "refused":
    case "failed":
      return "bad";
    case "approval_needed":
    case "waiting_for_extension":
      return "warn";
    case "run_started":
    case "discovered":
    case "matched":
    case "drafted":
    case "price_observed":
      return "info";
    default:
      return "plain";
  }
}

/** Activity detail is a redacted JSON string server-side;
 * render it as readable key: value pairs, or the raw string
 * when it is not JSON. */
function formatActivityDetail(detail: string): string {
  try {
    const parsed: unknown = JSON.parse(detail);
    if (parsed && typeof parsed === "object") {
      return Object.entries(parsed as Record<string, unknown>)
        .map(([k, v]) => `${k}: ${displayValue(v)}`)
        .join(" · ");
    }
    return displayValue(parsed);
  } catch {
    return detail;
  }
}

/** The editable bounds block for a draft mission. Bounds are
 * the one part of the draft with a fully specified shape
 * (DESIGN.md §3.5), so they get real controls; config is
 * type-specific and stays an editable JSON textarea. */
function BoundsEditor({
  bounds,
  onChange,
}: {
  bounds: Partial<MissionBounds>;
  onChange: (next: Partial<MissionBounds>) => void;
}) {
  const setNum = (
    key: "max_actions_per_day" | "max_applies_per_day" | "max_sends_per_day" | "min_score",
    value: string,
  ) => {
    onChange({ ...bounds, [key]: value === "" ? undefined : Number(value) });
  };
  const toggleApproval = (kind: string) => {
    const current = bounds.require_approval_for ?? [];
    onChange({
      ...bounds,
      require_approval_for: current.includes(kind)
        ? current.filter((k) => k !== kind)
        : [...current, kind],
    });
  };
  return (
    <fieldset>
      <legend>Bounds — the mission cannot act outside these</legend>
      <div className="form-grid">
        <div>
          <label htmlFor="draft-max-actions">Max actions per day</label>
          <input
            id="draft-max-actions"
            type="number"
            min={0}
            value={bounds.max_actions_per_day ?? ""}
            onChange={(e) => setNum("max_actions_per_day", e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="draft-max-applies">Max applies per day</label>
          <input
            id="draft-max-applies"
            type="number"
            min={0}
            value={bounds.max_applies_per_day ?? ""}
            onChange={(e) => setNum("max_applies_per_day", e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="draft-max-sends">Max sends per day</label>
          <input
            id="draft-max-sends"
            type="number"
            min={0}
            value={bounds.max_sends_per_day ?? ""}
            onChange={(e) => setNum("max_sends_per_day", e.target.value)}
          />
        </div>
        <div>
          <label htmlFor="draft-min-score">Minimum match score (0–100, job hunt)</label>
          <input
            id="draft-min-score"
            type="number"
            min={0}
            max={100}
            value={bounds.min_score ?? ""}
            onChange={(e) => setNum("min_score", e.target.value)}
          />
        </div>
      </div>
      <label htmlFor="draft-sites">
        Allowed sites (comma-separated, blank = current site only)
      </label>
      <input
        id="draft-sites"
        value={(bounds.allowed_sites ?? []).join(", ")}
        onChange={(e) =>
          onChange({
            ...bounds,
            allowed_sites: e.target.value
              .split(",")
              .map((s) => s.trim())
              .filter(Boolean),
          })
        }
        placeholder="internshala.com, linkedin.com"
        autoComplete="off"
        autoCorrect="off"
        autoCapitalize="none"
        spellCheck={false}
      />
      <fieldset>
        <legend>Needs your approval before</legend>
        <label className="check-row">
          <input
            type="checkbox"
            checked={(bounds.require_approval_for ?? []).includes("apply")}
            onChange={() => toggleApproval("apply")}
          />
          Applying to a role
        </label>
        <label className="check-row">
          <input
            type="checkbox"
            checked={(bounds.require_approval_for ?? []).includes("send")}
            onChange={() => toggleApproval("send")}
          />
          Sending a mail
        </label>
      </fieldset>
      <label className="check-row">
        <input
          type="checkbox"
          checked={bounds.auto_approve ?? false}
          onChange={(e) => onChange({ ...bounds, auto_approve: e.target.checked })}
        />
        Standing approval — apply/send within bounds without asking
      </label>
      {bounds.auto_approve && (
        <p role="note" className="warning">
          Standing approval only covers action kinds you UNCHECK above
          (“Needs your approval before”) — a kind left checked still asks
          you every time. Caps still apply, and every action still goes
          through the approval record.
        </p>
      )}
    </fieldset>
  );
}

/** The detail pane under a selected mission card: config,
 * pending approvals, linked tasks, activity timeline. */
function MissionDetailPane({
  detail,
  onSelectTask,
  onApprove,
  approving,
}: {
  detail: MissionDetail;
  onSelectTask?: (taskId: string) => void;
  onApprove: (a: MissionApproval) => void;
  approving: string | null;
}) {
  const { mission, activity, tasks, pending_approvals } = detail;
  const sorted = [...activity].sort((a, b) => b.created_at - a.created_at);
  const now = Date.now();
  return (
    <div>
      <h3>Config</h3>
      {Object.keys(mission.config).length === 0 ? (
        <p className="muted">No config stored.</p>
      ) : (
        <dl className="kv">
          {Object.entries(mission.config).map(([k, v]) => (
            <Fragment key={k}>
              <dt>{k}</dt>
              <dd className="mono">{displayValue(v)}</dd>
            </Fragment>
          ))}
        </dl>
      )}

      <h3>Pending approvals</h3>
      {pending_approvals.length === 0 ? (
        <p className="muted">Nothing waiting on you right now.</p>
      ) : (
        <ul className="plain-list">
          {pending_approvals.map((a) => {
            const expired = a.expires_at * 1000 < now;
            return (
              <li key={a.approval_id}>
                <StatusChip tone={expired ? "plain" : "warn"}>
                  {expired ? "expired" : a.action}
                </StatusChip>{" "}
                <span className="secondary">
                  {a.action} · task <span className="mono">{a.task_id}</span>
                  {a.step !== null ? ` · step ${a.step}` : ""}
                  {a.role_id ? ` · role ${a.role_id}` : ""}
                  {a.draft_id ? ` · draft ${a.draft_id}` : ""}
                </span>
                <p className="muted">
                  Requested {fmtDateTime(a.created_at)} ·{" "}
                  {expired
                    ? `expired ${fmtDateTime(a.expires_at)}`
                    : `expires ${fmtDateTime(a.expires_at)}`}
                </p>
                {expired ? (
                  <p className="muted">
                    Expired — the mission will ask again if it still needs
                    this.
                  </p>
                ) : (
                  <div className="task-actions">
                    <button
                      type="button"
                      className="primary"
                      onClick={() => onApprove(a)}
                      disabled={approving !== null}
                    >
                      {approving === a.approval_id ? "Approving…" : "Approve"}
                    </button>
                    {onSelectTask && (
                      <button type="button" onClick={() => onSelectTask(a.task_id)}>
                        Open task
                      </button>
                    )}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <h3>Linked tasks</h3>
      {tasks.length === 0 ? (
        <p className="muted">No tasks spawned yet — the first run creates one.</p>
      ) : (
        <ul className="plain-list">
          {tasks.map((t) => (
            <li key={t.id}>
              <StatusChip tone={statusTone(t.status)}>{t.status}</StatusChip>{" "}
              <span>{t.goal}</span>
              <p className="muted mono">
                {t.id} · {fmtDateTime(t.created_at)}
              </p>
              {onSelectTask && (
                <div className="task-actions">
                  <button type="button" onClick={() => onSelectTask(t.id)}>
                    Open in Agent view
                  </button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      <h3>Activity</h3>
      {sorted.length === 0 ? (
        <p className="muted">Nothing recorded yet.</p>
      ) : (
        <ul className="plain-list">
          {sorted.map((a) => (
            <li key={a.id}>
              <StatusChip tone={activityTone(a.kind)}>{a.kind}</StatusChip>{" "}
              <span className="muted mono">{fmtDateTime(a.created_at)}</span>
              <p>{formatActivityDetail(a.detail)}</p>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function MissionsView({
  token,
  onSelectTask,
  onGoToJobs,
}: {
  token: string | null;
  onSelectTask?: (taskId: string) => void;
  onGoToJobs?: () => void;
}) {
  const [missions, setMissions] = useState<Mission[]>([]);
  // 24h activity counts per mission; null = the count call
  // failed, so the card says "unknown", never a fake zero.
  const [activityCounts, setActivityCounts] = useState<Record<string, number | null>>({});
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastOk, setLastOk] = useState<number | null>(null);

  // Meta state (profile, resume, type registry): load once,
  // refreshed after resume changes.
  const [profile, setProfile] = useState<Profile | null>(null);
  const [profileMissing, setProfileMissing] = useState(false);
  const [resume, setResume] = useState<ResumeStatus | null>(null);
  const [typeSpecs, setTypeSpecs] = useState<MissionTypeSpec[]>([]);

  // Intake composer + draft.
  const [message, setMessage] = useState("");
  const [drafting, setDrafting] = useState(false);
  const [intake, setIntake] = useState<IntakeResponse | null>(null);
  const [draftName, setDraftName] = useState("");
  const [draftType, setDraftType] = useState<MissionType>("job_hunt");
  const [draftConfigText, setDraftConfigText] = useState("");
  const [draftConfigError, setDraftConfigError] = useState<string | null>(null);
  const [draftBounds, setDraftBounds] = useState<Partial<MissionBounds>>({});
  const [creating, setCreating] = useState(false);
  const [draftError, setDraftError] = useState<string | null>(null);

  // Resume attach.
  const [resumeFile, setResumeFile] = useState<File | null>(null);
  const [resumeText, setResumeText] = useState("");
  const [resumeBusy, setResumeBusy] = useState<"upload" | "paste" | null>(null);
  const [resumeMsg, setResumeMsg] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  // Selected mission detail.
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MissionDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // Lifecycle actions + approvals.
  const [pending, setPending] = useState<string | null>(null);
  const [stopId, setStopId] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [approving, setApproving] = useState<string | null>(null);

  const load = async () => {
    try {
      const data = await api.listMissions();
      setMissions(data.missions);
      setError(null);
      setLastOk(Date.now());
      // 24h activity counts, one call per mission; a failed
      // count shows as unknown, never as zero.
      const counts = await Promise.allSettled(
        data.missions.map((m) => api.missionActivity(m.id, 100)),
      );
      const next: Record<string, number | null> = {};
      const dayAgo = Date.now() / 1000 - 24 * 3600;
      data.missions.forEach((m, i) => {
        const r = counts[i];
        next[m.id] =
          r.status === "fulfilled"
            ? r.value.activity.filter((a) => a.created_at >= dayAgo).length
            : null;
      });
      setActivityCounts(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const loadMeta = async () => {
    try {
      const data = await api.getProfile();
      setProfile(data.profile);
      setProfileMissing(false);
    } catch (err) {
      if (isHttpError(err, 404)) {
        setProfile(null);
        setProfileMissing(true);
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    }
    try {
      const r = await api.getResume();
      setResume(r);
    } catch (err) {
      // A 404 means no resume is stored — not an error.
      // (It also covers the missions router not existing yet.)
      if (isHttpError(err, 404)) {
        setResume({
          stored: false,
          filename: null,
          uploaded_at: null,
          extraction: null,
          detail: null,
        });
      } else {
        setError(err instanceof Error ? err.message : String(err));
      }
    }
    try {
      const t = await api.missionTypes();
      setTypeSpecs(t.types);
    } catch {
      // Types are a label nicety; the view falls back to the
      // three known types when the registry is unreachable.
    }
  };

  usePoll(load, [token], 15000);
  usePoll(loadMeta, [token], 0);

  // The detail pane polls while a mission is selected:
  // approvals expire in 10 minutes, so a load-once pane
  // would show dead Approve buttons.
  useEffect(() => {
    setDetail(null);
    setDetailError(null);
    if (!selectedId || !token) return;
    let alive = true;
    setDetailLoading(true);
    const loadDetail = async () => {
      try {
        const d = await api.getMission(selectedId);
        if (alive) {
          setDetail(d);
          setDetailError(null);
        }
      } catch (err) {
        if (alive) setDetailError(err instanceof Error ? err.message : String(err));
      } finally {
        if (alive) setDetailLoading(false);
      }
    };
    void loadDetail();
    const timer = setInterval(loadDetail, 15000);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  }, [selectedId, token]);

  const draftMission = async () => {
    const text = message.trim();
    if (!text) return;
    setDrafting(true);
    setDraftError(null);
    try {
      const res = await api.intakeMission(text);
      setIntake(res);
      setDraftName(res.draft.name || typeLabel(res.draft.type, typeSpecs));
      setDraftType(res.draft.type);
      setDraftConfigText(JSON.stringify(res.draft.config ?? {}, null, 2));
      setDraftBounds(res.draft.bounds ?? {});
      setDraftConfigError(null);
    } catch (err) {
      setDraftError(err instanceof Error ? err.message : String(err));
    } finally {
      setDrafting(false);
    }
  };

  const create = async () => {
    let config: Record<string, unknown>;
    try {
      config = JSON.parse(draftConfigText || "{}") as Record<string, unknown>;
    } catch {
      setDraftConfigError("Config is not valid JSON — fix it before creating.");
      return;
    }
    if (typeof config !== "object" || config === null || Array.isArray(config)) {
      setDraftConfigError("Config must be a JSON object, not an array or a value.");
      return;
    }
    if (!draftName.trim()) {
      setDraftError("Give the mission a name.");
      return;
    }
    setCreating(true);
    setDraftError(null);
    try {
      const m = await api.createMission({
        type: draftType,
        name: draftName.trim(),
        config,
        bounds: draftBounds,
      });
      setIntake(null);
      setMessage("");
      setSelectedId(m.id);
      await load();
    } catch (err) {
      setDraftError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  const upload = async () => {
    if (!resumeFile) return;
    setResumeBusy("upload");
    setResumeMsg(null);
    try {
      const r = await api.uploadResume(resumeFile);
      setResume(r);
      setResumeFile(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
      setResumeMsg(
        r.extraction === "ok"
          ? "Resume stored and parsed."
          : r.extraction === "limited"
            ? "Resume stored — only some fields could be read."
            : r.extraction === "failed"
              ? "Resume stored, but parsing found nothing usable."
              : "Resume stored.",
      );
      await loadMeta();
    } catch (err) {
      setResumeMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setResumeBusy(null);
    }
  };

  const pasteResume = async () => {
    if (resumeText.trim().length < 10) {
      setResumeMsg("The resume text needs at least 10 characters.");
      return;
    }
    setResumeBusy("paste");
    setResumeMsg(null);
    try {
      const r = await api.pasteResumeText(resumeText);
      setResume(r);
      setResumeText("");
      setResumeMsg(
        r.extraction === "ok"
          ? "Resume stored and parsed."
          : r.extraction === "limited"
            ? "Resume stored — only some fields could be read."
            : r.extraction === "failed"
              ? "Resume stored, but parsing found nothing usable."
              : "Resume stored.",
      );
      await loadMeta();
    } catch (err) {
      setResumeMsg(err instanceof Error ? err.message : String(err));
    } finally {
      setResumeBusy(null);
    }
  };

  const runAction = async (
    id: string,
    kind: "activate" | "pause" | "resume" | "run-now" | "stop" | "delete",
  ) => {
    setPending(`${kind}:${id}`);
    setError(null);
    try {
      if (kind === "activate") await api.activateMission(id);
      if (kind === "pause") await api.pauseMission(id);
      if (kind === "resume") await api.resumeMission(id);
      if (kind === "run-now") await api.runMissionNow(id);
      if (kind === "stop") await api.stopMission(id);
      if (kind === "delete") {
        await api.deleteMission(id);
        if (selectedId === id) setSelectedId(null);
      }
      await load();
      if (selectedId === id && kind !== "delete") {
        const d = await api.getMission(id);
        setDetail(d);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const approve = async (missionId: string, a: MissionApproval) => {
    setApproving(a.approval_id);
    setDetailError(null);
    try {
      // Draft approvals go through the mission approve-mail
      // endpoint (it marks the draft approved and mints a
      // draft-scoped grant); task approvals go through the
      // existing task gate. Either way the approval id comes
      // from the server's pending record, never a client-made id.
      if (a.draft_id) {
        await api.approveMissionMail(missionId, a.approval_id, a.draft_id);
      } else {
        await api.approveTask(a.task_id, a.approval_id);
      }
      if (selectedId) {
        const d = await api.getMission(selectedId);
        setDetail(d);
      }
      await load();
    } catch (err) {
      setDetailError(err instanceof Error ? err.message : String(err));
    } finally {
      setApproving(null);
    }
  };

  const typeOptions =
    typeSpecs.length > 0
      ? typeSpecs.map((s) => ({ type: s.type, label: s.label }))
      : KNOWN_TYPES;

  return (
    <div className="view missions-view">
      <div className="view-head">
        <h2>Missions</h2>
        <p className="muted">
          A mission watches and prepares on its own schedule — job hunt,
          cold mail, or price watch. Every apply and send still asks you
          first, unless you switch on standing approval. Missions run
          while this laptop and server are on.
        </p>
      </div>
      {error && <ErrorBanner error={error} lastOk={lastOk} onRetry={() => void load()} />}

      <section aria-label="Describe a mission" className="pane">
        <SectionHeader title="Describe a mission" />
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void draftMission();
          }}
        >
          <label htmlFor="mission-message">Describe the mission in one message</label>
          <textarea
            id="mission-message"
            name="message"
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => {
              // IME-safe Enter-to-submit (guide:
              // ime-safe-enter-submit): Enter drafts,
              // Shift+Enter is a newline, and an Enter that
              // only confirms an IME candidate never submits.
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229) return;
                void draftMission();
              }
            }}
            placeholder="e.g. watch for SDE intern roles in Hyderabad, min ₹10,000 stipend, apply to at most 3 a day"
            rows={3}
            maxLength={2000}
            required
            aria-describedby="mission-message-help"
          />
          <p id="mission-message-help" className="muted">
            One message is enough — the server drafts the mission and you
            review it before anything is created. Enter drafts, Shift+Enter
            adds a line. {message.length}/2000 characters.
          </p>
          <button type="submit" className="primary" disabled={drafting || !message.trim()}>
            {drafting ? "Drafting…" : "Draft mission"}
          </button>
          {draftError && (
            <p role="alert" className="danger">
              {draftError}
            </p>
          )}
        </form>

        {intake && (
          <div className="inline-form" aria-label="Draft mission">
            <SectionHeader title="Draft mission — review before creating" />
            <p role="status">
              {/* The intake source is always labelled, never hidden. */}
              <StatusChip tone={intake.source === "planner" ? "info" : "plain"}>
                {intake.source === "planner"
                  ? "Drafted by the planner"
                  : "Drafted by rules"}
              </StatusChip>{" "}
              <StatusChip tone={intake.confidence === "high" ? "ok" : "warn"}>
                confidence: {intake.confidence}
              </StatusChip>
            </p>
            {intake.warnings.length > 0 && (
              <ul className="plain-list">
                {intake.warnings.map((w, i) => (
                  <li key={i} className="warning">
                    {w}
                  </li>
                ))}
              </ul>
            )}
            {intake.missing_fields.length > 0 && (
              <div>
                <p className="secondary">
                  Still missing — fill it in below, or say it in a new message:
                </p>
                <ul className="plain-list">
                  {intake.missing_fields.map((f) => (
                    <li key={f}>{MISSING_FIELD_PROMPTS[f] ?? f}</li>
                  ))}
                </ul>
              </div>
            )}
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void create();
              }}
            >
              <label htmlFor="draft-name">Mission name</label>
              <input
                id="draft-name"
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                maxLength={120}
                required
              />
              <label htmlFor="draft-type">Type</label>
              <select
                id="draft-type"
                value={draftType}
                onChange={(e) => setDraftType(e.target.value as MissionType)}
              >
                {typeOptions.map((t) => (
                  <option key={t.type} value={t.type}>
                    {t.label}
                  </option>
                ))}
              </select>
              <label htmlFor="draft-config">
                Config (JSON — edit what the draft got wrong)
              </label>
              <textarea
                id="draft-config"
                className="mono"
                value={draftConfigText}
                onChange={(e) => {
                  setDraftConfigText(e.target.value);
                  setDraftConfigError(null);
                }}
                rows={6}
                spellCheck={false}
              />
              {draftConfigError && (
                <p role="alert" className="danger">
                  {draftConfigError}
                </p>
              )}
              <BoundsEditor bounds={draftBounds} onChange={setDraftBounds} />
              <div className="task-actions">
                <button type="submit" className="primary" disabled={creating}>
                  {creating ? "Creating…" : "Create mission"}
                </button>
                <button type="button" onClick={() => setIntake(null)}>
                  Discard draft
                </button>
              </div>
              {draftError && (
                <p role="alert" className="danger">
                  {draftError}
                </p>
              )}
            </form>
          </div>
        )}

        <div className="inline-form">
          <SectionHeader title="Resume" />
          <p role="status" className="secondary">
            {resume === null
              ? "Checking for a stored resume…"
              : resume.stored
                ? (
                  <>
                    Resume stored
                    {resume.filename ? (
                      <>
                        : <span className="mono">{resume.filename}</span>
                      </>
                    ) : (
                      ""
                    )}
                    {resume.uploaded_at ? ` · uploaded ${fmtDateTime(resume.uploaded_at)}` : ""}{" "}
                    — extraction {resume.extraction ?? "unknown"}
                    {resume.detail ? ` (${resume.detail})` : ""}.
                  </>
                )
                : "No resume stored yet — cold-mail missions attach it to drafts."}
          </p>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void upload();
            }}
          >
            <label htmlFor="resume-file">Attach a resume file</label>
            <input
              id="resume-file"
              ref={fileInputRef}
              type="file"
              accept=".pdf,.txt,.md,.doc,.docx"
              onChange={(e) => setResumeFile(e.target.files?.[0] ?? null)}
            />
            {resumeFile && <p className="muted">Selected: {resumeFile.name}</p>}
            <button
              type="submit"
              className="primary"
              disabled={!resumeFile || resumeBusy !== null}
            >
              {resumeBusy === "upload" ? "Uploading…" : "Upload resume"}
            </button>
          </form>
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void pasteResume();
            }}
          >
            <label htmlFor="resume-text">Or paste resume text</label>
            <textarea
              id="resume-text"
              value={resumeText}
              onChange={(e) => setResumeText(e.target.value)}
              rows={4}
              placeholder="Paste the resume text here…"
            />
            <button type="submit" disabled={!resumeText.trim() || resumeBusy !== null}>
              {resumeBusy === "paste" ? "Saving…" : "Save pasted resume"}
            </button>
          </form>
          {resumeMsg && (
            <p role="status" className="secondary">
              {resumeMsg}
            </p>
          )}
          <p className="muted">
            {profileMissing ? (
              <>
                No job profile stored — job hunt and cold mail missions match
                roles against it.{" "}
                {onGoToJobs ? (
                  <button type="button" onClick={onGoToJobs}>
                    Add it in the Jobs view
                  </button>
                ) : (
                  "Add it in the Jobs view."
                )}
              </>
            ) : profile ? (
              <>
                Job profile stored{profile.name ? ` for ${profile.name}` : ""}
                {profile.skills.length > 0
                  ? ` · ${profile.skills.length} skill${profile.skills.length === 1 ? "" : "s"}`
                  : ""}
                .
              </>
            ) : (
              "Checking for a job profile…"
            )}
          </p>
        </div>
      </section>

      <section aria-label="Missions" className="pane">
        <SectionHeader title={`Missions (${missions.length})`} />
        {loading && <p className="muted">Loading missions…</p>}
        {!loading && missions.length === 0 && (
          <EmptyState>
            No missions yet. Describe one above — job hunt, cold mail, or
            price watch — and it will run on its own schedule.
          </EmptyState>
        )}
        <ul className="watch-list">
          {missions.map((m) => {
            const count = activityCounts[m.id];
            const countKnown = m.id in activityCounts;
            return (
              <li key={m.id}>
                <div className="watch-head">
                  <strong>{m.name}</strong>
                  <span>
                    <StatusChip tone="plain">
                      {typeLabel(m.type, typeSpecs)}
                    </StatusChip>{" "}
                    <StatusChip tone={statusTone(m.status)}>{m.status}</StatusChip>
                  </span>
                </div>
                <p className="secondary">{lastRunLine(m)}</p>
                <p className="muted">{boundsSummary(m)}</p>
                <p className="muted">
                  {!countKnown
                    ? "Activity in the last 24 hours: loading…"
                    : count === null
                      ? "Activity in the last 24 hours: unknown"
                      : `${count} activit${count === 1 ? "y" : "ies"} in the last 24 hours`}
                  {m.next_run_at
                    ? ` · next run ${fmtDateTime(m.next_run_at)}`
                    : m.status === "active"
                      ? " · next run not scheduled"
                      : ""}
                </p>
                {m.bounds?.auto_approve && (
                  <p className="warning">
                    Standing approval: applies/sends within bounds without
                    asking.
                  </p>
                )}
                <div className="task-actions">
                  <button
                    type="button"
                    onClick={() => setSelectedId(selectedId === m.id ? null : m.id)}
                    aria-expanded={selectedId === m.id}
                    aria-controls={`mission-detail-${m.id}`}
                    disabled={pending !== null}
                  >
                    {selectedId === m.id ? "Hide details" : "Details"}
                  </button>
                  {m.status === "draft" && (
                    <button
                      type="button"
                      onClick={() => void runAction(m.id, "activate")}
                      disabled={pending !== null}
                    >
                      {pending === `activate:${m.id}` ? "Activating…" : "Activate"}
                    </button>
                  )}
                  {m.status === "active" && (
                    <>
                      <button
                        type="button"
                        onClick={() => void runAction(m.id, "pause")}
                        disabled={pending !== null}
                      >
                        {pending === `pause:${m.id}` ? "Pausing…" : "Pause"}
                      </button>
                      <button
                        type="button"
                        title="Schedules an immediate tick — the runner picks it up within a minute."
                        onClick={() => void runAction(m.id, "run-now")}
                        disabled={pending !== null}
                      >
                        {pending === `run-now:${m.id}` ? "Scheduling…" : "Run now"}
                      </button>
                    </>
                  )}
                  {m.status === "paused" && (
                    <>
                      <button
                        type="button"
                        onClick={() => void runAction(m.id, "resume")}
                        disabled={pending !== null}
                      >
                        {pending === `resume:${m.id}` ? "Resuming…" : "Resume"}
                      </button>
                      <button
                        type="button"
                        title="Schedules an immediate tick — the runner picks it up within a minute."
                        onClick={() => void runAction(m.id, "run-now")}
                        disabled={pending !== null}
                      >
                        {pending === `run-now:${m.id}` ? "Scheduling…" : "Run now"}
                      </button>
                    </>
                  )}
                  {(m.status === "active" ||
                    m.status === "paused" ||
                    m.status === "draft") && (
                    <button
                      type="button"
                      onClick={() => setStopId(m.id)}
                      disabled={pending !== null}
                    >
                      Stop
                    </button>
                  )}
                  {(m.status === "draft" || m.status === "stopped") && (
                    <button
                      type="button"
                      onClick={() => setDeleteId(m.id)}
                      disabled={pending !== null}
                    >
                      Delete
                    </button>
                  )}
                </div>
                {selectedId === m.id && (
                  <div id={`mission-detail-${m.id}`} className="inline-form">
                    {detailLoading && !detail ? (
                      <p className="muted">Loading detail…</p>
                    ) : detailError ? (
                      <p role="alert" className="danger">
                        {detailError}
                      </p>
                    ) : detail ? (
                      <MissionDetailPane
                        detail={detail}
                        onSelectTask={onSelectTask}
                        onApprove={(a) => void approve(detail.mission.id, a)}
                        approving={approving}
                      />
                    ) : null}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      </section>

      <ConfirmDialog
        open={stopId !== null}
        title="Stop this mission?"
        confirmLabel="Stop mission"
        danger
        onConfirm={() => {
          const id = stopId;
          setStopId(null);
          if (id) void runAction(id, "stop");
        }}
        onCancel={() => setStopId(null)}
      >
        <p>
          Stopping is terminal — the mission cannot be restarted. Its live
          approval grants are revoked, pending approvals are consumed, and
          its running tasks are cancelled.
        </p>
      </ConfirmDialog>
      <ConfirmDialog
        open={deleteId !== null}
        title="Delete this mission?"
        confirmLabel="Delete mission"
        danger
        onConfirm={() => {
          const id = deleteId;
          setDeleteId(null);
          if (id) void runAction(id, "delete");
        }}
        onCancel={() => setDeleteId(null)}
      >
        <p>
          The mission and its activity log are deleted. Receipts, drafts and
          tasks stay — they are the audit trail.
        </p>
      </ConfirmDialog>
    </div>
  );
}
