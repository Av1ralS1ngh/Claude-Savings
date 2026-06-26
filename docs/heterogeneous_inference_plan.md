# Heterogeneous Inference for the Local Student — Design Plan (v2, post-Codex review)

Status: **PROPOSAL (revised after a tough Codex review).** Not implemented.
This v2 reframes the v1 around the review's central correction: the product is a **pluggable,
OpenAI-compatible student backend with measured health/preflight + an explicit fallback policy** — not
"Decompute-managed llama.cpp clustering." llama.cpp RPC becomes the *first experimental* backend,
promoted only after security + benchmark gates pass.

Changelog vs v1 (what the review changed):
- Reframed: the product is the **backend contract + preflight + fallback**, not the clustering runtime.
- Added a hard **precondition gate**: prove (by eval) that a bigger student actually deflects more
  traffic *and pays for itself* before building any clustering. "Bigger = more savings" is an
  assumption to validate, not a premise.
- Corrected the exo facts (the live repo is Apache-2.0 + active but Apple-Silicon-tier-1; the GPL repo
  is the superseded archive).
- Hardened security far beyond "LAN-only"; labeled llama.cpp RPC **experimental/insecure** per its own
  docs.
- Replaced the memory-only feasibility math with an **empirical** model (GGUF metadata + KV + scratch +
  overhead + a measured tokens/s & link benchmark).
- Fixed the fallback claim: fallback is a **pre-stream / pre-side-effect circuit breaker**, not magic
  recovery.
- Re-sliced V1 into V1a/V1b/V1c.

---

## 0. PRECONDITION — prove the value before building the runtime

The whole premise is "a bigger student deflects more coding traffic from Claude." **That is a
hypothesis, not a fact, and distributed inference is the most expensive way to test it.** Before any
clustering work, run a cheap eval:

**Design (gate-grade, not vibes).** Compare **7B vs 14B/16B vs 32B**, each served via an
OpenAI-compatible endpoint (single big box / cloud GPU — no clustering needed to measure). Hold
everything else identical across models: routing, prompts, verifier, **judge model**, temperature,
context budget, timeout.

- **Task set:** a **stratified** sample of real/representative Decompute coding tasks (e.g. by intent:
  simple_code_patch / lint_fix / docs_edit / test_generation, and by size), **n ≥ 300** per model so
  accept-rate differences carry error bars (report 95% CIs).
- **Metrics per model:** verifier **accept-rate** (student_only), **false-accept rate** (judge/post-hoc
  said wrong), Claude **escalation rate**, **human/test acceptance** on a sampled subset, **p50/p95
  TTFT**, **p50/p95 total latency**, **endpoint $**, **Claude judge $**, and **avoided Claude
  generation $**.
- **Cost formula (per task):**
  `net = endpoint_cost + claude_judge_cost − avoided_claude_generation_cost`. Sum across the set; a
  model "pays" only if `Σ net < Σ net(7B)` by a material margin.
- **Predeclared thresholds (set the numbers BEFORE running — these are business calls, placeholders
  here):** ship a bigger student only if it
  **(N)** raises verified accept-rate by **≥ N pp** (e.g. 10), **(M)** cuts Σ net cost by **≥ M%**
  (e.g. 15), **(FA)** keeps false-accept rate **non-inferior** to the 7B, and **(T)** keeps **p95 total
  latency < T** (e.g. 8 s for interactive coding). All four must pass.
- **Go/no-go:** if a 32B doesn't clear all four vs 7B, do **not** build clustering (and probably not even
  the bigger single-box student). If it clears them on a single box, *then* ask whether a home cluster
  is the cheapest way to deliver that bigger model (often it isn't — §4 `openai_endpoint`).

**Also evaluate the cheaper-than-sharding levers first** (any of these may beat clustering):
- Better routing / a sharper coding-intent gate (already improved).
- A stronger *small* coding-specialist model or a better quant (fits one device).
- Context caps + the existing prompt compression to shrink KV/latency.
- Speculative / draft decoding (small draft + verify) for speed.
- "Single bigger box" support — let a user point the student at one beefy machine / a private GPU,
  which needs **none** of the clustering complexity (it's just the backend contract, §4).

If the eval says "yes, a bigger student pays off," proceed — but note the cheapest way to *deliver* a
bigger student is often "one bigger box or a hosted endpoint," not a home cluster.

---

## 1. The goal (boss's words)

> "Introduce heterogeneous inference… enable people with heterogeneous compute to host a larger student
> model." Reference: [exo-explore/exo](https://github.com/exo-explore/exo).

Today the student runs on **one** machine via llama.cpp; single-device memory caps it at ~3B–7B (a
16 GB Mac froze on a 12B — measured). The aim is to let a bigger student be hosted, raising the local
serve-rate. Per §0, "host a bigger student" need not mean "shard across a home cluster" — that's one
implementation among several.

---

## 2. The load-bearing technical fact (unchanged, and it's the good news)

**Every candidate backend speaks the same OpenAI `/v1/chat/completions` the gateway already uses.** The
`LocalStudentProvider` already carries a `base_url` + a `backend` field. So at the wire level the
student is already "an OpenAI-compatible endpoint." The product should make that explicit and general;
clustering is then just one kind of endpoint behind it.

---

## 3. Background research (corrected)

### 3a. exo — corrected facts
- The **live** repo is `exo-explore/exo`: **Apache-2.0, active**, OpenAI/Claude/Ollama-compatible,
  topology-aware placement, Thunderbolt RDMA. Its **current Tier-1 support is Apple-Silicon macOS**;
  **Linux GPU and Windows are not there yet** (Linux is CPU-only). So exo is **not** a "Mac + PC + CPU"
  answer today — it's an Apple-Silicon-cluster answer.
- The GPL-3.0 `exo-explore/ex-exo` is the **archived, superseded** predecessor ("current development
  happens in exo-explore/exo"). The earlier plan's "GPL old exo matches the vision" framing was wrong:
  that repo is dead, and the GPL concern is largely moot for the live Apache repo.
- **Implication:** exo is a good **bring-your-own backend for Apple-Silicon users**, integrated over its
  HTTP API as a separate process — not the default for a heterogeneous Mac+PC+CPU fleet, and not a
  bundled dependency.

### 3b. llama.cpp RPC — corrected status
- Real and attractive: remote CUDA/Metal/CPU devices, `llama-server --rpc`, memory-proportional
  weight/KV split, `--tensor-split` override, optional RDMA on Linux, **reuses our GGUF catalog +
  existing provider path**.
- **But its own docs call the RPC backend "proof-of-concept," fragile, and insecure, and warn not to
  run it on an open or sensitive network.** There have been critical *unauthenticated* RPC/server
  memory-safety advisories. → It is an **experimental cluster runtime**, not a production-default.

### 3c. vLLM / Ray / SGLang
- Production-grade distributed inference (tensor + pipeline parallelism, Ray multi-node, real
  observability) — but assume **homogeneous GPU clusters and private unencrypted networks**. Great as
  **enterprise / BYOC OpenAI-compatible backends**; *not* a consumer Mac+Windows+CPU mesh answer.

---

## 4. The architecture (reframed): the product is the backend CONTRACT

**Ship "any OpenAI-compatible student endpoint, with measured health/preflight + an explicit fallback
policy" as the product.** Concrete backends are implementations behind one contract:

| Backend | What it is | Tier |
|---|---|---|
| `llama_cpp` | single local `llama-server` | **GA (today)** |
| `openai_endpoint` | any user-supplied OpenAI-compatible URL (a big box, a private/hosted GPU, vLLM, SGLang) | **GA — cheapest way to get a bigger student** |
| `exo` | a user-run exo cluster (`:52415/v1`), Apple-Silicon | **Experimental BYO** |
| `llama_cpp_rpc` | GGUF sharded across a home cluster via llama.cpp RPC | **Experimental, gated** |

**The contract is split into a generic base (every backend) + an optional cluster extension (only
cluster backends).** Forcing `openai_endpoint`/vLLM/hosted to report pooled-memory/per-node was wrong —
a random OpenAI-compatible server exposes none of that.

`BaseCapability` — **every** backend must provide:
1. OpenAI `/v1/chat/completions` (non-streaming for the verified student path — see §7).
2. **Reachable + model id**, and a **measured readiness**: a canary call returning **TTFT + tokens/s**
   (not just "port open").
3. A **conformance profile** (§13): tool-calls supported?, JSON/structured-output mode?, context
   window, streaming chunk shape, stop-sequence + error semantics. "OpenAI-compatible" is leaky; we
   probe it, we don't assume it.
4. Honor the **fallback policy** (§7): abandonable for Claude before any user-visible output or side
   effect.

`ClusterCapability` (**optional**, only `llama_cpp_rpc` / `exo` / multi-node vLLM) — adds: pooled
memory, **per-node status**, interconnect benchmark, and the empirical feasibility verdict (§5).

A `preflight` runs the security posture (§6) + the benchmark floor before the gateway trusts any backend
to serve.

This makes "single bigger box," "hosted GPU," "exo," and "llama.cpp cluster" the *same* product surface
— and lets us ship the high-value, low-risk part (point the student at a bigger endpoint) without the
clustering runtime.

---

## 5. Feasibility / preflight model (empirical, not memory-math)

The v1 "Σ memory − reserve × 1.6" is **too memory-centric and will produce false positives** (esp. CPU
nodes that "fit" but destroy decode speed). Replace with:

- **Memory:** weight bytes from **GGUF metadata** (not a catalog guess) + KV-cache bytes computed from
  `model × context × concurrency × KV dtype` + graph/scratch buffers + RPC cache/transfer overhead +
  a per-device OS reserve + placement granularity (you can't split a layer arbitrarily).
- **Interconnect (empirical gate, not a guess):** run a **link benchmark** (bandwidth/latency between
  nodes) and a **tiny distributed-model canary** before declaring a topology usable. Tiers:
  - **WiFi → unsupported.**
  - **1/2.5 GbE → "loads, maybe; interactive, no."**
  - **10 GbE / direct Thunderbolt → the realistic floor for a 32B coding student.**
- **Speed gate:** a topology only passes if a measured **TTFT + tokens/s** clears a usability floor for
  interactive coding. Memory-fit alone never passes.

Output a `ClusterCapability{feasible, measured_tps, ttft_ms, bottleneck, per_node[]}` — feasibility is a
*measurement*, not a calculation.

---

## 6. Security (hardened — "LAN-only" is not a boundary)

llama.cpp RPC/`llama-server` must be treated as **insecure software on a hostile network**. For the
experimental cluster backend we **require** (not recommend):
- **Pinned llama.cpp commit/build** on coordinator + every worker (version skew is both a correctness
  and a security risk).
- Worker runs as an **unprivileged user with no secrets/credentials** on the box.
- **Firewall allowlist**: workers accept connections **only** from the coordinator; nothing else.
- **Tailscale ACLs or mTLS** between nodes — encrypted, authenticated transport. Tailscale alone
  (flat tailnet) is not enough; scope it.
- **Model hash verification** (the GGUF the cluster loads is the one we expect).
- **Kill-switch + auto-update policy** for a CVE in the RPC/server path.
- **A log line for every fallback** (cluster → Claude) for audit + cost attribution.

If a deployment can't meet this bar, the cluster backend stays **disabled**. exo/vLLM BYO backends
inherit the same "you own the endpoint's security" contract, surfaced explicitly.

---

## 7. Fallback is a pre-stream circuit breaker (correctness fix)

The v1 "fail-open to Claude" claim is only true **before any user-visible output or side effect.**
Once tokens have streamed to the user, or a tool/patch was partially applied, you **cannot**
transparently retry with Claude (duplication / state-repair). So:

- For the **experimental student backends** (cluster/exo), the verified-serve path is
  **non-streaming-until-verified**: the student drafts privately, the verifier runs, and **only a
  verifier-accepted answer is ever exposed** — at which point it's final and safe. If the student
  fails/escalates, **nothing was shown**, so Claude takes over cleanly.
- This is implemented as a **circuit breaker in the route/serve planner**, with a hard timeout to Claude
  *before* first byte. It is **not** a magic mid-stream recovery.
- Interaction with the existing streaming-serve I built: streaming a student answer to Claude Code is
  fine for a *trusted, low-latency* student, but for the experimental cluster backend, serve must be
  buffered-until-verified. (Document this divergence; don't stream an unverified cluster draft.)

---

## 8. Phasing (re-sliced per the review)

**V1a — the backend contract (no clustering).** Generalize the provider to "any OpenAI-compatible
student endpoint" (`openai_endpoint` backend). Ship "point your student at a bigger box / private GPU /
hosted endpoint." Delivers most of the value (a bigger student) with **none** of the cluster risk — but
it is **not trivial**; do it properly. **V1a must include:**
- **Endpoint config + auth-secret storage** (the endpoint's API key handled like the Claude key —
  chmod-600, never logged, never echoed).
- **Canary health check** with measured **TTFT + tokens/s** (the `BaseCapability` readiness).
- **Endpoint conformance profile** — probe tools, JSON mode, context window, streaming, stop behavior
  (§13); refuse/limit a student that can't do what the verify path needs.
- **Pre-stream timeout/fallback** — the §7 circuit-breaker (buffered-until-verified for any non-trusted
  endpoint; no streaming an unverified third-party draft).
- **Cost + judge-call accounting** — record endpoint $ and the Claude judge $ that still fires on the
  verify path, so savings are honest (§13).
- **Privacy warning + consent** for a **non-local** endpoint — sending proprietary code to a third party
  is a trust-boundary change; require explicit opt-in + a clear notice (§13).
- **SSRF protections + TLS rules** — the gateway accepts a user-supplied URL, so: block
  internal/metadata IPs (169.254.169.254, RFC-1918 unless explicitly a local student, localhost rules),
  require https for non-loopback, validate the host. (Mirror the existing `local_student_base_url`
  loopback guard, generalized.)
- **Metrics** — local accept-rate, **fallback reason**, and the **Claude spend delta** vs baseline.
- **The §0 eval harness/instrumentation lands HERE** — V1a is exactly what makes the §0 decision
  measurable (it's how you serve a 32B from a single endpoint and record the metrics). Build the eval
  on top of V1a's accounting, not as a throwaway script.

**V1b — experimental llama.cpp RPC, one validated topology.** A single, validated **2-node** topology;
**one pinned llama.cpp build**; **one pinned GGUF**; **no WiFi**; **no student streaming**; the §6
security bar enforced; hard timeout → Claude before any output. Behind an experimental flag, off by
default, loud "experimental" labeling.

**V1c — exo adapter (bring-your-own).** Detect a user-run exo cluster at `:52415`, read its
`/models`/`/state`, point the student there. Especially for Apple-Silicon users. No orchestration we own.

**Later (only if §0 eval + V1b gates justify it):** Decompute-managed cluster UX (`decompute cluster
add/list/status`), auto-placement, the setup wizard, 3+ devices, 70B, a support matrix.

---

## 9. Production bites to design for (from the review)

Version skew (coordinator/workers); **silent backend fallback to CPU** (fits but crawls); sleep/thermal
throttling on laptops; cold model-load times; **KV-cache blowup at long Claude Code contexts**; usage
accounting when the **judge still calls Claude** on the verify path; **cluster degradation causing
surprise Claude spend** (alert on it); model **license compliance** for the bigger weights;
tool-call/API incompatibilities across local servers; and the **support burden of home networks**
(this is the quiet killer — every flaky router becomes a support ticket).

---

## 10. Testing / validation

- **Eval gate (§0):** the deflection + net-cost study — *the* go/no-go.
- **Unit:** GGUF-metadata feasibility; KV math; preflight pass/fail; backend-contract conformance (mock
  transport); pre-stream circuit-breaker (no output leaks before verify).
- **Integration (mocked):** capability/health rollup; degraded node → fallback-before-first-byte.
- **Live (pre-promotion):** the one validated 2-node llama.cpp RPC topology on 10GbE/Thunderbolt —
  measured TTFT/tokens/s, a real `student_only` serve a single device couldn't do, and a
  kill-a-worker-mid-request test that hits Claude before any byte.
- **Cost regression:** confirm the judge-still-calls-Claude accounting and that a degraded cluster
  doesn't silently spike Claude spend.

---

## 11. Verdict (matching the review)

**Build from the pluggable-backend idea, not from a Decompute-managed clustering V1.** The product is
**"OpenAI-compatible student backend + measured health/preflight + explicit fallback policy"**;
llama.cpp RPC is the **first experimental implementation**, promoted only after the §0 eval, the §6
security bar, and the §5 benchmark gates pass. exo is a bring-your-own Apple-Silicon backend; vLLM/SGLang
are enterprise BYOC backends; "single bigger box / hosted GPU" is the cheapest way to deliver a bigger
student and should ship first (V1a).

---

## 12. Open questions for the boss / next review

1. **Who is the launch audience?** If it's high-end Apple-Silicon clusters → exo is viable as primary.
   If it's "everyday Mac+PC+CPU" → llama.cpp RPC is the only hetero option, but it's experimental and
   network-bound; manage expectations.
2. **Do we even need clustering for V1, or does "point at a bigger box / hosted GPU" (V1a) + the §0 eval
   satisfy the goal** ("host a larger student") at a fraction of the risk?
3. **Cost guardrails:** are we comfortable that a degraded cluster silently shifts spend back to Claude,
   and how do we alert on it?

---

## 13. New risks the reframe introduces (V1a's real hard parts)

Opening up `openai_endpoint` changes what Decompute *is*, and these must be designed up front:

1. **Trust-boundary change (the biggest one).** Today it's "local student or Claude." Once
   `openai_endpoint` is GA, Decompute can send **proprietary code to any configured third-party
   server**. This needs: explicit **per-endpoint consent**, a **privacy notice**, **audit logs** of
   what was sent where, **per-project policy** (an org can forbid non-local endpoints), and clear docs.
   A non-loopback endpoint is **off by default** and opt-in. Local endpoints (loopback/Tailscale) are
   the trusted default.
2. **"OpenAI-compatible" is a leaky abstraction.** Chat-completions shape is not enough — coding agents
   depend on **tool calls, structured/JSON output, stop sequences, context size, streaming chunk shape,
   and error semantics**, which drift across servers (vLLM ≠ llama.cpp ≠ exo ≠ hosted). **Build a
   conformance suite early** (it backs the `BaseCapability` conformance profile in §4): probe each
   endpoint once, record what it actually supports, and degrade/refuse on the verify path accordingly.
3. **Savings attribution gets harder — be honest.** A hosted 32B may deflect Claude but **cost money
   elsewhere**. If the user hasn't configured the endpoint's pricing, the dashboard must say **"Claude
   spend reduced"**, **not** "net savings." Only claim net savings when we can compute
   `endpoint_cost + judge_cost − avoided_claude_cost`. Never overstate.

---

## 14. Status: APPROVED to build V1a (per second Codex review)

Codex's verdict: *"sound to start building V1a from,"* conditioned on two pre-implementation changes,
**both now incorporated**: (a) the backend contract is split into `BaseCapability` + optional
`ClusterCapability` (§4), and (b) the §0 eval has explicit predeclared thresholds (N/M/FA/T), sample
size, stratification, false-accept tracking, and a cost formula. V1a's full requirement list is in §8;
its new hard parts (trust boundary, conformance, honest attribution) are §13. Clustering (V1b/V1c)
remains gated behind the §0 eval + §5/§6 gates. **Awaiting the boss's green light to implement V1a.**
