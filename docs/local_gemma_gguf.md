# Choosing a local student model (GGUF) and the hardware reality

The local student runs through llama.cpp's `llama-server` (OpenAI-compatible). Choosing a model is a
RAM problem first and a quality problem second. **The gateway enforces a device-feasibility gate**
(`yellowstone/student_capability.py`) so it never tries to load a model that would freeze the machine.

## The 16 GB Mac reality (measured)

On a 16 GB Mac the doc's **Gemma 12B coder has no usable quant**:

- **Q4_K_M (~7.4 GB)** — loads, then thrashes swap and **freezes the machine**. After the OS and your
  editor + Claude Code, ~10 GB is usable; a 12B Q4 needs ~12 GB with runtime headroom. It does not fit.
- **Q2_K (~4.5 GB)** — loads, but 2-bit is too lossy for a 12B and produces **incoherent garbage**.

Conclusion: a 16 GB device should use a **smaller coder model** (a ~3–4 GB quant of a 3B–7B coder) or
**route coding tasks to Claude** — never the 12B. This is why the RAM check and model/quant tiering
are not optional.

## The feasibility gate

`assess_local_student(device, model)` computes:

```
required_gb = model_weight_gb * 1.6      # weights + KV cache + runtime overhead
usable_gb   = device_total_gb - 6        # OS + editor + Claude Code reserve
feasible    = usable_gb >= required_gb
```

| Device RAM | Usable (~) | Largest feasible weight (~) | 12B Q4 (7.4 GB)? |
|---|---|---|---|
| 16 GB | 10 GB | ~6 GB | ❌ no |
| 32 GB | 26 GB | ~16 GB | ✅ yes |
| 64 GB | 58 GB | ~36 GB | ✅ yes |

The `start_student_llama_cpp.sh` launcher runs the same check (`WEIGHT_GB=...`) and refuses to launch
an infeasible model unless you set `FORCE=1`.

## Picking a model

1. Check your RAM and the table above.
2. Choose a coder GGUF whose file size leaves headroom (`required_gb` ≤ `usable_gb`).
3. Prefer **Q4_K_M or better** for small models; avoid Q2_K (too lossy).
4. Start it and verify coherence: `decompute student smoke "Write add(a,b)"`.

## Starting it

```bash
# user-managed; nothing is downloaded by the gateway.
decompute student start --model <hf-repo:quant> --weight-gb <size-in-gb>
# or directly:
WEIGHT_GB=3.5 MODEL=<hf-repo:quant> bash scripts/start_student_llama_cpp.sh
```

The endpoint contract is `http://127.0.0.1:18080/v1`. Set `DECOMPUTE_LOCAL_STUDENT_BASE_URL` to match.

## Quality expectations

The student is a **narrow coding assistant**, not an agent. It never receives tool-use turns, and its
answers are always run through the escalation-biased verifier before they are trusted. A small local
model with no tests and no judge will almost always escalate to Claude — that is by design until you
have calibration data and (optionally) enable sandboxed test execution and the judge.
