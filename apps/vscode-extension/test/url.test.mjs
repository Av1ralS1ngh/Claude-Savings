import { test } from "node:test";
import assert from "node:assert/strict";
import {
  CLAUDE_SETUP_COMMAND,
  DEFAULT_GATEWAY_URL,
  normalizeGatewayUrl,
  parseHttpUrl,
  setupCommandForGateway,
  validateGatewayInput,
} from "../dist/url.mjs";

test("normalizeGatewayUrl defaults to the local gateway", () => {
  assert.equal(normalizeGatewayUrl(""), DEFAULT_GATEWAY_URL);
  assert.equal(normalizeGatewayUrl(undefined), DEFAULT_GATEWAY_URL);
});

test("normalizeGatewayUrl trims trailing slashes", () => {
  assert.equal(normalizeGatewayUrl("http://127.0.0.1:8080///"), DEFAULT_GATEWAY_URL);
});

test("parseHttpUrl accepts only http and https URLs", () => {
  assert.equal(parseHttpUrl("https://gateway.decompute.run")?.protocol, "https:");
  assert.equal(parseHttpUrl("http://example.test")?.protocol, "http:");
  assert.equal(parseHttpUrl("file:///tmp/nope"), undefined);
  assert.equal(parseHttpUrl("not a url"), undefined);
});

test("validateGatewayInput requires a valid URL", () => {
  assert.equal(validateGatewayInput("https://gateway.decompute.run"), undefined);
  assert.match(validateGatewayInput("") || "", /Enter/);
  assert.match(validateGatewayInput("example") || "", /valid/);
});

test("setupCommandForGateway is explicit for the local Claude-plan path", () => {
  assert.equal(setupCommandForGateway(DEFAULT_GATEWAY_URL), CLAUDE_SETUP_COMMAND);
});

test("setupCommandForGateway includes non-default gateway explicitly", () => {
  assert.equal(
    setupCommandForGateway("https://staging.decompute.run/"),
    "decompute configure claude-code --yes --auth-mode claude_subscription --gateway-url https://staging.decompute.run"
  );
});

test("setupCommandForGateway can build API-key setup commands", () => {
  assert.equal(
    setupCommandForGateway("https://staging.decompute.run/", "anthropic_api"),
    "decompute configure claude-code --yes --auth-mode api_key --gateway-url https://staging.decompute.run"
  );
});
