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

test("normalizeGatewayUrl defaults to the hosted HTTPS gateway", () => {
  assert.equal(normalizeGatewayUrl(""), DEFAULT_GATEWAY_URL);
  assert.equal(normalizeGatewayUrl(undefined), DEFAULT_GATEWAY_URL);
});

test("normalizeGatewayUrl trims trailing slashes", () => {
  assert.equal(normalizeGatewayUrl("https://gateway.decompute.run///"), DEFAULT_GATEWAY_URL);
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

test("setupCommandForGateway keeps hosted setup command clean", () => {
  assert.equal(setupCommandForGateway(DEFAULT_GATEWAY_URL), CLAUDE_SETUP_COMMAND);
});

test("setupCommandForGateway includes non-default gateway explicitly", () => {
  assert.equal(
    setupCommandForGateway("https://staging.decompute.run/"),
    `${CLAUDE_SETUP_COMMAND} --gateway-url https://staging.decompute.run`
  );
});
