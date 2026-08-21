from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import os
import time

from .agents.coder import agent_loop
from .classifier import classify
from .policy import FastLocalGate, FastLocalPolicy, choose_mode, requires_codex_gate
from .accounting.performance import PerformanceTracker
from .providers import LMStudioProvider, OpenAICompatibleProvider
from .providers.local_backend import LocalBackend
from .providers.base import DiscoveredModel
from .providers.registry import ModelRegistry
from .result import AgentResult
from .task import Mode, Task
from .model_policy import SelectionPolicy
from .orchestration.sequential import api_local
from .codex_status import CodexHarnessState


@dataclass
class Router:
    root: Path
    local: LocalBackend
    api: OpenAICompatibleProvider
    max_iterations: int = 6
    selection_policy: SelectionPolicy = SelectionPolicy()
    model_registry: ModelRegistry = field(default_factory=ModelRegistry)
    fast_local_policy: FastLocalPolicy = field(default_factory=FastLocalPolicy)
    performance: PerformanceTracker = field(default_factory=PerformanceTracker)

    @classmethod
    def default(cls, root: Path | None = None, selection_policy: SelectionPolicy | None = None, fast_local_policy: FastLocalPolicy | None = None) -> "Router":
        return cls((root or Path.cwd()).resolve(), LocalBackend(), OpenAICompatibleProvider(), selection_policy=selection_policy or SelectionPolicy(), performance=PerformanceTracker.for_current_user(), fast_local_policy=fast_local_policy or FastLocalPolicy())

    def provider_models(self, provider_name: str) -> tuple[list[str], list[str]]:
        if not self.selection_policy.providers.permits(provider_name):
            return [], []
        provider = self.local if provider_name == "local" else self.api
        discovered = provider.candidate_models() if hasattr(provider, "candidate_models") else provider.models()
        return discovered, self.selection_policy.eligible(provider_name, discovered)

    def status(self) -> dict:
        local_models = self.provider_models("local")[1]
        api_models = self.provider_models("api")[1]
        direct_backend = getattr(self.local, "managed", None)
        direct = direct_backend.status() if direct_backend is not None else {"server_running": "NO", "model_count": 0}
        return {"LOCAL_AVAILABLE": "YES" if local_models else "NO", "LOCAL_BACKEND_CONFIG": "YES", "DIRECT_LOCAL_BACKEND": "YES", "DIRECT_LOCAL_SERVER_RUNNING": direct["server_running"], "DIRECT_LOCAL_MODEL_COUNT": direct["model_count"], "DIRECT_LOCAL_PORT": direct.get("port"), "DIRECT_LOCAL_PORT_IN_USE": direct.get("port_in_use"), "DIRECT_LOCAL_PORT_OWNER": direct.get("port_owner"), "DIRECT_LOCAL_AUTO_PORT_FALLBACK": direct.get("auto_port_fallback"), "LMSTUDIO_FALLBACK_ONLY": "YES", "API_CONFIGURED": "YES" if api_models else "NO", "MULTI_AGENT_SHARED_WRITE_TREE": "NO", "DEFAULT_MODE": "AUTO_TRIAD", "CODEX_BUDGET_MODE": os.getenv("CODEX_BUDGET_MODE", "SAVE"), "EXTERNAL_API_ALLOWED": "YES" if self.selection_policy.providers.permits("api") else "NO", **CodexHarnessState().as_dict()}

    def route(self, prompt: str, requested: Mode | None = None) -> dict:
        category, risk = classify(prompt)
        local_models = self.provider_models("local")[1]
        local_ok = bool(local_models) and FastLocalGate(self.fast_local_policy).permits(prompt, category, risk, any(self.performance.is_degraded(f"local:{model}", self.fast_local_policy.slow_streak_limit) for model in local_models))
        api_ok = bool(self.provider_models("api")[1])
        mode = choose_mode(category, risk, requested, local_ok, api_ok)
        return {"category": category, "risk": risk.value, "mode": mode.value, "codex_gate": requires_codex_gate(risk)}

    def delegate(self, prompt: str, requested: Mode | None = None, api_model: str | None = None, local_model: str | None = None, role: str = "coder") -> AgentResult:
        decision = self.route(prompt, requested)
        mode, risk = Mode(decision["mode"]), decision["risk"]
        if decision["codex_gate"] or mode is Mode.CODEX_ONLY:
            return AgentResult("CODEX_ACTION_REQUIRED", "Current Codex must implement or approve this task", risk=risk, needs_escalation=True, warnings=["No internal Codex provider is invoked"])
        if mode is Mode.API_LOCAL:
            api_found, _ = self.provider_models("api")
            local_found, _ = self.provider_models("local")
            api_selected = self.selection_policy.choose("api", api_found, role, api_model)
            local_selected = self.selection_policy.choose("local", local_found, role, local_model)
            if not api_selected or not local_selected:
                return AgentResult("CODEX_ACTION_REQUIRED", "API_LOCAL requires two policy-eligible providers", risk=risk, needs_escalation=True)
            self.api.model, self.local.model = api_selected, local_selected
            return api_local(self.api, self.local, prompt, self.root, risk, min(self.max_iterations, self.fast_local_policy.max_agent_steps))
        provider_name = "local" if mode is Mode.LOCAL_ONLY else "api"
        if mode is Mode.AUTO_TRIAD:
            provider_name = "local" if self.route(prompt).get("mode") == Mode.LOCAL_ONLY.value else "api"
        discovered, eligible = self.provider_models(provider_name)
        override = local_model if provider_name == "local" else api_model
        selected = self.selection_policy.choose(provider_name, discovered, role, override)
        if not selected:
            policy_blocked = (not self.selection_policy.providers.permits(provider_name)) or bool(discovered)
            if policy_blocked:
                return AgentResult("NO_ELIGIBLE_MODEL", "Provider/model is forbidden or no discovered model satisfies policy", risk=risk, needs_escalation=True)
            fallback_name = "api" if provider_name == "local" else "local"
            discovered, eligible = self.provider_models(fallback_name)
            selected = self.selection_policy.choose(fallback_name, discovered, role, None)
            if not selected:
                return AgentResult("CODEX_ACTION_REQUIRED", "No provider/model satisfies policy", risk=risk, needs_escalation=True)
            provider_name = fallback_name
        provider = self.local if provider_name == "local" else self.api
        if not provider.available():
            # The provider might disappear between discovery and request; never bypass policy.
            if not self.provider_models("api" if provider_name == "local" else "local")[1]:
                return AgentResult("CODEX_ACTION_REQUIRED", "All helper providers unavailable", risk=risk, needs_escalation=True)
            return AgentResult("CODEX_ACTION_REQUIRED", "Selected provider became unavailable", risk=risk, needs_escalation=True)
        provider.model = selected
        previous_timeout = getattr(provider, "timeout", None)
        if provider_name == "local" and previous_timeout is not None:
            provider.timeout = min(previous_timeout, self.fast_local_policy.hard_timeout_seconds)
        started = time.monotonic()
        result = agent_loop(provider, prompt, self.root, risk, min(self.max_iterations, self.fast_local_policy.max_agent_steps) if provider_name == "local" else self.max_iterations)
        elapsed = time.monotonic() - started
        if previous_timeout is not None: provider.timeout = previous_timeout
        if provider_name == "local" and result.status == "STRUCTURED_ACTION_UNAVAILABLE":
            alternatives = [model for model in discovered if model != selected and model in eligible][:1]
            if alternatives:
                provider.model = alternatives[0]
                retried = agent_loop(provider, prompt, self.root, risk, self.max_iterations)
                retried.warnings.append("LOCAL_STRUCTURED_MODEL_FALLBACK")
                result = retried
        if provider_name == "local":
            self.performance.record(f"local:{provider.model}", elapsed, result.status not in {"STRUCTURED_ACTION_UNAVAILABLE", "ESCALATE"}, result.status not in {"TEXT_ONLY_RESULT", "STRUCTURED_ACTION_UNAVAILABLE"}, self.fast_local_policy.simple_task_budget_seconds)
            needs_api_fallback = result.status == "STRUCTURED_ACTION_UNAVAILABLE" or elapsed > self.fast_local_policy.simple_task_budget_seconds
            if needs_api_fallback and requested is None and self.provider_models("api")[1]:
                api_selected = self.selection_policy.choose("api", self.provider_models("api")[0], role, api_model)
                if api_selected:
                    self.api.model = api_selected
                    result = agent_loop(self.api, prompt, self.root, risk, self.max_iterations)
                    result.warnings.append("FALLBACK_TO_API")
        # Capability observations are deliberately conservative: a completed
        # structured response proves structured output, while a text fallback
        # proves text only.  Proposed actions do not imply unrestricted tools.
        observed = {"TEXT"}
        if result.status not in {"TEXT_ONLY_RESULT", "STRUCTURED_ACTION_UNAVAILABLE"}:
            observed.add("STRUCTURED_OUTPUT")
        if result.actions:
            observed.add("CODING")
        qualified = f"{provider_name}:{provider.model}"
        self.model_registry.update(provider_name, [DiscoveredModel(provider_name, provider.model, provider.model, None, None, datetime.now(timezone.utc))])
        self.model_registry.set_capabilities(qualified, observed)
        if mode in {Mode.CODEX_API, Mode.CODEX_LOCAL}:
            result.status = "CODEX_ACTION_REQUIRED"
            result.needs_escalation = True
            result.warnings.append("Delegated analysis complete; current Codex final gate required")
        return result

    def doctor(self) -> dict:
        import shutil
        lmstudio = getattr(self.local, "lmstudio", self.local)
        return {**self.status(), "PYTHON": "YES", "GIT": "YES" if shutil.which("git") else "NO", "CURRENT_REPO": "YES" if (self.root / ".git").exists() else "NO", "WORKTREE_CAPABLE": "YES" if shutil.which("git") else "NO", "LMSTUDIO_BASE_URL": getattr(lmstudio, "base_url", "http://127.0.0.1:1234/v1"), "API_KEY_CONFIGURED": "YES" if self.api.available() else "NO"}
