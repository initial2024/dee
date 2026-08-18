from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Mode(str, Enum):
    CODEX_ONLY = "CODEX_ONLY"
    API_ONLY = "API_ONLY"
    LOCAL_ONLY = "LOCAL_ONLY"
    CODEX_API = "CODEX_API"
    CODEX_LOCAL = "CODEX_LOCAL"
    API_LOCAL = "API_LOCAL"
    AUTO_TRIAD = "AUTO_TRIAD"


class Risk(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


@dataclass(frozen=True)
class Task:
    prompt: str
    root: Path
    requested_mode: Mode | None = None
    category: str = "UNKNOWN"
    risk: Risk = Risk.MEDIUM
    metadata: dict[str, str] = field(default_factory=dict)
