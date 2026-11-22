/** DOM + accessibility-tree walker (Sprint 2).
 *
 * Runs in the content script on the live page. Takes a `Document` so
 * the same code can be tested in Node against fixture HTML (linkedom).
 * DOM gives exact boxes for ~80–90% of web elements; YOLO + OCR cover
 * the rest. The walker never records password / OTP / CVV values.
 */
import type { ElementInfo } from "./types.js";

const IMPLICIT_ROLES: Record<string, string> = {
  a: "link",
  button: "button",
  h1: "heading",
  h2: "heading",
  h3: "heading",
  h4: "heading",
  h5: "heading",
  h6: "heading",
  img: "img",
  input: "textbox",
  select: "combobox",
  textarea: "textbox",
  ul: "list",
  ol: "list",
  li: "listitem",
  nav: "navigation",
  main: "main",
  header: "banner",
  footer: "contentinfo",
  form: "form",
  table: "table",
};

const INPUT_ROLES: Record<string, string> = {
  button: "button",
  submit: "button",
  reset: "button",
  checkbox: "checkbox",
  radio: "radio",
  range: "slider",
  search: "searchbox",
  password: "textbox",
  email: "textbox",
  tel: "textbox",
  url: "textbox",
  number: "spinbutton",
  date: "textbox",
};

const SENSITIVE_INPUT_TYPES = new Set(["password"]);
const SENSITIVE_NAME_HINTS = /otp|cvv|cvc|security.?code|one.?time/i;

/** CSS.escape exists in browsers; Node test runtimes may lack it. */
function cssEscape(s: string): string {
  const css = (globalThis as unknown as { CSS?: { escape(s: string): string } }).CSS;
  if (css?.escape) return css.escape(s);
  return s.replace(/"/g, '\\"');
}

const INTERACTABLE_TAGS = new Set([
  "a",
  "button",
  "input",
  "select",
  "textarea",
  "summary",
]);

/** The next ancestor of an element, crossing shadow-root and
 *  same-origin-iframe boundaries (null at the top document).
 *  parentElement alone stops at a shadow root (the top shadow
 *  child's parentElement is null) and at a document boundary. */
function nextAncestor(el: Element): Element | null {
  if (el.parentElement) return el.parentElement;
  // Shadow boundary: continue from the host element.
  const root =
    typeof el.getRootNode === "function" ? el.getRootNode() : null;
  const host = (root as { host?: Element } | null)?.host;
  if (host) return host;
  // Iframe boundary: continue from the embedding <iframe> in the
  // parent document (null for the top document's window).
  try {
    const win = el.ownerDocument?.defaultView as unknown as
      | { frameElement?: Element | null }
      | null
      | undefined;
    if (win?.frameElement) return win.frameElement;
  } catch {
    // Cross-origin or no window; stop here.
  }
  return null;
}

type ComputedStyleLike = { display: string; visibility: string };

/** The element's computed style in a real browser (null in
 *  linkedom, which has no layout). The window is resolved from
 *  the element's OWN document — an ancestor across an iframe
 *  boundary lives under a different window. */
function computedStyleOf(el: Element): ComputedStyleLike | null {
  const win = el.ownerDocument?.defaultView as unknown as
    | { getComputedStyle?: (e: Element) => ComputedStyleLike }
    | null
    | undefined;
  if (!win?.getComputedStyle) return null;
  try {
    return win.getComputedStyle(el);
  } catch {
    // No layout in this context.
    return null;
  }
}

/** Hidden by the element's own attributes / inline style.
 *  `checkVisibility` is true only for the element itself (see
 *  isHidden for why ancestors are not checked for visibility). */
function hiddenByOwnStyle(el: Element, checkVisibility: boolean): boolean {
  if (el.hasAttribute("hidden")) return true;
  const style = (el.getAttribute("style") ?? "").toLowerCase();
  if (/display\s*:\s*none/.test(style)) return true;
  if (checkVisibility && /visibility\s*:\s*hidden/.test(style)) return true;
  return false;
}

function isHidden(el: Element): boolean {
  // The element itself: hidden attribute, inline style, computed
  // style (display or visibility). Real browsers have computed
  // style; linkedom has none.
  if (hiddenByOwnStyle(el, true)) return true;
  const selfCs = computedStyleOf(el);
  if (selfCs && (selfCs.display === "none" || selfCs.visibility === "hidden")) {
    return true;
  }
  // Ancestors: display:none on ANY ancestor removes the whole
  // subtree — a button inside a display:none container keeps its
  // own computed display (the computed value is the element's
  // own specified value), so checking only the element walked
  // invisible controls as visible and the executor would click
  // what the user cannot see. Ancestors are NOT checked for
  // visibility: it is inherited but overridable, so a child with
  // visibility:visible inside a visibility:hidden parent IS
  // rendered.
  let cur = nextAncestor(el);
  while (cur) {
    if (hiddenByOwnStyle(cur, false)) return true;
    const cs = computedStyleOf(cur);
    if (cs && cs.display === "none") return true;
    cur = nextAncestor(cur);
  }
  return false;
}

/** The element's OWN direct text (child text nodes only) — NOT the
 *  recursive textContent. A container's textContent is the whole
 *  page concatenated; reporting it per element duplicates every PII
 *  value once per ancestor and breaks label-prefixed redaction
 *  ("Name: ..." is not at the start of the concatenation). */
function ownText(el: Element): string {
  let out = "";
  for (const node of Array.from(el.childNodes)) {
    if (node.nodeType === 3) out += node.textContent ?? "";
  }
  return out.trim().replace(/\s+/g, " ");
}

/** Does the element contain a nested interactable descendant?
 *  Used by the accessible-name fallback: a button's label may sit
 *  in a <span> (most real buttons, shadcn included, wrap it), but
 *  a container's textContent is the whole subtree — only use the
 *  recursive text when no nested control owns part of it. */
function hasInteractableDescendant(el: Element): boolean {
  return (
    el.querySelector("a, button, input, select, textarea, [role='button']") !== null
  );
}

function accessibleName(el: Element, doc: Document): string {
  const ariaLabel = el.getAttribute("aria-label");
  if (ariaLabel?.trim()) return ariaLabel.trim();
  const labelledBy = el.getAttribute("aria-labelledby");
  if (labelledBy) {
    const parts = labelledBy
      .split(/\s+/)
      .map((id) => doc.getElementById(id)?.textContent?.trim() ?? "")
      .filter(Boolean);
    if (parts.length) return parts.join(" ");
  }
  if (el.tagName.toLowerCase() === "input" || el.tagName.toLowerCase() === "textarea") {
    const id = el.getAttribute("id");
    if (id) {
      const label = doc.querySelector(`label[for="${cssEscape(id)}"]`);
      if (label?.textContent?.trim()) return label.textContent.trim();
    }
    const wrapping = el.closest("label");
    if (wrapping?.textContent?.trim()) return wrapping.textContent.trim();
    const placeholder = el.getAttribute("placeholder");
    if (placeholder?.trim()) return placeholder.trim();
  }
  if (el.tagName.toLowerCase() === "img") {
    return el.getAttribute("alt") ?? "";
  }
  // Fallback: the element's own direct text. (textContent would be
  // the whole subtree for a container — see ownText.) But when the
  // own text is empty and the element is itself a label-bearing
  // control (a button or link), its label may be wrapped in a
  // <span> — use the recursive text then, e.g.
  // <button><span>Apply now</span></button>. Containers (html,
  // body, div) NEVER get this fallback: their textContent is the
  // whole page, and reporting it per element duplicates every PII
  // value once per ancestor.
  const own = ownText(el);
  if (own) return own.slice(0, 120);
  const tag = el.tagName.toLowerCase();
  const role = el.getAttribute("role");
  const isLabelBearingControl =
    tag === "button" || tag === "a" || role === "button" || role === "link";
  if (isLabelBearingControl && !hasInteractableDescendant(el)) {
    const full = (el.textContent ?? "").trim().replace(/\s+/g, " ");
    if (full) return full.slice(0, 120);
  }
  return "";
}

function roleOf(el: Element): string {
  const explicit = el.getAttribute("role");
  if (explicit) return explicit;
  const tag = el.tagName.toLowerCase();
  if (tag === "input") {
    return INPUT_ROLES[(el.getAttribute("type") ?? "text").toLowerCase()] ?? "textbox";
  }
  return IMPLICIT_ROLES[tag] ?? "generic";
}

function rectOf(el: Element): { x: number; y: number; width: number; height: number } {
  const anyEl = el as Element & { getBoundingClientRect?: () => DOMRect };
  if (typeof anyEl.getBoundingClientRect === "function") {
    try {
      const r = anyEl.getBoundingClientRect();
      return { x: r.x, y: r.y, width: r.width, height: r.height };
    } catch {
      return { x: 0, y: 0, width: 0, height: 0 };
    }
  }
  return { x: 0, y: 0, width: 0, height: 0 };
}

/** All elements in document order, descending into open shadow
 *  roots and same-origin iframes. querySelectorAll("*") alone
 *  never pierces either, so web-component forms and embedded
 *  same-origin frames were invisible to the walker (their PII
 *  was never observed — and their fields never plannable).
 *  A cross-origin iframe throws on contentDocument access and
 *  is skipped; a closed shadow root has shadowRoot null and is
 *  skipped the same way. Each element carries its viewport
 *  offset (dx, dy): shadow children share the host's space
 *  (offset 0), but an iframe's document lives in the iframe's
 *  own viewport, so its elements' rects must be shifted by the
 *  iframe's position — without that, planner clicks and
 *  blackout boxes inside an iframe land in the wrong place. */
function allElements(doc: Document): { el: Element; dx: number; dy: number }[] {
  const out: { el: Element; dx: number; dy: number }[] = [];
  const visit = (root: Document | ShadowRoot, dx: number, dy: number): void => {
    for (const el of Array.from(root.querySelectorAll("*"))) {
      out.push({ el, dx, dy });
      if (el.shadowRoot) visit(el.shadowRoot, dx, dy);
      if (el.tagName.toLowerCase() === "iframe") {
        try {
          const inner = (el as HTMLIFrameElement).contentDocument;
          if (inner) {
            const r = rectOf(el);
            visit(inner, dx + r.x, dy + r.y);
          }
        } catch {
          // Cross-origin iframe: unreadable by design.
        }
      }
    }
  };
  visit(doc, 0, 0);
  return out;
}

/** Tags that can own a form association (the form-associated
 *  elements a `form="…"` attribute can bind). */
const FORM_ASSOCIATED_TAGS = new Set([
  "button",
  "fieldset",
  "input",
  "object",
  "output",
  "select",
  "textarea",
]);

/** Whether the element belongs to a form — by ancestry, by the
 *  IDL form owner, or by a `form="…"` attribute naming a form
 *  elsewhere in the document. closest("form") alone misses the
 *  attribute case: a submit button outside its <form> with
 *  form="f1" still submits it, and the server's
 *  consequential-action check reads this flag. The IDL `.form`
 *  covers both cases in a real browser; the attribute lookup is
 *  the fallback for runtimes (linkedom) that do not implement
 *  the IDL. The lookup is scoped to the element's own tree
 *  (shadow roots included) — a form association never crosses
 *  a shadow boundary. */
function inFormOf(el: Element, doc: Document): boolean {
  if (el.closest("form") !== null) return true;
  if (!FORM_ASSOCIATED_TAGS.has(el.tagName.toLowerCase())) return false;
  // IDL form owner (real browsers).
  const idl = (el as { form?: { tagName?: string } | null }).form;
  if (idl && typeof idl.tagName === "string" && idl.tagName.toLowerCase() === "form") {
    return true;
  }
  // form="id" attribute (fallback when the IDL is unavailable).
  const formId = el.getAttribute("form")?.trim();
  if (!formId) return false;
  const root = typeof el.getRootNode === "function" ? el.getRootNode() : doc;
  const owner = (root as Document).getElementById?.(formId);
  return !!owner && owner.tagName.toLowerCase() === "form";
}

export interface WalkOptions {
  /** Maximum elements to return (plan: DOM walk is cheap; cap at 2000). */
  maxElements?: number;
}

/** Walk the page and return every visible element with its role,
 *  accessible name, text and rect, plus the id → DOM node map the
 *  executor uses to resolve a planner action to a real element.
 *  Sensitive values are never recorded. Returns `truncated: true`
 *  when the 2000-element cap cut the walk short — the planner must
 *  know the observation is incomplete (it is not a full page). */
function walkInternal(doc: Document, opts: WalkOptions = {}): {
  info: ElementInfo[];
  nodes: Map<string, Element>;
  truncated: boolean;
} {
  const maxElements = opts.maxElements ?? 2000;
  const out: ElementInfo[] = [];
  const nodes = new Map<string, Element>();
  const all = allElements(doc);
  let n = 0;
  let truncated = false;
  for (const { el, dx, dy } of all) {
    if (out.length >= maxElements) {
      truncated = true;
      break;
    }
    const tag = el.tagName.toLowerCase();
    if (tag === "script" || tag === "style" || tag === "head" || tag === "meta" || tag === "link") {
      continue;
    }
    // A hidden input is not visible or clickable, but it IS
    // walked: its value is submitted with the form, so the
    // redactor must see it. This check comes BEFORE isHidden:
    // in a real browser the UA stylesheet gives type=hidden
    // inputs display:none, so getComputedStyle reports them
    // hidden and the isHidden skip below would drop them before
    // the carve-out was ever reached (linkedom has no
    // getComputedStyle, so the tests never caught it).
    const type = (el.getAttribute("type") ?? "").toLowerCase();
    const isHiddenInput = tag === "input" && type === "hidden";
    if (!isHiddenInput && isHidden(el)) continue;
    // Ids must be unique and stable in one walk. A duplicate explicit
    // id keeps the FIRST occurrence (matching resolveElement's
    // first-wins); later duplicates get a synthetic id. And synthetic
    // ids use a distinct prefix (eclipse_el_) so they can never
    // collide with a page element's own id (a page with id="el_0"
    // used to collide with the synthetic namespace).
    let id = el.getAttribute("id")?.trim();
    if (id && nodes.has(id)) {
      id = undefined; // duplicate — fall through to a synthetic id
    }
    if (!id) {
      do {
        id = `eclipse_el_${n}`;
        n += 1;
      } while (nodes.has(id));
    } else {
      n += 1;
    }
    const nameText = accessibleName(el, doc);
    const labelText = [
      el.getAttribute("aria-label") ?? "",
      el.getAttribute("placeholder") ?? "",
      el.getAttribute("name") ?? "",
      id,
    ].join(" ");
    const sensitive =
      SENSITIVE_INPUT_TYPES.has(type) || SENSITIVE_NAME_HINTS.test(labelText);
    let value: string | undefined;
    if ((tag === "input" || tag === "textarea" || tag === "select") && !sensitive) {
      // Live value first (user-typed in a real browser), then the
      // initial value attribute (linkedom test runtimes included).
      value =
        (el as HTMLInputElement).value ||
        el.getAttribute("value") ||
        "";
    }
    const rawRect = rectOf(el);
    // Elements inside an iframe report rects in the iframe's
    // viewport — shift them into the top page's space (dx/dy
    // are 0 for top-level and shadow-DOM elements).
    const rect =
      dx === 0 && dy === 0
        ? rawRect
        : { x: rawRect.x + dx, y: rawRect.y + dy, width: rawRect.width, height: rawRect.height };
    const disabled = el.hasAttribute("disabled") || el.getAttribute("aria-disabled") === "true";
    const interactable =
      !disabled &&
      !isHiddenInput &&
      (INTERACTABLE_TAGS.has(tag) ||
        el.hasAttribute("onclick") ||
        el.getAttribute("role") === "button" ||
        (el.getAttribute("tabindex") ?? "") !== "");
    // The accessible name falls back to the own text, so for a leaf
    // element name and text are often identical — do not carry the
    // same string twice in the payload.
    const textVal = ownText(el).slice(0, 200);
    out.push({
      id,
      tag,
      role: roleOf(el),
      name: nameText,
      text: nameText && textVal === nameText ? "" : textVal,
      value,
      sensitive,
      // The server's consequential-action check needs the type
      // and the form membership (a type=submit button, or any
      // button inside a form, is a submit).
      type: type || undefined,
      inForm: inFormOf(el, doc),
      rect,
      visible: !isHiddenInput,
      interactable,
    });
    nodes.set(id, el);
  }
  return { info: out, nodes, truncated };
}

export function walkDom(doc: Document, opts: WalkOptions = {}): ElementInfo[] {
  return walkInternal(doc, opts).info;
}

/** Walk the page and return the element infos plus the id → DOM
 *  node map. The executor resolves a planner action's element_id
 *  through this map — never a guessed selector. */
export function walkDomWithNodes(doc: Document, opts: WalkOptions = {}): {
  info: ElementInfo[];
  nodes: Map<string, Element>;
  truncated: boolean;
} {
  return walkInternal(doc, opts);
}

/** The accessible name of an element (same logic the walker uses).
 *  Exported so the executor can fingerprint a resolved node: the
 *  tag + accessible name must still match the observation's element
 *  info, or the DOM moved between observe and execute and the
 *  action is refused (positional synthetic ids go stale otherwise). */
export function accessibleNameOf(el: Element, doc: Document): string {
  return accessibleName(el, doc);
}

/** Whether the element is hidden (same check the walker uses). */
export function isHiddenElement(el: Element): boolean {
  return isHidden(el);
}

/** Find the element the planner's action should resolve to, by id.
 *  The planner must name a real element id from the walk — never a
 *  guessed selector. Returns null when the id is not in the walk. */
export function resolveElement(elements: ElementInfo[], id: string): ElementInfo | null {
  return elements.find((e) => e.id === id) ?? null;
}
