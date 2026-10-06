# Hardware capacity analysis

## Mac (measured 2026-10-04)
MacBook Pro Mac16,1 · Apple M4 (4P + 6E) · 16 GB unified memory · macOS 15.5 · ~159 GB free SSD.

- **Memory budget.** With the normal desktop load (IDE, browsers, other AI apps) free memory sat at
  25–50 %. The Mac stays responsive with **the 4B model (3.3 GB) plus the 0.8 GB router resident**.
  `qwen3.5:9b` is **excluded on the Mac** (`exclude_models`): loaded next to a normal desktop workload it
  pushed the Mac to ~15.1 GB used and ~5.8 GB swap (2026-10-04).
- **Measured speed** (Ollama 0.35.1, Metal): qwen3.5:4b ≈ 29 tok/s generation, ≈ 360 tok/s cold
  prefill. qwen2.5-coder:7b ≈ 19–21 tok/s. tev1:0.8b routing decision 0.12 s warm. Full table in BENCHMARKS.md §1.
- **Reading the Mac's memory correctly (2026-10-06).** "Used" includes the compressor (5.8 GB of 12.8 GB
  "used" that day) and is misleading. The Resource Manager uses macOS's own availability estimate
  (`kern.memorystatus_level`, the "free percentage" of `memory_pressure`) minus a 2 GB reserve, plus the
  pressure level (normal / warn / critical). At ~50 % available, a 3.3 GB model fits and a 6 GB one doesn't,
  consistent with the 9B swap episode (15.1 GB used, 5.8 GB swap; back to normal after unloading).
- **Recommended Ollama settings on the Mac:** `OLLAMA_MAX_LOADED_MODELS=2`, `OLLAMA_NUM_PARALLEL=1`,
  `OLLAMA_KEEP_ALIVE=10m`, `OLLAMA_FLASH_ATTENTION=1`, `OLLAMA_KV_CACHE_TYPE=q8_0`. Leave `OLLAMA_HOST` at its 127.0.0.1 default.

## Dell (not yet measured)
16 GB, CPU-only inference. Expectation to *verify* (E-HW-2): a 4B Q4 model at roughly 5–15 tok/s,
depending on the CPU and whether RAM is dual-channel. 7–9B models at a fraction of that. Up to ~12 GB
of model is possible because the Dell does nothing else. Its best role is **background and batch
work** (summaries, classification, test generation, overnight evaluation runs), keeping the Mac free
for interactive work.

**What the Dell must measure to earn each role** (from the Resource Manager break-even sweep, BENCHMARKS §7):
| qwen3.5:4b decode on the Dell | Role it earns |
|---|---|
| < ~7 tok/s | Runs models the Mac can't (9B) and absorbs overflow and parallel batch tasks only |
| ≥ ~7 tok/s | Also takes background work while you're using the Mac |
| ≥ ~12 tok/s | Also takes background work when the Mac is idle |
| ≥ ~22 tok/s | Batches can protect the Mac and still finish faster than the Mac alone |

## What the pair can and can't do
| Goal | Feasible now? |
|---|---|
| Two agents in parallel (one per machine) | ✅ built (worker) |
| Interactive chat/code with the 4B model | ✅ Mac (9B: Dell only) |
| Background batch on the Dell | ✅ once installed |
| A 20–30B model (qwen3.8 27B, qwen3.6 35B-A3B, gpt-oss 20B) | ❌ on either alone. Only via llama.cpp RPC across both (E-LAN-2), expected to be slow |
| Frontier MoE (GLM-5.x, Kimi K3, DeepSeek V4) | ❌ (Colibrì at < 1 tok/s at best, and needs 85 GB–1.6 TB of disk) |
| LoRA fine-tuning ≤ 4B | ✅ Mac via MLX (Soup / mlx-lm), Phase 5 |

## Android / tablet
Use as a UI client only (web UI, voice, monitoring). There is no evidence that a phone adds useful
inference capacity, so it is excluded from model execution.

## Purchase policy
No purchase until a gate in ROADMAP.md "Hardware gates" is met by a benchmark.
