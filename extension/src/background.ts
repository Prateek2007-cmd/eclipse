/** Service worker (Sprint 2).
 *
 * Wires the content script to the offscreen document. The full
 * orchestration (planner calls, executor, checkpoints) is Sprint 3;
 * this file only owns the observe path:
 *   content script walks the DOM → service worker captures the
 *   screenshot → offscreen document observes + redacts + gates →
 *   the gated redacted payload comes back here.
 */
const OFFSCREEN_URL = "offscreen.html";

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

chrome.runtime.onMessage.addListener((msg: { type: string; tabId?: number; taskKind?: string; egressTarget?: string }, _sender, sendResponse) => {
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
