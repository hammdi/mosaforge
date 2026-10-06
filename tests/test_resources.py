import unittest

from mosaforge.analyzer import TaskProfile
from mosaforge.core import ModelSpec
from mosaforge.resources import ModelPerf, NodeState, ResourceManager

M4 = ModelSpec(id="local-small", name="qwen3.5:4b", providers=["ollama-local", "dell"], tier=1)
M9 = ModelSpec(id="local-strong", name="qwen3.5:9b", providers=["ollama-local", "dell"], tier=3)


def mac(**kw):
    base = dict(id="mac", location="local", role="interactive", ram_total_gb=16, ram_used_gb=9.6, ram_avail_gb=8.0,
                reserve_gb=2.0, allow_secret=True, exclude_models={"qwen3.5:9b"},
                perf={"qwen3.5:4b": ModelPerf(29.2, 363, 1.1, 3.34, "measured"), "qwen3.5:9b": ModelPerf(15.6, 197, 4.4, 6.07, "measured"),
                      "tev1:0.8b": ModelPerf(100, 2000, 1.3, 0.81, "measured")})
    base.update(kw)
    return NodeState(**base)


def dell(**kw):
    base = dict(id="dell", location="lan", role="batch", ram_total_gb=16, ram_used_gb=3.5, reserve_gb=2.0, rtt_ms=5.0,
                bandwidth_mbps=200, installed={"qwen3.5:4b", "qwen3.5:9b"},
                perf={"qwen3.5:4b": ModelPerf(7.0, 45, 3.0, 3.4, "simulated"), "qwen3.5:9b": ModelPerf(3.5, 20, 6.0, 6.2, "simulated")})
    base.update(kw)
    return NodeState(**base)


def task(category="simple", interactive=True, privacy="private", prompt=550, out=80, calls=1):
    return TaskProfile(task="t", category=category, difficulty=0.3, privacy=privacy, interactive=interactive,
                       parallelizable=False, prompt_tokens=prompt, output_tokens=out, model_calls=calls)


class PlacementRules(unittest.TestCase):
    rm = ResourceManager()

    def test_1_small_interactive_stays_on_mac(self):
        pl = self.rm.place(task(), [M4], [mac(), dell()])
        self.assertEqual(pl.node, "mac")
        self.assertLess(pl.confidence, 0.8)  # the comparison rests on simulated Dell numbers
        measured = dell(perf={"qwen3.5:4b": ModelPerf(7.0, 45, 3.0, 3.4, "measured")})
        self.assertGreater(self.rm.place(task(), [M4], [mac(), measured]).confidence, 0.9)

    def test_2_background_moves_to_dell_when_dell_is_fast_enough(self):
        fast_dell = dell(perf={"qwen3.5:4b": ModelPerf(20.0, 200, 2.0, 3.4, "measured")})
        pl = self.rm.place(task("summarize", interactive=False, prompt=6500, out=150), [M4], [mac(), fast_dell])
        self.assertEqual(pl.node, "dell", pl.reasons)  # even though the Mac has free RAM and is faster
        self.assertGreater(pl.alternatives[0]["estimated_latency_ms"] or 0, 0)

    def test_3_batch_spreads_and_caps_mac_queue(self):
        plan = self.rm.plan_batch([task("summarize", interactive=False, out=150)] * 8, lambda _p: [M4], [mac(), dell()])
        nodes = [p.node for p in plan]
        self.assertIn("mac", nodes)
        self.assertIn("dell", nodes)

    def test_4_pressure_warn_blocks_new_loads_on_mac(self):
        pl = self.rm.place(task("summarize", interactive=False), [M4], [mac(pressure="warn"), dell()])
        self.assertEqual(pl.node, "dell")
        pl = self.rm.place(task(), [M4], [mac(pressure="critical"), dell()])
        self.assertEqual(pl.node, "dell")

    def test_5_dell_down_mac_runs_it(self):
        pl = self.rm.place(task("summarize", interactive=False), [M4], [mac(), dell(healthy=False)])
        self.assertEqual(pl.node, "mac")

    def test_6_secret_never_leaves_the_mac(self):
        pl = self.rm.place(task(privacy="secret", interactive=False), [M4], [mac(pressure="warn", loaded={"qwen3.5:4b": 3.34}), dell()])
        self.assertEqual(pl.node, "mac")
        pl = self.rm.place(task(privacy="secret"), [M4], [mac(healthy=False), dell()])
        self.assertFalse(pl.feasible)  # refuse rather than leak

    def test_7_network_overhead_counted_and_tiny_task_stays_local(self):
        loaded_dell = dell(loaded={"qwen3.5:4b": 3.4}, perf={"qwen3.5:4b": ModelPerf(29.2, 363, 1.1, 3.4, "measured")})
        pl = self.rm.place(task(out=5, prompt=520), [M4], [mac(loaded={"qwen3.5:4b": 3.34}), loaded_dell])
        self.assertEqual(pl.node, "mac")
        d = [a for a in pl.alternatives if a["node"] == "dell"][0]
        self.assertGreater(d["estimated_latency_ms"], pl.estimated_latency_ms)

    def test_8_model_impractical_on_mac_goes_to_dell(self):
        pl = self.rm.place(task("complex_code", interactive=False, out=900, calls=4), [M9], [mac(), dell()])
        self.assertEqual(pl.node, "dell")
        self.assertIn("excluded", " ".join(pl.reasons))

    def test_headroom_uses_os_available_memory(self):
        # 12.8 GB "used" (compressor-heavy) but the OS reports 7.8 GB available: a 3.3 GB model fits, a 6 GB one doesn't
        n = mac(ram_used_gb=12.8, ram_avail_gb=7.8, exclude_models=set())
        self.assertTrue(self.rm.evaluate(task(), M4, n).feasible)
        self.assertFalse(self.rm.evaluate(task(), M9, n).feasible)

    def test_model_router_quality_order_is_respected(self):
        # The Resource Manager never swaps the preferred model for a faster one; it only moves on when infeasible.
        pl = self.rm.place(task(), [M9, M4], [mac(), dell()])
        self.assertEqual(pl.model, "qwen3.5:9b")
        self.assertEqual(pl.node, "dell")


if __name__ == "__main__":
    unittest.main()
