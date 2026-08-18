from __future__ import annotations

from ..result import AgentResult


def judge(local: AgentResult, api: AgentResult) -> AgentResult:
    agree = local.needs_escalation == api.needs_escalation and local.risk == api.risk
    if not agree:
        return AgentResult("ESCALATE", "Reviewers disagree", risk="HIGH", needs_escalation=True, warnings=["ESCALATE_TO_CODEX=YES"])
    return AgentResult("PASS", "Reviewers agree", confidence=min(1.0, (local.confidence + api.confidence) / 2 + 0.1), risk=local.risk)
