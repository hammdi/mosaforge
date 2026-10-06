# MosaForge AI

> **One assistant. Multiple minds. Your infrastructure.**

MosaForge is a local-first, model-agnostic personal AI system. It decides **which model, which agent
and which machine** should handle a task, checks the result, escalates only when needed, and
remembers what worked, all on hardware you own. Cloud models (Claude, OpenAI…) are optional,
high-value resources, not dependencies.

It is not "a local Claude". Its value is systemic: routing, delegation, verification, memory and
measured efficiency. It reuses mature open-source components wherever they are strong
([COMPONENTS.md](COMPONENTS.md)) and only builds the glue that nobody else provides.

## Status: v0.1 POC (2026-10-04)

Working today on an M4 MacBook Pro (16 GB) with Ollama 0.35:

```
task ─▶ router (tev1:0.8b decision model, ~0.12 s) ─▶ agent (general | coder, jailed tools)
     ─▶ model via Ollama (Mac) or MosaForge worker (Dell, LAN) ─▶ verification ─▶ escalate on fail
     ─▶ memory + experience log (SQLite, ~/.mosaforge)
Claude Code ──MCP──▶ mosaforge (delegate cheap subtasks to local models)
```

## Quick start

```bash
# prerequisites: Python >= 3.11, Ollama >= 0.35
ollama pull tev1:0.8b && ollama pull qwen3.5:4b && ollama pull qwen3.5:9b
pip install -e .            # or: python3 -m mosaforge.cli …  (no third-party deps)

mosaforge models                                   # providers, health, installed models
mosaforge route "Write a function that merges intervals"
mosaforge ask "Summarize: …"                       # route → agent → verify → remember
mosaforge ask --verify "python3 -m pytest tests/test_x.py" "Fix the bug in x.py"
mosaforge memory add --scope long_term "I prefer answers in French"
mosaforge stats                                    # pass rate / latency / cost per category × model × node
mosaforge bench speed && mosaforge bench suite     # measure on your hardware
```

**Claude Code as supervisor:** open this repo in Claude Code. `.mcp.json` registers the `mosaforge`
MCP server and `.claude/skills/delegate-local` tells Claude when to delegate.

**Dell as LAN worker:**
```bash
# on the Dell (Ollama stays on 127.0.0.1)
export MOSAFORGE_WORKER_TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')
mosaforge worker --host <dell-lan-ip> --models qwen3.5:4b,qwen3.5:9b
# on the Mac: ~/.mosaforge/mosaforge.toml
#   [providers.dell]
#   base_url = "http://<dell-lan-ip>:8765"
#   enabled = true
export MOSAFORGE_WORKER_TOKEN=<same token>
mosaforge ask --node dell "…"
```

## Documents
| | |
|---|---|
| [RESEARCH.md](RESEARCH.md) | Verified findings on Claude Code, runtimes, agent frameworks, Jev, models, LAN options |
| [COMPONENTS.md](COMPONENTS.md) | Every external project: role, verdict, licence, integration, risk |
| [ARCHITECTURE.md](ARCHITECTURE.md) | The architecture as built, and where plug-ins go |
| [DECISIONS.md](DECISIONS.md) | ADRs with their evidence |
| [BENCHMARKS.md](BENCHMARKS.md) | Methodology, first measurements, and the experiment plan for the research question |
| [ROADMAP.md](ROADMAP.md) | Phases and measurable gates |
| [HARDWARE.md](HARDWARE.md) · [SECURITY.md](SECURITY.md) | Capacity analysis · threat model |

## Research question
*Can a local-first, distributed, model-agnostic system achieve a better overall cost/privacy/
resource-efficiency profile than sending every task to a single frontier model?* We don't assume yes.
BENCHMARKS.md §5 defines the experiment that can show either answer.

## Open questions for you
1. **Which "Spark" did you mean?** X33834/spark (local coding agent, rejected for security reasons),
   NVIDIA DGX Spark (hardware), Meta Muse Spark (model), or something else?
2. **Which "Jarvis"?** OpenJarvis (Stanford, Apache-2.0, a full framework) or isair/jarvis (voice app,
   non-commercial licence)? Both are evaluated.
3. **ADR-009:** at Gate 2, should MosaForge move onto OpenJarvis as a base, or keep its thin core?
