"""LAN worker: inference-only HTTP service for a second machine (the Dell).

Security model (SECURITY.md §LAN):
- Exposes *inference only*. No filesystem, no shell, no tool execution. Tools always run on the
  caller, inside its workspace sandbox.
- Bearer token required on every endpoint except /health; constant-time comparison.
- Refuses to bind a non-loopback address without a token of >= 32 chars.
- Model allowlist: only models named in --models (or MOSAFORGE_WORKER_MODELS) can be invoked,
  so a stolen token cannot pull or run arbitrary models.
- Ollama on the worker stays bound to 127.0.0.1; this service is the only door.
- Request body size capped. Plain HTTP is acceptable only on a trusted LAN segment; put it behind
  TLS (Caddy/Tailscale) otherwise.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .core import Message, ModelSpec
from .providers import OllamaProvider, ProviderError
from . import sysstats

MAX_BODY = 2 * 1024 * 1024


class WorkerState:
    def __init__(self, token: str, models: set[str], ollama_url: str, max_concurrent: int):
        self.token = token
        self.models = models
        self.ollama = OllamaProvider("worker-ollama", ollama_url)
        self.sem = threading.BoundedSemaphore(max_concurrent)
        self.node = socket.gethostname()
        self.served = 0


def make_handler(state: WorkerState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "MosaForgeWorker/0.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # one line per request, no bodies
            print(f"[worker] {self.client_address[0]} {fmt % args}", flush=True)

        def _send(self, code: int, obj: dict[str, Any]) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authed(self) -> bool:
            got = self.headers.get("Authorization", "")
            ok = got.startswith("Bearer ") and hmac.compare_digest(got[7:].encode(), state.token.encode())
            if not ok:
                self._send(401, {"error": "unauthorized"})
            return ok

        def do_GET(self) -> None:
            if self.path == "/health":
                return self._send(200, {"ok": True, "busy": state.sem._value == 0})  # type: ignore[attr-defined]
            if not self._authed():
                return
            if self.path == "/v1/models":
                installed = state.ollama.installed()
                return self._send(200, {"node": state.node, "models": sorted(state.models & installed)})
            if self.path == "/v1/status":
                try:
                    loaded = [{"name": m["name"], "size": m.get("size"), "size_vram": m.get("size_vram")} for m in state.ollama.loaded()]
                except Exception:
                    loaded = []
                return self._send(200, {"node": state.node, "served": state.served, "loaded": loaded, "system": sysstats.snapshot()})
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            if not self._authed():
                return
            n = int(self.headers.get("Content-Length", "0") or 0)
            if n <= 0 or n > MAX_BODY:
                return self._send(413, {"error": "body missing or too large"})
            try:
                req = json.loads(self.rfile.read(n))
            except json.JSONDecodeError:
                return self._send(400, {"error": "invalid json"})
            if self.path != "/v1/chat":
                return self._send(404, {"error": "not found"})
            name = req.get("model", "")
            if name not in state.models:
                return self._send(403, {"error": f"model {name!r} not allowlisted on this worker"})
            msgs = [Message(m["role"], m.get("content", ""), m.get("tool_calls"), m.get("tool_name")) for m in req.get("messages", [])]
            spec = ModelSpec(id=name, name=name, providers=[], tier=0, tools=bool(req.get("tools")), think=bool(req.get("think")),
                             num_ctx=min(int(req.get("num_ctx", 8192)), 32768))
            if not state.sem.acquire(timeout=300):
                return self._send(503, {"error": "worker busy"})
            try:
                t0 = time.time()
                r = state.ollama.chat(spec, msgs, tools=req.get("tools"), temperature=float(req.get("temperature", 0.2)))
                state.served += 1
                self._send(200, {"content": r.content, "tool_calls": r.tool_calls, "prompt_tokens": r.prompt_tokens,
                                 "completion_tokens": r.completion_tokens, "tokens_per_s": r.tokens_per_s, "load_s": r.load_s,
                                 "latency_s": time.time() - t0, "node": state.node})
            except ProviderError as e:
                self._send(502, {"error": str(e)[:300]})
            finally:
                state.sem.release()

    return Handler


def check_bind(host: str, token: str, allow_all_interfaces: bool = False) -> None:
    """Refuse configurations that could expose the worker beyond the LAN."""
    if not token:
        raise SystemExit("MOSAFORGE_WORKER_TOKEN is required (even on loopback)")
    if host in ("localhost",):
        return
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise SystemExit(f"--host must be an IP address (got {host!r}); bind the Dell's LAN IP, not a name")
    if ip.is_loopback:
        return
    if len(token) < 32:
        raise SystemExit("refusing to listen on a non-loopback address without MOSAFORGE_WORKER_TOKEN (>= 32 chars). "
                         "Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'")
    if ip.is_unspecified and not allow_all_interfaces:
        raise SystemExit("refusing to bind all interfaces (0.0.0.0/::). Bind the LAN IP, or pass --allow-all-interfaces")
    if not ip.is_unspecified and not ip.is_private:
        raise SystemExit(f"refusing to bind public address {host}: the worker is LAN-only")


def serve(host: str, port: int, models: list[str], ollama_url: str = "http://127.0.0.1:11434", max_concurrent: int = 1,
          allow_all_interfaces: bool = False) -> None:
    token = os.environ.get("MOSAFORGE_WORKER_TOKEN", "")
    check_bind(host, token, allow_all_interfaces)
    loopback = host in ("127.0.0.1", "localhost", "::1")
    if not models:
        raise SystemExit("--models is required: the worker serves only an explicit allowlist")
    state = WorkerState(token, set(models), ollama_url, max_concurrent)
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"[worker] {state.node} serving {sorted(state.models)} on http://{host}:{port} (concurrency {max_concurrent}"
          f"{', loopback only' if loopback else ''})", flush=True)
    httpd.serve_forever()
