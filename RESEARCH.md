# MosaForge — Research notes

Status: 2026-10-04. Every claim below comes from an official repository, its docs, or a source file
under `upstream/` (pinned commits in [upstream/MANIFEST.md](upstream/MANIFEST.md)). Where the
baseline report (`MosaForge_AI_Mini_Rapport_FR.pdf`) or the poster disagrees with the sources, the
**Correction** lines say so. Measurements on our own hardware are in [BENCHMARKS.md](BENCHMARKS.md).

## 0. What the baseline documents got right and wrong

| Baseline claim | Verdict |
|---|---|
| Fragmentation, not a lack of components, is the problem | **Confirmed.** Every project below solves one slice and most of them grow their own orchestrator, memory and skill system. |
| Agent parallelism before model parallelism on Mac + Dell | **Confirmed in principle, still to be measured on the LAN.** llama.cpp RPC is "fragile and insecure" (its own README), MLX distributed needs Apple Silicon on every node, and exo targets Macs. See §6. |
| "Jev → routing" | **Partly right.** TypeSafe's Jev is a *hosted* API (early access, no weights). Its API shape is now implemented locally by Ollama 0.35 (`/v1/systemone`) with open decision models (tev1, nimble, clef). We use the API, not the service. |
| "Jarvis → voice/personal assistant" | **Ambiguous name.** Two serious candidates: OpenJarvis (Stanford, Apache-2.0, a full local-first agent framework) and isair/jarvis (voice app, non-commercial licence). See §3.4. |
| "Spark" | **Ambiguous name.** No well-known AI project called Spark matches the role. The best match found is X33834/spark (local coding agent, 11 days old, 0 stars). NVIDIA DGX Spark (hardware) and Meta Muse Spark (model) also exist. **Needs confirmation from you.** |
| Claude Code can supervise local models directly | **Not directly.** Subagents cannot use a different provider or base URL than the main session, and Anthropic does not support routing Claude Code to non-Claude models. The clean path is MCP (§2). |
| MLX: distributed inference over Thunderbolt 5 RDMA | **Confirmed** (JACCL backend in `mlx/docs/src/usage/distributed.rst`), but irrelevant for a Dell node. |
| Ollama exposes an OpenAI-compatible API | **Confirmed**, plus an Anthropic Messages-compatible API. However the `/v1` endpoint drops or flattens thinking output (DeerFlow `config.example.yaml`, ollama#15293), so MosaForge uses the native `/api/chat`. |

## 1. Hardware facts (measured on this machine)

- Mac: MacBook Pro `Mac16,1`, **Apple M4** (4P + 6E cores), **16 GB** unified memory, macOS 15.5,
  ~159 GB free SSD. Ollama 0.35.1 installed. With the IDE, browser and other agents open, free memory
  was **25–50 %**, so a realistic model budget is **≤ 6–7 GB resident** if the Mac is to stay responsive.
- Dell: 16 GB, no useful GPU. Not reachable from this session, so nothing on the Dell is measured yet.
  The worker was tested over loopback on the Mac (BENCHMARKS.md §3).

## 2. Claude Code (v2.1.179 installed; docs at code.claude.com)

| Capability | Finding | Source |
|---|---|---|
| Subagents | Markdown + YAML frontmatter; `model` accepts `sonnet`/`opus`/`haiku`/`fable`, full Claude IDs or `inherit`; `tools`/`disallowedTools`, `permissionMode`, per-agent `mcpServers`, `hooks`, `memory` (user/project/local), `isolation: worktree`, `background`, nesting up to 3 levels | docs/en/sub-agents |
| Per-agent provider | **No.** "Subagents cannot use a different provider or `ANTHROPIC_BASE_URL` than the main session" | docs/en/sub-agents |
| Local/third-party models | Any gateway speaking the Anthropic format works through `ANTHROPIC_BASE_URL`, but "Anthropic … doesn't support routing Claude Code to non-Claude models through any gateway". Ollama's Anthropic-compatible endpoint makes it *technically* possible to run a whole session on a local model (unsupported). | docs/en/llm-gateway, ollama docs/api/anthropic-compatibility |
| Parallel work | Subagents, agent view, agent teams (experimental), dynamic workflows, projects. "In every approach the workers are Claude sessions. To involve a different tool, expose it to Claude as an MCP server." | docs/en/agents |
| Hooks | ~30 events (PreToolUse can allow/deny/ask/rewrite input; SubagentStart/Stop; PreModelSwitch …); types: command, http, mcp_tool, prompt, agent | docs/en/hooks |
| Skills / plugins / mods | Skills = SKILL.md folders. Plugins bundle skills, agents, hooks, MCP servers. **Mods** (new) are JS hook modules that can register tools/commands, draw panes, call `$.model.complete` (Claude only, user's credentials) and `$.http.fetch` any URL. | docs/en/plugins/mods/api |
| Headless | `claude -p --output-format json --model … --tools "" --no-session-persistence`; reports `total_cost_usd` and usage | `claude --help` |
| Sandbox | OS-level (Seatbelt on macOS, bubblewrap on Linux) for **Bash only**; MCP servers, hooks and file tools run outside it | docs/en/sandboxing |

**Conclusion.** Claude Code works well as an optional supervisor if local models are exposed as
**MCP tools** (`delegate`, `route`, `memory_*`, `feedback`, `stats`). That is what the POC ships
(`mosaforge mcp`, `.mcp.json`, `.claude/skills/delegate-local`). A mod could do the same with a
richer UI (a pane showing routing decisions). Revisit that once the MCP path is validated.

## 3. Projects

Legend: ✅ yes · ⚠️ partial or with caveats · ❌ no.

### 3.1 Runtimes (ModelProvider candidates)

| | **Ollama** | **llama.cpp** | **MLX / mlx-lm** | **Colibrì** | **exo** |
|---|---|---|---|---|---|
| Repo / licence | ollama/ollama, MIT | ggml-org/llama.cpp, MIT | ml-explore/mlx + mlx-lm, MIT | JustVugg/colibri, Apache-2.0 | exo-explore/exo, Apache-2.0 |
| Activity | very active (v0.35.1 2026-09-29; v0.40 rc) | very active | very active | very active (created 2026-07-01, 39k★) | active |
| Purpose | Model manager + server | C/C++ inference engine, `llama-server` | Apple array framework, LLM serving and fine-tuning | Streams MoE experts from disk to run 284B–2.8T models with 16–32 GB RAM | Distributed inference across devices |
| Apple Silicon | ✅ (Metal; **MLX engine by default from v0.40**) | ✅ Metal | ✅ native | ⚠️ experimental Metal tier | ✅ primary target |
| Linux CPU (Dell) | ✅ | ✅ | ⚠️ CPU backend exists, not its strength | ✅ | ⚠️ |
| OpenAI-compatible API | ✅ `/v1` (+ Anthropic `/v1/messages`) | ✅ `llama-server` | ✅ `mlx_lm.server` | ✅ `openai_server.py` | ✅ |
| Tool calling | ✅ native `tool_calls` (quality model-dependent, §BENCHMARKS) | ✅ (grammar/Jinja) | ⚠️ | ⚠️ per-engine | ⚠️ |
| Decision models | ✅ `/v1/systemone` (Jev API) since 0.35 | ❌ | ❌ | ⚠️ "Brio mode" (closed-question scoring) | ❌ |
| Distributed | ❌ | ⚠️ RPC backend: "fragile and insecure. **Never run the RPC server on an open network**" | ✅ ring (TCP), JACCL (RDMA over Thunderbolt), MPI, NCCL; Apple-only in practice | ⚠️ expert workers on other Macs over TCP | ✅ |
| Auth | ❌ none on the API; binds 127.0.0.1 by default | ⚠️ `--api-key` | ❌ | ❌ coordinator example binds 0.0.0.0 | ❌ |
| Fit for 16 GB | ✅ 0.8–9B models | ✅ | ✅ | ❌ for interactive use: **0.05–0.1 tok/s** on a 25 GB box (own README), GLM-5.2 needs 372 GB disk | ⚠️ |

### 3.2 Agent runtimes and harnesses

| | **DeerFlow 2** | **OpenHands** | **OpenJarvis** | **ECC** | **Spark (X33834)** |
|---|---|---|---|---|---|
| Repo / licence | bytedance/deer-flow, MIT | OpenHands/OpenHands (Agent Canvas) + OpenHands/software-agent-sdk, MIT | open-jarvis/OpenJarvis, Apache-2.0 | affaan-m/ECC (formerly everything-claude-code), MIT | X33834/spark, MIT |
| Activity / maturity | 83k★, 2.0 rewrite (Feb 2026), very active | 90k★, SDK reports SWE-Bench 77.6 (with frontier models); Agent Canvas is **beta** | 10.5k★, Stanford Hazy Research, paper arXiv:2605.17172 | 273k★, v2.2.3 (2026-10-01) | **0★, created 2026-09-23** |
| What it is | LangGraph "super agent harness": sub-agents, sandbox, DeerMem memory, skills, MCP, IM channels, scheduler | Coding agent SDK (Python/TS/REST), agent-server, Docker/K8s workspaces; Canvas runs OpenHands, Claude Code, Codex via ACP | Local-first personal AI framework: engines, agents, memory, **router policies (heuristic, learned, bandit, GRPO)**, traces, energy/cost telemetry, evals, LoRA | Config pack: 68 agents, 293 skills, 94 commands, hooks, rules, memory vault, AgentShield scanner | Local coding agent with approval gate, injection flagging, FTS5 memory, MCP, web terminal |
| Size | 223k Python LOC (backend), 60+ deps, Python 3.12+, Node 22+, Docker recommended | 155k LOC (SDK), LiteLLM, Docker for sandbox | 691 Python files + Rust extension | 2.7 MB of SKILL.md text | 11k LOC |
| Hardware | 8 GB RAM minimum, 16 GB recommended, *before* any local model | Docker + model | Ollama + model | none | none |
| Local models | ✅ ChatOllama; but recommends 100k+ context, strong tool use, reasoning: **beyond 4–9B models** | ✅ via LiteLLM (`ollama_chat/…`) | ✅ Ollama-first | via harness | ✅ Ollama + Chinese APIs |
| MCP | ✅ | ✅ | ✅ | ✅ (configs) | ✅ |
| Memory | ✅ DeerMem | ⚠️ | ✅ | ✅ Memory Vault (Markdown) | ✅ FTS5 |
| Multi-agent | ✅ | ✅ | ✅ | ✅ (via Claude Code) | ⚠️ 2 sub-agents |
| Security notes | Designed for 127.0.0.1; "Gateway admin is equivalent to code execution"; LAN exposure needs ACL + auth gateway | "Without a sandbox … full access to your filesystem" | injection scanner, subprocess sandbox, SSRF guard | hooks execute code; AgentShield scanner | **Binds 0.0.0.0 with no token by default**, exposing a browser terminal to the LAN; token accepted in the query string, non-constant-time compare (`spark2/cli.py`, `web/api_common.py`) |
| Overlap with MosaForge | orchestrator, memory, skills, sandbox | coding agent, sandbox, remote workers | **nearly everything**: router, traces, eval, learning, memory | skills/agents/memory/eval | coding agent, memory |

### 3.3 Decision models and Jev

- **TypeSafe Jev** (released 2026-09-15): a "System One" model that returns typed answers
  (`choice` / `noul` = boolean probability / `score`) with calibrated probabilities and decodes no text.
  Hosted API only (`console.typesafe.ai`, early access, $42 per billion input tokens). No weights,
  no self-hosting. For a local-first system it can only be an optional cloud component.
- **Ollama 0.35 `/v1/systemone`** implements the Jev API locally. Models: **tev1** (Together AI,
  fine-tuned from Qwen3.5, 0.8B and 4B, MIT training code), **nimble** (Bespoke Labs, 9B), **clef / clef-flash**
  (Cloudflare, 27B / 9B, multimodal). `ollama show` reports the capability as `decision`.
- **AgentJev-0.6B** (malevrigns/agent-jev, Apache-2.0, 2 weeks old): a community open-weights
  re-implementation on Qwen3-0.6B. Reports 79.25 % top-1 against teacher labels on "Typed Decisions"
  (not a measured agent success rate). Needs torch + transformers, is not packaged for Ollama, and its
  weights are published under a different account (`aimeigaoshou`). Watch it, don't adopt it yet.
- **Measured here:** tev1:0.8b classified 5 of 5 probe tasks correctly, in **0.12 s warm median**
  (1.4 s cold). Suite accuracy is in BENCHMARKS.md.

### 3.4 Personal assistant / voice ("Jarvis")

- **OpenJarvis** (open-jarvis/OpenJarvis, Apache-2.0). Built on the "Intelligence Per Watt" research,
  which reports that local models already handle 88.7 % of single-turn chat and reasoning queries
  (their claim, their paper). Same thesis as MosaForge. Ships presets (morning digest with TTS, deep
  research, code assistant), skills in agentskills.io format, and a learning loop over traces.
  **It is the closest existing system to MosaForge's vision** (see DECISIONS ADR-009).
- **isair/jarvis**: polished voice-first desktop app (wake word, Whisper STT, TTS, MCP, local diary
  memory), works with Ollama or any OpenAI-compatible server. Licence: **non-commercial, share-alike**
  ("Jarvis AI Assistant License"). Use it as a separate app pointed at MosaForge's endpoint. Never copy its code.

### 3.5 Training: Soup

MakazhanAlpamys/Soup (Apache-2.0, `pip install soup-cli`, v0.75): YAML-driven SFT/LoRA/DPO/GRPO
with **an MLX backend**. Layer streaming fits an 8B NF4 LoRA into 3.32 GB VRAM on an RTX 3050
(their measurement, flagged as being re-measured). Python **3.10–3.12 only**. Many forks with an
identical description exist (e.g. `vaputa/Soup`, `EchoJohn/Soup`). Always install from PyPI or the
canonical repo. Role: Phase 5, after we have a verified-experience dataset.

### 3.6 Models (sizes from ollama.com/library; licences from Hugging Face)

| Family | Local-viable here (≤ ~7 GB) | Too big for 16 GB (local) | Licence |
|---|---|---|---|
| **Qwen** | qwen3.5 0.8B/2B/4B (3.3 GB)/9B (6.6 GB); qwen2.5-coder 7B (4.7 GB) | qwen3.8 27B (18 GB), qwen3.6 27B/35B-A3B, qwen3-coder 30B-A3B (19 GB) | Apache-2.0 |
| **DeepSeek** | deepseek-r1 8B (Qwen3 distill, 5.2 GB) | V4 Flash 284B, V4.1 Flash 552B (Colibrì only, at < 1 tok/s) | MIT |
| **Kimi** | — | K3 2.8T (1.6 TB disk) | modified ("kimi-k3") |
| **GLM** | — | GLM-4.7-Flash (19 GB), GLM-5.3-Flash 321B, GLM-5.2/5.3 744B | MIT (5.3: custom) |
| **Gemma 4** | e2b (4.6 GB), e4b (6.6 GB), 12B (7.7 GB, tight) | 26B, 31B | Gemma terms (verify before shipping) |
| **Others** | ministral-3 3B/8B (Apache-2.0), granite4 micro/tiny (Apache-2.0), nemotron-3-nano 4B, gpt-oss 20B (14 GB: Dell only, tight) | muse-glimmer 30B (18 GB), devstral-small-2 24B | various |
| **Decision** | tev1 0.8B (0.8 GB) / 4B (2.7 GB q4) | nimble 9B fits; clef 27B doesn't | — |
| **Embeddings** | embeddinggemma 300M, qwen3-embedding 0.6B, nomic-embed-text | — | — |

Kimi, GLM-5.x and DeepSeek V4 are frontier-class open models. On this hardware they are reachable only
(a) through their vendors' cloud APIs (a privacy decision), (b) through Ollama `:cloud` tags (also cloud),
or (c) through Colibrì at well under 1 tok/s. They are **not** a realistic local tier until a RAM-heavy server exists.

## 4. Responsibility map: who should own what

| Responsibility | Owner | Why |
|---|---|---|
| Model serving (Mac, Dell) | **Ollama** | Simplest, native tool calling, decision models, MLX engine coming, keeps 127.0.0.1 default |
| Experimental / alternative serving | llama.cpp `llama-server`, `mlx_lm.server` | Same OpenAI-compatible adapter; adopt when a benchmark beats Ollama |
| Routing decision primitive | **Ollama `/v1/systemone` + tev1** | Local, 120 ms, typed and calibrated, Jev-API compatible (swap to Jev/nimble/clef) |
| Routing policy, escalation, experience | **MosaForge** | Nobody else does it for *our* nodes and *our* cost model. OpenJarvis has the closest design (borrow ideas). |
| Supervisor (optional) | **Claude Code** via MCP | Strong planner/reviewer; MCP is the documented integration point |
| Simple tool-loop agents | **MosaForge** (≈150 LOC) | Small models need a tiny prompt and few tools. The big harnesses' prompts alone overflow them. |
| Long-horizon research | DeerFlow (later, external service) | Needs ≥100k context and strong models: cloud or a future server |
| Complex coding in a sandbox | OpenHands SDK (later, adapter) | Docker workspaces, mature agent; evaluate against the built-in coder |
| Skills / workflows | **SKILL.md format**, cherry-picked from ECC | One standard shared by Claude Code, OpenJarvis, DeerFlow, ECC |
| Memory | **MosaForge SQLite/FTS5** | One store, under user control, exportable. Do not run DeerMem + ECC vault + OpenJarvis memory side by side |
| Voice / UI | isair/jarvis (as a client) or OpenJarvis presets | Later; both can point at an OpenAI-compatible endpoint |
| Fine-tuning | Soup (MLX backend) | Phase 5 |
| Huge MoE experiments | Colibrì | Research track only |

## 5. Security research summary

See [SECURITY.md](SECURITY.md). The two concrete hazards found in upstream code are Spark's default
`0.0.0.0` binding without a token, and llama.cpp RPC's own warning. Two structural hazards apply to
every candidate: hooks and stdio MCP servers execute arbitrary code (ECC, DeerFlow admin, Claude Code
plugins), and agent tools must treat file and web content as data (prompt injection).

## 6. LAN strategy: evidence

- **Agent parallelism**: each request goes to one machine. Network cost is one HTTP round trip of a
  few KB per model call. Measured overhead through the worker on loopback: ≈0 s added to a 3.1 s call.
  This works with *any* runtime on the Dell.
- **Model parallelism options for Mac + Dell**:
  - MLX distributed: every rank runs MLX; the Dell (x86, no Apple GPU) would be a CPU rank and the
    slowest link in the ring. Not viable.
  - exo: Apple-first. Not viable for this pair.
  - llama.cpp RPC: viable on paper (`rpc-server` on the Dell, `--rpc` from the Mac), the only way to load
    a 15–20 GB model across both. But it is unauthenticated, self-described as insecure, and each token
    crosses the network once per layer split; decode speed would be bounded by the Dell's DDR bandwidth
    and the LAN. Experiment E-LAN-2 in BENCHMARKS.md measures it on an isolated link.
- **Conclusion for now:** agent-level distribution. Model-level only if E-LAN-2 shows a 20 GB-class
  model at ≥ 5 tok/s *and* that model beats `qwen3.5:9b` on the suite by a margin that matters.

## 7. Routing and resource scheduling in existing projects (source-level, 2026-10-06)

Question: does anything already decide **which model** *and* **which machine** under live system state?
Sources: `upstream/` (OmniRoute `23a11484` and Graphify `5c7b847` added to MANIFEST), Claude Code docs.

| Project | What it already solves (where in the source) | What it does not solve | Reuse | Wrap | MosaForge owns | Duplicated if we rebuilt |
|---|---|---|---|---|---|---|
| **Ollama** | Single-host placement: `server/sched.go` loads a model only if it fits `availableMemoryForLoad()` (iGPU: min of GPU free and *system* free), evicts idle runners (`findRunnerToUnload`) when `OLLAMA_MAX_LOADED_MODELS` is reached; `NUM_PARALLEL`, `keep_alive`, auto-shrinks `num_ctx` on OOM | "Fits" means *fits right now*, with no reserve for the user's apps (that's how the 9B pushed the Mac into 5.8 GB of swap). No multi-host view, no notion of interactive vs background, no cost model | Its loading/eviction on each node; `/api/ps` (resident size), `/api/tags` | via `OllamaProvider` (exists) | Whether a model *should* be loaded on a node at all (reserve, pressure, role) | per-host loader, quantization, KV management |
| **llama.cpp** | `llama-server --fit on --fit-target 1024` (MiB margin per device) auto-sizes params to fit; slots (`--parallel`), `/slots`, `/health`, Prometheus `/metrics`; RPC for model-level splitting | Single host; RPC is unauthenticated "proof of concept" | `--fit-target`'s **margin idea** = our `reserve_gb`; `/metrics` + `/slots` as telemetry if it becomes a provider | OpenAI-compatible provider (Phase 2) | Cluster-level margin and placement | per-device fitting |
| **MLX / mlx-lm** | `maybe_set_recommended_wired_limit`, LRU prompt cache, distributed ring/JACCL across Macs | Apple-only; no scheduler | wired-limit awareness when an MLX provider exists | provider later | — | — |
| **OpenJarvis** | `engine/multi.py` routes by *which engine owns the model name*; `ModelSpec.min_vram_gb`, `active_parameter_count_b`; heuristic/learned/bandit router policies; energy/GPU telemetry (`telemetry/energy_apple.py`…); A2A server for agent-to-agent tasks | No cross-node placement, no RAM/pressure-aware choice, no interactive protection | `min_vram_gb`-style model metadata, bandit policy (Phase 4), Apple energy telemetry, A2A as a candidate task protocol | — | Resource Manager | engine registry, router policy classes |
| **OmniRoute** (MIT, 73k★, TypeScript/Next.js, 883 MB clone) | Cloud/provider gateway. `open-sse/services/autoCombo/scoring.ts`: 16-factor weighted score (health from circuit breaker, quota, cost, p95 latency, latency stability, task fit, quality feedback…). 19 combo strategies (priority, weighted, p2c, least-used, cost-optimized, LKGP…). 3-layer resilience: provider circuit breaker, per-key cooldown with backoff, per-model lockout. `X-OmniRoute-Decision` header explains each choice | **No host resources at all** (no RAM/CPU/load/model residency). Accounts and quotas, not machines. Ships "TLS stealth" and proxy evasion of provider blocks (ToS/legal risk) and affiliate links | **Patterns:** circuit breaker + cooldown for worker health; p95 latency + stability as score inputs; decision transparency (our `reason` + `considered`); task-fit × tier composition | Possibly as the *cloud* tier gateway later, if multi-provider cloud is ever wanted (privacy review first) | Everything local | provider failover logic (if we adopted it wholesale we'd import a second router) |
| **Graphify** (Apache-2.0, 124k★) | Context/knowledge layer: tree-sitter AST → `graph.json` + `GRAPH_REPORT.md`, offline for code; docs/media need an LLM backend (supports Ollama); MCP server; Claude Code/Codex install via skills + PreToolUse hooks | Not a router or scheduler | As the **Context / Knowledge Layer** for code tasks; its graph can feed the Task Analyzer (files/symbols a task touches → scope estimate) | CLI/MCP tool, used with `--backend ollama` only (privacy) | Task Analyzer | code indexing |
| **Claude Code** | Concurrency caps (`CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`, spawn depth), background subagents, per-subagent Claude model, worktree isolation | Workers are always Claude sessions; no hardware awareness | Supervisor via MCP (exists) | — | — | — |
| **DeerFlow** | Concurrency limits (`subagents` batch concurrency 3, `max_total_per_run`, `queue_timeout_seconds`), Redis **leases with heartbeat + TTL** for run/sandbox ownership across gateway workers, sandbox provisioner | Model choice is static config; no hardware-aware placement | **Lease/heartbeat pattern** for worker liveness and in-flight ownership | AgentRuntime later (unchanged) | — | job ownership |
| **OpenHands** | Agent-server backends (local, Docker, VM, cloud) selectable in Agent Canvas; ACP to run other agents | Backend choice is manual | Remote agent-server idea for a future GPU node | AgentRuntime later | — | — |

**Conclusion.** Per-host loading (Ollama, llama.cpp `--fit`) and provider scoring (OmniRoute) exist.
**Nobody decides which of my machines should run this model, given the user is working on one of
them.** That is MosaForge's to own, and it is small. The design borrows explicitly from the above:
Ollama's fit/evict semantics (we never fight its loader, we just don't send it work it would swap for),
llama.cpp's per-device margin (`reserve_gb`), OmniRoute's health/latency factors and decision
transparency, DeerFlow's lease/heartbeat for liveness, and OpenJarvis's model metadata and telemetry.
