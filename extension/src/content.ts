/** Content script (Sprint 2 walk + blackout; Sprint 3 executor).
 *
 * Three jobs:
 *  1. Walk the live DOM + accessibility tree on demand and hand the
 *     elements to the offscreen document (which redacts them).
 *  2. Paint destructive blackout overlays over sensitive regions
 *     when the offscreen document says so (the screenshot blackout
 *     is the proof; the overlay is what the user sees).
 *  3. Execute one validated planner action on the live page
 *     (guarded executor, Sprint 3). The action must resolve to a
 *     real element id from the current walk.
 */
import { executeAction, type ExecutorAction, type ExecuteOptions } from "./executor.js";
import { buildExecuteOptions } from "./execute-options.js";
import { readSelectorValue } from "./monitor.js";
import { walkDom, walkDomWithNodes } from "./vision/dom-walker.js";
import type { Box, ElementInfo } from "./vision/types.js";

function paintOverlay(boxes: Box[]): void {
  for (const box of boxes) {
    const div = document.createElement("div");
    div.setAttribute("data-eclipse-blackout", "true");
    div.style.cssText = [
      "position: fixed",
      `left: ${box.x * 100}vw`,
      `top: ${box.y * 100}vh`,
      `width: ${box.w * 100}vw`,
      `height: ${box.h * 100}vh`,
      "background: #000",
      "z-index: 2147483647",
      "pointer-events: none",
    ].join(";");
    document.documentElement.appendChild(div);
  }
}

function clearOverlays(): void {
  document.querySelectorAll("[data-eclipse-blackout]").forEach((el) => el.remove());
}

chrome.runtime.onMessage.addListener(
  (
    msg: {
      type: string;
      boxes?: Box[];
      action?: ExecutorAction;
      options?: ExecuteOptions & { expected?: ElementInfo; fill?: Record<string, string> };
      // Sprint 8 watch check: the selector to read on this page.
      selector?: string;
    },
    _sender,
    sendResponse,
  ) => {
    if (msg.type === "walk") {
      sendResponse({
        elements: walkDom(document),
        viewport: { width: window.innerWidth, height: window.innerHeight },
        url: location.href,
      });
      return true;
    }
    if (msg.type === "eclipse.watchCheck") {
      // Sprint 8: read one selector's current value for a watch.
      // No PII is recorded here — the value goes to the server,
      // which compares it against the stored last value.
      // M12: a missing or non-string selector gets a not-found
      // answer, not a thrown TypeError with no response.
      if (typeof msg.selector !== "string" || msg.selector.length === 0) {
        sendResponse({ found: false, value: null });
        return true;
      }
      sendResponse(readSelectorValue(document, msg.selector));
      return true;
    }
    if (msg.type === "execute" && msg.action) {
      (async () => {
        try {
          // Re-walk fresh for the node map, but the fingerprint
          // comes from the OBSERVATION the planner saw — the
          // element info the server sent in msg.options.expected.
          // Using this walk's own info as the fingerprint compares
          // the node against itself and can never fire, so a stale
          // synthetic id would silently retarget the action. Only
          // when the caller supplied no expected info (an old
          // caller) do we fall back to this walk's info.
          const { info, nodes } = walkDomWithNodes(document);
          const fresh = info.find((e) => e.id === msg.action!.element_id);
          // The vault fill is a plain map over the message (a
          // function cannot cross chrome.runtime.sendMessage);
          // buildExecuteOptions turns it into the executor's fill
          // function — the raw Record must never be spread into
          // the options (see execute-options.ts for why).
          const options = buildExecuteOptions(msg.options, fresh);
          const result = await executeAction(msg.action!, document, nodes, options);
          sendResponse(result);
        } catch (err) {
          sendResponse({ ok: false, detail: err instanceof Error ? err.message : String(err), url: location.href });
        }
      })();
      return true; // async response
    }
    if (msg.type === "blackout" && msg.boxes) {
      clearOverlays();
      paintOverlay(msg.boxes);
      sendResponse({ ok: true });
      return true;
    }
    if (msg.type === "clear-blackout") {
      clearOverlays();
      sendResponse({ ok: true });
      return true;
    }
    return false;
  },
);
