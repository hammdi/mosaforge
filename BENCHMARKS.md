# MosaForge — Benchmarks

All numbers were measured on 2026-10-04 on the Mac (M4, 16 GB, macOS 15.5, Ollama 0.35.1, Metal), with
the normal desktop workload running (IDE, browser, other AI apps). Reproduce with `mosaforge bench speed`
and `mosaforge bench suite`. Raw JSON goes to `bench-results/`, runs to `~/.mosaforge/bench.db`.

## 1. Runtime speed (`bench speed`)

Prompt ≈ 750 tokens, 200 generated tokens, thinking off, temperature 0. "Cold" = model unloaded first.
Warm prefill numbers are inflated by Ollama's prompt cache (identical prompt), so read cold prefill for real throughput.

| Model | Resident | Cold load | Cold prefill | Gen tok/s (cold / warm) | Cold TTFT | Warm total for 200 tok |
|---|---|---|---|---|---|---|
| qwen3.5:4b | 3.3 GB | 1.1 s | 363 tok/s | 28.9 / 29.5 | 3.1 s | 5.5 s |
| qwen2.5-coder:7b | 5.0 GB | 0.9 s | 226 tok/s | 19.4 / 20.6 | 4.3 s | 4.3 s |
| qwen3.5:9b | 6.1 GB | 4.4 s | 197 tok/s | 15.1 / 16.1 | 8.2 s | 9.1 s |
| tev1:0.8b (decision) | 0.8 GB | — | — | 1 decision: **1.43 s cold, 0.12 s warm median** | — | — |

Memory pressure: system free memory fell to **15 %** with qwen3.5:9b loaded next to the desktop
workload, versus 25 % with the 4B model. **The 9B model is the practical ceiling on this Mac**, and it
shouldn't be kept resident while you work. Set `keep_alive` short for tier 3.

## 2. Ground-truth task suite (`bench suite`)

14 tasks in 5 categories (3 simple, 3 summarize/extract, 4 code with hidden unit tests, 3 arithmetic
reasoning with exact answers, 1 complex code). Every task has a deterministic grader in
[mosaforge/bench.py](mosaforge/bench.py). "Forced" = model fixed, no escalation. "Routed" = full
MosaForge pipeline (router + verification + escalation).

### 2.1 Quality and latency by model

| Config | Pass (ground truth) | Total latency | Tokens | Notes |
|---|---|---|---|---|
| qwen3.5:4b, no thinking (`local-small`) | **13/14 (93 %)** | 90 s | 9.8k | only miss: c3 `parse_duration` (input validation) |
| qwen2.5-coder:7b (`local-coder`) | 7/14 (50 %) | 91 s | 17.3k | failed French translation, 2 extractions, all 3 arithmetic problems; same code score as the 4B |
| qwen3.5:9b, thinking on, tools on | 12/14 (86 %) | **487 s** | 35.6k | c3 and x1 burned their 10-step tool budget exploring and never answered |
| qwen3.5:9b, thinking off (`local-strong`, current default) | 12/14 (86 %) | 142 s | 9.8k | missed c3, c4 |
| **ROUTED** (tev1 → local-small; escalate on `fail`) | **13/14 (93 %)** | **80 s** | 9.7k | router category accuracy 13/14 (x1 labelled `code`); no escalation fired (see §2.3) |

### 2.2 Verifier vs ground truth (first run, before ADR-008)

At that point the verifier could certify: compile check = pass, tev1 judge ≥ 0.5 = pass.

| | truly correct | truly wrong |
|---|---|---|
| verifier said pass | 20 | **8** |
| verifier said fail | 0 | **0** |

The judge and compile check **never caught a wrong answer** (0 of 8). The tev1 judge measures "is this
on-topic", not correctness. Consequence (ADR-008): only deterministic checks may return `pass`, the
judge can only veto, and escalation fires on `fail` alone. Separately, the very first end-to-end coding
run produced *no code at all* (qwen2.5-coder wrote its tool calls as text) and the judge still said
pass at p=0.97. That run is why the text-tool-call fallback and the "coding task produced no code → fail" rule exist.

### 2.3 What the strong-model and routed runs show

1. **On this suite, bigger was not better.** The 9B model didn't beat the 4B on any task (12/14 vs 13/14)
   and was 1.6× slower with thinking off, 5.4× slower with thinking on. The suite is too easy to separate
   the tiers, so the Phase 1 suite needs harder tasks. As it stands, the evidence doesn't justify keeping
   the 9B resident on the Mac.
2. **Thinking + tools on a 9B model is a trap on 16 GB.** It's slow and tends to wander through tools.
   After adding a forced final answer when the step budget runs out, x1 passed, but it took 287 s, and c3
   returned an empty answer after 8 tool calls. Default is now `think = false`. Thinking is reserved for
   future, explicitly budgeted reasoning runs.
3. **Routing matched the best single model at the lowest cost**: 93 % pass, 80 s total (6× faster than
   always-9B-with-thinking, 1.8× faster than always-9B-without), same token count as the 4B alone. The
   router itself added ~0.12 s per task.
4. **Escalation never triggered.** The one wrong routed answer (c3) compiled, so it was `unknown`, not
   `fail`. Without a test or a deterministic check, MosaForge cannot know it needs to escalate. That's
   the main argument for supplying `verify_cmd`, and for the E-V-1 verification experiments.
5. With the veto-only verifier (ADR-008), the strong-model run had 2 judged answers, both correctly `fail`
   (empty answers). Nothing wrong was certified.

Caveat: 14 tasks and one seed. These numbers show the mechanism works. They don't settle the research question.

## 3. LAN worker (loopback simulation)

The Dell wasn't reachable from this session, so the worker was run on the Mac (`127.0.0.1:8765`)
and called through `WorkerProvider` exactly as the Dell would be.

- Auth: no token → 401; wrong token → 401; non-allowlisted model → 403; unknown endpoint → 404;
  non-loopback bind without a ≥ 32-char token → refused at startup (unit tests + manual run).
- `ask --node dell --model local-coder "fib(n)…"` → answered in **3.1 s** at 15 tok/s, logged as node `lan`.
  Worker overhead over loopback is below measurement noise. Real LAN latency: E-LAN-1.

## 4. Integration checks

| Check | Result |
|---|---|
| MCP stdio: initialize → tools/list → `delegate` → `stats` | ✅ 6 tools listed; summarize task delegated to a local model, 3.1 s |
| Claude escalation via `claude -p` | ✅ 3.1 s round trip; cost logged from `total_cost_usd` |
| Privacy: forced cloud model with `privacy=secret` | ❌ **found a bypass** (a 2-letter test prompt reached Claude), fixed the same day: hard check in router + orchestrator, 3 regression tests |
| Tool jail: `..`, absolute paths, symlinks, `.env`, `.git`, `.ssh`, shell metacharacters | ✅ unit tests |
| Python 3.11 (Dell target) install + tests | ✅ `uv venv --python 3.11`, `pip install -e .`, 14 tests pass |

## 5. Experiment plan for the research question

> Can routing between local models, agents, machines and an optional frontier model beat "one frontier
> model for everything" on cost / privacy / resource efficiency, **at acceptable quality**?

**Arms** (same task set, 3 seeds each): A = always Claude (Sonnet via `claude -p`) · B = always
`local-strong` · C = always `local-small` · D = MosaForge routed, local-only · E = MosaForge routed
with cloud escalation on `fail`.

**Task set** (Phase 1): ~150 tasks, 30 per category, each with a deterministic grader. At least half
come from your real work (your repos' functions + tests, your documents for extraction).

**Metrics per run:** ground-truth pass · latency (p50/p95) · tokens · cloud $ · bytes sent off-device
(privacy) · Mac memory pressure during the run · energy (later: OpenJarvis telemetry/`powermetrics`).

**Decision rule (pre-registered):**
- *Yes* if E reaches ≥ 95 % of A's pass rate at ≤ 30 % of A's cloud cost, or D reaches ≥ 90 % with zero cloud.
- *No* if E needs > 60 % of A's cost to reach 95 % of A's quality. Then MosaForge's value reduces to
  privacy and offline use, and we say so.
- Report per category: the honest result is most likely "yes for simple/summarize/code-with-tests,
  no for complex reasoning". The router's job is to know which is which.

## 6. Pending experiments

| ID | What | Why |
|---|---|---|
| E-HW-2 | `bench speed` on the Dell (CPU) | Size the Dell's useful models |
| E-LAN-1 | Worker over Wi-Fi vs Ethernet: RTT, added latency, throughput with Mac + Dell in parallel | Validate agent-level distribution |
| E-LAN-2 | llama.cpp RPC: 20 GB-class model split Mac + Dell on an isolated link; tok/s | Only path to bigger models without buying hardware |
| E-LAN-3 | MLX ring / exo | Only if a second Apple Silicon machine appears |
| E-CC-1 | Whole Claude Code session on Ollama (`ANTHROPIC_BASE_URL=http://localhost:11434`) with qwen3.5:9b | Unsupported by Anthropic, but quantifies how far a local model gets inside a big harness |
| E-RT-1 | OpenHands SDK vs built-in coder on `complex_code` | ADR-009 |
| E-V-1 | Non-code verifiers: self-consistency, cross-model agreement, tev1:4b / nimble judge | ADR-008 follow-up |
| E-R-1 | Router: tev1:0.8b vs 4b vs nimble vs keywords on the 150-task set | ADR-004 |
| E-OL-40 | Ollama 0.40 (MLX engine) vs 0.35 (Metal/llama.cpp) | ADR-003 |

## 7. Resource Manager prototype (2026-10-06, simulated Dell)

Real Mac state (LocalMonitor: macOS available memory, pressure level, CPU, Ollama `/api/ps`)
plus a **simulated** Dell (CPU-only estimate: 4B at 7 tok/s gen and 45 tok/s prefill, 9B at 3.5 and 20,
RTT 5 ms, 200 Mbps). Every Dell number below is a hypothesis to be replaced by measurement.
A decision takes 45–60 ms, including a live Mac snapshot. `mosaforge place --demo`:

| Case | Decision | Est. latency | Why |
|---|---|---|---|
| 1 small interactive | 4B @ Mac | 5.2 s | 5× faster than the Dell estimate |
| 2a long background analysis, Mac idle | 4B @ Mac | 24.2 s | Dell estimate 169 s: too slow even with the protection penalty |
| 2b same, Mac in use | 4B @ Mac (close call) | 31.1 s | score 44 vs 51. Flips to the Dell if it's ≥ 25 % of the Mac's speed |
| 3 batch of 8 background tasks | 3 Mac + 5 Dell | makespan 167 s (all-Mac 62 s) | Mac queue cap 15 s. The Mac is free after 21 s |
| 4 Mac pressure *warn* | 4B @ Dell | 69 s | no new loads on the Mac at warn |
| 5 Dell down | 4B @ Mac | 11.8 s | only feasible node |
| 6 secret task, Mac busy | 4B @ Mac | 14.5 s | Dell not allowed secret tasks |
| 7 tiny task | 4B @ Mac | 5.3 s | network adds 20 ms on the Dell, but its compute is the real difference |
| 8 complex code (9B) | 9B @ Dell | 440 s | the 9B is excluded on the Mac. Estimate shows a CPU 9B is very slow |

**Break-even sweep.** Dell speed as a fraction of the Mac's (gen and prefill scaled together):

| Dell speed (4B gen) | interactive | background, Mac idle | background, Mac in use | batch of 8: capped / uncapped / all-Mac |
|---|---|---|---|---|
| 15 % (4.4 tok/s) | Mac | Mac | Mac | 225 / 48 / 62 s |
| 25 % (7.3 tok/s) | Mac | Mac | **Dell** | 136 / 48 / 62 s |
| 40 % (11.7 tok/s) | Mac | **Dell** | Dell | 86 / 41 / 62 s |
| 75 % (21.9 tok/s) | Mac | Dell | Dell | 47 / 34 / 62 s |

Reading:
- Even a slow Dell shortens an uncapped batch by ~23 % (it takes one or two tasks).
- Protecting the Mac's responsiveness costs throughput unless the Dell reaches ~75 % of the Mac's speed.
- Network overhead (tens of ms) never decides anything on a LAN. Compute speed and model residency do.
- **The Dell's measured 4B decode speed is the number that decides its role:**
  - < ~7 tok/s: it can only run models the Mac can't, and absorb overflow.
  - ≥ ~7 tok/s: it takes background work while you use the Mac.
  - ≥ ~12 tok/s: it takes background work even when the Mac is idle.

### Plan for the real Dell (replaces E-HW-2 / E-LAN-1)

| ID | Measure | Validates |
|---|---|---|
| E-RM-1 | Predicted vs actual latency per placement (A/B/C runs via `bench lan`), error per term (load, prefill, gen, network) | the cost model; replaces simulated perf with measured |
| E-RM-2 | Interactive slowdown on the Mac while a background 4B inference runs on the Mac vs on the Dell (latency of a fixed interactive task, Mac CPU, swap) | the protection weights (ADR-018) |
| E-RM-3 | 8–14-task batches: makespan and Mac-busy time, capped vs uncapped vs all-Mac | queue cap value |
| E-RM-4 | Mac memory pressure and swap during the A/B/C runs (sampled every 1 s) | headroom rule (ADR-017) |
| E-RM-5 | Fault cases: Dell killed mid-task, token wrong, model missing | fallback and error reporting |
