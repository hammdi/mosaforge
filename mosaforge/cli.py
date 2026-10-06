"""mosaforge CLI: ask | route | models | memory | stats | feedback | bench | worker | mcp"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path


def _print_table(rows: list[dict]) -> None:
    if not rows:
        print("(no rows)")
        return
    cols = list(rows[0])
    w = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    print("  ".join(c.ljust(w[c]) for c in cols))
    for r in rows:
        print("  ".join(str(r.get(c, "")).ljust(w[c]) for c in cols))


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="mosaforge")
    ap.add_argument("--workspace", help="directory agents may read/write (default from config)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("ask", help="run a task through router -> agent -> verification -> memory")
    a.add_argument("task", nargs="+")
    a.add_argument("--model")
    a.add_argument("--category", choices=["simple", "summarize", "code", "complex_code", "reasoning"])
    a.add_argument("--node", help="provider id to force, e.g. dell")
    a.add_argument("--privacy", default="private", choices=["public", "private", "secret"])
    a.add_argument("--verify", dest="verify_cmd", help="allowlisted acceptance command")
    a.add_argument("--allow-cloud", action="store_true", help="permit escalation to cloud models for this task")
    a.add_argument("--read-only", action="store_true")
    a.add_argument("--json", action="store_true")

    r = sub.add_parser("route", help="dry-run the router")
    r.add_argument("task", nargs="+")

    sub.add_parser("models", help="configured models and provider health")

    pc = sub.add_parser("place", help="Resource Manager dry run: which model on which node, and why (no execution)")
    pc.add_argument("task", nargs="*")
    pc.add_argument("--category", choices=["simple", "summarize", "code", "complex_code", "reasoning"])
    pc.add_argument("--difficulty", type=float)
    pc.add_argument("--privacy", default="private", choices=["public", "private", "secret"])
    pc.add_argument("--background", action="store_true", help="nobody is waiting for the answer")
    pc.add_argument("--context-tokens", type=int, default=0, help="extra input (documents) in tokens")
    pc.add_argument("--batch", type=int, default=1, help="plan N independent copies of the task")
    pc.add_argument("--scenario", help="JSON file or built-in case name overriding node states")
    pc.add_argument("--demo", action="store_true", help="run the built-in placement cases (mosaforge/scenarios.py)")

    m = sub.add_parser("memory", help="private memory")
    m.add_argument("action", choices=["add", "search", "list", "forget"])
    m.add_argument("text", nargs="*")
    m.add_argument("--scope", default="project", choices=["project", "long_term", "skill", "experience"])

    sub.add_parser("stats", help="experience log aggregated by category/model/node")
    f = sub.add_parser("feedback", help="mark a run as correct/incorrect")
    f.add_argument("run_id")
    f.add_argument("verdict", choices=["good", "bad"])

    b = sub.add_parser("bench", help="benchmarks (speed | suite)")
    b.add_argument("kind", choices=["speed", "suite", "lan"])
    b.add_argument("--config", choices=["A", "B", "B9", "C"], help="lan: which configuration (see bench.LAN_CONFIGS)")
    b.add_argument("--models", default="local-small,local-coder,local-strong")
    b.add_argument("--tasks", help="comma-separated task ids (default all)")
    b.add_argument("--no-routed", action="store_true")
    b.add_argument("--out", default="bench-results")

    w = sub.add_parser("worker", help="serve inference to the LAN (run on the Dell)")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--models", required=True, help="comma-separated Ollama model names this worker may serve")
    w.add_argument("--ollama", default="http://127.0.0.1:11434")
    w.add_argument("--concurrency", type=int, default=1)
    w.add_argument("--allow-all-interfaces", action="store_true", help="permit 0.0.0.0 (not recommended)")

    sub.add_parser("mcp", help="MCP stdio server (for Claude Code)")

    args = ap.parse_args(argv)

    if args.cmd == "worker":
        from .worker import serve
        serve(args.host, args.port, [x for x in args.models.split(",") if x], args.ollama, args.concurrency, args.allow_all_interfaces)
        return
    if args.cmd == "mcp":
        from .mcp_server import main as mcp_main
        mcp_main()
        return

    from .orchestrator import MosaForge

    if args.cmd == "bench":
        from . import bench
        mf = MosaForge(workspace=args.workspace, db_name="bench.db")
        models = [x for x in args.models.split(",") if x]
        out = Path(args.out)
        out.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if args.kind == "speed":
            rows = bench.speed(mf, models)
            _print_table(rows)
            (out / f"speed-{stamp}.json").write_text(json.dumps(rows, indent=1))
        elif args.kind == "lan":
            res = bench.lan(mf, args.config, ids=args.tasks.split(",") if args.tasks else None, log=lambda s: print(s, file=sys.stderr))
            (out / f"lan-{args.config}-{stamp}.json").write_text(json.dumps(res, indent=1))
            print(json.dumps(res["summary"], indent=1))
        else:
            res = bench.suite(mf, models, routed=not args.no_routed, ids=args.tasks.split(",") if args.tasks else None,
                              log=lambda s: print(s, file=sys.stderr))
            (out / f"suite-{stamp}.json").write_text(json.dumps(res, indent=1))
            print(json.dumps(res["summary"], indent=1))
        return

    mf = MosaForge(workspace=args.workspace)
    if args.cmd == "ask":
        if args.allow_cloud:
            mf.router.allow_cloud = True
        try:
            o = mf.ask(" ".join(args.task), privacy=args.privacy, model=args.model, category=args.category, node=args.node,
                       verify_cmd=args.verify_cmd, allow_writes=not args.read_only)
        except PermissionError as e:
            sys.exit(f"refused by privacy policy: {e}")
        if args.json:
            print(json.dumps({"answer": o.answer, "verified": o.verified, "route": o.route.reasons,
                              "attempts": [{"model": x.model, "node": x.node, "verified": x.verified, "detail": x.verify_detail,
                                            "latency_s": round(x.latency_s, 2), "error": x.error} for x in o.attempts],
                              "run_id": o.final.run_id}, indent=1))
        else:
            print(o.answer)
            print("\n---")
            print(f"category={o.route.category} agent={o.route.agent} run_id={o.final.run_id}")
            for x in o.attempts:
                print(f"  attempt {x.attempt}: {x.model}@{x.provider or '?'} verified={x.verified} ({x.verify_detail[:90]}) "
                      f"{x.latency_s:.1f}s {x.tokens_per_s:.0f} tok/s{' ERROR ' + x.error[:120] if x.error else ''}")
            print("  route: " + " | ".join(o.route.reasons))
    elif args.cmd == "place":
        from . import scenarios
        from .resources import decide
        if args.demo:
            for name, c in scenarios.CASES.items():
                d = decide(mf, c["task"], category=c.get("category"), difficulty=c.get("difficulty"),
                           privacy=c.get("privacy", "private"), interactive=c.get("interactive", True),
                           context_tokens=c.get("context_tokens", 0), scenario=c.get("scenario"), batch=c.get("batch", 1))
                if "batch" in d:
                    print(f"{name:34s} batch={d['batch']} -> {d['assignments']} makespan {d['estimated_makespan_ms'] / 1000:.1f} s "
                          f"(vs {d['single_node_serial_ms'] / 1000:.1f} s all on {d['single_node']})")
                else:
                    print(f"{name:34s} -> {d['model'] or '-'} @ {d['node'] or 'NONE'}  est {d['estimated_latency_ms'] / 1000:.1f} s  "
                          f"net {d['network_overhead_ms']:.0f} ms  conf {d['confidence']}  | {d['reason']}")
            return
        scen = None
        if args.scenario:
            p = Path(args.scenario)
            scen = json.loads(p.read_text()) if p.is_file() else scenarios.CASES[args.scenario].get("scenario")
        d = decide(mf, " ".join(args.task), category=args.category, difficulty=args.difficulty, privacy=args.privacy,
                   interactive=not args.background, context_tokens=args.context_tokens, scenario=scen, batch=args.batch)
        print(json.dumps(d, indent=1, default=list))
    elif args.cmd == "route":
        d = mf.router.route(" ".join(args.task))
        print(json.dumps({"category": d.category, "model": d.model.id, "agent": d.agent, "confidence": round(d.confidence, 2),
                          "difficulty": round(d.difficulty, 2), "reasons": d.reasons}, indent=1))
    elif args.cmd == "models":
        rows = []
        for p_id, p in mf.providers.items():
            inst = p.installed() if hasattr(p, "installed") else set()
            for spec in mf.cfg.models:
                if p_id in spec.providers:
                    rows.append({"model": spec.id, "runtime_name": spec.name, "tier": spec.tier, "provider": p_id,
                                 "location": p.location, "healthy": p.healthy(),
                                 "installed": (spec.name in inst) if hasattr(p, "installed") else "n/a"})
        _print_table(rows)
    elif args.cmd == "memory":
        text = " ".join(args.text)
        if args.action == "add":
            print("saved #", mf.store.add(args.scope, text, source="cli", workspace=str(mf.cfg.workspace)))
        elif args.action == "search":
            _print_table([{k: (str(v)[:80]) for k, v in r.items()} for r in mf.store.search(text, limit=10)])
        elif args.action == "list":
            _print_table([{k: str(r[k])[:80] for k in ("id", "scope", "content", "uses")} for r in mf.store.list(args.scope)])
        else:
            mf.store.forget(int(text))
            print("forgotten")
    elif args.cmd == "stats":
        _print_table(mf.store.stats())
    elif args.cmd == "feedback":
        print(mf.store.mark(args.run_id, 1 if args.verdict == "good" else 0, 0 if args.verdict == "good" else 1), "run(s) updated")


if __name__ == "__main__":
    main()
