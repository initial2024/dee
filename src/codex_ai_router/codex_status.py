from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ChatGPTCodexQuota(str, Enum):
    AVAILABLE = "AVAILABLE"
    LOW = "LOW"
    EXHAUSTED = "EXHAUSTED"
    UNKNOWN = "UNKNOWN"


class CodexAgentAvailability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class CodexHarnessState:
    """Subscription quota and Harness availability intentionally never infer each other."""
    quota: ChatGPTCodexQuota = ChatGPTCodexQuota.UNKNOWN
    agent: CodexAgentAvailability = CodexAgentAvailability.AVAILABLE

    def as_dict(self) -> dict[str, str]:
        return {"CHATGPT_CODEX_QUOTA": self.quota.value, "CODEX_AGENT": self.agent.value}
