"""Benchmarks: raw runtime speed, and a ground-truth task suite (model matrix + routed mode).

Every task has a deterministic grader, so we can measure (a) per-model quality on this hardware,
(b) router accuracy, (c) how well the cheap verifier agrees with ground truth, and (d) whether
routing beats "always use the strongest local model" on latency at equal quality.
Results go to bench.db (separate from the user's experience DB) and a JSONL file.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from .core import Message
from .orchestrator import MosaForge
from .providers import OllamaProvider

FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.S)


def _code_grader(tests: str) -> Callable[[str], bool]:
    def grade(answer: str) -> bool:
        blocks = FENCE.findall(answer)
        code = max(blocks, key=len) if blocks else answer
        with tempfile.TemporaryDirectory() as d:
            Path(d, "sol.py").write_text(code)
            Path(d, "check.py").write_text("from sol import *\n" + tests)
            try:
                p = subprocess.run([sys.executable, "check.py"], cwd=d, capture_output=True, timeout=15)
            except subprocess.TimeoutExpired:
                return False
            return p.returncode == 0
    return grade


def _contains(*needles: str) -> Callable[[str], bool]:
    return lambda a: all(n.lower() in a.lower() for n in needles)


def _number(n: float) -> Callable[[str], bool]:
    def g(a: str) -> bool:
        nums = re.findall(r"-?\d+(?:[.,]\d+)?", a.replace(",", ""))
        return any(abs(float(x) - n) < 1e-6 for x in nums[-3:])
    return g


DOC = ("Meeting notes, 3 March: The team agreed to migrate the billing service from MySQL to PostgreSQL by 15 May. "
       "Aïcha owns the data migration; Tom owns the rollback plan. Budget approved: 12,000 EUR. "
       "Risk: the reporting jobs depend on MySQL-specific SQL and must be rewritten first. Next review on 20 March.")

SUITE: list[dict[str, Any]] = [
    {"id": "s1", "cat": "simple", "task": "What is the capital of Australia? Answer in one word.", "grade": _contains("canberra")},
    {"id": "s2", "cat": "simple", "task": "Rewrite more politely: 'send me the report now'. One sentence only.", "grade": lambda a: "report" in a.lower() and len(a) < 300},
    {"id": "s3", "cat": "simple", "task": "Translate to French: 'The meeting is postponed until Monday.'", "grade": _contains("réunion", "lundi")},
    {"id": "m1", "cat": "summarize", "task": f"From these notes, who owns the rollback plan and what is the deadline of the migration? Notes: {DOC}", "grade": _contains("tom", "15 may")},
    {"id": "m2", "cat": "summarize", "task": f"Summarize in two sentences, keeping the budget figure: {DOC}", "grade": lambda a: "12" in a and "postgres" in a.lower()},
    {"id": "m3", "cat": "summarize", "task": "Classify the sentiment of this review as positive, negative or mixed, one word: 'Great battery, but the screen cracked after a week and support ignored me.'", "grade": _contains("mixed")},
    {"id": "c1", "cat": "code", "task": "Write a Python function `is_palindrome(s: str) -> bool` that ignores case, spaces and punctuation. Reply with one python code block.",
     "grade": _code_grader("assert is_palindrome('A man, a plan, a canal: Panama')\nassert not is_palindrome('hello')\nassert is_palindrome('')\n")},
    {"id": "c2", "cat": "code", "task": "Write a Python function `merge_intervals(iv: list[list[int]]) -> list[list[int]]` that merges overlapping closed intervals and returns them sorted. Reply with one python code block.",
     "grade": _code_grader("assert merge_intervals([[1,3],[2,6],[8,10],[15,18]])==[[1,6],[8,10],[15,18]]\nassert merge_intervals([[1,4],[4,5]])==[[1,5]]\nassert merge_intervals([])==[]\nassert merge_intervals([[5,6],[1,2]])==[[1,2],[5,6]]\n")},
    {"id": "c3", "cat": "code", "task": "Write a Python function `parse_duration(s: str) -> int` converting strings like '1h30m', '45s', '2h', '3m15s' to total seconds. Raise ValueError on invalid input such as '' or '5x'. Reply with one python code block.",
     "grade": _code_grader("assert parse_duration('1h30m')==5400\nassert parse_duration('45s')==45\nassert parse_duration('3m15s')==195\nassert parse_duration('2h')==7200\nimport sys\nfor bad in ['', '5x', 'h']:\n    try:\n        parse_duration(bad); sys.exit(1)\n    except ValueError: pass\n")},
    {"id": "c4", "cat": "code", "task": "Write a Python class `LRUCache` with `__init__(self, capacity: int)`, `get(self, key) -> int` returning -1 if absent, and `put(self, key, value)`, evicting the least recently used item. Reply with one python code block.",
     "grade": _code_grader("c=LRUCache(2)\nc.put(1,1);c.put(2,2)\nassert c.get(1)==1\nc.put(3,3)\nassert c.get(2)==-1\nc.put(4,4)\nassert c.get(1)==-1 and c.get(3)==3 and c.get(4)==4\n")},
    {"id": "r1", "cat": "reasoning", "task": "A train leaves at 09:40 and arrives at 13:15 the same day. It stopped for 25 minutes in total. How many minutes was it moving? Give the final number last.", "grade": _number(190)},
    {"id": "r2", "cat": "reasoning", "task": "I have 3 boxes. Box A has twice as many apples as box B. Box C has 5 fewer than box A. Altogether there are 45 apples. How many apples are in box C? Give the final number last.", "grade": _number(15)},
    {"id": "r3", "cat": "reasoning", "task": "A laptop costs 1200 EUR. It is discounted by 15%, then 20% VAT is added to the discounted price. What is the final price in EUR? Give the final number last.", "grade": _number(1224)},
    {"id": "x1", "cat": "complex_code", "task": "Design and implement, in Python, a thread-safe token-bucket rate limiter class `TokenBucket(rate: float, capacity: int)` with method `allow(n: int = 1) -> bool`, using time.monotonic. Reply with one python code block.",
     "grade": _code_grader("import time\nb=TokenBucket(rate=1000, capacity=5)\nassert all(b.allow() for _ in range(5))\nassert not b.allow(10)\ntime.sleep(0.01)\nassert b.allow()\n")},
]


def speed(mf: MosaForge, models: list[str], prompt_tokens_target: int = 600) -> list[dict[str, Any]]:
    """Cold + warm generation speed per local model, with Ollama-reported memory footprint."""
    prov = mf.providers.get("ollama-local")
    assert isinstance(prov, OllamaProvider)
    filler = ("MosaForge routes tasks between local and cloud models. " * (prompt_tokens_target // 9))
    out = []
    for mid in models:
        spec = mf.cfg.model(mid)
        if spec.name not in prov.installed():
            out.append({"model": spec.name, "error": "not installed"})
            continue
        try:  # unload first so the first call is a true cold start
            prov.chat(spec, [Message("user", "hi")], keep_alive=0, num_predict=1)
        except Exception:
            pass
        row: dict[str, Any] = {"model": spec.name}
        for phase in ("cold", "warm"):
            r = prov.chat(spec, [Message("user", filler + "\nIn about 150 words, explain why routing helps.")], num_predict=200, think=False, temperature=0)
            raw = r.raw
            pe_n, pe_d = raw.get("prompt_eval_count", 0), raw.get("prompt_eval_duration", 0) or 1
            row[f"{phase}_load_s"] = round(r.load_s, 2)
            row[f"{phase}_prefill_tok_s"] = round(pe_n / (pe_d / 1e9), 1)
            row[f"{phase}_gen_tok_s"] = round(r.tokens_per_s, 1)
            row[f"{phase}_ttft_s"] = round(r.load_s + pe_d / 1e9, 2)
            row[f"{phase}_total_s"] = round(r.latency_s, 2)
        row["prompt_tokens"] = pe_n
        loaded = {m["name"]: m for m in prov.loaded()}
        m = loaded.get(spec.name, {})
        row["mem_gb"] = round(m.get("size", 0) / 1e9, 2)
        row["gpu_gb"] = round(m.get("size_vram", 0) / 1e9, 2)
        out.append(row)
    dm = mf.cfg.router.get("decision_model")
    if dm:
        t = []
        for i in range(6):
            t0 = time.time()
            prov.decide(dm, SUITE[i]["task"], {"c": {"type": "choice", "instructions": "category", "criteria": {"a": "x", "b": "y", "c": "z"}}})
            t.append(time.time() - t0)
        out.append({"model": dm, "decision_first_s": round(t[0], 2), "decision_warm_median_s": round(sorted(t[1:])[len(t[1:]) // 2], 3)})
    return out


def suite(mf: MosaForge, models: list[str], routed: bool = True, ids: list[str] | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    tasks = [t for t in SUITE if not ids or t["id"] in ids]
    rows: list[dict[str, Any]] = []
    modes = [(m, "forced") for m in models] + ([("router", "routed")] if routed else [])
    for model, mode in modes:
        for t in tasks:
            if mode == "forced":
                o = mf.ask(t["task"], model=model, category=t["cat"], escalate=False, allow_writes=False, remember=False)
            else:
                o = mf.ask(t["task"], escalate=True, allow_writes=False, remember=False)
            truth = bool(t["grade"](o.answer))
            for r in o.attempts:
                mf.store.mark(r.run_id, int(bool(t["grade"](r.answer))))
            row = {"mode": mode, "task": t["id"], "expected_cat": t["cat"], "routed_cat": o.route.category,
                   "model": o.final.model, "attempts": [a.model for a in o.attempts], "truth": truth, "verified": o.verified,
                   "first_verified": o.attempts[0].verified, "first_truth": bool(t["grade"](o.attempts[0].answer)),
                   "latency_s": round(sum(a.latency_s for a in o.attempts), 2), "tokens": sum(a.prompt_tokens + a.completion_tokens for a in o.attempts),
                   "error": o.final.error[:120]}
            rows.append(row)
            log(json.dumps(row))
    return {"rows": rows, "summary": summarize(rows)}


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by: dict[str, dict[str, Any]] = {}
    for r in rows:
        key = r["model"] if r["mode"] == "forced" else "ROUTED"
        b = by.setdefault(key, {"n": 0, "pass": 0, "latency": 0.0, "tokens": 0})
        b["n"] += 1
        b["pass"] += int(r["truth"])
        b["latency"] += r["latency_s"]
        b["tokens"] += r["tokens"]
    table = {k: {"tasks": v["n"], "pass_rate": round(v["pass"] / v["n"], 2), "total_latency_s": round(v["latency"], 1), "tokens": v["tokens"]} for k, v in by.items()}
    routed = [r for r in rows if r["mode"] == "routed"]
    router_acc = round(sum(r["routed_cat"] == r["expected_cat"] for r in routed) / len(routed), 2) if routed else None
    # Verifier vs ground truth, on first attempts of forced runs (where the verifier had a real decision to make).
    judged = [r for r in rows if r["mode"] == "forced" and r["first_verified"] != "unknown"]
    tp = sum(r["first_verified"] == "pass" and r["first_truth"] for r in judged)
    fp = sum(r["first_verified"] == "pass" and not r["first_truth"] for r in judged)
    tn = sum(r["first_verified"] == "fail" and not r["first_truth"] for r in judged)
    fn = sum(r["first_verified"] == "fail" and r["first_truth"] for r in judged)
    return {"by_model": table, "router_category_accuracy": router_acc,
            "verifier": {"judged": len(judged), "tp": tp, "fp": fp, "tn": tn, "fn": fn,
                         "accuracy": round((tp + tn) / len(judged), 2) if judged else None}}


class _Sampler:
    """Polls Mac (local) and Dell (worker /v1/status) RAM/CPU every `interval` seconds."""

    def __init__(self, mf: MosaForge, interval: float = 1.0):
        import threading
        from . import sysstats
        self.mf, self.interval, self.sysstats = mf, interval, sysstats
        self.mac: list[dict[str, Any]] = []
        self.dell: list[dict[str, Any]] = []
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        dell = self.mf.providers.get("dell")
        while not self._stop.is_set():
            try:
                self.mac.append({**self.sysstats.memory(), "cpu_pct": self.sysstats.cpu_percent()})
            except Exception:
                pass
            if dell is not None:
                try:
                    self.dell.append(dell.status().get("system", {}))
                except Exception:
                    pass
            self._stop.wait(self.interval)

    def __enter__(self) -> "_Sampler":
        self._t.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._stop.set()
        self._t.join(timeout=10)

    @staticmethod
    def _agg(samples: list[dict[str, Any]], base: dict[str, Any] | None = None) -> dict[str, Any]:
        if not samples:
            return {}
        cpu = [s["cpu_pct"] for s in samples if s.get("cpu_pct") is not None]
        out = {"ram_used_peak_gb": max(s.get("used_gb", 0) for s in samples),
               "swap_peak_gb": max(s.get("swap_used_gb", 0) for s in samples),
               "cpu_avg_pct": round(sum(cpu) / len(cpu), 1) if cpu else None, "cpu_peak_pct": max(cpu) if cpu else None}
        if base:
            out["ram_delta_peak_gb"] = round(out["ram_used_peak_gb"] - base.get("used_gb", 0), 2)
            out["swap_delta_peak_gb"] = round(out["swap_peak_gb"] - base.get("swap_used_gb", 0), 2)
        return out


LAN_CONFIGS = {
    "A": {"desc": "Mac local: qwen3.5:4b on the Mac", "model": "local-small", "node": "ollama-local", "concurrency": 1},
    "B": {"desc": "Dell worker: qwen3.5:4b on the Dell", "model": "local-small", "node": "dell", "concurrency": 1},
    "B9": {"desc": "Dell worker: qwen3.5:9b on the Dell", "model": "local-strong", "node": "dell", "concurrency": 1},
    "C": {"desc": "Routed: Mac router; least-busy of Mac/Dell; 2 tasks in flight", "model": None, "node": None, "concurrency": 2},
}


def lan(mf: MosaForge, config: str, ids: list[str] | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    from concurrent.futures import ThreadPoolExecutor
    from . import sysstats

    cfg = LAN_CONFIGS[config]
    tasks = [t for t in SUITE if not ids or t["id"] in ids]
    dell = mf.providers.get("dell")
    base_mac = sysstats.memory()
    base_dell = dell.status().get("system", {}) if dell is not None else {}

    def run(t: dict[str, Any]) -> dict[str, Any]:
        t0 = time.time()
        try:
            o = mf.ask(t["task"], model=cfg["model"], node=cfg["node"], category=t["cat"] if cfg["model"] else None,
                       escalate=cfg["model"] is None, allow_writes=False, remember=False)
        except Exception as e:  # count, don't crash the batch
            return {"task": t["id"], "truth": False, "error": f"{type(e).__name__}: {e}"[:160], "wall_s": round(time.time() - t0, 2)}
        for r in o.attempts:
            mf.store.mark(r.run_id, int(bool(t["grade"](r.answer))))
        a = o.attempts
        row = {"task": t["id"], "cat": t["cat"], "routed_cat": o.route.category, "truth": bool(t["grade"](o.answer)),
               "attempts": [f"{x.model}@{x.provider}" for x in a], "verified": o.verified,
               "latency_s": round(sum(x.latency_s for x in a), 2), "wall_s": round(time.time() - t0, 2),
               "load_s": round(sum(x.load_s for x in a), 2), "tok_s": round(a[-1].tokens_per_s, 1),
               "tokens": sum(x.prompt_tokens + x.completion_tokens for x in a), "network_s": round(sum(x.network_s for x in a), 3),
               "bytes_lan": sum(x.bytes_lan for x in a), "error": a[-1].error[:160]}
        log(json.dumps(row))
        return row

    with _Sampler(mf) as s:
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as ex:
            rows = list(ex.map(run, tasks))
        wall = time.time() - t0
    n = len(rows)
    by_node: dict[str, int] = {}
    for r in rows:
        for a in r.get("attempts", []):
            by_node[a.split("@")[-1]] = by_node.get(a.split("@")[-1], 0) + 1
    summary = {
        "config": config, "desc": cfg["desc"], "tasks": n, "correct": sum(r["truth"] for r in rows),
        "failures": sum(bool(r.get("error")) for r in rows), "batch_wall_s": round(wall, 1),
        "sum_latency_s": round(sum(r.get("latency_s", 0) for r in rows), 1),
        "p50_latency_s": sorted(r.get("latency_s", 0) for r in rows)[n // 2] if n else None,
        "cold_load_s": max((r.get("load_s", 0) for r in rows), default=0), "mean_tok_s": round(sum(r.get("tok_s", 0) for r in rows) / max(n, 1), 1),
        "tasks_per_min": round(60 * n / wall, 2) if wall else None, "lan_bytes": sum(r.get("bytes_lan", 0) for r in rows),
        "network_s": round(sum(r.get("network_s", 0) for r in rows), 2), "attempts_by_node": by_node,
        "mac": s._agg(s.mac, base_mac), "dell": s._agg(s.dell, base_dell),
    }
    return {"summary": summary, "rows": rows}
