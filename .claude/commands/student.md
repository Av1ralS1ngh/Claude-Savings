---
description: Answer this coding task with the LOCAL student model (verified; escalates to Claude only if not confident)
argument-hint: [coding task]
disable-model-invocation: true
allowed-tools: Bash(DECOMPUTE_BASE_URL=* * ts-run *)
---

The user invoked the local student model for this coding task. The answer below was produced by the
local model on this machine (verified by the gateway; it falls back to Claude only if the verifier
isn't confident). Present the answer as-is — it is the response to the user's request.

## Local student answer

!`DECOMPUTE_BASE_URL=http://127.0.0.1:8090 PYTHONPATH=/Users/avii/Documents/Claude-Coder/packages/python-sdk/src /Users/avii/Documents/Claude-Coder/.venv/bin/python -m decompute_ai.cli ts-run "$ARGUMENTS"`

## Your job
Show the user the local student's answer above (the code and the route line). Do NOT re-answer the
task yourself — the whole point is that the local model answered it. If the route says
`teacher_corrected` or `teacher_only`, briefly note that the verifier wasn't confident so it escalated
to Claude, and the shown answer is the corrected one.
