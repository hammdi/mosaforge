"""Placement scenarios for the Resource Manager prototype (simulated node states).

Each scenario overrides node states from mosaforge.toml [resources.nodes]. A node absent from a
scenario keeps its configured monitor: the real Mac (LocalMonitor) and the simulated Dell.
"""

from __future__ import annotations

from typing import Any

LONG_DOC_TOKENS = 6000  # ~24 KB of text to analyse

MAC_BUSY = {"ollama-local": {"simulated": True, "location": "local", "role": "interactive", "ram_used_gb": 11.5,
                             "loaded": {"qwen3.5:4b": 3.34, "tev1:0.8b": 0.81}, "inflight": 1, "busy_s": 8.0, "cpu_pct": 55.0}}
MAC_WARN = {"ollama-local": {"simulated": True, "location": "local", "role": "interactive", "ram_used_gb": 13.2,
                             "pressure": "warn", "loaded": {"tev1:0.8b": 0.81}, "cpu_pct": 30.0}}
DELL_DOWN = {"dell": {"healthy": False}}
DELL_BUSY = {"dell": {"inflight": 1, "busy_s": 120.0, "loaded": {"qwen3.5:9b": 6.2}, "ram_used_gb": 9.7}}

# name -> (task, category, difficulty, interactive, privacy, parallel batch size, context tokens, scenario)
CASES: dict[str, dict[str, Any]] = {
    "1-small-interactive": {"task": "What is the capital of Australia?", "category": "simple", "interactive": True},
    "2a-background-analysis-mac-idle": {"task": "Analyse this 24 KB incident log and list root causes.", "category": "summarize",
                                        "interactive": False, "context_tokens": LONG_DOC_TOKENS},
    "2b-background-analysis-mac-in-use": {"task": "Analyse this 24 KB incident log and list root causes.", "category": "summarize",
                                          "interactive": False, "context_tokens": LONG_DOC_TOKENS, "scenario": MAC_BUSY},
    "3-parallel-batch": {"task": "Summarise one customer review in one sentence.", "category": "summarize", "interactive": False,
                         "batch": 8},
    "4-mac-pressure-rising": {"task": "Summarise the attached design notes.", "category": "summarize", "interactive": False,
                              "context_tokens": 1500, "scenario": MAC_WARN},
    "5-dell-unavailable": {"task": "Summarise the attached design notes.", "category": "summarize", "interactive": False,
                           "context_tokens": 1500, "scenario": DELL_DOWN},
    "6-secret-task-mac-busy": {"task": "Summarise my medical notes.", "category": "summarize", "interactive": False,
                               "privacy": "secret", "scenario": MAC_BUSY},
    "7-tiny-task-network-vs-local": {"task": "Is 'great product, broke in a week' positive or negative?", "category": "simple",
                                     "interactive": True},
    "8-model-impractical-on-mac": {"task": "Refactor the payment module across the codebase.", "category": "complex_code",
                                   "difficulty": 0.8, "interactive": False},
}
