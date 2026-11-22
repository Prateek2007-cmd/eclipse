import type { ReactNode } from "react";

/** Shared small pieces for the views: empty states, error
 * banners, status chips, section headers. Nothing here fetches
 * or holds state of its own. */

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
        <button type="button" onClick={onRetry}>
          Retry
        </button>
      )}
    </p>
  );
}

type ChipTone = "ok" | "warn" | "bad" | "info" | "plain";

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
      return "bad";
    case "ask_user":
    case "paused":
    case "muted":
    case "draft":
    case "handoff":
      return "warn";
    case "running":
    case "planned":
    case "executing":
    case "pending":
      return "info";
    default:
      return "plain";
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
