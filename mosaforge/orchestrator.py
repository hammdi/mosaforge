"""User -> Router -> Agent(model via provider) -> Tools -> Verification -> (escalate) -> Memory + experience log."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from .agents import ToolLoopAgent
from .config import Config, load_config
from .core import ModelSpec, RouteDecision, TaskRecord
from .memory import Store
from .providers import ProviderError, build_providers
from .router import Router
from .tools import Workspace, build_tools
from .verify import verify


@dataclass
class Outcome:
    answer: str
    verified: str
    route: RouteDecision
    attempts: list[TaskRecord] = field(default_factory=list)

    @property
    def final(self) -> TaskRecord:
        return self.attempts[-1]


class MosaForge:
    def __init__(self, cfg: Config | None = None, workspace: str | None = None, db_name: str = "mosaforge.db"):
        self.cfg = cfg or load_config(workspace=workspace)
        self.providers = build_providers(self.cfg.providers)
        self.store = Store(self.cfg.data_dir / db_name)
        self.router = Router(self.cfg, self.providers, self.store)
        v = self.cfg.verify
        self.ws = Workspace(self.cfg.workspace, v.get("allowed_commands", []), int(v.get("command_timeout_s", 60)))
        self.tools = build_tools(self.ws, self.store)
        self.node_policy: dict[str, list[str]] = dict(self.cfg.router.get("node_policy", {}))
        self._inflight: dict[str, int] = {}
        self._installed: dict[str, tuple[float, set[str]]] = {}
        self._lock = threading.Lock()

    def _is_installed(self, pid: str, p: Any, name: str) -> bool:
        if not hasattr(p, "installed"):
            return True
        t, names = self._installed.get(pid, (0.0, set()))
        if time.time() - t > 30 or name not in names:  # cache 30 s; refresh on miss
            names = p.installed()
            self._installed[pid] = (time.time(), names)
        return name in names

    def _provider_for(self, model: ModelSpec, node: str | None, privacy: str = "private", category: str = "") -> tuple[str, Any]:
        """Pick the node. Order: forced node > node_policy[category] > model.providers. Among usable nodes,
        the least busy wins (ties keep preference order). Node rules are hard: exclude_models, allow_secret."""
        if node:
            order = [node]
        else:
            pol = [pid for pid in self.node_policy.get(category, []) if pid in model.providers]
            order = pol or list(model.providers)
        usable: list[tuple[int, int, str, Any]] = []
        for rank, pid in enumerate(order):
            p = self.providers.get(pid)
            if p is None or model.name in getattr(p, "exclude_models", set()):
                continue
            if privacy == "secret" and not getattr(p, "allow_secret", False):
                if node:
                    raise PermissionError(f"node {pid} ({p.location}) is not allowed to run privacy=secret tasks")
                continue
            if not p.healthy() or not self._is_installed(pid, p, model.name):
                continue
            usable.append((self._inflight.get(pid, 0), rank, pid, p))
        if not usable:
            raise ProviderError(f"no usable node for {model.id} (tried {order}, privacy={privacy})")
        _, _, pid, p = min(usable, key=lambda u: (u[0], u[1]))
        return pid, p

    def ask(self, task: str, *, privacy: str = "private", model: str | None = None, category: str | None = None,
            node: str | None = None, verify_cmd: str | None = None, allow_writes: bool = True, escalate: bool = True,
            remember: bool = True) -> Outcome:
        route = self.router.route(task, privacy=privacy, force_model=model, force_category=category)
        mems = self.store.search(task, limit=4, workspace=str(self.cfg.workspace))
        context = "\n".join(f"- [{m['scope']}] {m['content'][:400]}" for m in mems)
        current: ModelSpec | None = route.model
        outcome = Outcome(answer="", verified="unknown", route=route)
        max_attempts = int(self.cfg.router.get("max_attempts", 3)) if escalate else 1
        prev = ""
        while current is not None and len(outcome.attempts) < max_attempts:
            rec = TaskRecord(task=task, category=route.category, agent=route.agent, model=current.id, privacy=privacy,
                             route_confidence=route.confidence, route_reasons=" | ".join(route.reasons),
                             attempt=len(outcome.attempts) + 1, escalated_from=prev)
            t0 = time.time()
            try:
                self.router.check_privacy(current, privacy)  # defence in depth, re-checked per attempt
                with self._lock:
                    pid, provider = self._provider_for(current, node, privacy, route.category)
                    self._inflight[pid] = self._inflight.get(pid, 0) + 1
                rec.provider, rec.node = pid, provider.location
                agent = ToolLoopAgent(route.agent, self.tools, allow_writes=allow_writes and not current.cloud)
                note = ""
                if outcome.attempts:
                    last = outcome.attempts[-1]
                    note = (f"\n- [previous attempt by {last.model} FAILED verification] {last.verify_detail[:600]}"
                            f"\n  Its answer was: {last.answer[:800]}")
                try:
                    res = agent.run(task, current, provider, context=context + note)
                finally:
                    with self._lock:
                        self._inflight[pid] -= 1
                rec.network_s = sum(float(c.raw.get("network_s", 0.0)) for c in res.calls)
                rec.bytes_lan = sum(int(c.raw.get("bytes_out", 0)) + int(c.raw.get("bytes_in", 0)) for c in res.calls)
                rec.load_s = sum(c.load_s for c in res.calls)
                rec.answer = res.answer
                rec.prompt_tokens, rec.completion_tokens = res.prompt_tokens, res.completion_tokens
                rec.tokens_per_s, rec.tool_calls = res.tokens_per_s, len(res.tool_log)
                rec.cost_usd = res.cost_usd or (res.prompt_tokens * current.cost_in_per_mtok + res.completion_tokens * current.cost_out_per_mtok) / 1e6
                rec.verified, rec.verify_detail = verify(
                    route.category, task, res.answer, res.written_files, self.ws, self.router.decider,
                    self.cfg.verify.get("judge_model", ""), float(self.cfg.verify.get("judge_threshold", 0.5)), verify_cmd)
            except (ProviderError, OSError, ValueError) as e:  # PermissionError is an OSError
                rec.error, rec.verified = str(e)[:500], "fail"
            rec.latency_s = time.time() - t0
            self.store.record(rec)
            outcome.attempts.append(rec)
            outcome.verified = rec.verified
            if rec.answer:
                outcome.answer = rec.answer
            if rec.verified != "fail":
                break
            prev = current.id
            current = self.router.next_after(current, privacy)
            if current is not None:
                route.reasons.append(f"verification failed on {prev} ({rec.verify_detail[:80]}): escalate to {current.id}")
        if remember and outcome.verified == "pass" and outcome.final.attempt > 1:
            # Distilled experience: which model finally worked after which failed.
            self.store.add("experience", f"For a '{route.category}' task like: {task[:160]!r} — {outcome.attempts[0].model} failed "
                           f"verification; {outcome.final.model} passed.", key=f"escalation:{route.category}", source="orchestrator",
                           workspace=str(self.cfg.workspace))
        return outcome
