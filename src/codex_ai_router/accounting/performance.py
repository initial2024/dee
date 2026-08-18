from __future__ import annotations

import json
import os
from pathlib import Path


class PerformanceTracker:
    """Non-secret model performance history used only for routing decisions."""
    def __init__(self, path: Path | None = None):
        self.path, self.data = path, {"models": {}}
        if path and path.exists():
            try: self.data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError): self.data = {"models": {}}

    @classmethod
    def for_current_user(cls) -> "PerformanceTracker":
        home = Path(os.getenv("USERPROFILE") or Path.home())
        return cls(home / ".codex-ai-router" / "performance.json")

    def record(self, qualified_model: str, latency_seconds: float, success: bool, structured: bool, slow_budget_seconds: int) -> None:
        item = self.data.setdefault("models", {}).setdefault(qualified_model, {"requests": 0, "successes": 0, "structured_successes": 0, "total_latency_seconds": 0.0, "slow_streak": 0})
        item["requests"] += 1; item["total_latency_seconds"] += latency_seconds
        item["successes"] += int(success); item["structured_successes"] += int(structured)
        item["slow_streak"] = item["slow_streak"] + 1 if latency_seconds > slow_budget_seconds else 0
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            temporary.replace(self.path)

    def is_degraded(self, qualified_model: str, limit: int = 3) -> bool:
        return self.data.get("models", {}).get(qualified_model, {}).get("slow_streak", 0) >= limit
