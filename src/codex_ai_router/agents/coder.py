from __future__ import annotations

from pathlib import Path
from ..result import AgentResult
from ..security.secrets import redact
from ..providers.base import ProviderError
from .structured import parse_structured, readonly_task

PROTOCOL = '''Return ONLY JSON with status, summary, confidence, risk, needs_escalation, actions, tests, warnings. Do not include secrets. You may propose edits and targeted tests, but cannot execute unrestricted shell commands.'''


def ask_structured(provider, task: str, risk: str, retries: int = 1) -> AgentResult:
    prompt = redact(f"{PROTOCOL}\nTask: {task}\nRisk: {risk}")
    raw = ""
    for attempt in range(retries + 1):
        try:
            raw = provider.ask(prompt if attempt == 0 else redact("Return only a valid JSON object matching the required schema. Previous response:\n" + raw))
            data = parse_structured(raw)
            if data is not None: return AgentResult.from_json(__import__("json").dumps(data))
        except (ValueError, KeyError, ProviderError):
            continue
    if readonly_task(task, risk) and raw:
        return AgentResult("TEXT_ONLY_RESULT", redact(raw), risk=risk, warnings=["LOW_RISK_LOCAL_TEXT_FALLBACK"])
    return AgentResult("STRUCTURED_ACTION_UNAVAILABLE", "Reliable structured actions are unavailable", risk=risk, needs_escalation=True, warnings=["STRUCTURED_OUTPUT_RETRY_EXHAUSTED"])


def agent_loop(provider, task: str, root: Path, risk: str, max_iterations: int = 6) -> AgentResult:
    """Bounded inspect-plan-edit-test loop. V1 delegates proposals; edits stay isolated."""
    result = ask_structured(provider, task, risk)
    result.actions = result.actions[:max_iterations]
    if max_iterations <= 0:
        result.needs_escalation, result.status = True, "ESCALATE"
    return result
