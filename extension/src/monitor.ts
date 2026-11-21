/** Live monitoring alarms (Sprint 8, plan §8).
 *
 * Watches run on chrome.alarms inside the extension. Alarms
 * throttle when Chrome is closed and do not run at all if Chrome
 * is not running — watches only work while Chrome is open. There
 * is no background server polling: the server never fetches a
 * page by itself.
 *
 * The alarm flow: the service worker asks the server which
 * watches are due (GET /monitor/due), then for each due watch it
 * finds an open tab whose URL matches, asks the content script
 * for the selector's current value, and reports the observation
 * back (POST /monitor/watches/{id}/check). The server decides
 * fired / broken / unchanged and logs the audit entry.
 *
 * A due watch with no open tab is skipped, not failed — the
 * user has to have the page open for Eclipse to see it.
 */
export type Watch = {
  id: string;
  url: string;
  selector: string;
  condition: string;
  interval_min: number;
  status: string;
};

export type WatchObservation = { found: boolean; value: string | null };

export type WatchCheckDeps = {
  /** Fetch the due watches from the server. */
  fetchDue: () => Promise<Watch[]>;
  /** Find an open tab whose URL matches the watch URL. */
  findTab: (url: string) => Promise<number | null>;
  /** Ask the tab's content script for the selector's value. */
  observeTab: (tabId: number, watch: Watch) => Promise<WatchObservation>;
  /** Report the observation to the server; returns its verdict. */
  reportCheck: (watchId: string, obs: WatchObservation) => Promise<{
    fired: boolean;
    broken: boolean;
    status: string;
    reason: string;
  }>;
  /** Fetch the server token (for the briefing call). */
  fetchToken?: () => Promise<string>;
  /** Build the morning briefing on the server. */
  buildBriefing?: (token: string) => Promise<unknown>;
};

export type WatchCheckSummary = {
  checked: number;
  fired: number;
  broken: number;
  skippedNoTab: number;
  errors: string[];
};

export function watchAlarmName(watchId: string): string {
  return `eclipse-watch-${watchId}`;
}

export const BRIEFING_ALARM = "eclipse-briefing";

export async function checkDueWatches(deps: WatchCheckDeps): Promise<WatchCheckSummary> {
  const summary: WatchCheckSummary = {
    checked: 0,
    fired: 0,
    broken: 0,
    skippedNoTab: 0,
    errors: [],
  };
  let due: Watch[];
  try {
    due = await deps.fetchDue();
  } catch (err) {
    summary.errors.push(`could not fetch due watches: ${err instanceof Error ? err.message : String(err)}`);
    return summary;
  }
  for (const watch of due) {
    let tabId: number | null;
    try {
      // B4: findTab is inside the per-watch try — one bad watch
      // (a malformed URL) must not abort the whole alarm run.
      tabId = await deps.findTab(watch.url);
    } catch (err) {
      summary.errors.push(
        `${watch.id}: ${err instanceof Error ? err.message : String(err)}`,
      );
      continue;
    }
    if (tabId === null) {
      summary.skippedNoTab += 1;
      continue;
    }
    try {
      const obs = await deps.observeTab(tabId, watch);
      const verdict = await deps.reportCheck(watch.id, obs);
      summary.checked += 1;
      if (verdict.fired) summary.fired += 1;
      if (verdict.broken) summary.broken += 1;
    } catch (err) {
      summary.errors.push(
        `${watch.id}: ${err instanceof Error ? err.message : String(err)}`,
      );
    }
  }
  return summary;
}

/** The value of a selector on a page (content-script side).
 * `selector` is a CSS selector, or `text:<phrase>` to search
 * the page text (case-insensitive). Returns found=false when
 * nothing matches — the server counts the miss, it never
 * crashes the check.
 *
 * A CSS selector reads the FIRST matching element (M15); if
 * several elements match, the watch follows the first one in
 * document order. A `text:` search returns the matched snippet
 * (40 chars of context each side, whitespace normalised), not
 * the search phrase itself — otherwise a `changed` watch on a
 * `text:` selector could never fire (M14). */
export function readSelectorValue(
  doc: Document,
  selector: string,
): WatchObservation {
  if (selector.toLowerCase().startsWith("text:")) {
    const phrase = selector.slice("text:".length).trim();
    if (phrase.length === 0) return { found: false, value: null };
    const bodyText = (doc.body?.textContent ?? "").replace(/\s+/g, " ");
    const at = bodyText.toLowerCase().indexOf(phrase.toLowerCase());
    if (at < 0) return { found: false, value: null };
    const start = Math.max(0, at - 40);
    const end = Math.min(bodyText.length, at + phrase.length + 40);
    return { found: true, value: bodyText.slice(start, end).trim() };
  }
  let el: Element | null = null;
  try {
    el = doc.querySelector(selector);
  } catch {
    return { found: false, value: null };
  }
  if (!el) return { found: false, value: null };
  const text = (el.textContent ?? "").trim().replace(/\s+/g, " ");
  return { found: true, value: text.slice(0, 1000) };
}
