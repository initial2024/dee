from __future__ import annotations

from dataclasses import dataclass

from .task import Mode, Risk


@dataclass(frozen=True)
class FastLocalPolicy:
    estimated_context_threshold: int = 4000
    first_token_budget_seconds: int = 8
    simple_task_budget_seconds: int = 20
    hard_timeout_seconds: int = 30
    max_agent_steps: int = 2
    structured_retry_max: int = 1
    slow_streak_limit: int = 3
    local_write: bool = False

    @classmethod
    def from_config(cls, config: dict | None) -> "FastLocalPolicy":
        values = (config or {}).get("local", {})
        if not isinstance(values, dict): return cls()
        allowed = {key: values[key] for key in cls.__dataclass_fields__ if key in values}
        return cls(**allowed)


class FastLocalGate:
    """Conservatively limits Local-first routing to short, safe helper work."""
    LOCAL_CATEGORIES = {"SIMPLE_READ", "LOG_ANALYSIS", "DOCS", "TRANSLATION", "CONTEXT_COMPACTION", "SMALL_REVIEW"}
    WRITE_WORDS = ("edit", "write", "apply patch", "implement", "fix", "create file", "run test")

    def __init__(self, policy: FastLocalPolicy = FastLocalPolicy()): self.policy = policy

    def permits(self, prompt: str, category: str, risk: Risk, degraded: bool = False) -> bool:
        estimated_context = max(1, len(prompt) // 4)
        estimated_steps = 3 if any(word in prompt.lower() for word in self.WRITE_WORDS) else 1
        return bool(
            risk is Risk.LOW
            and category in self.LOCAL_CATEGORIES
            and estimated_context <= self.policy.estimated_context_threshold
            and estimated_steps <= self.policy.max_agent_steps
            and not degraded
        )


def choose_mode(category: str, risk: Risk, requested: Mode | None = None, local_available: bool = True, api_available: bool = True) -> Mode:
    if requested:
        return requested
    if risk in (Risk.HIGH, Risk.CRITICAL):
        return Mode.CODEX_ONLY if risk is Risk.CRITICAL else Mode.CODEX_API
    if category in {"SIMPLE_READ", "LOG_ANALYSIS", "DOCS", "TRANSLATION", "CONTEXT_COMPACTION", "SMALL_REVIEW"}:
        return Mode.LOCAL_ONLY if local_available else (Mode.API_ONLY if api_available else Mode.CODEX_ONLY)
    if category in {"TEST_GENERATION", "SMALL_CODE"}:
        return Mode.API_ONLY if api_available else Mode.CODEX_ONLY
    if category == "LARGE_REFACTOR":
        return Mode.CODEX_API
    return Mode.AUTO_TRIAD if (local_available or api_available) else Mode.CODEX_ONLY


def requires_codex_gate(risk: Risk) -> bool:
    return risk in (Risk.HIGH, Risk.CRITICAL)
