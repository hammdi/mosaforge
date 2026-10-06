"""Core abstractions MosaForge owns. External projects plug in behind these.

ModelProvider  <- Ollama, llama.cpp server, MLX server, LAN worker, Claude
Tool           <- built-in tools, later MCP tools
Agent          <- built-in tool-loop agents, later DeerFlow/OpenHands as AgentRuntime
MemoryProvider <- SQLite/FTS5 store (memory.py)
Router         <- rules + decision model + experience (router.py)
EvaluationEngine <- run log + stats (evaluation.py)
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

# Task categories the router understands. Keep this list short: every category
# multiplies the evaluation matrix (category x model x node).
CATEGORIES = {
    "simple": "Short factual question, greeting, rewording or other trivial request",
    "summarize": "Summarize, extract or classify information from given text",
    "code": "Write, fix or explain a small piece of code or a single function",
    "complex_code": "Multi-file change, architecture, debugging across a codebase",
    "reasoning": "Multi-step reasoning, planning, math, analysis or trade-offs",
}

PRIVACY_LEVELS = ("public", "private", "secret")  # secret = never leaves this machine


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str
    tool_calls: list[dict[str, Any]] | None = None
    tool_name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = self.tool_calls
        if self.tool_name:
            d["tool_name"] = self.tool_name
        return d


@dataclass
class ChatResult:
    content: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_s: float = 0.0
    tokens_per_s: float = 0.0
    load_s: float = 0.0
    cost_usd: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ModelSpec:
    """One model as the router sees it. `id` is MosaForge's name, `name` the runtime's."""

    id: str
    name: str
    providers: list[str]  # provider ids, in preference order (e.g. ["ollama-local", "dell"])
    tier: int  # 1 small local, 2 local coder, 3 strong local, 4 cloud frontier
    cloud: bool = False
    tools: bool = True
    think: bool = False
    num_ctx: int = 8192
    cost_in_per_mtok: float = 0.0
    cost_out_per_mtok: float = 0.0
    tags: list[str] = field(default_factory=list)
    ladder: bool = True  # False: used only when forced, never picked by routing/escalation


@runtime_checkable
class ModelProvider(Protocol):
    id: str
    location: str  # "local" | "lan" | "cloud"

    def chat(
        self,
        model: ModelSpec,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        **opts: Any,
    ) -> ChatResult: ...

    def healthy(self) -> bool: ...


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    fn: Callable[..., str]
    writes: bool = False  # mutating tools are gated by agent permissions

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": self.parameters},
        }


@dataclass
class RouteDecision:
    category: str
    model: ModelSpec
    agent: str
    confidence: float
    reasons: list[str]
    difficulty: float = 0.0


@dataclass
class TaskRecord:
    """One row of experience. Everything the router may later learn from."""

    task: str
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    started_at: float = field(default_factory=time.time)
    category: str = ""
    agent: str = ""
    model: str = ""
    provider: str = ""
    node: str = ""
    privacy: str = "private"
    route_confidence: float = 0.0
    route_reasons: str = ""
    attempt: int = 1
    escalated_from: str = ""
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    tokens_per_s: float = 0.0
    tool_calls: int = 0
    verified: str = "unknown"  # pass | fail | unknown
    verify_detail: str = ""
    success: int | None = None  # 1/0, None = unknown
    human_correction: int | None = None
    cost_usd: float = 0.0
    load_s: float = 0.0
    network_s: float = 0.0
    bytes_lan: int = 0
    error: str = ""
    answer: str = ""


class Memory(Protocol):
    def add(self, scope: str, content: str, key: str = "", source: str = "") -> int: ...

    def search(self, query: str, scopes: tuple[str, ...] = ..., limit: int = 5) -> list[dict[str, Any]]: ...
