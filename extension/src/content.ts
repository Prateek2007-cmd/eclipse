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
import { walkDom, walkDomWithNodes } from "./vision/dom-walker.js";
import type { Box } from "./vision/types.js";

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
      options?: ExecuteOptions;
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
    if (msg.type === "execute" && msg.action) {
      (async () => {
        try {
          // Re-walk fresh, and hand the executor the element info
          // from THIS walk as the fingerprint: the resolved node
          // must still match what the planner saw.
          const { info, nodes } = walkDomWithNodes(document);
          const expected = info.find((e) => e.id === msg.action!.element_id);
          const options: ExecuteOptions = {
            ...(msg.options ?? {}),
            ...(expected ? { expected } : {}),
          };
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
