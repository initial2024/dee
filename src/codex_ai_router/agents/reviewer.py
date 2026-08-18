from ..result import AgentResult
from .coder import ask_structured


def review(provider, diff: str, risk: str) -> AgentResult:
    return ask_structured(provider, "Review this diff: " + diff, risk)
