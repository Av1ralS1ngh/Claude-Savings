// Local-first: the setup wizard installs a gateway on the user's machine that listens here, and
// compression runs locally (BYOK). A hosted/self-managed gateway is an opt-in override.
export const DEFAULT_GATEWAY_URL = "http://127.0.0.1:8080";
export type ClaudeAuthMode = "anthropic_api" | "claude_subscription" | "enterprise_gateway";
export const CLAUDE_SETUP_COMMAND =
  `decompute configure claude-code --yes --auth-mode claude_subscription --gateway-url ${DEFAULT_GATEWAY_URL}`;

export function normalizeGatewayUrl(value: string | null | undefined, fallback = DEFAULT_GATEWAY_URL): string {
  const raw = String(value ?? "").trim();
  const selected = raw || fallback;
  return selected.replace(/\/+$/, "");
}

export function parseHttpUrl(value: string): URL | undefined {
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" && url.protocol !== "http:") return undefined;
    if (!url.hostname) return undefined;
    return url;
  } catch {
    return undefined;
  }
}

export function validateGatewayInput(value: string): string | undefined {
  const normalized = normalizeGatewayUrl(value, "");
  if (!normalized) return "Enter a gateway URL.";
  if (!parseHttpUrl(normalized)) return "Use a valid http or https URL.";
  return undefined;
}

function shellQuote(value: string): string {
  if (/^[A-Za-z0-9_./:=+-]+$/.test(value)) return value;
  return "'" + value.replace(/'/g, "'\\''") + "'";
}

export function setupCommandForGateway(value: string | null | undefined, authMode: ClaudeAuthMode = "claude_subscription"): string {
  const gateway = normalizeGatewayUrl(value);
  const mode = authMode === "claude_subscription" ? "claude_subscription" : "api_key";
  return `decompute configure claude-code --yes --auth-mode ${mode} --gateway-url ${shellQuote(gateway)}`;
}
