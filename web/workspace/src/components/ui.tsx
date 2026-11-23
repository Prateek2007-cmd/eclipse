import type { ReactNode } from "react";
import { fmtDateTime, fmtRelative, useRelativeNow } from "../hooks";

/** Shared small pieces for the views: empty states, error
 * banners, status chips, section headers, disclosures, and
 * the PR #6 human-first components. Nothing here fetches or
 * holds state of its own (except RelativeTime's 1/min tick).
 *
 * CONTRACT (PR #6): the view workers code against this API.
 * Do not rename exports or change prop shapes without
 * reporting it loudly. */

/** An honest empty state: what is missing, and the next action
 * that would fill it. Never a fake number. */
export function EmptyState({
  children,
  action,
}: {
  children: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty-state">
      <p className="muted">{children}</p>
      {action}
    </div>
  );
}

/** Inline retry icon (PR #6 Worker 11): a circular arrow,
 * 16px, currentColor, aria-hidden — the button's label
 * carries the meaning. */
function RetryIcon() {
  return (
    <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M21 12a9 9 0 1 1-2.64-6.36" />
      <path d="M21 3v6h-6" />
    </svg>
  );
}

/** An error banner. role="alert" announces it (guide:
 * accessible-error-announcement). The optional stale note tells
 * the user the data below may be old. */
export function ErrorBanner({
  error,
  lastOk,
  onRetry,
}: {
  error: string;
  lastOk?: number | null;
  onRetry?: () => void;
}) {
  return (
    <p role="alert" className="danger error-banner">
      {error}{" "}
      {lastOk && (
        <span className="muted">
          Showing data from {new Date(lastOk).toLocaleTimeString()} — it may
          be stale.
        </span>
      )}{" "}
      {onRetry && (
        <button type="button" className="btn" onClick={onRetry}>
          <RetryIcon /> Retry
        </button>
      )}
    </p>
  );
}

export type ChipTone = "ok" | "warn" | "bad" | "info" | "plain";

/** A small status pill. Tone is colour AND text — never colour
 * alone. */
export function StatusChip({ tone, children }: { tone: ChipTone; children: ReactNode }) {
  return <span className={`chip chip-${tone}`}>{children}</span>;
}

/** Map a task/watch/draft status string to a chip tone. */
export function statusTone(status: string): ChipTone {
  switch (status) {
    case "done":
    case "submitted":
    case "approved":
    case "active":
    case "connected":
    case "sent":
      return "ok";
    case "failed":
    case "broken":
    case "cancelled":
    case "bounced":
    case "needs_reconnect":
    case "unavailable":
    case "stalled":
      return "bad";
    case "ask_user":
    case "paused":
    case "muted":
    case "draft":
    case "handoff":
    case "waiting_for_extension":
      return "warn";
    case "running":
    case "planned":
    case "executing":
    case "pending":
      return "info";
    case "stopped":
      return "plain";
    default:
      return "plain";
  }
}

/** Human label for a status string. Server statuses are
 * snake_case enums (ask_user, awaiting_approval) — users
 * read words, not enums. Unknown values get underscores
 * replaced and a capital letter, so a new status never
 * renders raw. (PR #6 critique: no raw enums on screen.) */
export function humanStatus(status: string): string {
  switch (status) {
    case "ask_user":
      return "Waiting for you";
    case "awaiting_approval":
      return "Awaiting approval";
    case "needs_reconnect":
      return "Needs reconnect";
    case "waiting_for_extension":
      return "Waiting for extension";
    default:
      return status.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
  }
}

/** A section header with an optional action on the right. */
export function SectionHeader({
  title,
  action,
}: {
  title: string;
  action?: ReactNode;
}) {
  return (
    <div className="section-header">
      <h3>{title}</h3>
      {action}
    </div>
  );
}

/* ---------- PR #6 components ---------- */

/** A native <details>/<summary> accordion (guide: accordion).
 * The chevron sits next to the label and rotates; the open
 * animation is in app.css (.disclosure). Secondary content
 * goes behind one of these, never inline (guide:
 * content-first-noise-reduction). */
export function Disclosure({
  summary,
  children,
  defaultOpen,
  className,
}: {
  summary: ReactNode;
  children: ReactNode;
  defaultOpen?: boolean;
  className?: string;
}) {
  return (
    <details
      className={className ? `disclosure ${className}` : "disclosure"}
      open={defaultOpen}
    >
      <summary>{summary}</summary>
      <div className="disclosure-body">{children}</div>
    </details>
  );
}

/** Human-readable fact chips ("Remote · Hyderabad · ₹10k+ ·
 * ≤3 applies/day"). No JSON anywhere. A chip renders its
 * label muted and its value plain; pass label "" for a bare
 * value. Renders nothing for an empty list. */
export function SummaryChips({
  items,
}: {
  items: { label: string; value: string }[];
}) {
  if (items.length === 0) return null;
  return (
    <ul className="summary-chips">
      {items.map((item, i) => (
        <li key={`${item.label}-${item.value}-${i}`} className="summary-chip">
          {item.label ? (
            <span className="summary-chip-label">{item.label}</span>
          ) : null}
          {item.value}
        </li>
      ))}
    </ul>
  );
}

/** A human error: one short plain line, an optional retry,
 * and the technical text behind a Details expand. Never dumps
 * raw text inline. */
export function HumanError({
  title,
  detail,
  onRetry,
}: {
  title: string;
  detail?: string;
  onRetry?: () => void;
}) {
  return (
    <div className="human-error" role="alert">
      <div className="human-error-head">
        <p className="human-error-title">{title}</p>
        {onRetry && (
          <button type="button" className="btn" onClick={onRetry}>
            <RetryIcon /> Retry
          </button>
        )}
      </div>
      {detail ? (
        <Disclosure summary="Details">
          <p className="human-error-detail">{detail}</p>
        </Disclosure>
      ) : null}
    </div>
  );
}

/** A relative timestamp — "2 min ago" — that refreshes once a
 * minute. The title attribute holds the absolute date/time,
 * and the <time> element carries the machine-readable value.
 * ts is unix SECONDS; null/undefined renders an em dash. */
export function RelativeTime({ ts }: { ts: number | null | undefined }) {
  const now = useRelativeNow();
  if (ts === null || ts === undefined) {
    return <span className="muted">—</span>;
  }
  return (
    <time dateTime={new Date(ts * 1000).toISOString()} title={fmtDateTime(ts)}>
      {fmtRelative(ts, now)}
    </time>
  );
}

/** One line of activity: an icon (or a tone dot) + a short
 * title + a relative time. `detail` expands below the title.
 * `when` is a unix-seconds timestamp. */
export function ActivityLine({
  icon,
  title,
  detail,
  when,
  tone,
}: {
  icon?: ReactNode;
  title: string;
  detail?: ReactNode;
  when?: number | null;
  tone?: ChipTone;
}) {
  return (
    <div className="activity-line">
      {icon ? (
        <span className="activity-icon" aria-hidden="true">
          {icon}
        </span>
      ) : tone ? (
        <span className={`activity-dot tone-${tone}`} aria-hidden="true" />
      ) : null}
      <div className="activity-body">
        <p className="activity-title">{title}</p>
        {detail ? <Disclosure summary="Details">{detail}</Disclosure> : null}
      </div>
      {when !== undefined && when !== null ? (
        <span className="activity-when">
          <RelativeTime ts={when} />
        </span>
      ) : null}
    </div>
  );
}

/** The pending-approvals banner — the loudest thing on the
 * page. Warning tint, count in plain words. Actions come from
 * `children` (e.g. Approve/Decline buttons) or, when no
 * children are given, a single Review button calling
 * `onReview`. Renders nothing when count is 0. `text`
 * overrides the count sentence for non-task approvals
 * (e.g. a mail draft) — the count still gates rendering. */
export function ApprovalBanner({
  count,
  onReview,
  children,
  text,
}: {
  count: number;
  onReview?: () => void;
  children?: ReactNode;
  text?: string;
}) {
  if (count <= 0) return null;
  const hasActions = children != null || onReview !== undefined;
  return (
    <div className="approval-banner" role="alert">
      <p className="approval-banner-text">
        {text ??
          (count === 1
            ? "1 task is waiting for your approval."
            : `${count} tasks are waiting for your approval.`)}
      </p>
      {hasActions ? (
        <div className="approval-banner-actions">
          {children ??
            (onReview ? (
              <button type="button" className="btn btn-primary" onClick={onReview}>
                Review
              </button>
            ) : null)}
        </div>
      ) : null}
    </div>
  );
}

/** "Show N more" for a truncated list. Renders nothing once
 * everything is shown. `label` names the items ("Show 5 more
 * tasks"). */
export function ShowMore({
  total,
  shown,
  onShowMore,
  label,
}: {
  total: number;
  shown: number;
  onShowMore: () => void;
  label?: string;
}) {
  const remaining = total - shown;
  if (remaining <= 0) return null;
  return (
    <button type="button" className="show-more btn btn-ghost" onClick={onShowMore}>
      Show {remaining} more{label ? ` ${label}` : ""}
    </button>
  );
}

/** An accessible busy indicator (guide: spinner): a native
 * indeterminate <progress> ring inside a role="status"
 * wrapper. `label` is both the accessible name and the
 * visible text next to the ring; without one it announces
 * "Loading" and shows the ring alone. */
export function Spinner({ label }: { label?: string }) {
  const text = label ?? "Loading";
  return (
    <span role="status" aria-label={text} className="spinner-wrap">
      <progress className="spinner" aria-hidden="true" />
      {label ? <span>{label}</span> : null}
    </span>
  );
}

/** The ONLY place JSON may appear — collapsed by default
 * behind an "Advanced" expand (or `label`), rendered as mono
 * JSON in a <pre>. Never inline raw data anywhere else. */
export function RawData({
  label,
  data,
}: {
  label?: string;
  data: unknown;
}) {
  let text: string;
  try {
    text =
      typeof data === "string" ? data : (JSON.stringify(data, null, 2) ?? "—");
  } catch {
    text = String(data);
  }
  return (
    <Disclosure summary={label ?? "Advanced"} className="raw-data">
      <pre>{text}</pre>
    </Disclosure>
  );
}
