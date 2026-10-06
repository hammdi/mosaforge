"""Verification. Deterministic checks first (compile/tests), a cheap model gate second.

The model gate (decision model asked "does this answer address the request?") is a heuristic.
Its usefulness is an open question measured by `mosaforge bench` (BENCHMARKS.md, experiment V1).
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any

from .providers import OllamaProvider, ProviderError
from .tools import ToolError, Workspace

FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def verify(category: str, task: str, answer: str, written: list[str], ws: Workspace, decider: Any, judge_model: str,
           threshold: float, verify_cmd: str | None = None) -> tuple[str, str]:
    """Return (verdict, detail) with verdict in pass|fail|unknown."""
    if not answer.strip() and not written:
        return "fail", "empty answer"
    if verify_cmd:  # caller-supplied acceptance test, strongest signal
        try:
            code, out = ws.run(verify_cmd)
        except ToolError as e:
            return "fail", f"verify command refused: {e}"
        return ("pass" if code == 0 else "fail"), f"{verify_cmd} -> exit {code}: {out[-300:]}"
    if category in ("code", "complex_code"):
        py_files = [w for w in written if w.endswith(".py")]
        if py_files:
            try:
                code, out = ws.run("python3 -m py_compile " + " ".join(py_files))
            except ToolError as e:
                return "fail", str(e)
            if code != 0:
                return "fail", f"py_compile failed: {out[-300:]}"
            tests = [Path(w).stem for w in py_files if Path(w).name.startswith("test_") and "/" not in w]
            if tests:  # re-run the agent's own tests ourselves; never trust "tests passed" in its prose
                try:
                    code, out = ws.run("python3 -m unittest " + " ".join(tests))
                except ToolError as e:
                    return "fail", str(e)
                return ("pass" if code == 0 else "fail"), f"unittest {' '.join(tests)} -> exit {code}: {out[-200:]}"
        blocks = FENCE.findall(answer)
        for b in blocks:
            try:
                compile(b, "<answer>", "exec")
            except SyntaxError as e:
                return "fail", f"answer code block does not parse: {e}"
        if py_files or blocks:
            # Compiling is necessary, not sufficient (suite task c3 compiled and was wrong).
            return "unknown", "code compiles; no tests supplied (pass verify_cmd to get a real verdict)"
        # No code artifact at all for a coding task: the judge model is easily fooled by
        # tool-call JSON or prose that *describes* code, so do not ask it.
        return "fail", "coding task produced no code block and no written .py file"
    if isinstance(decider, OllamaProvider) and judge_model:
        try:
            r = decider.decide(judge_model, {"request": task[:3000], "answer": answer[:4000]}, {
                "ok": {"type": "noul", "instructions": "Does the answer directly and plausibly address the request without refusing or going off-topic?"}})
            p = float(r["answers"]["ok"]["noul"])
            # The judge may veto, never certify: on the 2026-10-04 suite it passed 8/8 wrong answers
            # (BENCHMARKS.md §2). A high score only means "on-topic", so the verdict stays unknown.
            return ("unknown" if p >= threshold else "fail"), f"judge {judge_model} p={p:.2f} (veto-only)"
        except (ProviderError, KeyError, TypeError, ValueError) as e:
            return "unknown", f"judge unavailable: {str(e)[:80]}"
    return "unknown", "no verifier applicable"


def scratch_workspace(allowed: list[str]) -> Workspace:
    return Workspace(Path(tempfile.mkdtemp(prefix="mosaforge-")), allowed)
