# Architecture Decision Records

Format: context → decision → evidence → consequences. Status: **Accepted** unless noted.
"Gate" refers to the measurable conditions in [ROADMAP.md](ROADMAP.md).

---

### ADR-001 — MosaForge owns a thin core. No orchestration framework in the POC
- **Context.** DeerFlow, OpenJarvis, OpenHands and ECC each bring their own orchestrator, memory and
  skills. Adopting one as the base means adopting its abstractions before we have measured anything.
- **Decision.** Implement the core interfaces (`ModelProvider`, `Tool`, agent contract, `Router`,
  `Store`, worker, MCP surface) in ~1,500 lines of stdlib Python. External projects plug in behind them.
- **Evidence.** DeerFlow backend is 223k LOC with 60+ dependencies. OpenJarvis has 691 Python files
  plus a Rust extension. The POC core runs on Python ≥ 3.11 with no dependencies on both machines.
- **Consequences.** We maintain some code that others already have (tool loop, FTS memory). Re-check
  at Gate 2 whether OpenJarvis should become the base (ADR-009).

### ADR-002 — Claude Code is an optional supervisor, connected through MCP
- **Context.** We wanted Claude to plan and review while local models do cheap work.
- **Decision.** Expose MosaForge as an MCP server (`delegate`, `route`, `memory_*`, `feedback`, `stats`).
  Do not try to make Claude Code subagents run on local models.
- **Evidence.** Claude Code docs: subagents "cannot use a different provider or `ANTHROPIC_BASE_URL`
  than the main session". Anthropic "doesn't support routing Claude Code to non-Claude models through
  any gateway". "To involve a different tool, expose it to Claude as an MCP server." Verified working:
  stdio round trip initialize → tools/list → delegate → stats (BENCHMARKS.md §4).
- **Consequences.** Works with any MCP client, not just Claude. In MCP mode MosaForge never calls the
  cloud itself. Running a *whole* Claude Code session on Ollama's Anthropic endpoint stays an
  unsupported experiment (E-CC-1).

### ADR-003 — Ollama is the default ModelProvider, spoken to via its native API
- **Decision.** `OllamaProvider` uses `/api/chat` and `/v1/systemone`. llama.cpp and MLX servers come
  later through one OpenAI-compatible adapter, and only if they beat Ollama on BENCHMARKS.md §1.
- **Evidence.** Ollama's `/v1` drops or flattens thinking content (DeerFlow config, ollama#15293).
  Ollama 0.35 adds decision models. 0.40-rc runs MLX by default on Apple Silicon, which removes most
  of the reason to run mlx-lm separately on the Mac.
- **Consequences.** Re-run the speed benchmark when 0.40 is final.

### ADR-004 — Routing uses a local decision model through the Jev-compatible API
- **Context.** "Jev → router" was a hypothesis. TypeSafe Jev is hosted-only (early access, no weights).
- **Decision.** Route with `tev1:0.8b` via Ollama `/v1/systemone` (choice + score questions), with a
  keyword fallback and a code-keyword veto. Any Jev-API model (tev1 4B, nimble, clef, or Jev itself
  as an opt-in cloud option) is a config change.
- **Evidence.** 5/5 probe tasks classified correctly. Suite accuracy in BENCHMARKS.md §2. 0.12 s warm
  median, 1.4 s cold, 0.8 GB resident.
- **Consequences.** Routing costs about one prefill. tev1 is labelled experimental, and its `confidence`
  measures concentration, not correctness. AgentJev-0.6B is deferred (torch dependency, provenance).

### ADR-005 — Agent-level LAN distribution first. The worker is inference-only
- **Decision.** The Dell runs `mosaforge worker` (Bearer token, model allowlist, no tools). Tools
  always execute on the caller. Model-level splitting is an experiment, not architecture.
- **Evidence.** MLX distributed and exo require Apple Silicon to be useful. llama.cpp RPC is
  self-described as "fragile and insecure". Worker overhead on loopback is negligible (§3).
- **Consequences.** The Dell can only run models that fit in its own 16 GB. That's acceptable until E-LAN-2.

### ADR-006 — One memory and experience store: SQLite + FTS5 under `~/.mosaforge`
- **Decision.** Scopes `project`, `long_term`, `skill`, `experience` plus a `runs` table. No vector DB yet.
- **Consequences.** Lexical recall only. Add embeddings (embeddinggemma / qwen3-embedding via Ollama)
  when a retrieval benchmark shows FTS misses. If DeerFlow, ECC or OpenJarvis are adopted, their memory
  features stay off.

### ADR-007 — Skills use the SKILL.md format. ECC is cherry-picked, not installed
- **Evidence.** ECC ships 293 skills (2.7 MB of text) and code-executing hooks. SKILL.md is shared by
  Claude Code, OpenJarvis, DeerFlow and ECC.
- **Consequences.** `skills/` will hold a small curated set with attribution (MIT). If ECC goes into
  Claude Code, use `--profile minimal --no-hooks`.

### ADR-008 — Only deterministic checks can certify. The model judge can only veto
- **Context.** The first coding run produced no code (tool calls emitted as text), yet the tev1 judge
  said "pass" with p=0.97.
- **Decision.** Verdict order: caller's `verify_cmd` → re-run the agent's own test files → compile →
  judge. Compile-only and judge-high results are `unknown`, never `pass`. Escalation happens only on `fail`.
- **Evidence.** Suite: the judge (plus compile check) passed 8 of 8 wrong answers and caught 0 (BENCHMARKS.md §2).
- **Consequences.** Without tests, many results are `unknown`. That's honest, and it pushes callers to
  supply `verify_cmd`. Better non-code verification (self-consistency, cross-model agreement) is a
  Phase 2 experiment.

### ADR-009 — Heavy runtimes are external `AgentRuntime`s adopted per category, by benchmark
- **Decision.** DeerFlow (long-horizon research, HTTP on 127.0.0.1:2026) and the OpenHands SDK
  (complex coding in Docker) are adopted only when they beat the built-in loop on the same tasks.
  OpenJarvis is the reference design for the learning loop. At Gate 2 we decide whether it becomes the base.
- **Status.** Proposed: needs your input (see README "Open questions").

### ADR-010 — Rejections and deferrals
- **Spark (X33834/spark): rejected.** Not runtime management. 0★, 11 days old. Binds `0.0.0.0` with no
  token by default and serves a web terminal.
- **isair/jarvis code: never copied.** Non-commercial share-alike licence. Usable later as a separate voice client.
- **Colibrì: research track.** 0.05–1 tok/s and 85 GB–1.6 TB of weights. Not interactive on this hardware.
- **Soup: Phase 5.** It needs a dataset. Its MLX backend makes the Mac a viable trainer for ≤ 4B LoRA.
- **exo / MLX distributed: not for Mac + Dell.** Revisit with ≥ 2 Apple Silicon machines.

### ADR-011 — Cloud escalation is opt-in, privacy-gated, and uses the user's own Claude login
- **Decision.** `ClaudeCLIProvider` shells out to `claude -p --tools "" --output-format json`. Default
  `allow_cloud = false`. Tasks with `privacy = secret` never reach a cloud model. No API key is stored
  by MosaForge, and the reported `total_cost_usd` is logged per run.

### ADR-012 — Upstream code is inspected in `upstream/`, never vendored by default
- **Decision.** Shallow clones, gitignored, commits pinned in `upstream/MANIFEST.md`. When a file is
  reused, it is copied with its licence header and listed in COMPONENTS.md.

### ADR-013 — Benchmarks change defaults. First change: code starts at tier 1
- **Evidence.** qwen3.5:4b (no thinking) scored 13/14 on the suite. qwen2.5-coder:7b scored 7/14 and
  was no better on the code tasks (BENCHMARKS.md §2).
- **Decision.** `category_tier.code = 1`, `reasoning = 1`. `local-coder` gets `ladder = false`
  (forced use only). Escalation goes small → strong.
- **Caveat.** 14 tasks is a small sample. Re-run with the extended suite (Phase 1) before treating this as settled.

### ADR-014 — Thinking off by default. Agents always end with a tool-free final answer
- **Evidence.** qwen3.5:9b with thinking + tools: 487 s for the suite against 142 s without, the same
  12/14, two tasks that never produced an answer, one empty answer (BENCHMARKS.md §2.3).
- **Decision.** `think = false` for every tier in the default config. When the step budget runs out,
  `ToolLoopAgent` makes one last call without tools and asks for the final answer.
- **Consequences.** Thinking comes back only as an explicit, token-budgeted option for `reasoning`
  tasks, once the Phase 1 suite contains reasoning tasks hard enough to measure its value.

### ADR-015 — Model routing and resource routing are separate decisions
- **Context.** v1 picked a model and then took the first healthy provider in a fixed order. The Dell
  was a fallback, so the Mac kept every task until something broke (e.g. the 9B swap).
- **Decision.** The Model Router returns acceptable models in quality order. The Resource Manager
  places the first placeable one on the cheapest feasible node under live state. It never trades
  quality for speed. Downgrades are the Model Router's (or the user's) call.
- **Consequences.** Two independently measurable loops: quality (pass rate per model) and placement
  (predicted vs actual latency per node).

### ADR-016 — MosaForge owns a small Resource Manager. Patterns are borrowed, no scheduler is adopted
- **Evidence (RESEARCH.md §7).** Ollama and llama.cpp place models on *one* host, filling free memory
  with no reserve for the user's apps. OmniRoute scores cloud providers by quota, cost and health, with
  no host resources. OpenJarvis routes by which engine owns a model name. None of them place work
  across machines by RAM, pressure, load and interactivity.
- **Decision.** `resources.py` (~350 lines, stdlib): hard constraints → latency estimate → weighted
  objective → batch list scheduling. Borrowed: Ollama's fit/evict semantics, llama.cpp's per-device
  margin (`reserve_gb`), OmniRoute's health and latency factors plus decision transparency, and
  DeerFlow's lease/heartbeat for worker liveness (planned).
- **Rejected.** Installing OmniRoute as the router. It's 883 MB of TypeScript, cloud-account oriented,
  ships "TLS stealth" and provider-block evasion, and would be a second router.

### ADR-017 — Mac headroom = OS-reported available memory − reserve, not "used"
- **Evidence.** On 2026-10-06 the Mac showed 12.8 GB "used" (5.8 GB of it compressor) while macOS
  reported 49 % available and pressure *normal*. A used-based rule refused even the 4B model.
  `kern.memorystatus_level` (the "free percentage" in `memory_pressure`) counts compressed and
  file-backed pages as reclaimable.
- **Decision.** headroom = total × memorystatus_level − `reserve_gb` (2.0 GB). New loads are also blocked
  at pressure *warn* on the interactive node, and at *critical* on any node. `exclude_models` stays as a hard
  stop for the 9B on the Mac.
- **Consequences.** At ~50 % available the 4B fits and the 9B doesn't, which matches what happened on 2026-10-04.

### ADR-018 — Background work is penalized on the interactive node, and batch queues there are capped
- **Decision.** Background work on the Mac pays `compute × 0.6 × (1 + inflight + cpu/100)`, ×2.0 at
  pressure warn. Batches minimize makespan, but at most 15 s of background work may queue on the Mac.
- **Evidence (simulated Dell, BENCHMARKS.md §7).** With the Dell at ~25 % of the Mac's speed (CPU estimate),
  capping the Mac's queue makes an 8-task batch *slower* (167 s vs 62 s all-Mac) but frees the Mac after
  21 s. Uncapped, the same batch finishes in 48 s on both machines. Both numbers are reported, and the
  cap is a config value, not hidden.
- **Status.** The weights (0.3, 0.6, 2.0, 15 s) are an initial policy. Calibrate them with E-RM-2
  (measured slowdown of interactive work while background inference runs on the Mac).

### ADR-019 — Graphify is the candidate Context/Knowledge layer, wrapped and local-only
- **Decision (planned).** Use Graphify's code graph (tree-sitter, offline) through its CLI/MCP as context
  for code tasks and as a scope signal for the Task Analyzer. Its semantic pass over docs/media is used
  only with `--backend ollama`. Its Claude Code hooks are not installed (they alter tool behaviour globally).
