/** Service worker (Sprint 2 observe; Sprint 3 execute).
 *
 * Wires the content script to the offscreen document and to the
 * guarded executor. The full orchestration (planner calls,
 * checkpoints) lives on the server (Sprint 3, server/tasks/loop.py);
 * this file only owns the browser side:
 *   observe: content script walks the DOM → service worker captures
 *     the screenshot → offscreen document observes + redacts + gates
 *     → the gated redacted payload comes back here.
 *   execute: one validated planner action → content script performs
 *     it on the live page (guarded executor) → the result comes back.
 */
const OFFSCREEN_URL = "offscreen.html";
const SERVER = "http://127.0.0.1:8000";

import { BRIEFING_ALARM, checkDueWatches, watchAlarmName, type Watch } from "./monitor.js";

async function serverToken(): Promise<string> {
  const stored = await chrome.storage.local.get("eclipseToken");
  const token = stored["eclipseToken"];
  if (typeof token !== "string" || token.length === 0) {
    throw new Error("no Eclipse server token stored (open the workspace once)");
  }
  return token;
}

async function serverCall(path: string, init?: RequestInit): Promise<unknown> {
  const token = await serverToken();
  const res = await fetch(`${SERVER}${path}`, {
    ...init,
    headers: { "X-Eclipse-Token": token, "Content-Type": "application/json", ...(init?.headers ?? {}) },
  });
  if (!res.ok) throw new Error(`server ${res.status} on ${path}`);
  return res.json();
}

/** (Re)schedule one alarm per watch plus the daily briefing
 * alarm. Called when the workspace saves a watch and on
 * service-worker startup. */
async function scheduleWatchAlarms(): Promise<void> {
  try {
    const data = (await serverCall("/monitor/watches")) as { watches: Watch[] };
    const existing = await chrome.alarms.getAll();
    for (const alarm of existing) {
      if (alarm.name.startsWith("eclipse-watch-") || alarm.name === BRIEFING_ALARM) {
        await chrome.alarms.clear(alarm.name);
      }
    }
    for (const watch of data.watches) {
      // M17: only active watches get an alarm — muted/broken
      // watches would wake the worker for nothing (/due filters
      // them out anyway).
      if (watch.status !== "active" || watch.interval_min < 15) continue;
      await chrome.alarms.create(watchAlarmName(watch.id), {
        periodInMinutes: watch.interval_min,
      });
    }
    // M13: the first briefing lands tomorrow morning at 08:00,
    // not up to 24h after startup.
    const now = new Date();
    const next = new Date(now);
    next.setHours(8, 0, 0, 0);
    if (next.getTime() <= now.getTime()) next.setDate(next.getDate() + 1);
    await chrome.alarms.create(BRIEFING_ALARM, {
      delayInMinutes: Math.ceil((next.getTime() - now.getTime()) / 60000),
      periodInMinutes: 1440,
    });
  } catch (err) {
    // B5: log instead of swallowing — if scheduling fails the
    // watches silently never run, and nobody would know.
    console.warn("[eclipse] could not schedule watch alarms:", err);
  }
}

// B5: the workspace (or any caller) reschedules alarms after a
// watch is created, muted, deleted, or repaired — new watches
// must not wait for a Chrome restart to start checking.
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (msg?.type === "eclipse.rescheduleWatches") {
    void scheduleWatchAlarms().then(() => sendResponse({ ok: true }));
    return true;
  }
  return false;
});

// B5: the token appears when the user first opens the workspace
// and stores it — schedule then, not only at startup.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes["eclipseToken"]?.newValue) {
    void scheduleWatchAlarms();
  }
});

/** Normalise a URL for tab matching: origin + pathname without
 * a trailing slash + search. Two URLs that differ only by a
 * trailing slash are the same page. */
export function normaliseWatchUrl(raw: string): string {
  const u = new URL(raw);
  const path = u.pathname.replace(/\/+$/, "") || "/";
  return `${u.origin}${path}${u.search}`;
}

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === BRIEFING_ALARM) {
    (async () => {
      try {
        await serverCall("/monitor/briefing", { method: "POST" });
      } catch {
        // The briefing is best-effort; the user can rebuild it
        // on demand from the workspace.
      }
    })();
    return;
  }
  if (!alarm.name.startsWith("eclipse-watch-")) return;
  (async () => {
    try {
      // B4/M16: a failed run is logged, not silently dropped.
      const summary = await checkDueWatches({
        fetchDue: async () => {
          const data = (await serverCall("/monitor/due")) as { watches: Watch[] };
          return data.watches;
        },
        findTab: async (url) => {
          // B3: match the tab's ACTUAL URL, not just its origin.
          // An origin-only match could evaluate the watch against
          // a different page on the same site — a missing element
          // there counts as a miss, and for a "disappeared"
          // watch it fires a false alert.
          let wanted: string;
          try {
            wanted = normaliseWatchUrl(url);
          } catch {
            return null;
          }
          const origin = new URL(url).origin;
          const tabs = await chrome.tabs.query({ url: `${origin}/*` });
          for (const tab of tabs) {
            if (!tab.url || tab.id === undefined) continue;
            try {
              if (normaliseWatchUrl(tab.url) === wanted) return tab.id;
            } catch {
              continue;
            }
          }
          return null;
        },
        observeTab: async (tabId, watch) => {
          const result = (await chrome.tabs.sendMessage(tabId, {
            type: "eclipse.watchCheck",
            selector: watch.selector,
          })) as { found: boolean; value: string | null };
          return result;
        },
        reportCheck: async (watchId, obs) => {
          const verdict = (await serverCall(`/monitor/watches/${watchId}/check`, {
            method: "POST",
            body: JSON.stringify(obs),
          })) as { fired: boolean; broken: boolean; status: string; reason: string };
          return verdict;
        },
      });
      if (summary.errors.length > 0) {
        console.warn("[eclipse] watch check errors:", summary);
      }
    } catch (err) {
      console.warn("[eclipse] watch alarm run failed:", err);
    }
  })();
});

chrome.runtime.onStartup.addListener(() => {
  void scheduleWatchAlarms();
});

async function ensureOffscreen(): Promise<void> {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: [chrome.runtime.ContextType.OFFSCREEN_DOCUMENT],
  });
  if (contexts.length > 0) return;
  await chrome.offscreen.createDocument({
    url: OFFSCREEN_URL,
    reasons: [chrome.offscreen.Reason.USER_MEDIA],
    justification: "On-device vision (YOLO, OCR, BlazeFace, Florence-2) via WebGPU.",
  });
}

chrome.runtime.onMessage.addListener(
  (
    msg: {
      type: string;
      tabId?: number;
      taskKind?: string;
      egressTarget?: string;
      action?: { action: string; element_id?: string; text?: string; url?: string; seconds?: number; reason?: string };
      // From the server's execute response: the task id (the
      // submit guard key), the allowed sites, the element info
      // the planner saw (the fingerprint), and the vault fill map
      // (placeholder → value, Sprint 5). Without these the live
      // path has no taskId, no allow-list and no fingerprint.
      options?: {
        taskId?: string;
        allowedSites?: string[];
        expected?: {
          id: string; tag: string; role: string; name: string;
          text: string; sensitive: boolean;
        };
        fill?: Record<string, string>;
      };
    },
    _sender,
    sendResponse,
  ) => {
    if (msg.type === "eclipse.execute" && msg.action) {
      (async () => {
        try {
          // The tab must be named: executing in "the active tab"
          // would land the action (including a vault-filled
          // password) in whatever tab is foreground if the user
          // switched tabs between observe and execute.
          const tabId = msg.tabId;
          if (!tabId) {
            sendResponse({ ok: false, detail: "eclipse.execute needs a tabId", url: "" });
            return;
          }
          const result = await chrome.tabs.sendMessage(tabId, {
            type: "execute",
            action: msg.action,
            options: msg.options ?? {},
          });
          sendResponse(result);
        } catch (err) {
          sendResponse({ ok: false, detail: err instanceof Error ? err.message : String(err), url: "" });
        }
      })();
      return true; // async response
    }
    if (msg.type !== "eclipse.observe") return false;
  (async () => {
    try {
      const tabId = msg.tabId ?? (await chrome.tabs.query({ active: true, currentWindow: true }))[0]?.id;
      if (!tabId) {
        sendResponse({ error: "no active tab" });
        return;
      }
      const walk = await chrome.tabs.sendMessage(tabId, { type: "walk" });
      const dataUrl = await chrome.tabs.captureVisibleTab({ format: "png" });
      // Decode the screenshot to raw pixels for the offscreen doc.
      const img = await fetch(dataUrl).then((r) => r.blob());
      const bitmap = await createImageBitmap(img);
      const canvas = new OffscreenCanvas(bitmap.width, bitmap.height);
      const ctx = canvas.getContext("2d")!;
      ctx.drawImage(bitmap, 0, 0);
      const pixels = ctx.getImageData(0, 0, bitmap.width, bitmap.height);
      await ensureOffscreen();
      const result = await chrome.runtime.sendMessage({
        type: "observe",
        url: walk.url,
        viewport: walk.viewport,
        elements: walk.elements,
        screenshot: { data: pixels.data, width: bitmap.width, height: bitmap.height },
        taskKind: msg.taskKind ?? "mixed",
        egressTarget: msg.egressTarget ?? "http://127.0.0.1:8000",
      });
      if (result?.blackoutBoxes?.length) {
        await chrome.tabs.sendMessage(tabId, { type: "blackout", boxes: result.blackoutBoxes });
      }
      sendResponse(result);
    } catch (err) {
      sendResponse({ error: err instanceof Error ? err.message : String(err) });
    }
  })();
  return true; // async response
});
