/** Fail-closed gate (Sprint 2).
 *
 * The gate is the last check before any payload leaves the device.
 * It is fail-CLOSED: anything unverified blocks the send. The rules:
 *  - DOM walker failed → block (no observation, no plan).
 *  - Second PII scan found raw PII → block.
 *  - Egress target not on the allow-list → block.
 *  - Image/mixed task with OCR failed → block (text is unreadable,
 *    so redaction cannot be verified).
 *  - Image/mixed task with face detection failed → block (a face
 *    could be present and unblacked).
 *  - YOLO failed → warning only: DOM still gives exact boxes for
 *    web elements, and scanned-doc tasks do not need YOLO.
 *  - Florence-2 failed → warning only: it is a fallback grounder,
 *    never required.
 */
import type { GateVerdict } from "./types.js";
import type { SecondScanResult } from "./second-scan.js";

export type TaskKind = "dom" | "image" | "mixed";

export interface GateInput {
  taskKind: TaskKind;
  domWalkerOk: boolean;
  yoloOk: boolean;
  ocrOk: boolean;
  blazefaceOk: boolean;
  florenceOk: boolean;
  secondScan: SecondScanResult;
  egressAllowed: boolean;
  egressReason?: string;
}

export function evaluateGate(input: GateInput): GateVerdict {
  const failed: string[] = [];
  const warnings: string[] = [];

  if (!input.domWalkerOk) failed.push("dom-walker failed — no observation");
  if (!input.secondScan.clean) {
    failed.push(
      `second PII scan found ${input.secondScan.remaining.length} raw value(s) in the payload`,
    );
  }
  if (!input.egressAllowed) {
    failed.push(`egress denied: ${input.egressReason ?? "target not on allow-list"}`);
  }
  const needsVision = input.taskKind !== "dom";
  // NOTE: the comparison is "not dom", not "image or mixed" — an
  // unknown taskKind (a typo, a future kind, a bad cast) defaults
  // to the STRICTEST kind (vision required), not to dom-only.
  // The old `=== "image" || === "mixed"` treated anything else
  // as dom-only, so an unknown kind with failed OCR sailed
  // through the gate.
  if (needsVision && !input.ocrOk) {
    failed.push("OCR failed on an image task — redaction cannot be verified");
  }
  if (needsVision && !input.blazefaceOk) {
    failed.push("face detection failed on an image task — a face could be unblacked");
  }
  if (!input.yoloOk) warnings.push("YOLO failed — DOM boxes only");
  if (!input.florenceOk) warnings.push("Florence-2 unavailable — DOM + YOLO + OCR only");

  if (failed.length) {
    return {
      allow: false,
      reason: `Blocked: ${failed.join("; ")}`,
      failedChecks: failed,
    };
  }
  return {
    allow: true,
    reason: warnings.length
      ? `Allowed with warnings: ${warnings.join("; ")}`
      : "Allowed — all required checks passed",
    failedChecks: [],
  };
}
