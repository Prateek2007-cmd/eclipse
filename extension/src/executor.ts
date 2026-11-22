/** Guarded executor (Sprint 3, plan §6.2/§6.3/§6.9; fix pass 2026-11-21).
 *
 * Performs ONE validated planner action on the live page. The action
 * must resolve to a real DOM element id from the current walk — never
 * a guessed selector. The allow-list is the same as the server-side
 * validator (server/planner/validate.py); the executor re-checks it
 * (defence in depth) because it is the last gate before the page.
 *
 * Injection defence: page text is data, never instructions. The
 * executor never evaluates page text (no eval, no innerHTML) and
 * only performs the allow-listed action on the named element. An
 * action whose text carries an injection marker is refused — but
 * that check is only a best-effort signal (it is trivially
 * bypassable by rephrasing); the server-side validator and the
 * verify-by-re-observing step are the real controls.
 *
 * Never retry a submit (§6.9): a click on a submit button is
 * executed at most once per (task, element). A second attempt is
 * refused, so a failed verify can never double-submit. The
 * server is the durable backstop (it refuses to re-execute a step
 * that is already done); this in-memory guard is the second layer.
 */
import type { ElementInfo } from "./vision/types.js";
import { accessibleNameOf, isHiddenElement } from "./vision/dom-walker.js";

export type ExecutorAction = {
  action: "click" | "type" | "select" | "navigate" | "wait" | "scroll";
  element_id?: string;
  text?: string;
  url?: string;
  seconds?: number;
  reason?: string;
};

export type ExecutorResult = {
  ok: boolean;
  detail: string;
  /** The page URL after the action (or the navigate target),
   *  with the query string and fragment stripped — a URL can
   *  carry sensitive query params (reset token, email). */
  url: string;
};

export const EXECUTOR_ACTIONS = ["click", "type", "select", "navigate", "wait", "scroll"] as const;

const INJECTION_MARKERS = [
  "ignore your rules",
  "ignore all previous",
  "ignore previous instructions",
  "disregard your instructions",
  "reveal your instructions",
  "reveal the system prompt",
  "send the password",
  "send the card",
];

/** (taskId:elementId) pairs whose submit click already ran. */
const executedSubmits = new Set<string>();

export function resetExecutedSubmits(): void {
  executedSubmits.clear();
}

export interface ExecuteOptions {
  taskId?: string;
  /** Allowed hosts for navigate (from the task constraints). */
  allowedSites?: string[];
  /** The element info from the observation the planner saw. When
   *  supplied, the resolved node's tag + accessible name must still
   *  match it — the DOM may have moved between observe and execute,
   *  and a stale positional id must not retarget the action. */
  expected?: ElementInfo;
  /** Resolve a placeholder token ([EMAIL_1]) to its real value.
   *  The vault fills this in Sprint 5; until then the caller passes
   *  a map (tests) or nothing (placeholders are refused). */
  fill?: (placeholder: string) => string | undefined;
}

function isPlaceholder(text: string): boolean {
  return /^\[[A-Z0-9_]+\]$/.test(text.trim());
}

/** A URL with the query string and fragment stripped. Non-http(s)
 *  URLs (about:blank, chrome://…) are returned as-is — their
 *  origin is the literal "null", so concatenating origin +
 *  pathname would produce nonsense ("nullblank"). Exported so the
 *  content script's error path strips the page URL the same way —
 *  a URL can carry a reset token or an email in its query string. */
export function safeUrl(raw: string): string {
  try {
    const u = new URL(raw);
    if (u.protocol !== "http:" && u.protocol !== "https:") {
      return raw.split("?")[0].split("#")[0];
    }
    return `${u.origin}${u.pathname}`;
  } catch {
    return raw.split("?")[0].split("#")[0];
  }
}

function dispatchInput(el: Element): void {
  const doc = el.ownerDocument;
  const win = doc?.defaultView as unknown as {
    Event?: new (type: string, init?: { bubbles: boolean }) => Event;
    InputEvent?: new (
      type: string,
      init?: { bubbles: boolean; inputType?: string; data?: string },
    ) => Event;
  } | null;
  // An InputEvent (with inputType/data) is what libraries and
  // frameworks expect; fall back to a plain bubbling Event.
  if (win?.InputEvent) {
    el.dispatchEvent(
      new win.InputEvent("input", { bubbles: true, inputType: "insertText" }),
    );
  } else if (win?.Event) {
    el.dispatchEvent(new win.Event("input", { bubbles: true }));
  }
  if (win?.Event) {
    el.dispatchEvent(new win.Event("change", { bubbles: true }));
  }
}

function dispatch(el: Element, type: string): void {
  const doc = el.ownerDocument;
  const win = doc?.defaultView as unknown as {
    Event?: new (type: string, init?: { bubbles: boolean }) => Event;
  } | null;
  if (win?.Event) {
    el.dispatchEvent(new win.Event(type, { bubbles: true }));
  }
}

function buttonType(node: Element): string {
  // The IDL .type property reflects the HTML default: a <button>
  // with a missing or invalid type attribute is a submit button.
  // Some runtimes (linkedom) do not implement that default, so fall
  // back to the attribute, and to "submit" for a missing/invalid one
  // (the spec's missing/invalid value default).
  const idl = (node as HTMLButtonElement).type;
  if (typeof idl === "string" && idl) return idl.toLowerCase();
  const attr = (node.getAttribute("type") ?? "").toLowerCase();
  if (attr === "submit" || attr === "button" || attr === "reset") return attr;
  return "submit";
}

function hostAllowed(host: string, allowedSites: string[] | undefined, currentHost: string): boolean {
  const h = host.toLowerCase();
  if (h === currentHost.toLowerCase()) return true; // the current site
  if (!allowedSites?.length) return false;
  return allowedSites.some(
    (s) => h === s.toLowerCase() || h.endsWith("." + s.toLowerCase()),
  );
}

export async function executeAction(
  action: ExecutorAction,
  doc: Document,
  nodes: Map<string, Element>,
  opts: ExecuteOptions = {},
): Promise<ExecutorResult> {
  const pageUrl = safeUrl(doc.URL ?? "");
  const currentHost = (() => {
    try {
      return new URL(doc.URL ?? "").hostname;
    } catch {
      return "";
    }
  })();
  if (!EXECUTOR_ACTIONS.includes(action.action as (typeof EXECUTOR_ACTIONS)[number])) {
    return { ok: false, detail: `unknown action: ${action.action}`, url: pageUrl };
  }
  const text = action.text ?? "";
  for (const marker of INJECTION_MARKERS) {
    if (text.toLowerCase().includes(marker)) {
      return { ok: false, detail: `refused: injection marker in action text (${marker})`, url: pageUrl };
    }
  }

  if (action.action === "navigate") {
    const url = action.url ?? "";
    if (!/^https?:\/\//i.test(url)) {
      return { ok: false, detail: "navigate needs a full http(s) URL", url: pageUrl };
    }
    // Fail closed: without an allow-list, only the current site.
    // A URL that passes the regex can still be unparseable
    // ("https://") — refuse it, do not let `new URL` throw out
    // of the executor (a throw becomes a bare error response).
    let host: string;
    try {
      host = new URL(url).hostname.toLowerCase();
    } catch {
      return { ok: false, detail: "navigate needs a valid http(s) URL", url: pageUrl };
    }
    if (!hostAllowed(host, opts.allowedSites, currentHost)) {
      return { ok: false, detail: `navigate to ${host} is outside the allowed sites`, url: pageUrl };
    }
    // Real browsers navigate here. Linkedom test runtimes have no
    // navigation; the result still reports the target URL.
    // The detail uses the stripped URL too — the raw URL can carry
    // a sensitive query string (a reset token, an email).
    try {
      const win = doc.defaultView as unknown as { location?: { href: string } } | null;
      if (win?.location) win.location.href = url;
    } catch {
      // Non-browser runtime: report the target, do not fail.
    }
    return { ok: true, detail: `navigating to ${safeUrl(url)}`, url: safeUrl(url) };
  }

  if (action.action === "wait") {
    const raw = Number(action.seconds);
    const secs = Number.isFinite(raw) ? Math.min(Math.max(raw, 1), 30) : 1;
    await new Promise((r) => setTimeout(r, secs * 1000));
    return { ok: true, detail: `waited ${secs}s`, url: pageUrl };
  }

  // Element actions: resolve the id through the walk's node map.
  const elId = action.element_id ?? "";
  const node = nodes.get(elId);
  if (!node) {
    return { ok: false, detail: `element ${elId} is not in the current walk`, url: pageUrl };
  }
  // The node map is a snapshot: the element may have left the page
  // between the walk and the execute (a detached node's click()
  // still fires its listeners). Never act on a node that is no
  // longer in the live document.
  if (!node.isConnected) {
    return { ok: false, detail: `element ${elId} is no longer in the page`, url: pageUrl };
  }
  const tag = node.tagName.toLowerCase();
  const type = (node.getAttribute("type") ?? "").toLowerCase();

  // Fingerprint: the resolved node must still be the element the
  // planner saw (tag + accessible name). A positional synthetic id
  // can go stale when the DOM moves between observe and execute.
  if (opts.expected) {
    if (typeof opts.expected.tag !== "string") {
      return {
        ok: false,
        detail: `element ${elId} has a malformed fingerprint (no tag); re-observe`,
        url: pageUrl,
      };
    }
    if (tag !== opts.expected.tag.toLowerCase()) {
      return {
        ok: false,
        detail: `element ${elId} changed since the walk (tag ${opts.expected.tag} → ${tag}); re-observe`,
        url: pageUrl,
      };
    }
    if (opts.expected.name) {
      const name = accessibleNameOf(node, doc);
      if (name !== opts.expected.name) {
        return {
          ok: false,
          detail: `element ${elId} changed since the walk (name changed); re-observe`,
          url: pageUrl,
        };
      }
    }
    // The submit semantics must match too: a button the planner
    // saw as type="button" outside a form must not have become a
    // submit (same tag, same name, different consequence). The
    // walker records type/inForm from the same attributes, so a
    // mismatch means the DOM moved. Form membership here is
    // closest("form") only — the walker records it that way; the
    // form-attribute association is a submit-guard concern below.
    if (opts.expected.type !== undefined && opts.expected.type !== type) {
      return {
        ok: false,
        detail: `element ${elId} changed since the walk (type ${opts.expected.type} → ${type || "(none)"}); re-observe`,
        url: pageUrl,
      };
    }
    if (
      opts.expected.inForm !== undefined &&
      opts.expected.inForm !== (node.closest("form") !== null)
    ) {
      return {
        ok: false,
        detail: `element ${elId} changed since the walk (form membership changed); re-observe`,
        url: pageUrl,
      };
    }
  }

  // Re-check the state at execution time: the walk's
  // visible/interactable flags are a snapshot. A button that became
  // disabled after the walk, or an element that is now hidden, must
  // not be clicked or typed into.
  const disabled =
    node.hasAttribute("disabled") ||
    node.getAttribute("aria-disabled") === "true";
  if (disabled && action.action !== "scroll") {
    return { ok: false, detail: `element ${elId} is disabled`, url: pageUrl };
  }
  if (isHiddenElement(node) && action.action !== "scroll") {
    return { ok: false, detail: `element ${elId} is hidden`, url: pageUrl };
  }

  if (action.action === "click") {
    // A file input opens a native picker the agent cannot complete.
    if (tag === "input" && type === "file") {
      return { ok: false, detail: "file input is human-only", url: pageUrl };
    }
    // A click on a link navigates: the href must pass the same
    // allow-list as navigate. A javascript: (or data:, or any
    // non-http(s)) href is refused outright — clicking it would
    // evaluate page text as code.
    if (tag === "a") {
      const href = (node as HTMLAnchorElement).getAttribute("href");
      if (href && !href.startsWith("#")) {
        let targetHost = "";
        let protocol = "";
        try {
          const parsed = new URL(href, doc.URL);
          targetHost = parsed.hostname.toLowerCase();
          protocol = parsed.protocol.toLowerCase();
        } catch {
          targetHost = "";
        }
        if (protocol && protocol !== "http:" && protocol !== "https:") {
          return { ok: false, detail: `refused: link uses ${protocol} (not http(s))`, url: pageUrl };
        }
        if (targetHost && !hostAllowed(targetHost, opts.allowedSites, currentHost)) {
          return { ok: false, detail: `link to ${targetHost} is outside the allowed sites`, url: pageUrl };
        }
      }
    }
    // Is this a submit? Use the IDL .type property (buttonType
    // falls back to the spec default): a <button> with a missing or
    // invalid type attribute defaults to "submit" when in a form,
    // and getAttribute returns "" for it. <input type="image">
    // also submits. And ANY button inside a form submits it — a
    // type="button" "Continue" in a multi-step form is a submit
    // (the common job-site pattern), even though its type says
    // "button". That covers <input type="button"> in a form too,
    // and a button associated with a form through its `form`
    // attribute (no ancestor <form>). A type="button" button with
    // no form association (an "Apply now" that only opens the
    // form) is not a submit.
    const formOwner = (node as HTMLButtonElement).form;
    const inForm = node.closest("form") !== null || formOwner != null;
    const isSubmit =
      (tag === "button" && (buttonType(node) === "submit" || inForm)) ||
      (tag === "input" && (type === "submit" || type === "image" || (type === "button" && inForm)));
    if (isSubmit) {
      // No taskId → no guard key → refuse the submit. Two different
      // tasks that omit taskId must not share one guard key.
      if (!opts.taskId) {
        return { ok: false, detail: "submit needs a taskId (no guard key)", url: pageUrl };
      }
      const key = `${opts.taskId}:${elId}`;
      if (executedSubmits.has(key)) {
        return { ok: false, detail: `submit already executed for ${elId} (never retry a submit)`, url: pageUrl };
      }
      executedSubmits.add(key);
    }
    (node as HTMLElement).click?.();
    return { ok: true, detail: `clicked ${elId}`, url: pageUrl };
  }

  if (action.action === "type") {
    if (!text) return { ok: false, detail: "type needs text", url: pageUrl };
    // Only text fields can be typed into. Typing into a button or
    // a select throws a cryptic TypeError in a real browser.
    const contentEditable = node.getAttribute("contenteditable") === "true";
    if (tag !== "input" && tag !== "textarea" && !contentEditable) {
      return { ok: false, detail: `cannot type into ${elId} (a ${tag})`, url: pageUrl };
    }
    if (tag === "input" && ["checkbox", "radio", "file", "submit", "button", "image"].includes(type)) {
      return { ok: false, detail: `cannot type into ${elId} (an input of type ${type})`, url: pageUrl };
    }
    if (node.hasAttribute("readonly")) {
      return { ok: false, detail: `element ${elId} is readonly`, url: pageUrl };
    }
    // Resolve placeholders anywhere in the text (not only when the
    // whole string is one placeholder). Any placeholder without a
    // vault value refuses the action — never type a literal token.
    let value = text;
    const placeholders = [...text.matchAll(/\[([A-Z0-9_]+)\]/g)].map((m) => m[0]);
    for (const token of placeholders) {
      const filled = opts.fill?.(token);
      if (filled === undefined) {
        return { ok: false, detail: `placeholder ${token} has no vault value (vault fills in Sprint 5)`, url: pageUrl };
      }
      value = value.replaceAll(token, filled);
    }
    const input = node as HTMLInputElement | HTMLTextAreaElement;
    // A contenteditable element has no .value setter — the
    // HTMLInputElement setter throws illegal-invocation when
    // `this` is a div in a real browser, and in linkedom it sets
    // a useless `value` property. Set the text content and
    // dispatch beforeinput + input with the data, which is what
    // editors (and React) listen for.
    if (contentEditable) {
      node.textContent = value;
      const win = doc.defaultView as unknown as {
        Event?: new (type: string, init?: { bubbles: boolean }) => Event;
        InputEvent?: new (
          type: string,
          init?: { bubbles: boolean; inputType?: string; data?: string },
        ) => Event;
      } | null;
      for (const [typeName, inputType] of [
        ["beforeinput", "insertText"],
        ["input", "insertText"],
      ] as const) {
        let event: Event | null = null;
        try {
          event = win?.InputEvent
            ? new win.InputEvent(typeName, { bubbles: true, inputType, data: value })
            : null;
        } catch {
          event = null;
        }
        if (!event && win?.Event) {
          event = new win.Event(typeName, { bubbles: true });
          (event as Event & { data?: string }).data = value;
        }
        if (event) node.dispatchEvent(event);
      }
      if (win?.Event) {
        node.dispatchEvent(new win.Event("change", { bubbles: true }));
      }
      return { ok: true, detail: `typed into ${elId}`, url: pageUrl };
    }
    (input as HTMLElement).focus?.();
    // Use the native setter so frameworks (React) see the change.
    const proto = tag === "textarea"
      ? (doc.defaultView as unknown as { HTMLTextAreaElement?: { prototype: unknown } } | null)?.HTMLTextAreaElement?.prototype
      : (doc.defaultView as unknown as { HTMLInputElement?: { prototype: unknown } } | null)?.HTMLInputElement?.prototype;
    const setter = (proto as { value?: unknown } | undefined)
      ? Object.getOwnPropertyDescriptor(proto as object, "value")?.set
      : undefined;
    if (setter) setter.call(input, value);
    else input.value = value;
    dispatchInput(node);
    (input as HTMLElement).blur?.();
    return { ok: true, detail: `typed into ${elId}`, url: pageUrl };
  }

  if (action.action === "select") {
    if (node.hasAttribute("disabled")) {
      return { ok: false, detail: `element ${elId} is disabled`, url: pageUrl };
    }
    const select = node as HTMLSelectElement;
    const options = Array.from(select.options ?? []);
    // Match by value or by visible label. textContent is used (not
    // option.text / option.value) because linkedom's option elements
    // expose neither; browsers expose both, and textContent agrees.
    const idx = options.findIndex((o) => {
      const label = (o.textContent ?? "").trim();
      const val = (o as HTMLOptionElement).value || label;
      return val === text || label === text.trim();
    });
    if (idx < 0) {
      return { ok: false, detail: `no option ${JSON.stringify(text)} in ${elId}`, url: pageUrl };
    }
    // selectedIndex works in browsers and in linkedom (where
    // select.value is getter-only and option.selected does not stick).
    // The detail names only the element — the option label can
    // carry PII, and it must not flow into the server/audit log.
    select.selectedIndex = idx;
    dispatch(node, "change");
    return { ok: true, detail: `selected an option in ${elId}`, url: pageUrl };
  }

  if (action.action === "scroll") {
    (node as HTMLElement).scrollIntoView?.();
    return { ok: true, detail: `scrolled to ${elId}`, url: pageUrl };
  }

  return { ok: false, detail: `unhandled action: ${action.action}`, url: pageUrl };
}
