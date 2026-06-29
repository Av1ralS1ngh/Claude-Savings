---
description: Draft a coding task with the LOCAL student model; you (Claude) verify it and escalate only if it's too hard
argument-hint: [coding task]
disable-model-invocation: true
allowed-tools: Bash(* local-chat *)
---

The user invoked the local student model for this coding task. The small local model on this machine
produced the DRAFT below. You are the teacher in a teacher-student flow — and you run on the user's
Claude subscription, so any escalation costs them nothing extra.

## Task
$ARGUMENTS

## The local student's draft
!`env -u DECOMPUTE_BASE_URL "/Users/avii/.local/bin/decompute" local-chat "$ARGUMENTS"`

## Your job — verify, and escalate only if needed
Judge the draft above against the task:
- If it is correct and complete → present it as the final answer and note: **"✓ handled locally by the student model."** Do not rewrite a correct answer.
- If it has bugs, misses edge cases, or the task is genuinely too hard for a small local model → solve it properly yourself and present your answer, noting: **"↑ escalated to Claude — the local draft wasn't sufficient."**

Judge honestly: a wrong local answer is worse than escalating. Keep your reply concise — the student
did the first pass; you are the verification + safety net.
