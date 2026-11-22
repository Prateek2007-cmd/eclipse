import { useState } from "react";
import { api, isHttpError, type Briefing, type Watch, type WatchEvent } from "../api";
import { fmtDateTime, fmtTime, usePoll } from "../hooks";
import { ConfirmDialog } from "../components/ConfirmDialog";
import { EmptyState, ErrorBanner, SectionHeader, StatusChip, statusTone } from "../components/ui";

const CONDITIONS = ["changed", "appeared", "disappeared", "contains", "not_contains"];

/** Watches + briefing (plan §8). Watches only run while
 * Chrome is open — the extension checks pages in open tabs on
 * a chrome.alarms schedule; the server never polls a page by
 * itself. The manual check here is for reporting an
 * observation you made yourself. */
export function WatchesView({ token }: { token: string | null }) {
  const [watches, setWatches] = useState<Watch[]>([]);
  const [events, setEvents] = useState<WatchEvent[]>([]);
  const [due, setDue] = useState<Watch[]>([]);
  const [briefing, setBriefing] = useState<Briefing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastOk, setLastOk] = useState<number | null>(null);

  const [url, setUrl] = useState("");
  const [selector, setSelector] = useState("");
  const [condition, setCondition] = useState("changed");
  const [intervalMin, setIntervalMin] = useState(60);
  const [creating, setCreating] = useState(false);

  const [repairId, setRepairId] = useState<string | null>(null);
  const [repairSelector, setRepairSelector] = useState("");
  const [repairUrl, setRepairUrl] = useState("");

  const [checkId, setCheckId] = useState<string | null>(null);
  const [checkValue, setCheckValue] = useState("");
  const [checkFound, setCheckFound] = useState(true);
  const [checkResult, setCheckResult] = useState<string | null>(null);

  const [muteMinutes, setMuteMinutes] = useState(60);
  const [pending, setPending] = useState<string | null>(null);
  const [deleteId, setDeleteId] = useState<string | null>(null);
  const [building, setBuilding] = useState(false);

  const load = async () => {
    try {
      const [w, e, d] = await Promise.all([
        api.watches(),
        api.watchEvents(undefined, 50),
        api.dueWatches(),
      ]);
      setWatches(w.watches);
      setEvents(e.events);
      setDue(d.watches);
      setError(null);
      setLastOk(Date.now());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const loadBriefing = async () => {
    try {
      const b = await api.briefingLatest();
      setBriefing(b);
    } catch (err) {
      // A 404 means none exists — not an error.
      if (!isHttpError(err, 404)) {
        setError(err instanceof Error ? err.message : String(err));
      }
    }
  };

  usePoll(load, [token], 15000);
  usePoll(loadBriefing, [token], 0);

  const createWatch = async () => {
    setCreating(true);
    setError(null);
    try {
      await api.createWatch({
        url: url.trim(),
        selector: selector.trim(),
        condition,
        interval_min: intervalMin,
      });
      setUrl("");
      setSelector("");
      setCondition("changed");
      setIntervalMin(60);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  const runWatchAction = async (
    id: string,
    kind: "mute" | "unmute" | "delete" | "repair" | "check",
  ) => {
    setPending(`${kind}:${id}`);
    setError(null);
    setCheckResult(null);
    try {
      if (kind === "mute") await api.muteWatch(id, muteMinutes);
      if (kind === "unmute") await api.unmuteWatch(id);
      if (kind === "delete") await api.deleteWatch(id);
      if (kind === "repair") {
        await api.repairWatch(id, {
          selector: repairSelector.trim() || undefined,
          url: repairUrl.trim() || undefined,
        });
        setRepairId(null);
      }
      if (kind === "check") {
        const result = await api.checkWatch(
          id,
          checkFound,
          checkFound ? checkValue : null,
        );
        setCheckResult(
          result.fired
            ? `Fired: ${result.reason}`
            : result.became_broken
              ? `Watch broke: ${result.reason}`
              : `No change: ${result.reason}`,
        );
        setCheckId(null);
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setPending(null);
    }
  };

  const buildBriefing = async () => {
    setBuilding(true);
    setError(null);
    try {
      const b = await api.buildBriefing();
      setBriefing(b);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBuilding(false);
    }
  };

  return (
    <div className="view watches-view">
      <div className="view-head">
        <h2>Watches &amp; Briefing</h2>
        <p className="muted">
          Watches follow a page element and fire when it changes. They only
          run while Chrome is open — the extension checks, never the server.
        </p>
      </div>
      {error && <ErrorBanner error={error} lastOk={lastOk} onRetry={() => void load()} />}
      {checkResult && (
        <p role="status" className="secondary">
          {checkResult}
        </p>
      )}

      <section aria-label="Create a watch" className="pane">
        <SectionHeader title="New watch" />
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void createWatch();
          }}
        >
          <label htmlFor="watch-url">Page URL</label>
          <input
            id="watch-url"
            type="url"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="https://…"
            required
          />
          <label htmlFor="watch-selector">CSS selector for the element to follow</label>
          <input
            id="watch-selector"
            value={selector}
            onChange={(e) => setSelector(e.target.value)}
            placeholder="e.g. #results .price"
            required
          />
          <div className="form-grid">
            <div>
              <label htmlFor="watch-condition">Fire when the element…</label>
              <select
                id="watch-condition"
                value={condition}
                onChange={(e) => setCondition(e.target.value)}
              >
                {CONDITIONS.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            </div>
            <div>
              <label htmlFor="watch-interval">Check every (minutes, min 15)</label>
              <input
                id="watch-interval"
                type="number"
                min={15}
                max={1440}
                value={intervalMin}
                onChange={(e) => setIntervalMin(Number(e.target.value))}
              />
            </div>
          </div>
          <button type="submit" className="primary" disabled={creating}>
            {creating ? "Creating…" : "Create watch"}
          </button>
        </form>
      </section>

      <section aria-label="Watches" className="pane">
        <SectionHeader
          title={`Watches (${watches.length})`}
          action={
            <span className="muted">
              {due.length > 0
                ? `${due.length} due for a check`
                : "None due right now"}
            </span>
          }
        />
        {loading && <p className="muted">Loading watches…</p>}
        {!loading && watches.length === 0 && (
          <EmptyState>
            No watches yet. Create one above — the extension checks it in an
            open tab, on schedule.
          </EmptyState>
        )}
        <ul className="watch-list">
          {watches.map((w) => (
            <li key={w.id} className={w.status === "broken" ? "watch-broken" : ""}>
              <div className="watch-head">
                <a href={w.url} target="_blank" rel="noreferrer">
                  {w.url}
                </a>
                <StatusChip tone={statusTone(w.status)}>{w.status}</StatusChip>
              </div>
              <div className="secondary">
                {w.condition} · <span className="mono">{w.selector}</span> · every{" "}
                {w.interval_min} min · {w.last_value ?? "no baseline yet"}
              </div>
              <div className="muted mono">
                {w.last_checked
                  ? `checked ${fmtTime(w.last_checked)}`
                  : "never checked"}
                {w.last_fired_at ? ` · fired ${fmtTime(w.last_fired_at)}` : ""}
                {w.misses > 0 ? ` · ${w.misses} missed check${w.misses === 1 ? "" : "s"}` : ""}
              </div>
              <div className="task-actions">
                {w.status === "muted" ? (
                  <button
                    type="button"
                    onClick={() => void runWatchAction(w.id, "unmute")}
                    disabled={pending !== null}
                  >
                    Unmute
                  </button>
                ) : (
                  <button
                    type="button"
                    onClick={() => void runWatchAction(w.id, "mute")}
                    disabled={pending !== null}
                  >
                    {pending === `mute:${w.id}` ? "Muting…" : `Mute ${muteMinutes}m`}
                  </button>
                )}
                <button
                  type="button"
                  onClick={() => {
                    setRepairId(w.id);
                    setRepairSelector(w.selector);
                    setRepairUrl(w.url);
                  }}
                  disabled={pending !== null}
                >
                  Repair
                </button>
                <button
                  type="button"
                  onClick={() => {
                    setCheckId(w.id);
                    setCheckValue(w.last_value ?? "");
                    setCheckFound(true);
                  }}
                  disabled={pending !== null}
                >
                  Report a check
                </button>
                <button
                  type="button"
                  onClick={() => setDeleteId(w.id)}
                  disabled={pending !== null}
                >
                  Delete
                </button>
              </div>
              {repairId === w.id && (
                <form
                  className="inline-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void runWatchAction(w.id, "repair");
                  }}
                >
                  <p className="muted">
                    Repair re-picks the element a broken watch follows and
                    restarts it from a fresh baseline.
                  </p>
                  <label htmlFor={`repair-selector-${w.id}`}>New selector</label>
                  <input
                    id={`repair-selector-${w.id}`}
                    value={repairSelector}
                    onChange={(e) => setRepairSelector(e.target.value)}
                  />
                  <label htmlFor={`repair-url-${w.id}`}>New URL (optional)</label>
                  <input
                    id={`repair-url-${w.id}`}
                    type="url"
                    value={repairUrl}
                    onChange={(e) => setRepairUrl(e.target.value)}
                  />
                  <div className="task-actions">
                    <button type="submit" className="primary" disabled={pending !== null}>
                      {pending === `repair:${w.id}` ? "Repairing…" : "Repair watch"}
                    </button>
                    <button type="button" onClick={() => setRepairId(null)}>
                      Cancel
                    </button>
                  </div>
                </form>
              )}
              {checkId === w.id && (
                <form
                  className="inline-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void runWatchAction(w.id, "check");
                  }}
                >
                  <p className="muted">
                    Report what you see on the page right now. The server
                    evaluates it against the watch's condition — a firing
                    is written to the audit log.
                  </p>
                  <label className="check-row">
                    <input
                      type="checkbox"
                      checked={checkFound}
                      onChange={(e) => setCheckFound(e.target.checked)}
                    />
                    The element is on the page
                  </label>
                  {checkFound && (
                    <>
                      <label htmlFor={`check-value-${w.id}`}>Current value</label>
                      <input
                        id={`check-value-${w.id}`}
                        value={checkValue}
                        onChange={(e) => setCheckValue(e.target.value)}
                        required
                      />
                    </>
                  )}
                  <div className="task-actions">
                    <button type="submit" className="primary" disabled={pending !== null}>
                      {pending === `check:${w.id}` ? "Checking…" : "Submit check"}
                    </button>
                    <button type="button" onClick={() => setCheckId(null)}>
                      Cancel
                    </button>
                  </div>
                </form>
              )}
            </li>
          ))}
        </ul>
        <div className="mute-setting">
          <label htmlFor="mute-minutes">Mute length (minutes)</label>
          <input
            id="mute-minutes"
            type="number"
            min={1}
            max={1440}
            value={muteMinutes}
            onChange={(e) => setMuteMinutes(Number(e.target.value))}
          />
        </div>
      </section>

      <div className="split">
        <section aria-label="Watch events" className="pane">
          <SectionHeader title="Events" />
          {events.length === 0 ? (
            <EmptyState>No watch has fired yet. Firings land here.</EmptyState>
          ) : (
            <ul className="plain-list">
              {events.map((e) => (
                <li key={e.id}>
                  <StatusChip tone={e.kind === "fired" ? "warn" : "bad"}>{e.kind}</StatusChip>{" "}
                  <span className="muted mono">{fmtDateTime(e.created_at)}</span>
                  <p>{e.detail}</p>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section aria-label="Morning briefing" className="pane">
          <SectionHeader
            title="Morning briefing"
            action={
              <button type="button" onClick={() => void buildBriefing()} disabled={building}>
                {building ? "Building…" : "Build briefing now"}
              </button>
            }
          />
          {!briefing ? (
            <EmptyState>
              No briefing built yet. A briefing never sends mail — it shows,
              you act.
            </EmptyState>
          ) : briefing.cards.length === 0 ? (
            <p className="muted">
              Briefing from {fmtDateTime(briefing.created_at)} — nothing to
              report.
            </p>
          ) : (
            <div>
              <p className="muted">Built {fmtDateTime(briefing.created_at)}</p>
              <ul className="briefing-list">
                {briefing.cards.map((c, i) => (
                  <li key={`${c.kind}-${i}`}>
                    <strong>{c.title}</strong>
                    <p>{c.body}</p>
                    {c.action && c.action_label && (
                      // Sprint 9 fix (review M5): offer cards carry a
                      // Gmail id, not a URL — render those as plain text,
                      // never an <a> with no href.
                      c.action.startsWith("http") ? (
                        <a href={c.action} target="_blank" rel="noreferrer">
                          {c.action_label}
                        </a>
                      ) : (
                        <span className="muted">{c.action_label}</span>
                      )
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>
      </div>

      <ConfirmDialog
        open={deleteId !== null}
        title="Delete this watch?"
        confirmLabel="Delete watch"
        danger
        onConfirm={() => {
          const id = deleteId;
          setDeleteId(null);
          if (id) void runWatchAction(id, "delete");
        }}
        onCancel={() => setDeleteId(null)}
      >
        <p>
          The watch and its history are deleted. The extension stops
          checking the page.
        </p>
      </ConfirmDialog>
    </div>
  );
}
