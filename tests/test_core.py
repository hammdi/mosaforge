import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from mosaforge.agents import text_tool_calls
from mosaforge.memory import Store
from mosaforge.router import heuristic_category
from mosaforge.tools import ToolError, Workspace, build_tools, call_tool
from mosaforge.worker import WorkerState, make_handler

ALLOWED = ["python3 -m py_compile", "python3 -m unittest"]


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.ws = Workspace(self.root, ALLOWED, timeout_s=10)
        self.tools = build_tools(self.ws)

    def test_path_escape_refused(self):
        for bad in ("../x", "/etc/passwd", "a/../../x", ".env", "sub/.env.local", ".git/config", ".ssh/id_rsa"):
            with self.assertRaises(ToolError, msg=bad):
                self.ws.resolve(bad)

    def test_write_and_read(self):
        self.assertIn("wrote", call_tool(self.tools, "write_file", {"path": "a/b.py", "content": "x = 1\n"}))
        self.assertEqual(call_tool(self.tools, "read_file", {"path": "a/b.py"}), "x = 1\n")

    def test_command_allowlist(self):
        for bad in ("rm -rf /", "python3 -c 'import os'", "bash -c ls", "python3 -m unittest x; rm -rf ~", "curl http://x"):
            self.assertTrue(call_tool(self.tools, "run_command", {"command": bad}).startswith("ERROR"), bad)
        (self.root / "ok.py").write_text("print(1)\n")
        self.assertTrue(call_tool(self.tools, "run_command", {"command": "python3 -m py_compile ok.py"}).startswith("exit=0"))

    def test_symlink_escape_refused(self):
        (self.root / "link").symlink_to("/etc")
        with self.assertRaises(ToolError):
            self.ws.resolve("link/hosts")


class AgentParsingTests(unittest.TestCase):
    def test_text_tool_calls(self):
        content = 'Sure.\n{"name": "write_file", "arguments": {"path": "a.py", "content": "x={1}"}}\n{"name": "nope", "arguments": {}}'
        calls = text_tool_calls(content, {"write_file"})
        self.assertEqual(calls, [{"name": "write_file", "arguments": {"path": "a.py", "content": "x={1}"}}])


class RouterTests(unittest.TestCase):
    def test_heuristics(self):
        self.assertEqual(heuristic_category("What is the capital of France?")[0], "simple")
        self.assertEqual(heuristic_category("Write a python function def foo() that parses a.py")[0], "code")
        self.assertEqual(heuristic_category("Refactor across the codebase: migrate every .py module")[0], "complex_code")
        self.assertEqual(heuristic_category("Summarize this article")[0], "summarize")


class MemoryTests(unittest.TestCase):
    def test_add_search_and_scopes(self):
        s = Store(Path(tempfile.mkdtemp()) / "m.db")
        s.add("long_term", "User prefers French answers and metric units")
        s.add("project", "MosaForge uses SQLite FTS5 for memory", workspace="/ws")
        self.assertEqual(s.search("which units does the user prefer")[0]["scope"], "long_term")
        self.assertEqual(len(s.search("FTS5 memory", workspace="/other")), 0)
        self.assertEqual(len(s.search("FTS5 memory", workspace="/ws")), 1)
        with self.assertRaises(ValueError):
            s.add("bogus", "x")


class WorkerAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        state = WorkerState("t" * 40, {"allowed-model"}, "http://127.0.0.1:9", 1)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def _req(self, path, body=None, token=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health_is_public(self):
        self.assertEqual(self._req("/health")[0], 200)

    def test_requires_token(self):
        self.assertEqual(self._req("/v1/chat", {"model": "allowed-model"})[0], 401)
        self.assertEqual(self._req("/v1/chat", {"model": "allowed-model"}, token="wrong")[0], 401)

    def test_model_allowlist(self):
        self.assertEqual(self._req("/v1/chat", {"model": "evil", "messages": []}, token="t" * 40)[0], 403)

    def test_no_other_endpoints(self):
        self.assertEqual(self._req("/v1/exec", {"cmd": "id"}, token="t" * 40)[0], 404)


if __name__ == "__main__":
    unittest.main()


class PrivacyTests(unittest.TestCase):
    def _router(self, allow_cloud):
        from mosaforge.config import load_config
        from mosaforge.router import Router
        cfg = load_config()
        r = Router(cfg, {"claude": object(), "ollama-local": object()})
        r.allow_cloud = allow_cloud
        r.decider = None
        return r

    def test_forced_cloud_model_blocked_for_secret(self):
        with self.assertRaises(PermissionError):
            self._router(True).route("hi", privacy="secret", force_model="claude-sonnet")

    def test_forced_cloud_model_blocked_without_allow_cloud(self):
        with self.assertRaises(PermissionError):
            self._router(False).route("hi", privacy="public", force_model="claude-sonnet")

    def test_cloud_never_a_candidate_for_secret(self):
        self.assertFalse(any(m.cloud for m in self._router(True).candidates("secret")))


class FakeProvider:
    def __init__(self, pid, location, installed=("m4", "m9"), healthy=True, exclude=(), allow_secret=None):
        self.id, self.location = pid, location
        self._installed, self._healthy = set(installed), healthy
        self.exclude_models = set(exclude)
        self.allow_secret = (location == "local") if allow_secret is None else allow_secret

    def healthy(self):
        return self._healthy

    def installed(self):
        return self._installed


class NodeSelectionTests(unittest.TestCase):
    def setUp(self):
        import os
        from mosaforge.core import ModelSpec
        from mosaforge.orchestrator import MosaForge
        self._home = os.environ.get("MOSAFORGE_HOME")
        os.environ["MOSAFORGE_HOME"] = tempfile.mkdtemp()
        self.mf = MosaForge(workspace=tempfile.mkdtemp())
        self.small = ModelSpec(id="small", name="m4", providers=["ollama-local", "dell"], tier=1)
        self.big = ModelSpec(id="big", name="m9", providers=["ollama-local", "dell"], tier=3)

    def tearDown(self):
        import os
        if self._home is None:
            os.environ.pop("MOSAFORGE_HOME", None)
        else:
            os.environ["MOSAFORGE_HOME"] = self._home

    def _set(self, mac, dell):
        self.mf.providers = {"ollama-local": mac, "dell": dell}

    def test_excluded_model_never_loads_on_mac(self):
        self._set(FakeProvider("ollama-local", "local", exclude=["m9"]), FakeProvider("dell", "lan"))
        self.assertEqual(self.mf._provider_for(self.big, None)[0], "dell")
        self._set(FakeProvider("ollama-local", "local", exclude=["m9"]), FakeProvider("dell", "lan", healthy=False))
        from mosaforge.providers import ProviderError
        with self.assertRaises(ProviderError):  # Dell down: fail rather than load the 9B on the Mac
            self.mf._provider_for(self.big, None)

    def test_secret_never_falls_back_to_lan(self):
        from mosaforge.providers import ProviderError
        self._set(FakeProvider("ollama-local", "local", healthy=False), FakeProvider("dell", "lan"))
        with self.assertRaises(ProviderError):
            self.mf._provider_for(self.small, None, privacy="secret")
        self.assertEqual(self.mf._provider_for(self.small, None, privacy="private")[0], "dell")

    def test_secret_forced_to_lan_is_refused_unless_allowed(self):
        self._set(FakeProvider("ollama-local", "local"), FakeProvider("dell", "lan"))
        with self.assertRaises(PermissionError):
            self.mf._provider_for(self.small, "dell", privacy="secret")
        self._set(FakeProvider("ollama-local", "local"), FakeProvider("dell", "lan", allow_secret=True))
        self.assertEqual(self.mf._provider_for(self.small, "dell", privacy="secret")[0], "dell")

    def test_node_policy_and_least_busy(self):
        self._set(FakeProvider("ollama-local", "local"), FakeProvider("dell", "lan"))
        self.assertEqual(self.mf._provider_for(self.small, None)[0], "ollama-local")
        self.mf.node_policy = {"summarize": ["dell", "ollama-local"]}
        self.assertEqual(self.mf._provider_for(self.small, None, category="summarize")[0], "dell")
        self.mf._inflight = {"ollama-local": 1}
        self.assertEqual(self.mf._provider_for(self.small, None, category="code")[0], "dell")


class WorkerExposureTests(unittest.TestCase):
    def test_bind_rules(self):
        from mosaforge.worker import check_bind
        good = "x" * 40
        check_bind("127.0.0.1", good)
        check_bind("192.168.100.5", good)
        for host, tok in (("192.168.100.5", "short"), ("0.0.0.0", good), ("8.8.8.8", good), ("dell.local", good), ("127.0.0.1", "")):
            with self.assertRaises(SystemExit, msg=host):
                check_bind(host, tok)
        check_bind("0.0.0.0", good, allow_all_interfaces=True)

    def test_worker_has_no_execution_surface(self):
        import ast
        import inspect
        import mosaforge.worker as w
        src = inspect.getsource(w)
        imported = {n.names[0].name.split(".")[0] for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Import)}
        imported |= {(n.module or "") for n in ast.walk(ast.parse(src)) if isinstance(n, ast.ImportFrom)}
        for banned in ("subprocess", "tools", "shutil", "agents", "orchestrator"):
            self.assertFalse(any(banned == i or i.endswith("." + banned) for i in imported), banned)
        self.assertNotIn("os.system", src)
        handler = w.make_handler(w.WorkerState("t" * 40, {"m"}, "http://127.0.0.1:9", 1))
        self.assertEqual({m for m in dir(handler) if m.startswith("do_")}, {"do_GET", "do_POST"})
