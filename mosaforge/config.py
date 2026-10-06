"""Load mosaforge.toml (repo default, then ~/.mosaforge/mosaforge.toml overrides)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core import ModelSpec

REPO_CONFIG = Path(__file__).resolve().parent.parent / "mosaforge.toml"


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


@dataclass
class Config:
    raw: dict[str, Any]
    data_dir: Path
    workspace: Path
    models: list[ModelSpec] = field(default_factory=list)

    @property
    def router(self) -> dict[str, Any]:
        return self.raw.get("router", {})

    @property
    def verify(self) -> dict[str, Any]:
        return self.raw.get("verify", {})

    @property
    def providers(self) -> dict[str, dict[str, Any]]:
        return self.raw.get("providers", {})

    def model(self, ident: str) -> ModelSpec:
        for m in self.models:
            if ident in (m.id, m.name):
                return m
        raise KeyError(f"unknown model {ident!r}; known: {[m.id for m in self.models]}")


def load_config(path: str | os.PathLike[str] | None = None, workspace: str | None = None) -> Config:
    raw: dict[str, Any] = {}
    home = Path(os.environ.get("MOSAFORGE_HOME", "~/.mosaforge")).expanduser()
    candidates = [REPO_CONFIG, home / "mosaforge.toml"]
    if path:
        candidates.append(Path(path))
    for p in candidates:
        if p.is_file():
            with open(p, "rb") as f:
                raw = _merge(raw, tomllib.load(f))
    paths = raw.get("paths", {})
    data_dir = Path(os.environ.get("MOSAFORGE_HOME", paths.get("data_dir", "~/.mosaforge"))).expanduser()
    ws = Path(workspace or os.environ.get("MOSAFORGE_WORKSPACE") or paths.get("workspace", ".")).expanduser().resolve()
    models = [
        ModelSpec(
            id=m["id"],
            name=m["name"],
            providers=list(m.get("providers", ["ollama-local"])),
            tier=int(m.get("tier", 1)),
            cloud=bool(m.get("cloud", False)),
            tools=bool(m.get("tools", True)),
            think=bool(m.get("think", False)),
            num_ctx=int(m.get("num_ctx", 8192)),
            cost_in_per_mtok=float(m.get("cost_in_per_mtok", 0.0)),
            cost_out_per_mtok=float(m.get("cost_out_per_mtok", 0.0)),
            tags=list(m.get("tags", [])),
            ladder=bool(m.get("ladder", True)),
        )
        for m in raw.get("models", [])
    ]
    data_dir.mkdir(parents=True, exist_ok=True)
    return Config(raw=raw, data_dir=data_dir, workspace=ws, models=models)
