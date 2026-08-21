"""Metadata-only local call records."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def ledger_path() -> Path:
    return Path(os.getenv("XIAOYU_ROUTER_CALL_LEDGER", str(Path.home() / ".codex-ai-router" / "call-ledger.jsonl")))


def append(record: dict[str, Any]) -> None:
    safe = {key: record.get(key) for key in ("id", "created_at", "mode", "model", "provider", "provider_type", "status", "duration_ms", "error_code", "content_detected", "normalized", "saved_body")}
    safe["created_at"] = safe.get("created_at") or datetime.now(timezone.utc).isoformat()
    safe["saved_body"] = False
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(safe, ensure_ascii=False, separators=(",", ":")) + "\n")
