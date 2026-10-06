"""Resource Manager: given a task profile and the models the Model Router accepts, decide WHERE to run.

Model routing ("which model?") and resource routing ("which node?") are separate decisions:
the Model Router hands over an ordered list of acceptable models (quality order); this module never
trades quality for speed. It picks the first model in that order that has a feasible placement, and
for that model the node with the lowest cost under the *current* state of every node.

Pipeline per (model, node):
  1. hard constraints  — healthy, privacy, exclude_models, installed, memory headroom, pressure
  2. latency estimate  — queue + load (+ evictions) + prefill + generation + network
  3. objective         — latency weighted by interactivity + protection of the interactive node
Batches use list scheduling (earliest estimated finish time) so independent tasks spread over nodes.

Nodes are observed through NodeMonitor implementations: LocalMonitor (real Mac: RAM, swap, pressure,
CPU, loaded/installed models from Ollama), SimulatedMonitor (scenario dicts, used until the Dell is
deployed), and later a WorkerMonitor reading the worker's /v1/status.
"""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from . import sysstats
from .analyzer import TaskProfile
from .core import ModelSpec

PRESSURE = {1: "normal", 2: "warn", 4: "critical"}  # kern.memorystatus_vm_pressure_level


@dataclass
class ModelPerf:
    gen_tok_s: float
    prefill_tok_s: float
    load_s: float
    resident_gb: float
    source: str = "estimated"  # measured | estimated | simulated

    @property
    def trust(self) -> float:
        return 1.0 if self.source.startswith("measured") else 0.6 if self.source.startswith("estimated") else 0.4


@dataclass
class NodeState:
    id: str
    location: str  # local | lan
    role: str  # interactive (the user's machine) | batch
    healthy: bool = True
    simulated: bool = False
    ram_total_gb: float = 16.0
    ram_used_gb: float = 0.0
    ram_avail_gb: float | None = None  # OS estimate of memory available without swapping (includes reclaimable)
    swap_used_gb: float = 0.0
    pressure: str = "normal"  # normal | warn | critical
    cpu_pct: float | None = None
    reserve_gb: float = 2.0  # RAM kept free for everything that is not a model
    loaded: dict[str, float] = field(default_factory=dict)  # runtime model name -> resident GB
    installed: set[str] = field(default_factory=set)
    inflight: int = 0
    slots: int = 1  # concurrent requests the runtime serves (OLLAMA_NUM_PARALLEL)
    busy_s: float = 0.0  # estimated seconds until in-flight work finishes
    rtt_ms: float = 0.0
    bandwidth_mbps: float = 1000.0
    allow_secret: bool = False
    exclude_models: set[str] = field(default_factory=set)
    perf: dict[str, ModelPerf] = field(default_factory=dict)
    sampled_at: float = field(default_factory=time.time)

    @property
    def model_headroom_gb(self) -> float:
        avail = self.ram_avail_gb if self.ram_avail_gb is not None else self.ram_total_gb - self.ram_used_gb
        return avail - self.reserve_gb


@dataclass
class Placement:
    model: str
    node: str | None
    feasible: bool
    estimated_latency_ms: float = 0.0
    network_overhead_ms: float = 0.0
    breakdown_ms: dict[str, float] = field(default_factory=dict)
    score: float = float("inf")
    confidence: float = 0.0
    reason: str = ""
    reasons: list[str] = field(default_factory=list)
    alternatives: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["estimated_latency_ms"] = round(self.estimated_latency_ms)
        d["network_overhead_ms"] = round(self.network_overhead_ms, 1)
        d["breakdown_ms"] = {k: round(v) for k, v in self.breakdown_ms.items()}
        d["score"] = round(self.score) if self.score != float("inf") else None
        d["confidence"] = round(self.confidence, 2)
        return d


class NodeMonitor(Protocol):
    def snapshot(self) -> NodeState: ...


def _perf_table(raw: dict[str, Any]) -> dict[str, ModelPerf]:
    return {name: ModelPerf(**{k: v for k, v in p.items() if k in ModelPerf.__dataclass_fields__}) for name, p in raw.items()}


class LocalMonitor:
    """Real measurements of this machine plus its Ollama runtime."""

    def __init__(self, node_id: str, node_cfg: dict[str, Any], provider: Any):
        self.id, self.cfg, self.provider = node_id, node_cfg, provider

    def _pressure(self) -> str:
        if sys.platform != "darwin":
            return "normal"
        out = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"], capture_output=True, text=True).stdout.strip()
        return PRESSURE.get(int(out or 1), "normal")

    def _available_gb(self, total_gb: float) -> float | None:
        # macOS: kern.memorystatus_level = % of memory available before pressure (the figure `memory_pressure`
        # reports as "free percentage"). It counts compressed and file-backed pages as reclaimable, unlike "used".
        if sys.platform != "darwin":
            return None
        out = subprocess.run(["sysctl", "-n", "kern.memorystatus_level"], capture_output=True, text=True).stdout.strip()
        return round(total_gb * int(out) / 100, 2) if out.isdigit() else None

    def snapshot(self) -> NodeState:
        mem = sysstats.memory()
        healthy = bool(self.provider and self.provider.healthy())
        loaded: dict[str, float] = {}
        installed: set[str] = set()
        if healthy:
            try:
                loaded = {m["name"]: round(m.get("size", 0) / 1e9, 2) for m in self.provider.loaded()}
                installed = self.provider.installed()
            except Exception:
                pass
        return NodeState(
            id=self.id, location="local", role=self.cfg.get("role", "interactive"), healthy=healthy,
            ram_total_gb=mem.get("total_gb", 0), ram_used_gb=mem.get("used_gb", 0), swap_used_gb=mem.get("swap_used_gb", 0),
            ram_avail_gb=self._available_gb(mem.get("total_gb", 0)),
            pressure=self._pressure(), cpu_pct=sysstats.cpu_percent(), reserve_gb=float(self.cfg.get("reserve_gb", 2.0)),
            loaded=loaded, installed=installed, slots=int(self.cfg.get("slots", 1)), rtt_ms=0.0,
            allow_secret=bool(self.cfg.get("allow_secret", True)),
            exclude_models=set(getattr(self.provider, "exclude_models", set())) | set(self.cfg.get("exclude_models", [])),
            perf=_perf_table(self.cfg.get("perf", {})))


class SimulatedMonitor:
    """A node described by a dict (config or scenario). Used for the Dell until it is deployed."""

    def __init__(self, node_id: str, state: dict[str, Any]):
        self.id, self.state = node_id, state

    def snapshot(self) -> NodeState:
        s = dict(self.state)
        perf = _perf_table(s.pop("perf", {}))
        fields = NodeState.__dataclass_fields__
        kw = {k: v for k, v in s.items() if k in fields and k not in ("simulated", "perf", "id")}
        for k in ("installed", "exclude_models"):
            if k in kw:
                kw[k] = set(kw[k])
        kw.setdefault("location", "lan")
        kw.setdefault("role", "batch")
        return NodeState(id=self.id, simulated=True, perf=perf, **kw)


class ResourceManager:
    def __init__(self, cfg: dict[str, Any] | None = None):
        c = cfg or {}
        self.background_latency_weight = float(c.get("background_latency_weight", 0.3))
        # Cost of running *background* work on the interactive node, as a fraction of its compute time,
        # by memory pressure. Interactive work on the interactive node pays nothing.
        self.protect = {"normal": float(c.get("protect_normal", 0.6)), "warn": float(c.get("protect_warn", 2.0)), "critical": float("inf")}
        self.reload_weight = float(c.get("evicted_model_reload_weight", 1.0))
        # Batches: background work may not queue longer than this on the interactive node, so a request
        # from the user never waits behind more than ~this many seconds of batch work.
        self.interactive_queue_cap_s = float(c.get("interactive_queue_cap_s", 15.0))

    # ---- one (model, node) pair ----------------------------------------
    def evaluate(self, p: TaskProfile, model: ModelSpec, n: NodeState) -> Placement:
        pl = Placement(model=model.name, node=n.id, feasible=False)

        def reject(why: str) -> Placement:
            pl.reason = why
            pl.reasons.append(f"{n.id}: {why}")
            return pl

        if not n.healthy:
            return reject("unavailable (health check failed)")
        if p.privacy == "secret" and not (n.location == "local" or n.allow_secret):
            return reject("privacy=secret and this node is not allowed secret tasks")
        if model.name in n.exclude_models:
            return reject(f"{model.name} is excluded on this node")
        if n.installed and model.name not in n.installed:
            return reject(f"{model.name} not installed")
        perf = n.perf.get(model.name)
        if perf is None:
            return reject(f"no performance data for {model.name}")
        is_loaded = model.name in n.loaded
        evicted: list[str] = []
        if not is_loaded:
            if n.pressure == "critical":
                return reject("memory pressure critical: no new model loads")
            if n.pressure == "warn" and n.role == "interactive":
                return reject("memory pressure rising on the interactive node: no new model loads")
            need = perf.resident_gb - n.model_headroom_gb
            if need > 0:  # Ollama would evict idle models (LRU); never count busy ones as reclaimable
                idle = sorted(((g, m) for m, g in n.loaded.items()), reverse=True) if n.inflight == 0 else []
                freed = 0.0
                for g, m in idle:
                    if freed >= need:
                        break
                    evicted.append(m)
                    freed += g
                if freed < need:
                    avail = n.ram_avail_gb if n.ram_avail_gb is not None else n.ram_total_gb - n.ram_used_gb
                return reject(f"needs {perf.resident_gb:.1f} GB, headroom {n.model_headroom_gb:.1f} GB "
                              f"(available {avail:.1f}/{n.ram_total_gb:.0f} GB, reserve {n.reserve_gb:.1f} GB)")

        # ---- latency estimate (ms) ----
        queue = max(n.busy_s, 0.0) * 1000 if n.inflight >= n.slots else 0.0
        load = 0.0 if is_loaded else perf.load_s * 1000
        reload = sum(n.perf[m].load_s * 1000 for m in evicted if m in n.perf) * self.reload_weight
        # first call prefills everything; later tool-loop calls reuse the cached prefix (~30% new tokens)
        prefill_tokens = p.prompt_tokens * (1 + 0.3 * (p.model_calls - 1))
        prefill = prefill_tokens / perf.prefill_tok_s * 1000
        gen = p.output_tokens / perf.gen_tok_s * 1000
        net = 0.0
        if n.location != "local":
            per_call = 2 * n.rtt_ms + p.payload_bytes * 8 / (n.bandwidth_mbps * 1000)  # ms
            net = p.model_calls * per_call + 2 * n.rtt_ms  # + health/models probes
        compute = load + prefill + gen
        total = queue + compute + net
        pl.breakdown_ms = {"queue": queue, "load": load, "prefill": prefill, "generate": gen, "network": net}
        pl.estimated_latency_ms, pl.network_overhead_ms = total, net

        # ---- objective ----
        weight = 1.0 if p.interactive else self.background_latency_weight
        protection = 0.0
        if n.role == "interactive" and not p.interactive:
            # grows with memory pressure, with work already running there, and with the user's own CPU load
            activity = 1 + n.inflight + (n.cpu_pct or 0.0) / 100
            protection = compute * self.protect.get(n.pressure, 0.6) * activity
        pl.score = weight * total + protection + reload
        pl.feasible = True
        parts = [f"{model.name} {'loaded' if is_loaded else 'cold'}", f"{perf.gen_tok_s:g} tok/s ({perf.source})",
                 f"est {total / 1000:.1f} s"]
        if net:
            parts.append(f"network {net:.0f} ms")
        if evicted:
            parts.append(f"evicts {', '.join(evicted)} (+{reload / 1000:.1f} s reload later)")
        if protection:
            parts.append(f"background work on the interactive node (+{protection / 1000:.1f} s penalty, pressure {n.pressure})")
        if n.inflight:
            parts.append(f"{n.inflight} task(s) in flight")
        pl.reasons.append(f"{n.id}: " + ", ".join(parts))
        pl.confidence = perf.trust
        return pl

    # ---- one task ---------------------------------------------------------
    def place(self, p: TaskProfile, models: list[ModelSpec], nodes: list[NodeState]) -> Placement:
        """models: acceptable models in quality order (from the Model Router). Quality first, then node."""
        tried: list[Placement] = []
        for model in models:
            options = [self.evaluate(p, model, n) for n in nodes]
            tried.extend(options)
            feasible = sorted((o for o in options if o.feasible), key=lambda o: o.score)
            if not feasible:
                continue
            best = feasible[0]
            others = feasible[1:]
            # confidence: trust in the chosen node's numbers, and in the comparison (margin x trust in the runner-up's numbers)
            margin = 1.0 if not others else min(1.0, (others[0].score - best.score) / max(others[0].score, 1.0) * 2)
            runner_trust = 1.0 if not others else others[0].confidence
            best.confidence = round(0.5 * best.confidence + 0.5 * margin * runner_trust, 2)
            best.reason = self._explain(p, best, others, model is not models[0])
            best.reasons = [r for o in options for r in o.reasons]
            best.alternatives = [{"model": o.model, "node": o.node, "feasible": o.feasible,
                                  "estimated_latency_ms": round(o.estimated_latency_ms) if o.feasible else None,
                                  "score": round(o.score) if o.feasible else None, "why_not": o.reason or None}
                                 for o in tried if o is not best]
            return best
        return Placement(model=models[0].name if models else "", node=None, feasible=False,
                         reason="no feasible node for any acceptable model", reasons=[r for o in tried for r in o.reasons],
                         alternatives=[{"model": o.model, "node": o.node, "why_not": o.reason} for o in tried])

    @staticmethod
    def _explain(p: TaskProfile, best: Placement, others: list[Placement], upgraded: bool) -> str:
        kind = "interactive" if p.interactive else "background"
        if not others:
            why = "only feasible node"
        else:
            o = others[0]
            if best.estimated_latency_ms <= o.estimated_latency_ms:
                why = f"faster than {o.node} ({best.estimated_latency_ms / 1000:.1f} s vs {o.estimated_latency_ms / 1000:.1f} s)"
            else:
                why = (f"keeps {o.node} free: {o.node} would be faster ({o.estimated_latency_ms / 1000:.1f} s vs "
                       f"{best.estimated_latency_ms / 1000:.1f} s) but this is {kind} work")
        return f"{kind} {p.category} task; {why}" + ("; preferred model not placeable, using next acceptable model" if upgraded else "")

    # ---- many independent tasks -------------------------------------------
    def plan_batch(self, profiles: list[TaskProfile], models_for: Any, nodes: list[NodeState]) -> list[Placement]:
        """List scheduling: longest task first, each to the node with the earliest estimated finish
        (makespan objective), subject to the interactive-node queue cap for background work.
        Node state is updated as tasks are assigned (busy time, loaded model), so work spreads."""
        state = {n.id: n for n in nodes}
        order = sorted(range(len(profiles)), key=lambda i: -(profiles[i].output_tokens + profiles[i].prompt_tokens))
        out: list[Placement | None] = [None] * len(profiles)
        for i in order:
            p = profiles[i]
            pl = Placement(model="", node=None, feasible=False, reason="no feasible node")
            for model in models_for(p):
                opts = [self.evaluate(p, model, n) for n in state.values()]
                ok = [o for o in opts if o.feasible]
                capped = [o for o in ok if p.interactive or state[o.node or ""].role != "interactive"
                          or state[o.node or ""].busy_s < self.interactive_queue_cap_s]
                pool = capped or ok  # if only the interactive node can run it, it still runs there
                if pool:
                    pl = min(pool, key=lambda o: o.estimated_latency_ms)
                    if len(capped) < len(ok):
                        pl.reasons.append(f"interactive node queue >= {self.interactive_queue_cap_s:g} s: background work sent elsewhere")
                    break
            out[i] = pl
            if pl.feasible and pl.node:
                n = state[pl.node]
                n.busy_s = pl.estimated_latency_ms / 1000  # estimate already includes the queue
                n.inflight = max(n.inflight, n.slots)  # subsequent tasks queue behind it
                if pl.model in n.perf:
                    n.loaded.setdefault(pl.model, n.perf[pl.model].resident_gb)
        return [p for p in out if p is not None]


def monitors_from_config(cfg: Any, providers: dict[str, Any], scenario: dict[str, Any] | None = None) -> list[Any]:
    """Real monitor for local Ollama nodes; simulated monitor for nodes marked simulated (or given in a scenario)."""
    nodes_cfg: dict[str, Any] = dict(cfg.raw.get("resources", {}).get("nodes", {}))
    mons: list[Any] = []
    for nid, ncfg in nodes_cfg.items():
        over = (scenario or {}).get(nid)
        if ncfg.get("simulated") or over is not None and over.get("simulated", ncfg.get("simulated")):
            mons.append(SimulatedMonitor(nid, {**ncfg, **(over or {})}))
        elif nid in providers:
            mons.append(LocalMonitor(nid, ncfg, providers[nid]))
    return mons


def decide(mf: Any, task: str, *, category: str | None = None, difficulty: float | None = None, privacy: str = "private",
           interactive: bool = True, context_tokens: int = 0, scenario: dict[str, Any] | None = None,
           batch: int = 1) -> dict[str, Any]:
    """Model Router -> Task Analyzer -> Resource Manager. Returns the decision as a dict (no execution)."""
    from .analyzer import analyze
    from .core import RouteDecision

    t0 = time.time()
    if category:
        route = RouteDecision(category=category, model=None, agent="", confidence=1.0,  # type: ignore[arg-type]
                              reasons=["category given"], difficulty=0.5 if difficulty is None else difficulty)
    else:
        route = mf.router.route(task, privacy=privacy)
        if difficulty is not None:
            route.difficulty = difficulty
    reasons: list[str] = []
    models = mf.router.acceptable(route.category, route.difficulty, privacy, reasons)
    profile = analyze(task, route, privacy=privacy, interactive=interactive, parallelizable=batch > 1,
                      context_tokens=context_tokens)
    nodes = [m.snapshot() for m in monitors_from_config(mf.cfg, mf.providers, scenario)]
    rm = ResourceManager(mf.cfg.raw.get("resources", {}))
    node_view = {n.id: {"simulated": n.simulated, "healthy": n.healthy, "ram_used_gb": n.ram_used_gb, "ram_avail_gb": n.ram_avail_gb,
                        "model_headroom_gb": round(n.model_headroom_gb, 2), "swap_used_gb": n.swap_used_gb,
                        "pressure": n.pressure, "cpu_pct": n.cpu_pct, "loaded": n.loaded, "inflight": n.inflight} for n in nodes}
    common = {"task_profile": {k: getattr(profile, k) for k in ("category", "difficulty", "privacy", "interactive",
                                                                "prompt_tokens", "output_tokens", "model_calls")},
              "model_router": {"acceptable_models": [m.name for m in models], "reasons": route.reasons + reasons},
              "nodes": node_view}
    if batch > 1:
        plan = rm.plan_batch([profile] * batch, lambda _p: models, nodes)
        per_node: dict[str, int] = {}
        for pl in plan:
            per_node[pl.node or "none"] = per_node.get(pl.node or "none", 0) + 1
        finish = {nid: round(max((pl.estimated_latency_ms for pl in plan if pl.node == nid), default=0)) for nid in per_node}
        single = rm.place(profile, models, [m.snapshot() for m in monitors_from_config(mf.cfg, mf.providers, scenario)])
        serial_ms = single.estimated_latency_ms * batch if single.feasible else None
        return {"batch": batch, "assignments": per_node, "estimated_finish_ms_by_node": finish,
                "estimated_makespan_ms": max(finish.values(), default=0),
                "single_node_serial_ms": round(serial_ms) if serial_ms else None, "single_node": single.node,
                "decision_ms": round((time.time() - t0) * 1000, 1), **common}
    pl = rm.place(profile, models, nodes)
    d = pl.to_dict()
    out = {k: d[k] for k in ("model", "node", "reason", "estimated_latency_ms", "network_overhead_ms", "confidence")}
    out.update({"feasible": pl.feasible, "breakdown_ms": d["breakdown_ms"], "considered": pl.reasons,
                "rejected_or_slower": d["alternatives"], "decision_ms": round((time.time() - t0) * 1000, 1), **common})
    return out
