"""ModelProvider adapters: Ollama (local), MosaForge LAN worker, Claude Code CLI (cloud).

Ollama is spoken to through its *native* API (/api/chat) rather than /v1: the
OpenAI-compatible endpoint flattens or drops thinking output (noted in DeerFlow's
config.example.yaml, ollama/ollama#15293). Decision models use /v1/systemone.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any

from .core import ChatResult, Message, ModelSpec


class ProviderError(RuntimeError):
    pass


def _post(url: str, body: dict[str, Any], timeout: float, headers: dict[str, str] | None = None) -> dict[str, Any]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise ProviderError(f"{url}: HTTP {e.code}: {e.read()[:300]!r}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        raise ProviderError(f"{url}: {e}") from e


def _get(url: str, timeout: float = 3.0, headers: dict[str, str] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def _normalize_tool_calls(calls: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    out = []
    for c in calls or []:
        fn = c.get("function", {})
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {"_raw": args}
        out.append({"name": fn.get("name", ""), "arguments": args})
    return out


class OllamaProvider:
    def __init__(self, id: str, base_url: str, location: str = "local", timeout: float = 600.0):
        self.id = id
        self.base_url = base_url.rstrip("/")
        self.location = location
        self.timeout = timeout

    def healthy(self) -> bool:
        try:
            _get(f"{self.base_url}/api/version", timeout=2.0)
            return True
        except Exception:
            return False

    def installed(self) -> set[str]:
        try:
            return {m["name"] for m in _get(f"{self.base_url}/api/tags").get("models", [])}
        except Exception:
            return set()

    def loaded(self) -> list[dict[str, Any]]:
        return _get(f"{self.base_url}/api/ps").get("models", [])

    def chat(self, model: ModelSpec, messages: list[Message], tools: list[dict[str, Any]] | None = None, **opts: Any) -> ChatResult:
        body: dict[str, Any] = {
            "model": model.name,
            "messages": [m.to_dict() for m in messages],
            "stream": False,
            "options": {"num_ctx": opts.get("num_ctx", model.num_ctx), "temperature": opts.get("temperature", 0.2)},
            "keep_alive": opts.get("keep_alive", "10m"),
        }
        if "num_predict" in opts:
            body["options"]["num_predict"] = opts["num_predict"]
        # Only send `think` explicitly: hybrid models (qwen3.x) default to thinking otherwise.
        body["think"] = bool(opts.get("think", model.think))
        if tools and model.tools:
            body["tools"] = tools
        if opts.get("format"):
            body["format"] = opts["format"]
        t0 = time.time()
        r = _post(f"{self.base_url}/api/chat", body, self.timeout)
        msg = r.get("message", {})
        ev_n, ev_d = r.get("eval_count", 0), r.get("eval_duration", 0) or 1
        return ChatResult(
            content=msg.get("content", "") or "",
            tool_calls=_normalize_tool_calls(msg.get("tool_calls")),
            prompt_tokens=r.get("prompt_eval_count", 0),
            completion_tokens=ev_n,
            latency_s=time.time() - t0,
            tokens_per_s=ev_n / (ev_d / 1e9) if ev_n else 0.0,
            load_s=r.get("load_duration", 0) / 1e9,
            raw={k: v for k, v in r.items() if k != "message"},
        )

    def decide(self, model: str, state: Any, questions: dict[str, Any], timeout: float = 60.0) -> dict[str, Any]:
        """Typed decision via /v1/systemone (choice | noul | score). Zero decoded text."""
        return _post(f"{self.base_url}/v1/systemone", {"model": model, "state": state, "questions": questions, "keep_alive": "30m"}, timeout)


class WorkerProvider:
    """A MosaForge worker on another LAN machine (see worker.py). Bearer-token authenticated."""

    def __init__(self, id: str, base_url: str, token_env: str = "MOSAFORGE_WORKER_TOKEN", location: str = "lan", timeout: float = 900.0):
        self.id = id
        self.base_url = base_url.rstrip("/")
        self.location = location
        self.timeout = timeout
        self.token_env = token_env

    def _headers(self) -> dict[str, str]:
        tok = os.environ.get(self.token_env, "")
        if not tok:
            raise ProviderError(f"worker {self.id}: ${self.token_env} is not set")
        return {"Authorization": f"Bearer {tok}"}

    def healthy(self) -> bool:
        try:
            return _get(f"{self.base_url}/health", timeout=2.0).get("ok") is True
        except Exception:
            return False

    def installed(self) -> set[str]:
        try:
            return set(_get(f"{self.base_url}/v1/models", headers=self._headers()).get("models", []))
        except Exception:
            return set()

    def chat(self, model: ModelSpec, messages: list[Message], tools: list[dict[str, Any]] | None = None, **opts: Any) -> ChatResult:
        # Tools are executed by the *caller* (the Mac), never by the worker: the worker
        # only does inference. This keeps the Dell free of filesystem/terminal exposure.
        body = {"model": model.name, "messages": [m.to_dict() for m in messages], "tools": tools if model.tools else None,
                "think": bool(opts.get("think", model.think)), "num_ctx": opts.get("num_ctx", model.num_ctx),
                "temperature": opts.get("temperature", 0.2)}
        t0 = time.time()
        r = _post(f"{self.base_url}/v1/chat", body, self.timeout, self._headers())
        bytes_out, bytes_in = len(json.dumps(body).encode()), len(json.dumps(r).encode())  # app-layer payload sizes
        res = ChatResult(**{k: r[k] for k in ("content", "prompt_tokens", "completion_tokens", "tokens_per_s", "load_s") if k in r})
        res.tool_calls = r.get("tool_calls", [])
        res.latency_s = time.time() - t0
        res.raw = {"worker_latency_s": r.get("latency_s"), "node": r.get("node"), "bytes_out": bytes_out, "bytes_in": bytes_in,
                   "network_s": max(res.latency_s - float(r.get("latency_s") or 0.0), 0.0)}
        return res

    def status(self) -> dict[str, Any]:
        return _get(f"{self.base_url}/v1/status", timeout=5.0, headers=self._headers())


class ClaudeCLIProvider:
    """Escalation to Claude through the user's own `claude -p` login (subscription or API key).

    Used only when the router allows cloud and the task's privacy level permits it.
    No tools are given to the cloud call: MosaForge executes tools locally.
    """

    def __init__(self, id: str, location: str = "cloud", timeout: float = 300.0):
        self.id = id
        self.location = location
        self.timeout = timeout

    def healthy(self) -> bool:
        return shutil.which("claude") is not None

    def chat(self, model: ModelSpec, messages: list[Message], tools: list[dict[str, Any]] | None = None, **opts: Any) -> ChatResult:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        convo = "\n\n".join(f"[{m.role}]\n{m.content}" for m in messages if m.role != "system")
        cmd = ["claude", "-p", convo, "--model", model.name, "--output-format", "json", "--tools", "",
               "--no-session-persistence", "--system-prompt", system or "You are a careful expert assistant."]
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        if p.returncode != 0:
            raise ProviderError(f"claude -p failed ({p.returncode}): {p.stderr[-400:]}")
        r = json.loads(p.stdout)
        usage = r.get("usage", {})
        return ChatResult(
            content=r.get("result", ""),
            prompt_tokens=usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            latency_s=time.time() - t0,
            cost_usd=float(r.get("total_cost_usd") or 0.0),
            raw={"session_id": r.get("session_id")},
        )


def build_providers(cfg_providers: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Instantiate enabled providers and attach node policy:
    exclude_models — runtime names this node must never load (e.g. the 9B model on the 16 GB Mac);
    allow_secret   — whether privacy=secret tasks may run here (default: only location == "local").
    """
    out: dict[str, Any] = {}
    for pid, p in cfg_providers.items():
        if p.get("enabled", True) is False:
            continue
        t = p.get("type")
        if t == "ollama":
            out[pid] = OllamaProvider(pid, p["base_url"], p.get("location", "local"))
        elif t == "worker":
            out[pid] = WorkerProvider(pid, p["base_url"], p.get("token_env", "MOSAFORGE_WORKER_TOKEN"), p.get("location", "lan"))
        elif t == "claude_cli":
            out[pid] = ClaudeCLIProvider(pid, p.get("location", "cloud"), float(p.get("timeout_s", 300)))
        else:
            continue
        out[pid].exclude_models = set(p.get("exclude_models", []))
        out[pid].allow_secret = bool(p.get("allow_secret", out[pid].location == "local"))
    return out
