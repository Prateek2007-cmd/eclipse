/** Content script (Sprint 2).
 *
 * Two jobs:
 *  1. Walk the live DOM + accessibility tree on demand and hand the
 *     elements to the offscreen document (which redacts them).
 *  2. Paint destructive blackout overlays over sensitive regions
 *     when the offscreen document says so (the screenshot blackout
 *     is the proof; the overlay is what the user sees).
 */
import { walkDom } from "./vision/dom-walker.js";
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

chrome.runtime.onMessage.addListener((msg: { type: string; boxes?: Box[] }, _sender, sendResponse) => {
  if (msg.type === "walk") {
    sendResponse({
      elements: walkDom(document),
      viewport: { width: window.innerWidth, height: window.innerHeight },
      url: location.href,
    });
    return true;
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
});
