from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import os

from .agents.coder import agent_loop
from .classifier import classify
from .policy import choose_mode, requires_codex_gate
from .providers import LMStudioProvider, OpenAICompatibleProvider
from .result import AgentResult
from .task import Mode, Task
from .model_policy import SelectionPolicy
from .orchestration.sequential import api_local


@dataclass
class Router:
    root: Path
    local: LMStudioProvider
    api: OpenAICompatibleProvider
    max_iterations: int = 6
    selection_policy: SelectionPolicy = SelectionPolicy()

    @classmethod
    def default(cls, root: Path | None = None, selection_policy: SelectionPolicy | None = None) -> "Router":
        return cls((root or Path.cwd()).resolve(), LMStudioProvider(), OpenAICompatibleProvider(), selection_policy=selection_policy or SelectionPolicy())

    def provider_models(self, provider_name: str) -> tuple[list[str], list[str]]:
        if not self.selection_policy.providers.permits(provider_name):
            return [], []
        provider = self.local if provider_name == "local" else self.api
        discovered = provider.candidate_models() if hasattr(provider, "candidate_models") else provider.models()
        return discovered, self.selection_policy.eligible(provider_name, discovered)

    def status(self) -> dict:
        local_models = self.provider_models("local")[1]
        api_models = self.provider_models("api")[1]
        return {"LOCAL_AVAILABLE": "YES" if local_models else "NO", "API_CONFIGURED": "YES" if api_models else "NO", "MULTI_AGENT_SHARED_WRITE_TREE": "NO", "DEFAULT_MODE": "AUTO_TRIAD", "CODEX_BUDGET_MODE": os.getenv("CODEX_BUDGET_MODE", "SAVE"), "EXTERNAL_API_ALLOWED": "YES" if self.selection_policy.providers.permits("api") else "NO"}

    def route(self, prompt: str, requested: Mode | None = None) -> dict:
        category, risk = classify(prompt)
        local_ok = bool(self.provider_models("local")[1])
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
            return api_local(self.api, self.local, prompt, self.root, risk, self.max_iterations)
        provider_name = "local" if mode is Mode.LOCAL_ONLY else "api"
        if mode is Mode.AUTO_TRIAD:
            provider_name = "local" if self.provider_models("local")[1] else "api"
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
        result = agent_loop(provider, prompt, self.root, risk, self.max_iterations)
        if mode in {Mode.CODEX_API, Mode.CODEX_LOCAL}:
            result.status = "CODEX_ACTION_REQUIRED"
            result.needs_escalation = True
            result.warnings.append("Delegated analysis complete; current Codex final gate required")
        return result

    def doctor(self) -> dict:
        import shutil
        return {**self.status(), "PYTHON": "YES", "GIT": "YES" if shutil.which("git") else "NO", "CURRENT_REPO": "YES" if (self.root / ".git").exists() else "NO", "WORKTREE_CAPABLE": "YES" if shutil.which("git") else "NO", "LMSTUDIO_BASE_URL": self.local.base_url, "API_KEY_CONFIGURED": "YES" if self.api.available() else "NO"}
