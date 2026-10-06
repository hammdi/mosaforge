# MosaForge — Threat model and security strategy

## Assets
Source code and documents in workspaces · credentials (SSH keys, `.env`, API keys, Claude login) ·
private memory (`~/.mosaforge/mosaforge.db`) · the machines themselves (Mac, Dell) · your cloud spend.

## Threats and controls

| # | Threat | Control in the POC | Planned |
|---|---|---|---|
| T1 | **LAN attacker reaches an agent and gets a shell or files** | The worker exposes inference only (`/v1/chat`); there is no exec/file endpoint (test `test_no_other_endpoints`). Bearer token ≥ 32 chars, constant-time compare. Refuses non-loopback bind without a token. Ollama stays on 127.0.0.1 | TLS (Caddy or Tailscale), per-client tokens, IP allowlist on the Dell firewall |
| T2 | Stolen worker token used to run arbitrary or huge models | Per-worker model allowlist (403 otherwise), body cap 2 MB, `num_ctx` capped at 32k, concurrency semaphore | Rate limiting; token rotation |
| T3 | **Prompt injection** via files, tool output or web pages | Tool output wrapped in `<tool_output>` and declared to be data. Read tools can't reach `.git`, `.ssh`, `.env*`, keys, or `upstream/`. Mutating tools only for the coder profile, disabled for cloud models and by default over MCP | Taint tracking: block `write_file`/`run_command` after untrusted content unless confirmed. Port OpenJarvis `injection_scanner` ideas |
| T4 | Agent runs destructive commands | No shell: argv-only, prefix allowlist (`python3 -m py_compile/pytest/unittest`), metacharacters refused, 60 s timeout, `cwd` = workspace, minimal env (`HOME` = workspace) | macOS `sandbox-exec` / Linux bubblewrap around `run_command`; Docker for OpenHands runtime |
| T5 | Path traversal / symlink escape | `resolve()` + parent check after symlink resolution (tests cover `..`, absolute paths, symlinks) | — |
| T6 | Allowlisted interpreter does too much (`pytest` executes test code the agent wrote) | Accepted risk inside the workspace jail; documented | Run tests in a sandbox profile with network off |
| T7 | **Malicious upstream code** (hooks, install scripts, look-alike forks) | Repos pulled with `git clone --depth 1` and **never executed or installed**. Canonical repos verified by stars, creation date and owner (e.g. Soup has many identical-description forks) | Pin hashes; review before any `pip install` of upstream |
| T8 | MCP servers / Claude Code hooks / plugins run arbitrary code | MosaForge's MCP server is local stdio with no network listener. `.mcp.json` is project-scoped (Claude Code asks for trust) | Use Claude Code `allowManagedHooksOnly` / managed MCP if this becomes multi-user |
| T9 | **Private data leaks to the cloud** | `allow_cloud = false` by default. `privacy = secret` never reaches a cloud model. MCP mode never escalates by itself. The cloud call gets no tools | Redaction pass (secrets regex) before any cloud call; per-workspace cloud policy |
| T10 | Memory poisoning (injected "facts" persisted) | Memory writes only from the user (CLI), the supervisor (`memory_add`), or the orchestrator's own escalation lessons. Memory is presented to agents as "data, may be outdated" | Review queue for automatic memories; provenance column already exists (`source`) |
| T11 | Ollama API exposed with `OLLAMA_HOST=0.0.0.0` (no auth) | Documented: never do this. Use the worker | Startup check that warns if Ollama listens on a non-loopback interface |
| T12 | Browser automation (later) | Not built | Separate profile, no logged-in sessions, domain allowlist, read-only by default |

## Findings in evaluated projects

- **Spark (X33834/spark):** `spark2 web` binds `0.0.0.0` with no access token by default and serves an
  xterm terminal. Its token check accepts `?token=` in the URL and uses `!=` (not constant-time).
  **Rejected.**
- **llama.cpp RPC:** "fragile and insecure. Never run the RPC server on an open network". Only on a
  dedicated cable or VLAN for experiment E-LAN-2, and torn down afterwards.
- **DeerFlow:** designed for 127.0.0.1. "Gateway admin is equivalent to code execution." Needs IP
  allowlist + pre-auth proxy for any LAN use.
- **OpenHands Agent Canvas:** "without a sandbox … full access to your filesystem". Docker mode only.
- **Colibrì cluster:** coordinator example binds `0.0.0.0` with no auth.
- **ECC / Claude Code plugins:** hooks are shell commands that run on events. Install with `--no-hooks`
  unless each hook is reviewed.
- **isair/jarvis:** keeps a rolling transcript of nearby speech while listening (privacy for other people in the room).

## Operating rules
1. Nothing that can read files or run commands listens on a non-loopback interface.
2. Every LAN endpoint is authenticated, allowlisted and logged.
3. Secrets never go into prompts, memory or the experience log. The deny-list keeps the obvious files out of tools.
4. New third-party code is inspected in `upstream/` before anything is installed or run.
