"""Minimal MCP server (stdio, JSON-RPC 2.0) exposing MosaForge to Claude Code or any MCP client.

Claude Code subagents cannot target a different provider/base URL than the main session, and
Anthropic's docs say other tools should be exposed to Claude as MCP servers. So: Claude stays the
supervisor, and cheap work is delegated to local models through these tools.

In MCP mode cloud escalation is disabled: the caller *is* the frontier model. A failed local
attempt is reported back with diagnostics so the supervisor decides whether to do it itself.
Writes are off unless the call sets allow_writes=true (Claude Code asks the user for MCP tool use,
but MCP servers run outside Claude Code's Bash sandbox, so MosaForge enforces its own workspace jail).
"""

from __future__ import annotations

import json
import sys
from typing import Any

from . import __version__
from .orchestrator import MosaForge

TOOLS = [
    {
        "name": "delegate",
        "description": ("Delegate a self-contained task to MosaForge's local models (private, free). Good for: summaries, "
                        "classification, boilerplate, small functions, tests, docstrings, file analysis. Returns the answer, "
                        "the model used, verification verdict and run_id. Check `verified` before trusting the result."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Complete, self-contained instructions (the local model sees nothing else)."},
                "category": {"type": "string", "enum": ["simple", "summarize", "code", "complex_code", "reasoning"]},
                "model": {"type": "string", "description": "Force a model id from mosaforge.toml (optional)."},
                "verify_cmd": {"type": "string", "description": "Allowlisted acceptance command, e.g. 'python3 -m pytest tests/test_x.py'."},
                "allow_writes": {"type": "boolean", "default": False},
                "privacy": {"type": "string", "enum": ["public", "private", "secret"], "default": "private"},
            },
            "required": ["task"],
        },
    },
    {"name": "route", "description": "Dry run: show which category/model/agent MosaForge would pick for a task, and why.",
     "inputSchema": {"type": "object", "properties": {"task": {"type": "string"}}, "required": ["task"]}},
    {"name": "memory_search", "description": "Search MosaForge's private local memory.",
     "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "memory_add", "description": "Save a fact to private local memory (scope: project | long_term | skill).",
     "inputSchema": {"type": "object", "properties": {"content": {"type": "string"}, "scope": {"type": "string", "enum": ["project", "long_term", "skill"]}},
                     "required": ["content", "scope"]}},
    {"name": "feedback", "description": "Record whether a delegated run's result was actually correct (improves routing).",
     "inputSchema": {"type": "object", "properties": {"run_id": {"type": "string"}, "success": {"type": "boolean"}, "needed_correction": {"type": "boolean"}},
                     "required": ["run_id", "success"]}},
    {"name": "stats", "description": "Per category/model pass rate, latency and cost from the experience log.",
     "inputSchema": {"type": "object", "properties": {}}},
]


class Server:
    def __init__(self) -> None:
        self.mf = MosaForge()
        self.mf.router.allow_cloud = False

    def call(self, name: str, a: dict[str, Any]) -> str:
        if name == "delegate":
            o = self.mf.ask(a["task"], privacy=a.get("privacy", "private"), model=a.get("model"), category=a.get("category"),
                            verify_cmd=a.get("verify_cmd"), allow_writes=bool(a.get("allow_writes", False)))
            f = o.final
            return json.dumps({"answer": o.answer, "verified": o.verified, "verify_detail": f.verify_detail, "model": f.model,
                               "category": o.route.category, "attempts": [(r.model, r.verified) for r in o.attempts],
                               "latency_s": round(sum(r.latency_s for r in o.attempts), 1), "run_id": f.run_id,
                               "error": f.error}, indent=1)
        if name == "route":
            r = self.mf.router.route(a["task"])
            return json.dumps({"category": r.category, "model": r.model.id, "agent": r.agent, "confidence": r.confidence,
                               "difficulty": r.difficulty, "reasons": r.reasons}, indent=1)
        if name == "memory_search":
            return json.dumps(self.mf.store.search(a["query"], limit=8), indent=1)
        if name == "memory_add":
            i = self.mf.store.add(a["scope"], a["content"], source="mcp", workspace=str(self.mf.cfg.workspace))
            return f"saved memory #{i}"
        if name == "feedback":
            n = self.mf.store.mark(a["run_id"], int(bool(a["success"])), int(bool(a.get("needed_correction", False))))
            return f"updated {n} run(s)"
        if name == "stats":
            return json.dumps(self.mf.store.stats(), indent=1)
        raise KeyError(name)

    def handle(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        mid, method = msg.get("id"), msg.get("method", "")
        if mid is None:  # notification
            return None
        try:
            if method == "initialize":
                res: dict[str, Any] = {"protocolVersion": msg.get("params", {}).get("protocolVersion", "2025-06-18"),
                                       "capabilities": {"tools": {}}, "serverInfo": {"name": "mosaforge", "version": __version__}}
            elif method == "tools/list":
                res = {"tools": TOOLS}
            elif method == "tools/call":
                p = msg.get("params", {})
                try:
                    res = {"content": [{"type": "text", "text": self.call(p["name"], p.get("arguments") or {})}], "isError": False}
                except Exception as e:  # report tool errors to the model, keep the server alive
                    res = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
            elif method == "ping":
                res = {}
            else:
                return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
            return {"jsonrpc": "2.0", "id": mid, "result": res}
        except Exception as e:
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(e)}}


def main() -> None:
    srv = Server()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        out = srv.handle(msg)
        if out is not None:
            sys.stdout.write(json.dumps(out) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
