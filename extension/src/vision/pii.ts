/** PII patterns and the placeholder registry (Sprint 2).
 *
 * Detection is deterministic: regexes for structured values, plus
 * field-label rules for values no regex can find (name, address, CVV,
 * OTP, password). All PII in tests is synthetic.
 */
import type { PiiHit, PiiType } from "./types.js";

/** One pattern per structured PII type. Order matters: card before
 *  generic 16-digit runs, Aadhaar before generic 12-digit runs. */
export const PII_PATTERNS: { type: PiiType; pattern: RegExp }[] = [
  {
    type: "email",
    pattern: /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g,
  },
  {
    type: "card_number",
    pattern: /\b(?:\d{4}[\s-]?){3}\d{4}\b/g,
  },
  {
    type: "card_number",
    // American Express: 15 digits in a 4-6-5 grouping
    // ("3782 822463 10005"). The 16-digit pattern above cannot
    // match it, so Amex numbers reached the planner unredacted.
    // (Same gap was fixed server-side in server/planner/scan.py.)
    pattern: /\b\d{4}[\s-]?\d{6}[\s-]?\d{5}\b/g,
  },
  {
    type: "aadhaar",
    pattern: /\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b/g,
  },
  {
    type: "pan",
    // Case-insensitive: OCR output lowercases (PP-OCRv4 rec), so a
    // PAN read from a scan arrives as "abcde1234f".
    pattern: /\b[A-Z]{5}\d{4}[A-Z]\b/gi,
  },
  {
    type: "upi",
    // UPI IDs have no dot-TLD, so the email pattern misses them
    // ("aarav@okhdfcbank", "9999988888@paytm"). The handle is a
    // curated list of common UPI handles (banks, wallets, apps);
    // `ok[a-z]+` covers the ok-bank family (okhdfcbank, okaxis,
    // okicici, oksbi, okbizaxis …). A bare "word@word" is NOT
    // matched — it false-positives on prose ("meet@noon").
    pattern:
      /\b[A-Za-z0-9._%+-]{2,}@(?:ok[a-z]+|paytm|ybl|axl|ibl|upi|barodampay|mahb|kotak|hdfcbank|icici|axisbank|sbi|freecharge|amazonpay|gpay|phonepe|slice|yesbank|federal|idfcfirst|jupiteraxis|rbl|indus|aubank|equitas|jiopay|airtel|payzapp|mobikwik|bhim|cred|navi)\b/gi,
  },
  {
    type: "passport",
    // Indian passport: one letter + seven digits ("A1234567").
    // Case-insensitive: OCR output lowercases. US passports are
    // nine bare digits — too generic to pattern-match safely.
    pattern: /\b[A-Z]\d{7}\b/gi,
  },
  {
    type: "phone",
    // Indian mobile, with or without +91 and separators. The
    // (?<!\d) guard stops the pattern matching a 10-digit
    // substring of a longer digit run ("19876543210" used to
    // redact as "1[PHONE_1]" — a mislabelled partial match).
    pattern: /(?<!\d)(?:\+91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}\b/g,
  },
  {
    type: "phone",
    // North-American numbers, with or without the +1 country
    // code and with or without parentheses: "+1 (415) 555-0132",
    // "415-555-0132", "4155550132". The overlap dedup in
    // findPatternPii keeps the longer card match when this
    // pattern fires inside a 16-digit card number.
    pattern: /(?<!\d)(?:\+?1[\s-]?)?\(?\d{3}\)?[\s-]?\d{3}[\s-]?\d{4}\b/g,
  },
  {
    type: "dob",
    // Month names case-insensitive: OCR output lowercases.
    // Day-first ("01 Jan 2005"), month-first ("Jan 15, 1990"),
    // slashes ("15/01/1990") and dashes ("15-01-1990"). ISO
    // yyyy-mm-dd ("2026-12-01") is DELIBERATELY not matched: the
    // safe paragraph in pii-text.html carries one, and a bare
    // ISO date is usually a workshop / order date, not a DOB.
    pattern:
      /\b\d{2}\s(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s\d{4}\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s\d{1,2},?\s\d{4}\b|\b\d{2}\/\d{2}\/\d{4}\b|\b\d{2}-\d{2}-\d{4}\b/gi,
  },
];

/** Field labels that mark a value as PII even when no regex matches.
 *  Matched against the element's name, id, label and placeholder.
 *  `not` excludes lookalikes: "File Name" / "Domain Name" are not a
 *  person's name, so they must not classify as one. */
export const SENSITIVE_FIELD_LABELS: { type: PiiType; match: RegExp; not?: RegExp }[] = [
  { type: "password", match: /pass(word|wd)|pwd/i },
  { type: "cvv", match: /cvv|cvc|security.?code/i },
  { type: "otp", match: /\botp\b|one.?time.?pass|verification.?code/i },
  { type: "card_number", match: /card.?num|card.?no|cc.?num/i },
  { type: "pan", match: /\bpan\b|permanent.?account/i },
  { type: "aadhaar", match: /aadhaar|aadhar|uidai/i },
  { type: "upi", match: /\bupi\b/i },
  { type: "passport", match: /passport/i },
  { type: "name", match: /name/i, not: /file|domain|host|site/i },
  { type: "address", match: /address|street/i },
  { type: "dob", match: /date.?of.?birth|\bdob\b|birth.?date/i },
  { type: "roll", match: /roll.?no|roll.?number|enroll|\broll\b/i },
  { type: "phone", match: /phone|mobile|contact.?no/i },
  { type: "email", match: /e.?mail/i },
];

/** Aadhaar-like numbers that are clearly test data are still PII for
 *  the redactor: it must not decide what is "real" — it redacts all. */
export function findPatternPii(text: string): { type: PiiType; value: string; index: number }[] {
  const found: { type: PiiType; value: string; index: number }[] = [];
  for (const { type, pattern } of PII_PATTERNS) {
    pattern.lastIndex = 0;
    let m: RegExpExecArray | null;
    while ((m = pattern.exec(text)) !== null) {
      found.push({ type, value: m[0], index: m.index });
    }
  }
  // Drop overlaps: keep the earliest, longest match at each position.
  found.sort((a, b) => a.index - b.index || b.value.length - a.value.length);
  const kept: typeof found = [];
  let lastEnd = -1;
  for (const hit of found) {
    if (hit.index >= lastEnd) {
      kept.push(hit);
      lastEnd = hit.index + hit.value.length;
    }
  }
  return kept;
}

/** Placeholder registry: the same value always maps to the same
 *  placeholder within one task, so the planner can reason about
 *  "[EMAIL_1] appears twice" without ever seeing the value.
 *
 *  Fixed placeholders (per the plan §6.4 scheme and the ground
 *  truth): card numbers → [CARD_LAST4], CVV → [CVV], OTP → [OTP].
 *  These are single-use per task; two different cards in one task
 *  would share [CARD_LAST4] — the vault knows which card by context.
 *  Everything else is numbered per type: [NAME_1], [EMAIL_1] … */
export class PlaceholderRegistry {
  private counters = new Map<PiiType, number>();
  private mapping = new Map<string, string>();
  private fixedIssued = new Set<PiiType>();

  private static readonly FIXED: Partial<Record<PiiType, string>> = {
    card_number: "[CARD_LAST4]",
    cvv: "[CVV]",
    otp: "[OTP]",
  };

  placeholderFor(type: PiiType, value: string): string {
    const fixed = PlaceholderRegistry.FIXED[type];
    if (fixed) {
      this.fixedIssued.add(type);
      return fixed;
    }
    const key = `${type}:${value}`;
    const existing = this.mapping.get(key);
    if (existing) return existing;
    const n = (this.counters.get(type) ?? 0) + 1;
    this.counters.set(type, n);
    const placeholder = `[${type.toUpperCase()}_${n}]`;
    this.mapping.set(key, placeholder);
    return placeholder;
  }

  /** All placeholders issued so far (for the redaction contract),
   *  including the fixed ones ([CARD_LAST4] / [CVV] / [OTP]) — the
   *  server needs them in the contract too, not just the numbered
   *  ones. */
  entries(): { type: PiiType; placeholder: string }[] {
    const out: { type: PiiType; placeholder: string }[] = [];
    for (const type of this.fixedIssued) {
      const fixed = PlaceholderRegistry.FIXED[type];
      if (fixed) out.push({ type, placeholder: fixed });
    }
    for (const [key, placeholder] of this.mapping) {
      const type = key.slice(0, key.indexOf(":")) as PiiType;
      out.push({ type, placeholder });
    }
    return out;
  }
}

/** Classify a form field by its label/name/id/placeholder text. */
export function classifyField(labelText: string): PiiType | null {
  for (const { type, match, not } of SENSITIVE_FIELD_LABELS) {
    if (not?.test(labelText)) continue;
    if (match.test(labelText)) return type;
  }
  return null;
}

/** Build a PiiHit for a value found in text or a field. */
export function makeHit(
  type: PiiType,
  value: string,
  registry: PlaceholderRegistry,
  extra: Partial<PiiHit> = {},
): PiiHit {
  return {
    type,
    value,
    placeholder: registry.placeholderFor(type, value),
    source: "dom",
    ...extra,
  };
}
