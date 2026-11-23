/** Popup script: shows Eclipse status, never fakes control.
 *
 * What it reads, and nothing more:
 *   - whether a server token is stored (chrome.storage.local,
 *     key "eclipseToken" — the value is never displayed, only
 *     sent to the local server as the X-Eclipse-Token header,
 *     exactly like background.ts does);
 *   - whether the local server answers GET /health (every server
 *     endpoint is token-authenticated, so a 401 still proves the
 *     server is up — it only proves the token is missing/bad).
 *
 * What it writes (PR #5 token-handoff fix): the token input
 * below saves the pasted token to chrome.storage.local
 * ("eclipseToken"). This is the ONLY write path for that key in
 * the extension — before it existed, nothing ever stored the
 * token, so every alarm run failed and no watch or briefing
 * ever ran. Saving also fires background.ts's
 * storage.onChanged listener, which schedules the watch and
 * briefing alarms. The stored value is never read back into
 * the input or displayed anywhere — the popup only shows
 * "Saved".
 *
 * What it does NOT do, on purpose:
 *   - no current-task lookup: the background script keeps no task
 *     state and exposes no task message, so the popup honestly
 *     shows "No task running" and points at the workspace;
 *   - no pause/resume: the background script has no pause
 *     message or API, and task orchestration lives on the server.
 */
const SERVER = "http://127.0.0.1:8000";
const WORKSPACE_URL = `${SERVER}/workspace`;
const PROOF_URL = `${SERVER}/proof`;
const CHECK_TIMEOUT_MS = 3000;

/** Shape of GET /health (server/api/system.py). */
interface HealthResponse {
  status: string;
  service: string;
  version: string;
}

/** Outcome of the /health probe. */
type ServerState =
  | "ok" // 200, token accepted
  | "reachable" // answered 401 with no token — server is up
  | "rejected" // answered 401 with a token — token is bad
  | "unreachable" // network error or timeout
  | "unexpected"; // answered, but not 200/401

type DotState = "ok" | "warn" | "bad" | "unknown";

function isHealthResponse(value: unknown): value is HealthResponse {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  return (
    typeof record["status"] === "string" &&
    typeof record["service"] === "string" &&
    typeof record["version"] === "string"
  );
}

function setRow(dotId: string, textId: string, state: DotState, text: string): void {
  const dot = document.getElementById(dotId);
  const label = document.getElementById(textId);
  if (dot) dot.setAttribute("data-state", state);
  if (label) label.textContent = text;
}

/** The stored token, or null. The value is only ever sent to the
 * local server — it is never rendered or logged. */
async function readToken(): Promise<string | null> {
  const stored = await chrome.storage.local.get("eclipseToken");
  const token = stored["eclipseToken"];
  return typeof token === "string" && token.length > 0 ? token : null;
}

async function probeServer(token: string | null): Promise<ServerState> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), CHECK_TIMEOUT_MS);
  try {
    const headers: Record<string, string> = {};
    if (token !== null) headers["X-Eclipse-Token"] = token;
    const res = await fetch(`${SERVER}/health`, { headers, signal: controller.signal });
    if (res.status === 200) {
      const body: unknown = await res.json();
      return isHealthResponse(body) && body.status === "ok" ? "ok" : "unexpected";
    }
    if (res.status === 401) return token === null ? "reachable" : "rejected";
    return "unexpected";
  } catch {
    return "unreachable";
  } finally {
    clearTimeout(timer);
  }
}

async function refreshStatus(): Promise<void> {
  const refresh = document.getElementById("refresh");
  if (refresh instanceof HTMLButtonElement) refresh.disabled = true;
  setRow("server-dot", "server-text", "unknown", "Checking…");
  setRow("token-dot", "token-text", "unknown", "Checking…");

  const token = await readToken();
  if (token === null) {
    setRow("token-dot", "token-text", "warn", "Not saved — paste the token below");
  } else {
    setRow("token-dot", "token-text", "ok", "Saved");
  }

  const state = await probeServer(token);
  switch (state) {
    case "ok":
      setRow("server-dot", "server-text", "ok", "Running — token accepted");
      break;
    case "reachable":
      setRow("server-dot", "server-text", "ok", "Reachable — save a token to verify it");
      break;
    case "rejected":
      setRow("server-dot", "server-text", "bad", "Running, but the saved token was rejected — paste the token again below");
      break;
    case "unreachable":
      setRow("server-dot", "server-text", "bad", "Not reachable — is the local server running?");
      break;
    case "unexpected":
      setRow("server-dot", "server-text", "warn", "Answered, but not as expected");
      break;
  }

  if (refresh instanceof HTMLButtonElement) refresh.disabled = false;
}

function setSaveText(text: string): void {
  const el = document.getElementById("token-save-text");
  if (el) el.textContent = text;
}

/** Save the pasted token to chrome.storage.local. The input is
 * cleared immediately and the value is never displayed — the
 * popup only ever shows "Saved". Saving triggers the
 * background script's storage.onChanged listener, which
 * schedules the watch + briefing alarms. */
async function saveToken(): Promise<void> {
  const input = document.getElementById("token-input");
  const save = document.getElementById("save-token");
  if (!(input instanceof HTMLInputElement)) return;
  const value = input.value.trim();
  if (value.length === 0) {
    setSaveText("Paste a token first.");
    input.focus();
    return;
  }
  if (save instanceof HTMLButtonElement) save.disabled = true;
  try {
    await chrome.storage.local.set({ eclipseToken: value });
    // Clear the field at once: the token must not sit visible
    // in the popup after saving.
    input.value = "";
    setSaveText("Saved — watches and the morning briefing are now scheduled.");
  } catch {
    setSaveText("Could not save the token — try again.");
    if (save instanceof HTMLButtonElement) save.disabled = false;
    return;
  }
  if (save instanceof HTMLButtonElement) save.disabled = false;
  // Re-probe with the new token so the status rows update.
  await refreshStatus();
}

document.getElementById("save-token")?.addEventListener("click", () => {
  void saveToken();
});

document.getElementById("token-input")?.addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    event.preventDefault();
    void saveToken();
  }
});

document.getElementById("refresh")?.addEventListener("click", () => {
  void refreshStatus();
});

document.getElementById("open-workspace")?.addEventListener("click", () => {
  void chrome.tabs.create({ url: WORKSPACE_URL });
});

document.getElementById("open-proof")?.addEventListener("click", () => {
  void chrome.tabs.create({ url: PROOF_URL });
});

void refreshStatus();
