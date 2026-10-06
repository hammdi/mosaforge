# MosaForge — Architecture v2

v2 (2026-10-06) treats the Mac and the Dell as **one heterogeneous compute pool**. It separates
*which model* (model routing) from *where it runs* (resource routing). Status per component:
**built**, **prototype** (code + tests, not yet in the request path), or **planned**. Evidence and
trade-offs are in [DECISIONS.md](DECISIONS.md) (ADR-015…019); the source comparison behind them
is in [RESEARCH.md §7](RESEARCH.md).

## 1. Components

```
 interfaces: CLI · Claude Code (MCP) · later voice/web
        │
 ┌──────▼───────┐  ┌─────────────────────┐
 │ 1 Task       │◀─│ 2 Context/Knowledge │  memory (FTS5), later Graphify code graph
 │   Analyzer   │  └─────────────────────┘
 └──────┬───────┘  TaskProfile: category, difficulty, tokens in/out, model calls,
        │          interactive?, parallelizable?, privacy
 ┌──────▼───────┐  ┌──────────────────┐
 │ 3 Model      │◀─│ 10 Privacy Policy│  hard rules on models (cloud) and nodes (LAN/secret)
 │   Router     │  └──────────────────┘
 └──────┬───────┘  acceptable models, quality order: [qwen3.5:4b, qwen3.5:9b…]
 ┌──────▼───────┐  ┌──────────────────┐  ┌──────────────────────────────┐
 │ 4 Resource   │◀─│ 11 Cost/Latency  │◀─│ Node monitors (live state):   │
 │   Manager    │  │    estimator     │  │ Mac: LocalMonitor (real)      │
 └──────┬───────┘  └──────────────────┘  │ Dell: Simulated → WorkerMonitor│
        │ Placement(model, node, est. latency, network, reason, confidence)
 ┌──────▼───────┐
 │ 5 Agent      │  tool-loop agents (general, coder); later DeerFlow/OpenHands runtimes
 │ Orchestrator │  tools always execute on the Mac (workspace jail)
 └──────┬───────┘
 ┌──────▼───────────────── 6 Provider abstraction ───────────────────────┐
 │ OllamaProvider (Mac)   WorkerProvider (Dell)   ClaudeCLIProvider (cloud) │
 └──────┬──────────────────────────┬─────────────────────────────────────┘
        │                          └─ 7 Worker abstraction: Dell worker (inference only, token, allowlist)
 ┌──────▼───────┐   fail → Model Router (next tier) → Resource Manager again
 │ 8 Verification│
 └──────┬───────┘
 ┌──────▼──────────────────────┐   ┌──────────────────────────┐
 │ 9 Memory / Experience Store │──▶│ 12 Evaluation / Learning │  runs: model, node, latency,
 └─────────────────────────────┘   └──────────────────────────┘  tokens, verdict → routing + perf tables
```

| # | Component | File(s) | Status | Responsibility | Borrowed from |
|---|---|---|---|---|---|
| 1 | Task Analyzer | `analyzer.py`, `router.classify` | prototype | Task → `TaskProfile` (category and difficulty via tev1; token, call and interactivity estimates) | — |
| 2 | Context / Knowledge | `memory.py` | built (memory); Graphify planned | Retrieval for prompts; code structure for scope estimates | Graphify (wrap, Ollama backend only) |
| 3 | Model Router | `router.py` (`route`, `acceptable`) | built | *Which model?* Tiers, experience adjustment, privacy for cloud. Never looks at node state | OpenJarvis router policies (later) |
| 4 | Resource Manager | `resources.py` | prototype | *Where?* Hard constraints, then cost minimization over live node state; batch scheduling | Ollama fit/evict, llama.cpp `--fit-target`, OmniRoute scoring/transparency |
| 5 | Agent Orchestrator | `orchestrator.py`, `agents.py` | built | Run agent, tools, escalation loop | — |
| 6 | Provider abstraction | `providers.py` | built | `chat`, `healthy`, `installed`, `decide` | — |
| 7 | Worker abstraction | `worker.py`, `WorkerProvider` | built, tested on loopback only | Inference-only LAN service: token, model allowlist, LAN-IP bind only, `/v1/status` with RAM/CPU | DeerFlow leases (planned heartbeat) |
| 8 | Verification Engine | `verify.py` | built | Deterministic checks certify; judge can only veto | — |
| 9 | Memory / Experience | `memory.py` (`runs`) | built | Every attempt with model, provider, node, latency, load, network time, LAN bytes, verdict | — |
| 10 | Privacy Policy | `router.check_privacy`, node `allow_secret` | built | Secret never reaches cloud or LAN nodes unless that node is explicitly allowed | — |
| 11 | Cost / Latency estimator | `ResourceManager.evaluate` | prototype | queue + load + prefill + generation + network, from per-node perf tables | — |
| 12 | Evaluation / Learning | `bench.py`, `runs` | built (eval); learning planned | Ground-truth suites; later perf-table updates and bandit policy from `runs` | OpenJarvis bandit/traces |

## 2. Model routing vs resource routing

| | Model Router | Resource Manager |
|---|---|---|
| Question | Which model is good enough for this task? | Where should that model run right now? |
| Inputs | category, difficulty, experience pass rates, privacy (cloud) | TaskProfile, node states (RAM, pressure, CPU, loaded/installed models, queue, RTT), perf tables, privacy (nodes) |
| Output | ordered list of acceptable models (quality order) | `Placement` for the first model that can be placed anywhere |
| Changes when | quality evidence changes | machine state changes (seconds) |

**Rule:** the Resource Manager never swaps the preferred model for a faster, weaker one. It takes the
list in quality order and places the first model it can. If no model can be placed, it reports
infeasible with the reason per node, and the Model Router (or the user) decides on a downgrade. This
keeps quality decisions measurable (Model Router, from `runs`) and placement decisions measurable
(Resource Manager: predicted vs actual latency).

Example: `Task → Model Router → qwen3.5:4b → Resource Manager → Mac` (interactive, Mac idle) versus
`… → qwen3.5:4b → Dell` (background, Mac pressure *warn*). Same model, different node.

## 3. Resource Manager decision policy

For every acceptable model × every node:

1. **Hard constraints** (reject, with a reason):
   - node unhealthy;
   - `privacy=secret` and the node is neither local nor `allow_secret`;
   - model in the node's `exclude_models` (e.g. `qwen3.5:9b` on the Mac);
   - model not installed;
   - no perf data;
   - not loaded and the node's pressure is *critical*, or *warn* on the interactive node (no new loads);
   - not loaded and the model doesn't fit the **headroom** (OS-reported available memory − `reserve_gb`), even after evicting idle models.
2. **Latency estimate** (ms): `queue` (busy time if all slots are taken) + `load` (if cold) +
   `prefill` (prompt tokens ÷ prefill rate, tool-loop calls re-prefill ~30 %) + `generate`
   (output tokens ÷ gen rate) + `network` (calls × (2·RTT + payload ÷ bandwidth) + probes).
3. **Objective** (lower wins):
   `score = w × latency + protection + eviction_reload`
   - `w = 1` for interactive work, `background_latency_weight` (0.3) for background work.
   - `protection` applies only to *background* work on the *interactive* node:
     `compute × protect[pressure] × (1 + inflight + cpu/100)`, with protect = 0.6 normal, 2.0 warn, ∞ critical.
   - `eviction_reload` = reload time of the models this load would evict (e.g. the tev1 router).
4. **Confidence** = ½·trust(chosen node's perf numbers) + ½·margin·trust(runner-up's numbers).
   Trust: measured 1.0, estimated 0.6, simulated 0.4. A decision that rests on simulated Dell numbers can't score above ~0.7.
5. **Batches** (independent tasks): longest first, each to the node with the **earliest estimated
   finish** (makespan objective). Background work may not queue more than `interactive_queue_cap_s`
   (15 s) on the interactive node, so the user never waits long behind batch work.

How the eight required behaviours map onto this policy (all covered by `tests/test_resources.py`):

| Case | Mechanism |
|---|---|
| 1 small interactive → Mac | w=1 and the Mac is faster; no protection for interactive work |
| 2 large background → Dell even if Mac RAM is free | protection + w=0.3. The Dell wins when it is at most ~2.5× slower than the Mac (Mac idle) or ~4× slower (Mac in use), per the sweep in BENCHMARKS.md §7 |
| 3 independent tasks → in parallel | `plan_batch` earliest-finish + interactive queue cap |
| 4 Mac pressure rising → heavy work moves | warn = no new loads on the Mac, protect ×2 |
| 5 Dell down → Mac if safe and feasible | unhealthy node rejected; Mac still subject to headroom |
| 6 sensitive → never remote unless allowed | `allow_secret`; infeasible rather than leak |
| 7 network > local time → stay local | network is part of the latency estimate |
| 8 model impractical on the Mac → Dell | `exclude_models` / headroom |

## 4. Data structures (`mosaforge/analyzer.py`, `mosaforge/resources.py`)

```python
TaskProfile(task, category, difficulty, privacy, interactive, parallelizable,
            prompt_tokens, output_tokens, model_calls)          # + payload_bytes
ModelPerf(gen_tok_s, prefill_tok_s, load_s, resident_gb, source)  # source: measured|estimated|simulated
NodeState(id, location, role, healthy, simulated, ram_total_gb, ram_used_gb, ram_avail_gb,
          swap_used_gb, pressure, cpu_pct, reserve_gb, loaded{name: GB}, installed, inflight,
          slots, busy_s, rtt_ms, bandwidth_mbps, allow_secret, exclude_models, perf{name: ModelPerf})
NodeMonitor.snapshot() -> NodeState   # LocalMonitor | SimulatedMonitor | (planned) WorkerMonitor
Placement(model, node, feasible, estimated_latency_ms, network_overhead_ms, breakdown_ms,
          score, confidence, reason, reasons[], alternatives[])
ResourceManager.place(profile, acceptable_models, nodes) -> Placement
ResourceManager.plan_batch(profiles, models_for, nodes) -> [Placement]
```

Configuration: `[resources]` weights and `[resources.nodes.<provider-id>]` (role, reserve, slots,
allow_secret, perf table, and for the Dell `simulated = true` until it is deployed) in `mosaforge.toml`.
Machine-specific overrides go in `~/.mosaforge/mosaforge.toml`.

Try it: `mosaforge place [--background] [--privacy secret] [--context-tokens N] [--batch N] "task"`,
or `mosaforge place --demo` for the eight cases.

## 5. Distribution levels

| Level | Meaning | Status |
|---|---|---|
| **A. Task-level** | Whole tasks placed on a node (one task = one model on one machine) | Resource Manager prototype; worker built |
| **B. Agent-level** | An agent's model calls run on the Dell while its tools run on the Mac; parallel agents on both machines | Worker protocol built (tools return to the caller), `plan_batch` for parallel tasks |
| **C. Model-level** | One model split across machines (llama.cpp RPC) | **Not implemented.** Experiment E-LAN-2 only |

First practical deployment:
- **Mac:** orchestration, routing, memory, verification, interactive agents, local Ollama (4B + tev1).
- **Dell:** worker + Ollama, independent models (4B, 9B), background, parallel and heavier tasks.

## 6. Memory

| Layer | Where | Written by | Read by |
|---|---|---|---|
| Working | the agent's message list | agent loop | agent |
| Project | `memories(scope=project, workspace=…)` | user (`memory add`), supervisor (`memory_add`) | retrieval, `memory_search` tool |
| Long-term | `memories(scope=long_term)` | user / supervisor | all workspaces |
| Skill | `memories(scope=skill)` + `skills/*/SKILL.md` (Phase 2) | user, later the learning engine | agents |
| Experience | `runs` (now with `load_s`, `network_s`, `bytes_lan`) + distilled `experience` memories | orchestrator, verifier, feedback | Model Router (`pass_rate`), `stats`; planned: perf tables for the Resource Manager |

Everything lives in `~/.mosaforge/mosaforge.db` (override with `MOSAFORGE_HOME`). No cloud memory, and
no second memory system.

## 7. Supervisor mode (Claude Code)

`.mcp.json` registers `mosaforge mcp`. Claude plans and reviews, and calls `delegate` for
self-contained subtasks (`.claude/skills/delegate-local`). In MCP mode MosaForge never escalates to
the cloud itself. Writes happen only with `allow_writes`.

## 8. Not built, deliberately

Model-level distributed inference, RL, automatic model pulls on nodes, a vector DB, a web UI, and
learned placement weights. Each one waits for a gate in [ROADMAP.md](ROADMAP.md).
