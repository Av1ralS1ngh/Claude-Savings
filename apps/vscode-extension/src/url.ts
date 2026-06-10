export const DEFAULT_GATEWAY_URL = "https://gateway.decompute.run";
export const CLAUDE_SETUP_COMMAND = "decompute configure claude-code";

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

export function setupCommandForGateway(value: string | null | undefined): string {
  const gateway = normalizeGatewayUrl(value);
  if (gateway === DEFAULT_GATEWAY_URL) return CLAUDE_SETUP_COMMAND;
  return `${CLAUDE_SETUP_COMMAND} --gateway-url ${shellQuote(gateway)}`;
}
