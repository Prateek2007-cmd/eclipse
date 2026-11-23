import { useState } from "react";
import "./JobsView.css";
import { api, isHttpError, type MatchResponse, type Profile, type RoleInput } from "../api";
import { usePoll, useTaskList } from "../hooks";
import { ConfirmDialog } from "../components/ConfirmDialog";
import {
  Disclosure,
  EmptyState,
  ErrorBanner,
  RelativeTime,
  SectionHeader,
  ShowMore,
  Spinner,
  StatusChip,
  SummaryChips,
  humanStatus,
  statusTone,
} from "../components/ui";

const EMPTY_ROLE: RoleInput = {
  id: "",
  title: "",
  company: "",
  site: "",
  url: "",
  stipend: null,
  location: "",
  deadline: null,
  skills: [],
  min_year: null,
  degree: null,
  min_cgpa: null,
  requires_experience: false,
};

/** How many shortlist cards show before "Show more". */
const SHORTLIST_PAGE_SIZE = 5;

/** Jobs view: the resume profile (parsed locally, nothing
 * leaves the device), role matching against it, the applied
 * check, and receipts. Roles are staged here by hand — there
 * is no role-listing endpoint; the extension or the user
 * supplies the roles to match. */
export function JobsView({ token }: { token: string | null }) {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [profileMissing, setProfileMissing] = useState(false);
  const [resumeText, setResumeText] = useState("");
  const [savingProfile, setSavingProfile] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [roleDraft, setRoleDraft] = useState<RoleInput>(EMPTY_ROLE);
  const [skillsText, setSkillsText] = useState("");
  const [staged, setStaged] = useState<RoleInput[]>([]);
  const [match, setMatch] = useState<MatchResponse | null>(null);
  const [matching, setMatching] = useState(false);
  const [shortlistShown, setShortlistShown] = useState(SHORTLIST_PAGE_SIZE);

  const [appliedRoleId, setAppliedRoleId] = useState("");
  const [appliedUrl, setAppliedUrl] = useState("");
  const [appliedResult, setAppliedResult] = useState<boolean | null>(null);
  const [appliedCheckedFor, setAppliedCheckedFor] = useState("");
  const [appliedCheckedAt, setAppliedCheckedAt] = useState<number | null>(null);
  const [checkingApplied, setCheckingApplied] = useState(false);

  const { tasks } = useTaskList(token);
  const [receiptTaskId, setReceiptTaskId] = useState("");
  const [receiptRoleId, setReceiptRoleId] = useState("");
  const [receiptTitle, setReceiptTitle] = useState("");
  const [receiptCompany, setReceiptCompany] = useState("");
  const [receiptUrl, setReceiptUrl] = useState("");
  const [receiptConfirmation, setReceiptConfirmation] = useState("");
  const [receiptStatus, setReceiptStatus] = useState<"submitted" | "failed">("submitted");
  const [confirmReceipt, setConfirmReceipt] = useState(false);
  // The receipt just recorded, kept for its confirmation row —
  // the form fields are cleared on save, so the row needs its
  // own copy of the title/company/status.
  const [savedReceipt, setSavedReceipt] = useState<{
    id: string;
    title: string;
    company: string;
    status: "submitted" | "failed";
    at: number;
  } | null>(null);
  const [savingReceipt, setSavingReceipt] = useState(false);

  usePoll(
    async () => {
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
    },
    [token],
    0,
  );

  const saveProfile = async () => {
    if (resumeText.trim().length < 10) {
      setError("The resume text needs at least 10 characters.");
      return;
    }
    setSavingProfile(true);
    setError(null);
    try {
      const data = await api.setProfile(resumeText);
      setProfile(data.profile);
      setProfileMissing(false);
      setResumeText("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingProfile(false);
    }
  };

  const addRole = () => {
    if (!roleDraft.id.trim() || !roleDraft.title.trim()) {
      setError("A role needs an id and a title.");
      return;
    }
    const role: RoleInput = {
      ...roleDraft,
      id: roleDraft.id.trim(),
      title: roleDraft.title.trim(),
      skills: skillsText
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean),
    };
    setStaged((prev) => [...prev.filter((r) => r.id !== role.id), role]);
    setRoleDraft(EMPTY_ROLE);
    setSkillsText("");
    setError(null);
  };

  const runMatch = async () => {
    if (staged.length === 0) return;
    setMatching(true);
    setError(null);
    try {
      const data = await api.matchRoles(staged);
      setMatch(data);
      setShortlistShown(SHORTLIST_PAGE_SIZE);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setMatching(false);
    }
  };

  const checkApplied = async () => {
    if (!appliedRoleId.trim() && !appliedUrl.trim()) {
      setError("Give a role id or a URL to check.");
      return;
    }
    setCheckingApplied(true);
    setError(null);
    try {
      const data = await api.checkApplied(appliedRoleId.trim(), appliedUrl.trim());
      setAppliedResult(data.already_applied);
      setAppliedCheckedFor(appliedRoleId.trim() || appliedUrl.trim());
      setAppliedCheckedAt(Math.floor(Date.now() / 1000));
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCheckingApplied(false);
    }
  };

  const saveReceipt = async () => {
    setConfirmReceipt(false);
    setSavingReceipt(true);
    setError(null);
    setSavedReceipt(null);
    try {
      const data = await api.addReceipt(receiptTaskId, {
        role_id: receiptRoleId.trim(),
        title: receiptTitle.trim(),
        company: receiptCompany.trim(),
        url: receiptUrl.trim(),
        confirmation: receiptConfirmation.trim(),
        status: receiptStatus,
      });
      setSavedReceipt({
        id: data.receipt_id,
        title: receiptTitle.trim(),
        company: receiptCompany.trim(),
        status: receiptStatus,
        at: Math.floor(Date.now() / 1000),
      });
      setReceiptRoleId("");
      setReceiptTitle("");
      setReceiptCompany("");
      setReceiptUrl("");
      setReceiptConfirmation("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSavingReceipt(false);
    }
  };

  const setRole = <K extends keyof RoleInput>(key: K, value: RoleInput[K]) =>
    setRoleDraft((prev) => ({ ...prev, [key]: value }));

  // Best-first: the server usually returns the shortlist
  // sorted, but the view does not trust the order.
  const sortedShortlist = match
    ? [...match.shortlist].sort((a, b) => b.score - a.score)
    : [];

  // The profile, summary-first: name, degree/year/CGPA/email
  // as one line, skills + locations + stipend floor as chips.
  const profileFacts = profile
    ? [
        profile.degree,
        profile.year !== null ? `Year ${profile.year}` : null,
        profile.cgpa !== null ? `CGPA ${profile.cgpa}` : null,
        profile.email,
      ].filter((part): part is string => part !== null && part !== "")
    : [];
  const profileChips: { label: string; value: string }[] = profile
    ? [
        ...profile.skills.map((skill) => ({ label: "", value: skill })),
        ...profile.locations.map((location) => ({
          label: "Location",
          value: location,
        })),
        ...(profile.stipend_min !== null
          ? [
              {
                label: "Stipend floor",
                value: `₹${profile.stipend_min}/month`,
              },
            ]
          : []),
      ]
    : [];

  return (
    <div className="view jobs-view">
      <div className="view-head">
        <h2>Jobs</h2>
        <p className="muted">
          The resume is parsed on this device. Matching ranks roles against
          it — nothing is applied to without a task and an approval.
        </p>
      </div>
      {error && <ErrorBanner error={error} />}

      <section aria-label="Profile" className="pane">
        <SectionHeader title="Profile" />
        {profile ? (
          <div className="profile-summary">
            <p className="profile-name">{profile.name ?? "Unnamed profile"}</p>
            {profileFacts.length > 0 && (
              <p className="muted profile-facts">{profileFacts.join(" · ")}</p>
            )}
            <SummaryChips items={profileChips} />
            {profileChips.length === 0 && (
              <p className="muted">
                No skills, locations, or stipend floor found in this resume.
              </p>
            )}
          </div>
        ) : profileMissing ? (
          <EmptyState>
            No profile stored yet. Paste your resume text and Eclipse parses
            it locally — CGPA, degree, year, skills, locations, stipend
            floor.
          </EmptyState>
        ) : (
          <div className="skeleton-stack" aria-hidden="true">
            <div className="skeleton skeleton-title" style={{ width: "40%" }} />
            <div className="skeleton skeleton-line" style={{ width: "70%" }} />
            <div className="skeleton skeleton-line" style={{ width: "55%" }} />
          </div>
        )}
        <Disclosure
          summary={profile ? "Edit profile" : "Add your resume"}
          defaultOpen={profileMissing}
          className="profile-edit"
        >
          <form
            onSubmit={(e) => {
              e.preventDefault();
              void saveProfile();
            }}
          >
            <label htmlFor="resume-text">Resume text</label>
            <textarea
              id="resume-text"
              value={resumeText}
              onChange={(e) => setResumeText(e.target.value)}
              placeholder="Paste the resume as plain text…"
              rows={6}
            />
            <button type="submit" className="primary" disabled={savingProfile || resumeText.trim().length < 10}>
              {savingProfile ? "Parsing…" : profile ? "Replace profile" : "Parse resume"}
            </button>
          </form>
        </Disclosure>
      </section>

      <section aria-label="Match roles" className="pane">
        <SectionHeader title="Match roles" />
        {!profile && (
          <p className="muted">
            Matching needs a stored profile first — the server refuses to
            match without one.
          </p>
        )}
        <form
          onSubmit={(e) => {
            e.preventDefault();
            addRole();
          }}
        >
          <div className="form-grid">
            <div>
              <label htmlFor="role-id">Role id</label>
              <input
                id="role-id"
                value={roleDraft.id}
                onChange={(e) => setRole("id", e.target.value)}
                placeholder="e.g. internshala-12345"
              />
            </div>
            <div>
              <label htmlFor="role-title">Title</label>
              <input
                id="role-title"
                value={roleDraft.title}
                onChange={(e) => setRole("title", e.target.value)}
                placeholder="e.g. SDE Intern"
              />
            </div>
            <div>
              <label htmlFor="role-company">Company</label>
              <input
                id="role-company"
                value={roleDraft.company ?? ""}
                onChange={(e) => setRole("company", e.target.value)}
              />
            </div>
            <div>
              <label htmlFor="role-url">URL</label>
              <input
                id="role-url"
                value={roleDraft.url ?? ""}
                onChange={(e) => setRole("url", e.target.value)}
              />
            </div>
            <div>
              <label htmlFor="role-stipend">Stipend (₹/month)</label>
              <input
                id="role-stipend"
                type="number"
                value={roleDraft.stipend ?? ""}
                onChange={(e) =>
                  setRole("stipend", e.target.value === "" ? null : Number(e.target.value))
                }
              />
            </div>
            <div>
              <label htmlFor="role-location">Location</label>
              <input
                id="role-location"
                value={roleDraft.location ?? ""}
                onChange={(e) => setRole("location", e.target.value)}
                placeholder="Remote, Hyderabad…"
              />
            </div>
            <div>
              <label htmlFor="role-deadline">Deadline</label>
              <input
                id="role-deadline"
                type="date"
                value={roleDraft.deadline ?? ""}
                onChange={(e) => setRole("deadline", e.target.value || null)}
              />
            </div>
            <div>
              <label htmlFor="role-skills">Skills (comma-separated)</label>
              <input
                id="role-skills"
                value={skillsText}
                onChange={(e) => setSkillsText(e.target.value)}
                placeholder="Python, React, SQL"
              />
            </div>
            <div>
              <label htmlFor="role-min-year">Minimum year</label>
              <input
                id="role-min-year"
                type="number"
                min={1}
                max={5}
                value={roleDraft.min_year ?? ""}
                onChange={(e) =>
                  setRole("min_year", e.target.value === "" ? null : Number(e.target.value))
                }
              />
            </div>
            <div>
              <label htmlFor="role-degree">Degree required</label>
              <input
                id="role-degree"
                value={roleDraft.degree ?? ""}
                onChange={(e) => setRole("degree", e.target.value || null)}
                placeholder="B.Tech"
              />
            </div>
            <div>
              <label htmlFor="role-cgpa">Minimum CGPA</label>
              <input
                id="role-cgpa"
                type="number"
                step="0.01"
                value={roleDraft.min_cgpa ?? ""}
                onChange={(e) =>
                  setRole("min_cgpa", e.target.value === "" ? null : Number(e.target.value))
                }
              />
            </div>
            <div className="check-row">
              <input
                id="role-exp"
                type="checkbox"
                checked={roleDraft.requires_experience ?? false}
                onChange={(e) => setRole("requires_experience", e.target.checked)}
              />
              <label htmlFor="role-exp">Requires experience</label>
            </div>
          </div>
          <button type="submit">Add role to match</button>
        </form>

        {staged.length > 0 && (
          <div className="staged-block">
            <h3>Staged roles ({staged.length})</h3>
            <ul className="staged-list">
              {staged.map((r) => (
                <li key={r.id} className="staged-row">
                  <span className="staged-role">
                    <strong>{r.title}</strong>
                    {r.company ? <span className="muted"> · {r.company}</span> : ""}
                    {" · "}
                    <span className="mono muted">{r.id}</span>
                  </span>
                  <button
                    type="button"
                    onClick={() => setStaged((prev) => prev.filter((x) => x.id !== r.id))}
                  >
                    Remove
                  </button>
                </li>
              ))}
            </ul>
            <button
              type="button"
              className="primary"
              onClick={() => void runMatch()}
              disabled={matching || !profile}
            >
              {matching ? <Spinner label="Matching" /> : "Run match"}
            </button>
          </div>
        )}

        {match ? (
          <div className="match-results">
            <h3>Shortlist ({match.shortlist.length} of {match.count})</h3>
            {sortedShortlist.length === 0 ? (
              <EmptyState>
                Nothing fits. See the excluded roles below for why.
              </EmptyState>
            ) : (
              <>
                <ul className="match-list">
                  {sortedShortlist.slice(0, shortlistShown).map((m) => {
                    // The match response carries only the role id —
                    // the title/company come from the staged role
                    // that was matched.
                    const stagedRole = staged.find((r) => r.id === m.role_id);
                    return (
                      <li key={m.role_id} className="match-card">
                        <div className="match-score" aria-label={`Score ${m.score}`}>
                          <span className="num">{m.score}</span>
                          <span className="match-score-label">score</span>
                        </div>
                        <div className="match-body">
                          <p className="match-title">
                            {stagedRole?.title ?? m.role_id}
                            {stagedRole?.company ? (
                              <span className="muted"> · {stagedRole.company}</span>
                            ) : null}
                          </p>
                          <SummaryChips
                            items={[
                              ...(m.stipend !== null
                                ? [{ label: "", value: `₹${m.stipend}/month` }]
                                : []),
                              ...(m.location
                                ? [{ label: "", value: m.location }]
                                : []),
                              ...(m.deadline
                                ? [{ label: "Deadline", value: m.deadline }]
                                : []),
                            ]}
                          />
                          {m.reasons.length > 0 && (
                            <ul className="match-reasons">
                              {m.reasons.map((reason) => (
                                <li key={reason}>{reason}</li>
                              ))}
                            </ul>
                          )}
                          {m.warnings.length > 0 && (
                            <p className="warning match-warnings">
                              {m.warnings.join(" · ")}
                            </p>
                          )}
                          <p className="muted mono match-id">{m.role_id}</p>
                        </div>
                      </li>
                    );
                  })}
                </ul>
                <ShowMore
                  total={sortedShortlist.length}
                  shown={shortlistShown}
                  onShowMore={() => setShortlistShown(sortedShortlist.length)}
                  label="roles"
                />
              </>
            )}
            {match.excluded.length > 0 && (
              <Disclosure
                summary={`Excluded (${match.excluded.length})`}
                className="match-excluded"
              >
                <ul className="plain-list">
                  {match.excluded.map((m) => {
                    const stagedRole = staged.find((r) => r.id === m.role_id);
                    return (
                      <li key={m.role_id}>
                        <strong>{stagedRole?.title ?? m.role_id}</strong> —{" "}
                        <span className="muted">
                          {m.missing.join("; ") || "does not fit"}
                        </span>
                      </li>
                    );
                  })}
                </ul>
              </Disclosure>
            )}
          </div>
        ) : (
          <EmptyState>
            {staged.length > 0
              ? "No roles matched yet — run a match above."
              : "No roles matched yet — add a role above, then run a match."}
          </EmptyState>
        )}
      </section>

      <section aria-label="Already applied" className="pane">
        <SectionHeader title="Already applied?" />
        <p className="muted">
          Checks receipts and memory for a role id or URL, so the same role
          is never applied to twice.
        </p>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void checkApplied();
          }}
        >
          <label htmlFor="applied-role">Role id</label>
          <input
            id="applied-role"
            value={appliedRoleId}
            onChange={(e) => setAppliedRoleId(e.target.value)}
          />
          <label htmlFor="applied-url">URL</label>
          <input
            id="applied-url"
            value={appliedUrl}
            onChange={(e) => setAppliedUrl(e.target.value)}
          />
          <button type="submit" disabled={checkingApplied}>
            {checkingApplied ? "Checking…" : "Check"}
          </button>
        </form>
        {appliedResult !== null && (
          <div role="status" className="applied-result">
            <StatusChip tone={appliedResult ? "warn" : "ok"}>
              {appliedResult ? "Already applied" : "Not applied yet"}
            </StatusChip>
            <span>
              {appliedResult
                ? "A receipt is on file for this role."
                : "No receipt on file for this role."}
            </span>
            <span className="muted mono">{appliedCheckedFor}</span>
            <span className="muted">
              Checked <RelativeTime ts={appliedCheckedAt} />
            </span>
          </div>
        )}
      </section>

      <section aria-label="Record a receipt" className="pane">
        <SectionHeader title="Record a receipt" />
        <p className="muted">
          A receipt is the record of a verified submit. The loop records one
          itself when it verifies a submit — this form is for a submit you
          verified another way. A duplicate for the same role is refused.
        </p>
        {tasks.length === 0 ? (
          <EmptyState>No tasks yet — a receipt belongs to a task.</EmptyState>
        ) : (
          <form
            onSubmit={(e) => {
              e.preventDefault();
              setConfirmReceipt(true);
            }}
          >
            <label htmlFor="receipt-task">Task</label>
            <select
              id="receipt-task"
              value={receiptTaskId}
              onChange={(e) => setReceiptTaskId(e.target.value)}
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
                <label htmlFor="receipt-role">Role id</label>
                <input
                  id="receipt-role"
                  value={receiptRoleId}
                  onChange={(e) => setReceiptRoleId(e.target.value)}
                  required
                />
              </div>
              <div>
                <label htmlFor="receipt-title">Title</label>
                <input
                  id="receipt-title"
                  value={receiptTitle}
                  onChange={(e) => setReceiptTitle(e.target.value)}
                  required
                />
              </div>
              <div>
                <label htmlFor="receipt-company">Company</label>
                <input
                  id="receipt-company"
                  value={receiptCompany}
                  onChange={(e) => setReceiptCompany(e.target.value)}
                />
              </div>
              <div>
                <label htmlFor="receipt-url">URL</label>
                <input
                  id="receipt-url"
                  value={receiptUrl}
                  onChange={(e) => setReceiptUrl(e.target.value)}
                />
              </div>
            </div>
            <label htmlFor="receipt-confirmation">Confirmation (what the site showed)</label>
            <textarea
              id="receipt-confirmation"
              value={receiptConfirmation}
              onChange={(e) => setReceiptConfirmation(e.target.value)}
              rows={3}
              required
              placeholder="e.g. “Application submitted — reference #12345”"
            />
            <label htmlFor="receipt-status">Outcome</label>
            <select
              id="receipt-status"
              value={receiptStatus}
              onChange={(e) => setReceiptStatus(e.target.value as "submitted" | "failed")}
            >
              <option value="submitted">Submitted</option>
              <option value="failed">Failed</option>
            </select>
            <button type="submit" className="primary" disabled={savingReceipt || !receiptTaskId}>
              {savingReceipt ? "Recording…" : "Record receipt"}
            </button>
            {savedReceipt && (
              <div role="status" className="receipt-row">
                <StatusChip tone={statusTone(savedReceipt.status)}>
                  {humanStatus(savedReceipt.status)}
                </StatusChip>
                <span>
                  Receipt recorded — <strong>{savedReceipt.title}</strong>
                  {savedReceipt.company ? ` · ${savedReceipt.company}` : ""}
                </span>
                <span className="muted">
                  <RelativeTime ts={savedReceipt.at} />
                </span>
                <span className="muted mono">{savedReceipt.id}</span>
              </div>
            )}
          </form>
        )}
      </section>

      <ConfirmDialog
        open={confirmReceipt}
        title="Record this receipt?"
        confirmLabel="Record receipt"
        onConfirm={() => void saveReceipt()}
        onCancel={() => setConfirmReceipt(false)}
      >
        <p>
          This records <strong>{receiptTitle || "this role"}</strong> as{" "}
          <strong>{receiptStatus}</strong> for the selected task. Only record
          it if you saw the confirmation yourself — a receipt claims the
          submit really happened.
        </p>
      </ConfirmDialog>

      <p className="muted view-foot">
        Receipts for a task also show in the Agent view, under the task. The
        server keeps them per task — there is no cross-task applied list
        endpoint, so this view checks one role at a time.
      </p>
    </div>
  );
}
