/** Second PII scan (Sprint 2).
 *
 * The redacted payload is re-scanned before it leaves the device.
 * If any PII pattern remains, the gate blocks and the payload is
 * re-redacted — a missed PII box is a privacy failure, not an
 * accuracy footnote. Placeholders ([EMAIL_1], [NAME_1] …) do not
 * match any PII pattern.
 *
 * What "clean" means, precisely: no *patterned* raw value (email,
 * phone, card, Aadhaar, PAN, DOB) remains, and none of the raw
 * values the redactor recorded (passed in as `rawValues`) is still
 * present as a substring. It does NOT mean "no name/address/roll
 * survives" on its own — those have no pattern and are caught by
 * the redactor's label rule, not by this scan. A clean scan is the
 * gate's last check, not a proof that redaction was complete.
 */
import { findPatternPii } from "./pii.js";

export interface SecondScanResult {
  clean: boolean;
  /** Raw PII still present in the payload (values are local-only). */
  remaining: { type: string; value: string }[];
}

/** Scan the final redacted payload string for leftover PII.
 *  `rawValues` are the raw values the redactor recorded (excluding
 *  synthetic ones — face@… and withheld:… are not real values);
 *  any of them still present in the payload means the replacement
 *  did not happen. Short values (< 4 chars, e.g. a 3-digit CVV)
 *  are skipped: they false-positive inside unrelated text. */
export function secondScan(redactedPayload: string, rawValues: string[] = []): SecondScanResult {
  const found = findPatternPii(redactedPayload);
  const remaining: { type: string; value: string }[] = found.map(({ type, value }) => ({
    type,
    value,
  }));
  for (const v of rawValues) {
    if (v && v.length >= 4 && redactedPayload.includes(v)) {
      remaining.push({ type: "unredacted-value", value: v });
    }
  }
  return {
    clean: remaining.length === 0,
    remaining,
  };
}
