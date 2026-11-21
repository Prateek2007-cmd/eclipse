/** Egress allow-list (Sprint 2).
 *
 * The extension may only send redacted payloads to listed hosts.
 * Everything else is denied by default — including hosts the plan
 * mentions but whose exact address is not yet confirmed (OrcaRouter:
 * NOT in the list until its host is verified; the planner falls back
 * to Groq or local Ollama instead).
 */
export interface EgressRule {
  origin: string;
  label: string;
}

export const EGRESS_ALLOW_LIST: EgressRule[] = [
  { origin: "http://127.0.0.1:8000", label: "local Eclipse server" },
  { origin: "http://localhost:8000", label: "local Eclipse server" },
  { origin: "https://akash-antseed.duckdns.org", label: "own Antseed gateway (live planner)" },
  { origin: "https://api.groq.com", label: "Groq fallback (gpt-oss-120b)" },
  { origin: "http://127.0.0.1:11434", label: "Ollama local (qwen2.5:7b)" },
  { origin: "http://localhost:11434", label: "Ollama local (qwen2.5:7b)" },
];

export interface EgressCheck {
  allowed: boolean;
  reason: string;
}

/** Is this URL an allowed egress target? */
export function checkEgress(url: string): EgressCheck {
  let origin: string;
  try {
    origin = new URL(url).origin;
  } catch {
    return { allowed: false, reason: `not a valid URL: ${url}` };
  }
  const rule = EGRESS_ALLOW_LIST.find((r) => r.origin === origin);
  if (rule) return { allowed: true, reason: `allowed: ${rule.label}` };
  return { allowed: false, reason: `origin ${origin} is not on the egress allow-list` };
}
