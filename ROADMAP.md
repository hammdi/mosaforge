# MosaForge — Roadmap

Each phase ends at a **gate**: measurable conditions that must hold before the next phase starts.
No phase buys hardware. No phase adds RL or self-modification before Phase 5.

## Phase 0 — POC on the Mac ✅ (2026-10-04)
- [x] Pull and inspect 15 upstream repos (`upstream/`, MANIFEST)
- [x] Core interfaces, Ollama provider (native API + `/v1/systemone`)
- [x] Router: tev1 decision model + keywords + experience adjustment + privacy rules
- [x] Two agents (general, coder) with jailed tools; text-tool-call recovery
- [x] Verification with escalation; SQLite memory (FTS5) + experience log
- [x] LAN worker (auth, allowlist, inference-only), tested over loopback
- [x] MCP server for Claude Code + `delegate-local` skill
- [x] Speed benchmark + 14-task ground-truth suite; first evidence-driven default change (ADR-013)

**Gate 0 → 1:** met. End-to-end works, suite runs, unit tests pass.

## Phase 1a — Resource Manager v2 (prototype ✅ 2026-10-06)
- [x] Source comparison of routing/scheduling in 9 projects (RESEARCH §7)
- [x] Architecture v2: model routing separated from resource routing (ARCHITECTURE, ADR-015…019)
- [x] `analyzer.py` (TaskProfile), `resources.py` (monitors, cost model, placement, batch plan), `mosaforge place`
- [x] Real Mac monitor (OS available memory, pressure, CPU, Ollama residency); simulated Dell; 10 rule tests
- [x] Worker hardening from the Dell prep: LAN-IP-only bind, allowlist required, `/v1/status` RAM/CPU, secret/9B node guards

**Minimal implementation plan to put it in the request path (each step small and tested):**
1. `WorkerMonitor`: build `NodeState` from the worker's `/v1/status` + `/v1/models` + measured RTT. Add a heartbeat
   with a TTL (DeerFlow lease pattern), so a silent worker counts as unhealthy.
2. Orchestrator: replace `_provider_for` with `ResourceManager.place` (keep the v1 path behind a flag for A/B).
   Pass `interactive` from the caller (CLI default true; MCP `delegate` and batch default false).
3. Perf tables from experience: after each run, update the (node, model) `ModelPerf` from `runs`
   (EWMA of tok/s, prefill, load) and mark the source `measured`.
4. Track in-flight work and `busy_s` per node from the orchestrator's own dispatches.
5. Deploy the Dell (procedure in the previous turn / README), switch `dell.simulated = false`, run E-RM-1…5.

**Gate 1a → 1:** placement predictions within ±30 % of actual latency on the real Dell. No swap growth on
the Mac during A/B/C runs. Secret-task and exclusion tests green against the real worker.

## Phase 1 — Real LAN + a credible evaluation (next)
1. Install on the Dell: Ollama (127.0.0.1), `mosaforge worker --host <lan-ip>`, token. Run
   `bench speed` there (BENCHMARKS E-HW-2) and over the LAN (E-LAN-1).
2. Grow the suite from 14 to ~150 tasks (30 per category), drawn from your real work and from public
   sets (HumanEval-style functions, GSM8K-style arithmetic, extraction from your own documents). Add
   3 seeds per task.
3. Baselines: always-`local-strong`, always-Claude (via `claude -p`, cost logged), routed. This is the
   core experiment for the research question (BENCHMARKS §5).
4. Non-code verification experiments: self-consistency (2 samples agree?), cross-model agreement,
   tev1:4b and nimble as judges.
5. Node selection: done by the Resource Manager (Phase 1a). Here: calibrate its weights on real runs.

**Gate 1 → 2:** the routed config reaches ≥ 95 % of always-Claude quality on the suite at ≤ 30 % of its
cloud cost, **or** we have the data to show it can't. Either outcome gets written up.

## Phase 2 — Supervisor workflow and skills
- Use Claude Code + `delegate` on real tasks for two weeks, with `feedback` on every delegation.
- Curated `skills/` (ECC: verification-loop, tdd-workflow, search-first; attribution). Skill injection by category.
- OpenAI-compatible provider adapter (llama-server, mlx_lm.server). Re-benchmark Ollama 0.40 (MLX engine).
- Decide ADR-009: OpenJarvis as base, or keep borrowing.

**Gate 2 → 3:** ≥ 30 % of supervisor subtasks delegated with ≥ 90 % accepted without correction.

## Phase 3 — External agent runtimes, per category
- `AgentRuntime` adapter. Benchmark OpenHands SDK (Docker workspace) on `complex_code`, and DeerFlow
  on long research, against the built-in loop on identical tasks.
- Adopt a runtime only for the categories where it wins. Its memory stays disabled.
- Voice: point isair/jarvis (as a separate app) or OpenJarvis presets at an OpenAI-compatible MosaForge endpoint.

## Phase 4 — Learning from experience (no weight updates)
- Bandit routing policy (Thompson sampling over category × model × node, with cost and latency in the
  reward), trained only on `runs` with ground truth. Borrow from OpenJarvis `learning/routing`.
- Automatic lessons → `skill` memories, behind a review queue.
- Offline evaluation: replay the suite. A policy ships only if it beats the hand-written rules.

## Phase 5 — Optional weight-level learning
- Export verified runs (`Store.export_jsonl`) → Soup (MLX backend) LoRA on a ≤ 4B model for the
  categories where it fails most. Accept only if it improves the held-out suite.
- RL (GRPO via Soup) as an isolated experiment on routing or tool use. Never a core dependency.

## Hardware gates (no purchase without these)
| Purchase | Only if a benchmark shows |
|---|---|
| Faster Ethernet / direct cable | LAN round trip is > 10 % of task latency (E-LAN-1) or E-LAN-2 is promising |
| RAM-heavy server (≥ 64–128 GB) | A ≥ 27B model wins ≥ 15 points on the suite over qwen3.5:9b for categories that account for ≥ 25 % of your tasks *and* would replace measurable cloud spend |
| GPU node | Same, plus throughput need (concurrent agents) the Mac/Dell can't meet |
| Second Apple Silicon Mac | MLX distributed / exo becomes viable (E-LAN-3) *and* the server option is worse per € |
