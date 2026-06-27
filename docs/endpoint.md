# Hosting a larger student on a bigger endpoint (V1a)

The local "student" model defaults to a `llama-server` on your own machine, which caps it at whatever
your RAM can run (a 16 GB Mac tops out around a 7B). To run a **much larger** student — a 14B/32B coder,
or bigger — point the student at any **OpenAI-compatible endpoint** that hosts it: a beefier box on your
LAN, a private GPU, or a hosted inference endpoint (vLLM, SGLang, Together, Fireworks, …).

The verifier, the judge, the `/student` slash command, and escalation-to-Claude all work exactly the
same — the student is just a bigger, smarter model behind the same interface.

This is **V1a** of the heterogeneous-inference plan (see `heterogeneous_inference_plan.md`). It delivers
"host a larger student" today, with no clustering. Multi-machine clustering (V1b/V1c) is a separate,
gated step.

## Quick start

```bash
# A bigger model on a machine on your LAN (keyless local server):
decompute student endpoint http://192.168.1.50:8080/v1 --model qwen2.5-coder-32b

# A hosted endpoint (third-party, needs a key + consent — see Security below):
decompute student endpoint https://api.together.xyz/v1 --model Qwen/Qwen2.5-Coder-32B-Instruct \
  --api-key sk-...

# Verify it's reachable, the trust tier, and that it's actually fast enough:
decompute ts-health --measure
```

`ts-health --measure` runs a tiny real generation and reports the endpoint's **trust tier**, **TTFT +
tokens/s**, and a **conformance** check (does it return well-formed OpenAI responses with usage). Use it
to confirm the endpoint is genuinely usable, not just reachable.

## Security model (what the gateway enforces)

Pointing the student off-box means your prompts (which may contain proprietary code) leave this machine.
The gateway enforces a trust-tier policy on the endpoint URL before it will ever route to it:

| Tier | What it is | Policy |
|---|---|---|
| **loopback** | `127.0.0.1` / `localhost` | always allowed (the default local student) |
| **private** | LAN (`10/8`, `192.168/16`, `172.16/12`) or Tailscale (`100.64/10`) | allowed with `DECOMPUTE_LOCAL_STUDENT_ALLOW_REMOTE=true` |
| **public** | a routable internet host | requires `DECOMPUTE_LOCAL_STUDENT_ALLOW_PUBLIC_ENDPOINT=true` **and** https |
| **blocked** | cloud-metadata / link-local / reserved IPs | **never** allowed (SSRF guard) — not overridable |

- The cloud-metadata IP (`169.254.169.254`) and other link-local/reserved ranges are **hard-blocked**,
  always — even if a flag is set, and even if a hostname resolves to one. This is an SSRF defense.
- A **public** endpoint also needs **https** (don't ship plaintext prompts across the internet) and an
  explicit consent flag, because it's a real trust-boundary change.
- The endpoint's **API key** is stored owner-only (`chmod 600`, `~/.decompute/student_endpoint.key`),
  never in plaintext config and never logged. The gateway reads it from that file.

The `decompute student endpoint` command warns you (and requires confirmation) for any non-loopback
endpoint, and tells you which flag a public endpoint needs.

## Is a bigger student actually worth it? (run the eval first)

A bigger student is only worth deploying if it deflects enough extra coding traffic from Claude to pay
for itself (after the Claude **judge** call that still runs on the verify path, plus latency). Measure
it — don't assume it:

```bash
# 1. Point at your current/small student, run the eval:
decompute student endpoint http://127.0.0.1:18080/v1 --model qwen-7b
decompute ts-eval --label 7b --out compare.jsonl

# 2. Point at the bigger endpoint, run the same eval:
decompute student endpoint http://192.168.1.50:8080/v1 --model qwen-32b
decompute ts-eval --label 32b --out compare.jsonl

# 3. Compare the two JSON lines in compare.jsonl:
#    local accept-rate, escalation rate, p50/p95 latency.
```

A bigger student is worth it when its **local accept-rate rises enough**, at **acceptable latency**, to
net-save after the judge cost. If it barely moves the accept-rate, it isn't worth the cost/latency.
(`ts-eval` ships with a small stratified task set; pass `--tasks-file` for a real, representative one.)

## Honest cost note

When the student runs on a **paid** endpoint, the dashboard's "Claude spend avoided" is **gross** — it
counts the Claude generation you skipped, but does **not** subtract the endpoint's own cost (which the
gateway doesn't know). The teacher-student dashboard card says so explicitly for a remote endpoint. Only
treat it as net savings once you've accounted for the endpoint's price.

## Reverting to the local student

```bash
decompute student models            # pick a local model again (sets backend back to llama_cpp)
# or point the endpoint back at the local server:
decompute student endpoint http://127.0.0.1:18080/v1 --model <local-model>
```
