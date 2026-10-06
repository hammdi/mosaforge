"""Built-in tools. Every file path is confined to the workspace; commands are allowlisted, no shell.

Tool output is returned to the model wrapped as data. It is never treated as instructions
(prompt-injection defence borrowed from Spark/OpenJarvis designs).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Callable

from .core import Tool

MAX_READ = 20_000
DENY_NAMES = {".env", "id_rsa", "id_ed25519", ".netrc", "credentials.json"}
DENY_DIRS = {".git", ".ssh", "upstream", "node_modules", ".venv"}


class ToolError(Exception):
    pass


class Workspace:
    def __init__(self, root: Path, allowed_commands: list[str], timeout_s: int = 60):
        self.root = root.resolve()
        self.allowed = [shlex.split(c) for c in allowed_commands]
        self.timeout_s = timeout_s

    def resolve(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"path escapes workspace: {rel}")
        parts = set(p.relative_to(self.root).parts)
        if parts & DENY_DIRS or p.name in DENY_NAMES or p.name.startswith(".env"):
            raise ToolError(f"path is denied by policy: {rel}")
        return p

    def run(self, command: str) -> tuple[int, str]:
        argv = shlex.split(command)
        if not any(argv[: len(a)] == a for a in self.allowed):
            raise ToolError(f"command not allowlisted: {command!r}")
        if any(ch in command for ch in ";|&`$<>"):
            raise ToolError("shell metacharacters are not allowed")
        if argv[0] in ("python", "python3"):
            argv[0] = os.environ.get("MOSAFORGE_PYTHON", "python3")
        env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.root), "PYTHONDONTWRITEBYTECODE": "1"}
        p = subprocess.run(argv, cwd=self.root, capture_output=True, text=True, timeout=self.timeout_s, env=env)
        return p.returncode, (p.stdout + p.stderr)[-4000:]


def build_tools(ws: Workspace, memory: Any | None = None) -> dict[str, Tool]:
    def read_file(path: str) -> str:
        p = ws.resolve(path)
        if not p.is_file():
            raise ToolError(f"not a file: {path}")
        return p.read_text(errors="replace")[:MAX_READ]

    def list_dir(path: str = ".") -> str:
        p = ws.resolve(path)
        entries = []
        for c in sorted(p.iterdir())[:200]:
            if c.name in DENY_DIRS or c.name.startswith("."):
                continue
            entries.append(c.name + ("/" if c.is_dir() else ""))
        return "\n".join(entries) or "(empty)"

    def search_text(pattern: str, path: str = ".") -> str:
        base = ws.resolve(path)
        rx = re.compile(pattern)
        hits: list[str] = []
        for f in base.rglob("*"):
            if len(hits) >= 50:
                break
            if not f.is_file() or set(f.relative_to(ws.root).parts) & DENY_DIRS or f.stat().st_size > 1_000_000:
                continue
            try:
                for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f.relative_to(ws.root)}:{i}: {line[:200]}")
                        if len(hits) >= 50:
                            break
            except OSError:
                continue
        return "\n".join(hits) or "no matches"

    def write_file(path: str, content: str) -> str:
        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return f"wrote {len(content)} chars to {path}"

    def run_command(command: str) -> str:
        code, out = ws.run(command)
        return f"exit={code}\n{out}"

    def memory_search(query: str) -> str:
        if memory is None:
            return "memory unavailable"
        rows = memory.search(query, limit=5)
        return "\n".join(f"- [{r['scope']}] {r['content'][:300]}" for r in rows) or "no memories"

    s = lambda **props: {"type": "object", "properties": props, "required": list(props)}  # noqa: E731
    string = {"type": "string"}
    tools = [
        Tool("read_file", "Read a UTF-8 text file inside the workspace.", s(path=string), read_file),
        Tool("list_dir", "List entries of a workspace directory ('.' is the root).", s(path=string), list_dir),
        Tool("search_text", "Regex search across workspace files. Returns file:line matches.", s(pattern=string, path=string), search_text),
        Tool("write_file", "Create or overwrite a text file inside the workspace.", s(path=string, content=string), write_file, writes=True),
        Tool("run_command", "Run an allowlisted command (e.g. 'python3 -m pytest tests'). No shell.", s(command=string), run_command, writes=True),
        Tool("memory_search", "Search MosaForge's private memory (project facts, preferences, past experience).", s(query=string), memory_search),
    ]
    return {t.name: t for t in tools}


def call_tool(tools: dict[str, Tool], name: str, args: dict[str, Any]) -> str:
    tool = tools.get(name)
    if tool is None:
        return f"ERROR: unknown tool {name}"
    try:
        fn: Callable[..., str] = tool.fn
        return fn(**{k: v for k, v in args.items() if not k.startswith("_")})
    except (ToolError, TypeError, OSError, re.error, subprocess.TimeoutExpired) as e:
        return f"ERROR: {e}"
