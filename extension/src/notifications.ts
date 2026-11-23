/** Extension notifications (PR #5).
 *
 * Thin wrappers over chrome.notifications.create for the three
 * things worth interrupting the user about: a mission blocked
 * on an approval, the morning briefing, and a fired watch.
 *
 * Honest limit: notifications only fire while Chrome is
 * running. When Chrome is closed, approvals wait silently until
 * the user opens the workspace or Chrome, and the briefing is
 * still built by the server tick and waits in
 * GET /monitor/briefing/latest. There is no server-side
 * notification path (the server cannot push to the extension).
 *
 * Dedup (chrome.storage.local): an approval is notified once
 * per approval id, a briefing once per IST day — the alarms
 * that call these run every 15 minutes / daily, and without
 * dedup the user would get the same notification on every run.
 * Watch firings need no dedup here: the server already applies
 * a 1-hour cooldown per watch.
 *
 * Clicking any Eclipse notification opens (or focuses) the
 * workspace tab. The tab query matches the workspace URL only,
 * which the existing http://127.0.0.1:8000/* host permission
 * covers — no "tabs" permission is needed for it.
 */
const WORKSPACE_URL = "http://127.0.0.1:8000/workspace";
const ICON_PATH = "icons/eclipse-128.png";

/** IST calendar date (YYYY-MM-DD) for a date (default now).
 * Uses Intl with an explicit Asia/Kolkata timezone — never the
 * host localtime. */
export function istDateString(date: Date = new Date()): string {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

/** Small stable hash for a dedup key when no approval id is
 * available (djb2, hex). */
function hashKey(text: string): string {
  let hash = 5381;
  for (let i = 0; i < text.length; i++) {
    hash = ((hash << 5) + hash + text.charCodeAt(i)) >>> 0;
  }
  return hash.toString(16);
}

/** True if `key` was already recorded (and this call should
 * stay silent); otherwise records it and returns false. If
 * storage is unavailable, returns false — a duplicate
 * notification is better than a missed approval. */
async function alreadyNotified(key: string): Promise<boolean> {
  try {
    const stored = await chrome.storage.local.get(key);
    if (stored[key]) return true;
    await chrome.storage.local.set({ [key]: true });
    return false;
  } catch {
    return false;
  }
}

async function createNotification(title: string, message: string): Promise<void> {
  try {
    await chrome.notifications.create({
      type: "basic",
      iconUrl: chrome.runtime.getURL(ICON_PATH),
      title,
      message,
    });
  } catch (err) {
    // A failed notification must never break an alarm run.
    console.warn("[eclipse] notification failed:", err);
  }
}

/** A mission is blocked on a human approval. Notified once per
 * approval id (or once per mission+detail when no id is given). */
export async function notifyApproval(
  missionName: string,
  detail: string,
  approvalId?: string,
): Promise<void> {
  const key = approvalId
    ? `notified:approval:${approvalId}`
    : `notified:approval:${hashKey(`${missionName}\n${detail}`)}`;
  if (await alreadyNotified(key)) return;
  await createNotification(
    `Eclipse: ${missionName} needs your approval`,
    detail,
  );
}

/** The morning briefing was built. Notified once per IST day. */
export async function notifyBriefing(cardCount: number): Promise<void> {
  const key = `notified:briefing:${istDateString()}`;
  if (await alreadyNotified(key)) return;
  const noun = cardCount === 1 ? "item" : "items";
  await createNotification(
    "Eclipse morning briefing",
    `Your 9 AM briefing is ready — ${cardCount} ${noun}.`,
  );
}

/** A watch fired. No dedup here — the server cools a watch
 * down for 1 hour after it fires. */
export async function notifyWatchFired(watchUrl: string): Promise<void> {
  await createNotification("Eclipse: a watched page changed", watchUrl);
}

async function openWorkspaceTab(): Promise<void> {
  const tabs = await chrome.tabs.query({ url: `${WORKSPACE_URL}*` });
  const existing = tabs.find((tab) => tab.id !== undefined);
  if (existing?.id !== undefined) {
    await chrome.tabs.update(existing.id, { active: true });
    if (existing.windowId !== undefined) {
      await chrome.windows.update(existing.windowId, { focused: true });
    }
    return;
  }
  await chrome.tabs.create({ url: WORKSPACE_URL });
}

// Clicking any Eclipse notification opens (or focuses) the
// workspace tab. Registered at import: background.ts imports
// this module, so the handler is live whenever the service
// worker is. Guarded with ?. — the "notifications" permission
// guarantees the API in production, but older test fakes that
// predate it (audit-*.test.mjs) bundle background.ts without
// a notifications stub, and the import must not throw there.
chrome.notifications?.onClicked?.addListener(() => {
  void openWorkspaceTab().catch((err: unknown) => {
    console.warn("[eclipse] could not open workspace tab:", err);
  });
});
