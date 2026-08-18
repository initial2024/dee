from __future__ import annotations

from pathlib import Path
from ..result import AgentResult
from ..security.secrets import redact
from ..providers.base import ProviderError

PROTOCOL = '''Return ONLY JSON with status, summary, confidence, risk, needs_escalation, actions, tests, warnings. Do not include secrets. You may propose edits and targeted tests, but cannot execute unrestricted shell commands.'''


def ask_structured(provider, task: str, risk: str, retries: int = 1) -> AgentResult:
    prompt = redact(f"{PROTOCOL}\nTask: {task}\nRisk: {risk}")
    for _ in range(retries + 1):
        try:
            return AgentResult.from_json(provider.ask(prompt))
        except (ValueError, KeyError, ProviderError):
            continue
    return AgentResult("ESCALATE", "Structured output parse failed twice", risk=risk, needs_escalation=True, warnings=["STRUCTURED_OUTPUT_RETRY_EXHAUSTED"])


def agent_loop(provider, task: str, root: Path, risk: str, max_iterations: int = 6) -> AgentResult:
    """Bounded inspect-plan-edit-test loop. V1 delegates proposals; edits stay isolated."""
    result = ask_structured(provider, task, risk)
    result.actions = result.actions[:max_iterations]
    if max_iterations <= 0:
        result.needs_escalation, result.status = True, "ESCALATE"
    return result
