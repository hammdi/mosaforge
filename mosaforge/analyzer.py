"""Task Analyzer: turns a task + routing classification into a TaskProfile the Resource Manager can cost.

Token defaults are calibrated on the 2026-10-04 suite (qwen3.5:4b, no thinking): short tasks used
~550-850 tokens in total, of which ~500 are system prompt + tool schemas.
"""

from __future__ import annotations

from dataclasses import dataclass

from .core import RouteDecision

SYSTEM_OVERHEAD_TOKENS = 500  # agent system prompt + tool schemas (measured: ~500 of the ~550 tokens of a one-line task)
OUTPUT_TOKENS = {"simple": 80, "summarize": 150, "code": 350, "reasoning": 300, "complex_code": 900}
MODEL_CALLS = {"simple": 1, "summarize": 1, "code": 2, "reasoning": 1, "complex_code": 4}  # agent steps (tool loop)


@dataclass
class TaskProfile:
    task: str
    category: str
    difficulty: float  # 0..1 from the decision model
    privacy: str  # public | private | secret
    interactive: bool  # a person is waiting for the answer
    parallelizable: bool  # independent of other tasks in the same batch
    prompt_tokens: int
    output_tokens: int
    model_calls: int  # expected round trips to the model (tool loop steps)

    @property
    def payload_bytes(self) -> int:
        """Approximate JSON bytes per model call (request + response), ~4 chars/token plus envelope."""
        return int((self.prompt_tokens + self.output_tokens) * 4 * 1.3) + 600


def analyze(task: str, route: RouteDecision, *, privacy: str = "private", interactive: bool = True,
            parallelizable: bool = False, context_tokens: int = 0) -> TaskProfile:
    cat = route.category
    out = OUTPUT_TOKENS.get(cat, 200)
    if route.difficulty >= 0.75:  # hard tasks write more
        out = int(out * 1.5)
    return TaskProfile(task=task, category=cat, difficulty=route.difficulty, privacy=privacy, interactive=interactive,
                       parallelizable=parallelizable, prompt_tokens=SYSTEM_OVERHEAD_TOKENS + len(task) // 4 + context_tokens,
                       output_tokens=out, model_calls=MODEL_CALLS.get(cat, 1))
