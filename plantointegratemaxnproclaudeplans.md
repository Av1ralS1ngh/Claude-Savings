# Plan to Integrate Claude Pro and Max Plans

Last updated: 2026-06-15

## Goal

Let Claude Pro, Max, Team, and Enterprise subscription users use Decompute with Claude Code without requiring an Anthropic API key, while keeping the existing BYOK/API-key path working.

The safe product shape is:

- BYOK API users: Decompute stores or receives an Anthropic `sk-ant-*` key and forwards it upstream as `x-api-key`.
- Claude subscription users: Decompute runs as a local gateway and forwards Claude Code's own OAuth bearer credential upstream as `Authorization: Bearer ...`.
- Hosted Decompute gateway: must not collect or route consumer Pro/Max OAuth tokens on behalf of users. Hosted mode remains Decompute platform auth plus BYOK/enterprise-supported upstream credentials.

## Official Docs Anchors

- Claude Code supports Pro and Max subscription login without an API key: https://code.claude.com/docs/en/iam
- Claude Code credential precedence is cloud provider, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_API_KEY`, `apiKeyHelper`, `CLAUDE_CODE_OAUTH_TOKEN`, then subscription OAuth from `/login`: https://code.claude.com/docs/en/iam
- `ANTHROPIC_AUTH_TOKEN` is sent as `Authorization: Bearer`; `ANTHROPIC_API_KEY` is sent as `X-Api-Key`: https://code.claude.com/docs/en/iam
- Claude Code gateways must implement `/v1/messages` and `/v1/messages/count_tokens`, and should forward `anthropic-beta` and `anthropic-version`: https://code.claude.com/docs/en/llm-gateway
- Gateway model discovery authenticates the same way as inference, including bearer auth: https://code.claude.com/docs/en/llm-gateway
- VS Code Claude Code works with any paid Claude subscription or Console account; no API key is required for subscription users: https://code.claude.com/docs/en/ide-integrations
- Pro and Max users see plan usage bars and usage breakdowns; dollar cost is not the authoritative billing metric for subscriptions: https://code.claude.com/docs/en/costs
- Legal/compliance constraint: do not build a hosted third-party service that collects or routes Free/Pro/Max credentials on behalf of users. Keep subscription OAuth passthrough local/user-controlled unless explicitly approved for an enterprise/self-managed case: https://code.claude.com/docs/en/legal-and-compliance

## Current State

- The VS Code extension setup wizard currently assumes BYOK and asks users to run `decompute init`, which prompts for an Anthropic API key.
- `~/.claude/settings.json` routing is already close to subscription-safe because the extension writes only:
  - `ANTHROPIC_BASE_URL`
  - `ANTHROPIC_CUSTOM_HEADERS`
- The extension does not write `ANTHROPIC_API_KEY`, which is good.
- The gateway currently extracts client `Authorization: Bearer` as a possible upstream credential, but provider code treats every upstream credential as an API key and sends it as `x-api-key`. That is the core blocker.
- A saved local `~/.decompute/claude.key` currently wins over client OAuth. That preserves existing BYOK users but will surprise a user who chooses subscription mode unless we make the selected auth mode explicit.

## Architecture Decision

Introduce an explicit upstream credential model:

```text
UpstreamAnthropicAuth
  kind: api_key | bearer
  value: secret
```

Then each layer can make a clear decision:

- Security/request extraction decides which credential is allowed.
- Provider headers decide how to serialize the credential:
  - `api_key` -> `x-api-key`
  - `bearer` -> `Authorization: Bearer ...`
- CLI and extension decide which setup path the user is in.
- Dashboard and support bundles must never expose either credential.

## Safety Rules

1. Local subscription passthrough is allowed by default only in local/dev mode.
2. Production/hosted mode must block client bearer passthrough by default.
3. Hosted/self-managed enterprise bearer passthrough requires an explicit env flag.
4. API-key users keep working exactly as before.
5. If an API key is present, tell users it takes precedence over subscription OAuth.
6. Never persist Claude OAuth tokens in Decompute files.
7. Never include bearer/API credentials in logs, dashboards, usage records, or support bundles.
8. Subscription users should see token/context savings language, not guaranteed bill-dollar savings.
9. The setup wizard should ask in human language: use an existing Claude plan, or use an API key.
10. If subscription mode is selected and `~/.decompute/claude.key` exists, warn once and ignore it. Do not delete it automatically.
11. Decompute Free/Pro quota is the same for subscription users and BYOK users.

## Implementation Phases

### Phase 1 - Gateway Credential Plumbing

Status: in progress

Atomic changes:

- Add `UpstreamAnthropicAuth(kind, value)`.
- Add `upstream_anthropic_auth(request, settings)`.
- Keep `upstream_anthropic_key(...)` as a legacy API-key-only helper for compatibility.
- Update `upstream_headers(...)` to serialize API keys and bearer tokens correctly.
- Add a production guard:
  - local/dev default: client bearer passthrough allowed
  - prod/staging/private_beta default: client bearer passthrough blocked
  - override: `DECOMPUTE_ALLOW_CLIENT_BEARER_PROVIDER_AUTH=true`
- Update `/v1/models`, `/v1/messages/count_tokens`, streaming, non-streaming, OpenAI-compatible proxy, and CCR retrieval paths to use typed upstream auth.
- Tests:
  - client `x-api-key` still forwards as `x-api-key`
  - client bearer forwards as `Authorization: Bearer` in local mode
  - client bearer is blocked in production by default
  - configured/static API key and saved BYOK key win over bearer
  - existing beta header forwarding still works

Exit criteria:

- Existing BYOK tests pass.
- New upstream auth tests pass.
- Streaming and non-streaming provider calls share the same auth behavior.

### Phase 2 - CLI Auth Modes

Status: in progress

Add explicit auth modes to `decompute init`:

```text
decompute init --auth-mode api_key
decompute init --auth-mode claude_subscription
decompute init --auth-mode enterprise_gateway
```

Interactive menu:

```text
Upstream Claude auth:
  [1] Claude Pro/Max/Team login - no API key, local gateway only
  [2] Anthropic API key - BYOK
  [3] Enterprise/custom Claude gateway
```

Subscription mode behavior:

- Do not prompt for `sk-ant-*`.
- Do not save an OAuth token.
- Save config:

```toml
[claude]
mode = "claude_subscription"
```

- Start the local gateway.
- Configure Claude Code routing only.
- Warn if `ANTHROPIC_API_KEY` exists in the current environment because Claude Code will use it before subscription OAuth.
- If `~/.decompute/claude.key` exists, either:
  - ignore it when `claude.mode = "claude_subscription"`, or
  - ask the user whether to remove it.

Recommended default:

- Existing installed BYOK users keep `api_key`.
- Fresh VS Code users choose between:
  - "Use my existing Claude plan" for Claude Pro/Max/Team/Enterprise login
  - "Use an API key" for Anthropic Console keys

### Phase 3 - VS Code Extension Setup Wizard

Status: in progress

Add a setup mode picker before Step 2:

```text
How do you want Claude Code to connect?

Use my existing Claude plan
Use an API key
```

Subscription mode flow:

- Ensure Claude Code extension is installed.
- Install/update Decompute CLI.
- Run a non-key setup path:

```text
decompute init --auth-mode claude_subscription --base-url <gateway>
```

- Write `~/.claude/settings.json` routing:
  - `ANTHROPIC_BASE_URL`
  - `ANTHROPIC_CUSTOM_HEADERS`
- Do not require `claudeKeyPresent()`.
- If `~/.decompute/claude.key` exists, show one warning that this setup will ignore it and keep it for later.
- Offer reload.
- Show status as `Claude auth: subscription passthrough`.

BYOK mode flow:

- Keep current API key setup behavior.
- Keep `claudeKeyPresent()` gating only for BYOK mode.

Status UI changes:

- Decompute plan: Free/Pro
- Claude auth: API key / Subscription / Enterprise
- Gateway: Local / Hosted / Custom

### Phase 4 - Usage and Dashboard Wording

Status: planned

For subscription users:

- Prefer "tokens saved", "context saved", and "plan usage reduced".
- Treat dollar values as estimates only.
- Do not imply exact Claude bill savings for Pro/Max.

For API users:

- Existing cost savings display is still useful because API billing is token-priced.

### Phase 5 - Packaging and Release

Status: planned

- Version target: extension `0.1.16` or later.
- Gateway wheel target: rebuild and upload through Cloudflare website if that remains the deployment path.
- Installer target: ensure `install.sh` pulls the new wheel and CLI.
- Marketplace notes:
  - "Now supports Claude Pro/Max subscription login via local gateway passthrough."
  - "No Anthropic API key required for subscription users."
  - "Subscription OAuth tokens are not stored by Decompute."

## Test Matrix

Gateway:

- API key request:
  - request has `x-api-key`
  - upstream receives `x-api-key`
  - no `Authorization` header upstream
- Subscription request:
  - request has `Authorization: Bearer oauth`
  - local gateway upstream receives `Authorization: Bearer oauth`
  - no `x-api-key` upstream
- Production hosted:
  - request has non-Decompute bearer
  - gateway does not forward it by default
- Static gateway key:
  - `ANTHROPIC_API_KEY` env set
  - static key wins over client bearer
- Saved BYOK key:
  - `~/.decompute/claude.key` exists
  - key wins in API-key mode
- Model discovery:
  - `/v1/models` works with bearer
- Token counting:
  - `/v1/messages/count_tokens` works with bearer
- Streaming:
  - `/v1/messages` streaming works with bearer

CLI:

- `decompute init --auth-mode claude_subscription --yes` does not prompt for API key.
- `decompute init --auth-mode api_key` preserves current behavior.
- `decompute config-show` shows auth mode but never secrets.
- `decompute doctor` explains stale API-key precedence.

VS Code:

- Fresh user can choose subscription mode and finish without an API key.
- Existing BYOK user can update without losing key.
- `nextSetupStep` does not demand `claude.key` in subscription mode.
- Routing writes only managed env keys.
- Disable routing removes only managed env keys.

## Open Questions for Avii/Hina

Answered 2026-06-15:

1. Setup should ask the user whether they want to use an existing Claude plan or an API key.
2. If a user chooses subscription mode and `~/.decompute/claude.key` exists, ping once and ignore it.
3. Decompute Free/Pro quota should be the same for subscription users and BYOK users.
4. Keep bearer passthrough local-only for better privacy unless a future enterprise need says otherwise.
5. Public/setup wording should avoid protocol terms. Preferred UX wording: "Use my existing Claude plan" vs "Use an API key".

## Work Log

- 2026-06-15: Wrote the integration plan and started Phase 1 gateway credential plumbing.
- 2026-06-15: Implemented the first Phase 1 slice:
  - added typed `UpstreamAnthropicAuth`
  - forwarded API keys as `x-api-key`
  - forwarded local subscription bearer auth as `Authorization: Bearer`
  - blocked client bearer passthrough by default in production
  - updated `/v1/models`, `/v1/messages/count_tokens`, `/v1/messages`, streaming, CCR retrieval, and OpenAI-compatible proxy paths to use typed auth
  - added focused gateway auth tests
- 2026-06-15: Captured product decisions from Avii and started Phase 2/3: CLI auth modes plus VS Code setup choice.
- 2026-06-15: Implemented the first Phase 2/3 slice:
  - `decompute init --auth-mode claude_subscription`
  - friendly CLI aliases such as `--auth-mode pro`, `max`, and `subscription`
  - subscription mode stores no Claude credential
  - subscription mode ignores a saved `claude.key` but warns once
  - `decompute claude` removes API-key env vars from the child process in subscription mode
  - VS Code setup now asks "Use my existing Claude plan" vs "Use an API key"
  - existing-plan setup is local-gateway-only
  - setup helper no longer requires `claude.key` for subscription mode
