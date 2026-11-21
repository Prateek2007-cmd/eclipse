/** Shared types for the Eclipse vision pipeline (Sprint 2).
 *
 * All coordinates in `Box` are relative (0..1) to the screenshot or
 * viewport. Element rects are CSS pixels, viewport-relative.
 */

export interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface ElementInfo {
  /** Stable id: the page's own id when present, else el_<n> in DOM order. */
  id: string;
  tag: string;
  role: string;
  /** Accessible name: aria-label, labelledby, <label>, alt, or text. */
  name: string;
  /** Visible text, truncated to 200 chars. */
  text: string;
  /** Current value for form controls (local only — redacted downstream). */
  value?: string;
  /** True for password / OTP / CVV fields: value is never recorded. */
  sensitive: boolean;
  rect: { x: number; y: number; width: number; height: number };
  visible: boolean;
  interactable: boolean;
}

export interface Detection {
  box: Box;
  label: string;
  confidence: number;
  source: "yolo" | "ocr" | "blazeface" | "florence2" | "dom";
  /** OCR text when source is "ocr". */
  text?: string;
}

export type PiiType =
  | "name"
  | "email"
  | "phone"
  | "aadhaar"
  | "pan"
  | "card_number"
  | "cvv"
  | "otp"
  | "password"
  | "address"
  | "dob"
  | "face"
  | "roll";

export interface PiiHit {
  type: PiiType;
  /** The raw value — local only, never leaves the device. */
  value: string;
  placeholder: string;
  source: "dom" | "ocr" | "image";
  box?: Box;
  elementId?: string;
}

export interface OcrWord {
  text: string;
  box: Box;
  confidence: number;
}

export interface Observation {
  url: string;
  /** Viewport size in CSS pixels (for element-rect → relative boxes). */
  viewport: { width: number; height: number };
  /** DOM + accessibility tree elements. */
  elements: ElementInfo[];
  /** YOLO boxes (interactable regions). */
  detections: Detection[];
  /** OCR words with boxes. */
  ocr: OcrWord[];
  /** Face boxes (BlazeFace). */
  faces: Box[];
  /** True when Florence-2 was consulted for this observation. */
  florenceUsed: boolean;
  /** Which detectors ran and which failed (fail-closed input). */
  detectorStatus: Record<string, "ok" | "failed" | "skipped">;
  /** Why a detector failed, when known (audit trail, not a leak). */
  detectorErrors?: Record<string, string>;
  observedAt: number;
}

export interface RedactionResult {
  /** Text with every PII value replaced by its placeholder. */
  redactedText: string;
  hits: PiiHit[];
  /** Pixel regions to black out destructively (relative coords). */
  blackoutBoxes: Box[];
}

export interface GateVerdict {
  allow: boolean;
  reason: string;
  failedChecks: string[];
}

export interface ModelStatus {
  loaded: boolean;
  /** Milliseconds the last inference took, when measured. */
  lastMs?: number;
  error?: string;
}
