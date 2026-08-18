from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json


@dataclass
class AgentResult:
    status: str
    summary: str
    confidence: float = 0.0
    risk: str = "MEDIUM"
    needs_escalation: bool = False
    actions: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, raw: str) -> "AgentResult":
        data = json.loads(raw)
        required = {"status", "summary", "confidence", "risk", "needs_escalation", "actions", "tests", "warnings"}
        missing = required.difference(data)
        if missing:
            raise ValueError(f"structured response missing: {sorted(missing)}")
        return cls(**{key: data[key] for key in cls.__dataclass_fields__ if key in data})
