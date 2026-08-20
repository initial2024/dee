from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
import os


_ALLOWED = {"timestamp", "active_provider", "active_model", "router_virtual_model", "task_mode", "duration_seconds", "success", "error_code", "estimated_route", "remote_provider_used", "local_provider_used"}
_SENSITIVE = re.compile(r"(?i)(sk-[a-z0-9_-]+|bearer\s+\S+|authorization\s*[:=]\s*\S+|api[_ -]?key\s*[:=]\s*\S+)")


class UsageLedger:
    """Local trend data only; never a proxy for official account quota."""
    def __init__(self, path: Path | None = None):
        home = Path(os.getenv("USERPROFILE") or Path.home())
        self.path = path or home / ".codex-ai-router" / "usage-ledger.jsonl"

    def append(self, record: dict) -> dict:
        safe = {key: record.get(key) for key in _ALLOWED if key in record}
        safe["timestamp"] = safe.get("timestamp") or datetime.now(timezone.utc).isoformat()
        for key, value in list(safe.items()):
            if isinstance(value, str): safe[key] = _SENSITIVE.sub("[REDACTED]", value)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False) + "\n")
        return safe

    def summary(self, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(timezone.utc); today = now.date(); week = now - timedelta(days=7)
        result = {"today_tasks": 0, "week_tasks": 0, "openai_provider_tasks": 0, "xiaoyu_router_tasks": 0, "lightboat_tasks": 0, "local_tasks": 0, "failed_or_timed_out": 0}
        if not self.path.exists(): return result
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try: item = json.loads(line); stamp = datetime.fromisoformat(item["timestamp"].replace("Z", "+00:00"))
            except (ValueError, KeyError, TypeError): continue
            if stamp.date() == today: result["today_tasks"] += 1
            if stamp >= week:
                result["week_tasks"] += 1
                provider = str(item.get("active_provider", "")); route = str(item.get("estimated_route", "")); model = str(item.get("router_virtual_model", ""))
                result["openai_provider_tasks"] += int(provider in {"OpenAI", "DEFAULT"})
                result["xiaoyu_router_tasks"] += int(provider == "XiaoyuRouter")
                result["lightboat_tasks"] += int("lightboat" in model.lower() or "lightboat" in route.lower())
                result["local_tasks"] += int(str(item.get("local_provider_used", "NO")) == "YES")
                result["failed_or_timed_out"] += int(not bool(item.get("success")) or "TIMEOUT" in str(item.get("error_code", "")).upper())
        return result


def provider_usage_warning(provider: str, recent_openai_tasks: int = 0) -> str:
    if provider == "XiaoyuRouter": return "Router-first inference is active; this is not an official quota statement."
    return "Current provider may consume official Codex quota" + (" quickly" if recent_openai_tasks >= 5 else "") + "."
