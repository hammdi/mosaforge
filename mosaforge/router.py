"""Router: hard rules -> decision model (typed, local) -> keyword fallback -> experience adjustment.

The decision model is any Ollama model exposing /v1/systemone (TypeSafe Jev-compatible API):
tev1 (Together AI, 0.8B/4B), nimble (Bespoke Labs, 9B), clef (Cloudflare). It returns a
probability per category with zero decoded tokens, so routing costs ~one prefill pass.
"""

from __future__ import annotations

import re
from typing import Any

from .config import Config
from .core import CATEGORIES, ModelSpec, RouteDecision
from .memory import Store
from .providers import OllamaProvider, ProviderError

CODE_HINTS = re.compile(r"```|\bdef |\bclass |\bfunction\b|\bbug\b|\btraceback\b|\bpytest\b|\brefactor|\bimplement|\bunit tests?\b|\.py\b|\.ts\b|\.js\b|\bcompile|\bregex\b", re.I)
COMPLEX_HINTS = re.compile(r"\barchitect|\bacross (the )?(codebase|repo|files)|\bmulti-file|\bmigrat|\bdesign (a|the) system|\bconcurren|\bsecurity review", re.I)
REASON_HINTS = re.compile(r"\bprove\b|\bwhy\b.*\?|\btrade-?offs?\b|\bcompare\b|\bplan\b|\bstep by step\b|\bestimate\b|\boptimi[sz]e\b|\bderive\b", re.I)
SUMMARY_HINTS = re.compile(r"\bsummari[sz]e|\btl;?dr|\bextract\b|\bclassify\b|\blist the\b|\bkey points\b", re.I)


def heuristic_category(task: str) -> tuple[str, float, str]:
    if COMPLEX_HINTS.search(task) and CODE_HINTS.search(task):
        return "complex_code", 0.6, "keywords: complex+code"
    if CODE_HINTS.search(task):
        return "code", 0.6, "keywords: code"
    if SUMMARY_HINTS.search(task):
        return "summarize", 0.6, "keywords: summarize"
    if REASON_HINTS.search(task) or len(task) > 600:
        return "reasoning", 0.5, "keywords/length: reasoning"
    return "simple", 0.5, "default: simple"


class Router:
    def __init__(self, cfg: Config, providers: dict[str, Any], store: Store | None = None):
        self.cfg = cfg
        self.providers = providers
        self.store = store
        rc = cfg.router
        self.decision_model = rc.get("decision_model", "")
        self.decider = providers.get(rc.get("decision_provider", "ollama-local"))
        self.min_conf = float(rc.get("min_confidence", 0.45))
        self.allow_cloud = bool(rc.get("allow_cloud", False))
        self.category_tier: dict[str, int] = dict(rc.get("category_tier", {}))

    # ---- classification ------------------------------------------------
    def classify(self, task: str) -> tuple[str, float, float, list[str]]:
        """Return (category, confidence, difficulty 0..1, reasons)."""
        reasons: list[str] = []
        if isinstance(self.decider, OllamaProvider) and self.decision_model:
            try:
                r = self.decider.decide(
                    self.decision_model,
                    task[:6000],
                    {
                        "category": {"type": "choice", "instructions": "Which category best describes the work this request needs?", "criteria": CATEGORIES},
                        "difficulty": {"type": "score", "instructions": "How hard is this request for a small local language model to do correctly?",
                                       "criteria": ["trivial", "easy", "moderate", "hard", "very hard, needs expert-level reasoning"]},
                    },
                )
                a = r["answers"]
                cat, conf = a["category"]["choice"], float(a["category"].get("confidence", 0.0))
                diff = float(a["difficulty"].get("score", 0.0)) / 4.0
                probs = a["category"].get("probabilities", {})
                reasons.append(f"decision-model {self.decision_model}: {cat} p={probs.get(cat, 0):.2f} conf={conf:.2f} difficulty={diff:.2f}")
                if conf >= self.min_conf and cat in CATEGORIES:
                    # Keyword veto: an explicit code signal beats a low-margin non-code label.
                    hcat, _, hwhy = heuristic_category(task)
                    if hcat in ("code", "complex_code") and cat not in ("code", "complex_code") and conf < 0.8:
                        reasons.append(f"override by {hwhy}")
                        return hcat, conf, diff, reasons
                    return cat, conf, diff, reasons
                reasons.append(f"confidence {conf:.2f} < {self.min_conf}: falling back to keywords")
            except (ProviderError, KeyError, TypeError, ValueError) as e:
                reasons.append(f"decision model unavailable ({str(e)[:80]}): keywords")
        cat, conf, why = heuristic_category(task)
        reasons.append(why)
        return cat, conf, {"simple": 0.1, "summarize": 0.25, "code": 0.4, "reasoning": 0.6, "complex_code": 0.75}[cat], reasons

    # ---- model selection -----------------------------------------------
    def candidates(self, privacy: str = "private") -> list[ModelSpec]:
        allowed = []
        for m in sorted(self.cfg.models, key=lambda m: m.tier):
            if not m.ladder:
                continue
            if m.cloud and (not self.allow_cloud or privacy == "secret"):
                continue
            if not any(p in self.providers for p in m.providers):
                continue
            allowed.append(m)
        return allowed

    def pick(self, category: str, difficulty: float, privacy: str, min_tier: int = 0, reasons: list[str] | None = None) -> ModelSpec | None:
        reasons = reasons if reasons is not None else []
        tier = max(self.category_tier.get(category, 1), min_tier)
        if difficulty >= 0.85 and tier < 3:
            tier = 3
            reasons.append("difficulty>=0.85: start at tier 3")
        cands = self.candidates(privacy)
        if not cands:
            return None
        # Experience adjustment: skip models that measurably fail this category.
        if self.store is not None:
            n_min = int(self.cfg.router.get("experience_min_samples", 5))
            p_min = float(self.cfg.router.get("experience_min_pass_rate", 0.6))
            for m in cands:
                if m.tier == tier:
                    n, p = self.store.pass_rate(category, m.id)
                    if n >= n_min and p < p_min:
                        reasons.append(f"experience: {m.id} pass={p:.2f} over {n} {category} runs -> tier {tier + 1}")
                        tier += 1
                    break
        for m in cands:
            if m.tier >= tier:
                # prefer tag match within the first eligible tier
                same = [c for c in cands if c.tier == m.tier]
                tagged = [c for c in same if category.split("_")[-1] in c.tags or category in c.tags]
                return (tagged or same)[0]
        return cands[-1]

    def acceptable(self, category: str, difficulty: float, privacy: str, reasons: list[str] | None = None) -> list[ModelSpec]:
        """Models acceptable for this task, in quality order: the picked tier first, then upgrades.
        The Resource Manager places the first one it can; it never downgrades quality on its own."""
        first = self.pick(category, difficulty, privacy, reasons=reasons)
        if first is None:
            return []
        return [first] + [m for m in self.candidates(privacy) if m.tier > first.tier and not m.cloud]

    def check_privacy(self, model: ModelSpec, privacy: str) -> None:
        """Hard rule, applies even to forced models: secret never leaves; cloud needs allow_cloud."""
        if model.cloud and privacy == "secret":
            raise PermissionError(f"{model.id} is a cloud model and this task is privacy=secret")
        if model.cloud and not self.allow_cloud:
            raise PermissionError(f"{model.id} is a cloud model and allow_cloud is off")

    def next_after(self, model: ModelSpec, privacy: str) -> ModelSpec | None:
        for m in self.candidates(privacy):
            if m.tier > model.tier:
                return m
        return None

    def route(self, task: str, privacy: str = "private", force_model: str | None = None, force_category: str | None = None) -> RouteDecision:
        if force_category:
            cat, conf, diff, reasons = force_category, 1.0, 0.5, ["category forced by caller"]
        else:
            cat, conf, diff, reasons = self.classify(task)
        if force_model:
            model = self.cfg.model(force_model)
            self.check_privacy(model, privacy)
            reasons.append(f"model forced: {model.id}")
        else:
            m = self.pick(cat, diff, privacy, reasons=reasons)
            if m is None:
                raise RuntimeError("no model available for this privacy level / provider set")
            model = m
        agent = "coder" if cat in ("code", "complex_code") else "general"
        return RouteDecision(category=cat, model=model, agent=agent, confidence=conf, reasons=reasons, difficulty=diff)
