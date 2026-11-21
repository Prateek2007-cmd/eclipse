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
          const options: ExecuteOptions = {
            ...(msg.options ?? {}),
            expected: msg.options?.expected ?? fresh,
          };
          // The vault fill is a plain map over the message (a
          // function cannot cross chrome.runtime.sendMessage);
          // build the fill function from it here. Sprint 5 wires
          // the vault into the background script, which will send
          // this map.
          const fillMap = (msg.options as { fill?: Record<string, string> } | undefined)?.fill;
          if (fillMap && !options.fill) {
            options.fill = (token: string) => fillMap[token];
          }
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
