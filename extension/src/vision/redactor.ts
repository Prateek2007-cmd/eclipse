/** Redactor — deterministic PII redaction (Sprint 2).
 *
 * Runs on-device, before anything leaves the machine. Two outputs:
 *  1. A redacted payload: every PII value replaced by a stable
 *     placeholder ([EMAIL_1], [CARD_LAST4], [NAME_1] …). The server
 *     knows the scheme via the redaction contract; it never sees the
 *     raw value.
 *  2. Blackout boxes: pixel regions to destroy destructively (never
 *     blur) in the screenshot — OCR boxes that held PII, face boxes,
 *     and sensitive input rects.
 *
 * Sensitive fields (password / OTP / CVV) are replaced by placeholder
 * regardless of content — the redactor never decides a password is
 * "safe to send".
 */
import type { Box, Observation, PiiHit, PiiType, RedactionResult } from "./types.js";
import { classifyField, findPatternPii, makeHit, PlaceholderRegistry } from "./pii.js";

const BLACKOUT_MARGIN = 0.02;

const LABEL_RE = /^([A-Za-z][A-Za-z .'-]{1,30}):\s*(.+)$/;
const PLACEHOLDER_TOKEN_RE = /\[[A-Z][A-Z0-9_]*\]/;
const NESTED_LABEL_RE = /[A-Za-z][A-Za-z .'-]{1,20}:\s/;

/** Types the label-prefix rule may redact wholesale: values no
 *  regex can find (a name, an address, a roll number) plus UPI —
 *  a "UPI ID:" label means the value is a UPI id by construction,
 *  even when its handle is not in the pattern's curated list.
 *  Structured types (email, phone, card, Aadhaar, PAN, DOB,
 *  passport) are left to the pattern pass — redacting "Email:
 *  contact us" as [EMAIL_1] is a false positive the pattern pass
 *  does not make, and "Passport: required for travel" is prose. */
const LABEL_WHOLESALE_TYPES: ReadonlySet<PiiType> = new Set(["name", "address", "roll", "upi"]);

/** Pattern-redact only (no label rule). Shared by both paths. */
function patternRedact(
  text: string,
  registry: PlaceholderRegistry,
  source: PiiHit["source"],
): { text: string; hits: PiiHit[] } {
  const found = findPatternPii(text);
  let out = text;
  const hits: PiiHit[] = [];
  for (let i = found.length - 1; i >= 0; i--) {
    const { type, value, index } = found[i];
    const hit = makeHit(type, value, registry, { source });
    hits.unshift(hit);
    out = out.slice(0, index) + hit.placeholder + out.slice(index + value.length);
  }
  return { text: out, hits };
}

/** Redact the value after a "Label:" prefix whose type is name /
 *  address / roll. Pattern-redact inside first (so an email inside
 *  the value gets its own placeholder), then redact the remaining
 *  free text up to the first placeholder or nested label as the
 *  label type, and recurse on the tail (nested labels, placeholders).
 *  This is what stops "Name: Aarav Test, email aarav.test@example.com"
 *  leaking the name: the old code returned after the pattern pass
 *  and never ran the label rule. */
function redactLabelValue(
  value: string,
  type: PiiType,
  registry: PlaceholderRegistry,
  source: PiiHit["source"],
): { text: string; hits: PiiHit[] } {
  const { text: pat, hits: patHits } = patternRedact(value, registry, source);
  const boundary = pat.search(
    new RegExp(`${PLACEHOLDER_TOKEN_RE.source}|${NESTED_LABEL_RE.source}`),
  );
  const head = boundary === -1 ? pat : pat.slice(0, boundary);
  const tail = boundary === -1 ? "" : pat.slice(boundary);
  const hits = [...patHits];
  let out = "";
  if (head.trim()) {
    const hit = makeHit(type, head.trim(), registry, { source });
    hits.push(hit);
    out += head.replace(head.trim(), hit.placeholder);
  } else {
    out += head;
  }
  if (tail) {
    const r = redactText(tail, registry, source);
    out += r.text;
    hits.push(...r.hits);
  }
  return { text: out, hits };
}

/** Redact PII patterns inside a free-text string.
 *
 * Order: (1) a "Label: value" shape whose label classifies as a
 * name / address / roll — the value is PII by construction, and
 * patterns alone cannot find a name; (2) otherwise, structured
 * patterns (email, phone, card, Aadhaar, PAN, DOB).
 * Over-redaction trade-off: "Name: Aarav Test" redacts the whole
 * value, including any trailing words. The safe paragraphs in the
 * fixtures have no "Label: value" shape, so they are unaffected. */
export function redactText(
  text: string,
  registry: PlaceholderRegistry,
  source: PiiHit["source"] = "dom",
): { text: string; hits: PiiHit[] } {
  const labelMatch = LABEL_RE.exec(text.trim());
  if (labelMatch) {
    const type = classifyField(labelMatch[1]);
    if (type && LABEL_WHOLESALE_TYPES.has(type)) {
      const value = labelMatch[2].trim();
      if (value) {
        const r = redactLabelValue(value, type, registry, source);
        return { text: `${labelMatch[1]}: ${r.text}`, hits: r.hits };
      }
    }
  }
  return patternRedact(text, registry, source);
}

/** Redact PII in a URL before it enters the payload.
 *
 * The URL used to travel raw: query params routinely carry PII
 * ("?email=…&name=…"), and a name in a query param sails past
 * the second scan (names have no pattern) straight to the
 * planner. Rules:
 *  - A query/fragment param whose KEY classifies as a PII field
 *    (email, name, phone, upi, …) has its whole value replaced
 *    by that type's placeholder — the key is the label.
 *  - Other param values, the path and a free-form fragment are
 *    pattern-redacted (emails, phones, cards … inside them).
 *  - The origin and the param keys are kept: the planner needs
 *    to know which page and which field it is looking at.
 *  - Placeholders are left unencoded in the rebuilt URL
 *    ("[EMAIL_1]", not "%5BEMAIL_1%5D") so the planner and the
 *    fill map still recognise them; `[` and `]` are legal in a
 *    query component. Unchanged values keep their original
 *    encoding. Residual gap: an UNLABELLED param value holding
 *    a name ("?q=Aarav Test") has no pattern and no label — it
 *    still leaks (see the audit notes).
 */
export function redactUrl(
  url: string,
  registry: PlaceholderRegistry,
): { text: string; hits: PiiHit[] } {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return redactText(url, registry, "dom");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return redactText(url, registry, "dom");
  }
  const hits: PiiHit[] = [];

  const decode = (raw: string): string => {
    try {
      return decodeURIComponent(raw.replace(/\+/g, " "));
    } catch {
      return raw;
    }
  };

  /** Redact one param value: wholesale when the key classifies
   *  as a PII field, pattern-redact otherwise. */
  const redactParamValue = (key: string, rawValue: string): string => {
    const decoded = decode(rawValue);
    const type = classifyField(key);
    if (type) {
      const hit = makeHit(type, decoded, registry, { source: "dom" });
      hits.push(hit);
      return hit.placeholder;
    }
    const r = redactText(decoded, registry, "dom");
    hits.push(...r.hits);
    return r.text === decoded ? rawValue : r.text;
  };

  /** Redact a "k=v&k=v" param string, keeping keys and order. */
  const redactParams = (params: string): string =>
    params
      .split("&")
      .map((pair) => {
        const eq = pair.indexOf("=");
        if (eq === -1) return pair;
        return `${pair.slice(0, eq)}=${redactParamValue(pair.slice(0, eq), pair.slice(eq + 1))}`;
      })
      .join("&");

  let search = "";
  if (parsed.search.length > 1) {
    search = `?${redactParams(parsed.search.slice(1))}`;
  }
  let hash = parsed.hash;
  if (parsed.hash.length > 1) {
    const frag = parsed.hash.slice(1);
    if (frag.includes("=")) {
      hash = `#${redactParams(frag)}`;
    } else {
      const decoded = decode(frag);
      const r = redactText(decoded, registry, "dom");
      hits.push(...r.hits);
      hash = `#${r.text}`;
    }
  }
  let path = parsed.pathname;
  {
    const decoded = decode(path);
    const r = redactText(decoded, registry, "dom");
    hits.push(...r.hits);
    if (r.text !== decoded) path = r.text;
  }
  return { text: `${parsed.origin}${path}${search}${hash}`, hits };
}

/** Expand a box by a margin (relative coords), clamped to [0,1]. */
export function expandBox(box: Box, margin = BLACKOUT_MARGIN): Box {
  const x = Math.max(0, box.x - margin);
  const y = Math.max(0, box.y - margin);
  const x2 = Math.min(1, box.x + box.w + margin);
  const y2 = Math.min(1, box.y + box.h + margin);
  return { x, y, w: x2 - x, h: y2 - y };
}

function overlaps(a: Box, b: Box): boolean {
  return (
    a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h
  );
}

function union(a: Box, b: Box): Box {
  const x = Math.min(a.x, b.x);
  const y = Math.min(a.y, b.y);
  const x2 = Math.max(a.x + a.w, b.x + b.w);
  const y2 = Math.max(a.y + a.h, b.y + b.h);
  return { x, y, w: x2 - x, h: y2 - y };
}

/** Merge overlapping blackout boxes so the screenshot is painted once. */
export function mergeBoxes(boxes: Box[]): Box[] {
  const merged: Box[] = [];
  for (const box of boxes) {
    let cur = box;
    const rest: Box[] = [];
    for (const m of merged) {
      if (overlaps(cur, m)) cur = union(cur, m);
      else rest.push(m);
    }
    rest.push(cur);
    merged.length = 0;
    merged.push(...rest);
  }
  return merged;
}

/** Redact a whole observation: elements, OCR words, faces. */
export function redactObservation(
  obs: Observation,
  registry: PlaceholderRegistry = new PlaceholderRegistry(),
): RedactionResult & { redactedPayload: string } {
  const hits: PiiHit[] = [];
  const blackout: Box[] = [];
  const { width: vw, height: vh } = obs.viewport;

  /** Black out an element's pixels: any element whose value or
   *  text held PII is still showing that PII on screen (the proof
   *  screenshot must not carry it). Skipped when the rect is empty
   *  (linkedom test runtimes have no layout). */
  const blackoutEl = (el: Observation["elements"][number]): void => {
    if (el.rect.width > 0 && el.rect.height > 0 && vw > 0 && vh > 0) {
      blackout.push(
        expandBox({
          x: el.rect.x / vw,
          y: el.rect.y / vh,
          w: el.rect.width / vw,
          h: el.rect.height / vh,
        }),
      );
    }
  };

  const redactedElements = obs.elements.map((el) => {
    const labelText = [el.name, el.id].join(" ");
    const fieldType: PiiType | null = classifyField(labelText);
    let { name, text, value } = el;
    if (el.sensitive) {
      // 1. Sensitive fields (password / OTP / CVV). The walker
      //    withholds the value, so the redactor never sees it — but
      //    the planner still needs a placeholder token to reference
      //    the field by, so one is issued here (the vault fills the
      //    real value at execution). A withheld value is recorded
      //    under a synthetic per-element key so distinct fields get
      //    distinct placeholders. The input's pixels are blacked
      //    out too: a type=text OTP is displayed on screen (the
      //    browser only masks type=password).
      const raw = value || `withheld:${el.id}`;
      const hit = makeHit(fieldType ?? "password", raw, registry, {
        source: "dom",
        elementId: el.id,
      });
      hits.push(hit);
      value = hit.placeholder;
      blackoutEl(el);
    } else if (value) {
      if (fieldType) {
        // 2. A labelled PII field (name, address, email, phone, roll,
        //    DOB, card number) holds PII by definition — replace the
        //    whole value. Patterns alone cannot find a name.
        const hit = makeHit(fieldType, value, registry, {
          source: "dom",
          elementId: el.id,
        });
        hits.push(hit);
        value = hit.placeholder;
        blackoutEl(el);
      } else {
        // 3. Unlabelled values: pattern-redact only.
        const r = redactText(value, registry, "dom");
        for (const h of r.hits) {
          h.elementId = el.id;
          hits.push(h);
        }
        if (r.hits.length) blackoutEl(el);
        value = r.text;
      }
    }
    // 4. Pattern PII inside the accessible name and visible text
    //    (e.g. a paragraph that prints an email address).
    for (const key of ["name", "text"] as const) {
      const current = key === "name" ? name : text;
      if (!current) continue;
      const r = redactText(current, registry, "dom");
      for (const h of r.hits) {
        h.elementId = el.id;
        hits.push(h);
      }
      if (r.hits.length) blackoutEl(el);
      if (key === "name") name = r.text;
      else text = r.text;
    }
    return { ...el, name, text, value };
  });

  // 3. OCR words: a word holding PII is redacted AND its box is blacked out.
  const redactedOcr = obs.ocr.map((word) => {
    const r = redactText(word.text, registry, "ocr");
    if (r.hits.length) {
      for (const h of r.hits) {
        h.box = word.box;
        hits.push(h);
      }
      blackout.push(expandBox(word.box));
    }
    return { ...word, text: r.text };
  });

  // 4. Faces: every face box is blacked out, no exceptions.
  for (const face of obs.faces) {
    hits.push(
      makeHit("face", `face@${Math.round(face.x * 1000)},${Math.round(face.y * 1000)}`, registry, {
        source: "image",
        box: face,
      }),
    );
    blackout.push(expandBox(face));
  }

  // The URL is redacted like any other field: query params
  // routinely carry PII, and a name in one sails past the
  // second scan (names have no pattern).
  const redactedUrl = redactUrl(obs.url, registry);
  hits.push(...redactedUrl.hits);

  const payload = {
    url: redactedUrl.text,
    elements: redactedElements,
    ocr: redactedOcr,
    faces: obs.faces,
    florenceUsed: obs.florenceUsed,
  };
  // Dedupe: a leaf element's accessible name and its text are often
  // the same string, so the same (element, type, value) is hit twice.
  // One hit per (element, type, value); the same value in a different
  // element stays a separate hit with the same placeholder.
  const seen = new Set<string>();
  const dedupedHits = hits.filter((h) => {
    const key = `${h.elementId ?? ""}|${h.type}|${h.value}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  return {
    redactedText: JSON.stringify(payload),
    redactedPayload: JSON.stringify(payload),
    hits: dedupedHits,
    blackoutBoxes: mergeBoxes(blackout),
  };
}
