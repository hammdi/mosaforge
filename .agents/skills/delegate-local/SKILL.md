---
name: delegate-local
description: Delegate cheap, self-contained subtasks (summaries, classification, boilerplate, small functions, unit tests, docstrings) to MosaForge's local models via the mosaforge MCP server, keeping Codex for planning, hard reasoning and final review.
---

# Delegate to local models

Use the `mosaforge` MCP tools when a subtask is **self-contained** and **checkable**.

1. Write the subtask so a 4–9B local model can do it with nothing else in context: inputs, expected
   output format, constraints. Prefer "reply with one python code block".
2. Call `delegate` with `category` when you know it, and a `verify_cmd` whenever a test can decide
   correctness (e.g. `python3 -m pytest tests/test_x.py`). Set `allow_writes: true` only when the
   local agent must write files.
3. Read `verified`. If it is `fail` or `unknown`, review or redo the work yourself. Never pass on an
   unverified local answer as correct.
4. Afterwards call `feedback` with the `run_id` and whether the result was actually right. This is the
   ground truth the router learns from.

Do not delegate: architecture decisions, security-sensitive changes, multi-file refactors, anything
the user marked as needing a frontier model, or tasks whose context you cannot fully restate.
