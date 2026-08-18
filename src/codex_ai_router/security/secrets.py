from __future__ import annotations

import re
from pathlib import Path

DENIED_NAMES = {".env", ".env.local", "id_rsa", "id_ed25519", "credentials.json"}
PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[a-z0-9._\-]+"),
    re.compile(r"(?i)(api[_-]?key\s*[=:]\s*)[^\s'\"]+"),
    re.compile(r"-----BEGIN [A-Z ]+PRIVATE KEY-----[\s\S]*?-----END [A-Z ]+PRIVATE KEY-----"),
]


def is_sensitive_path(path: Path) -> bool:
    return path.name.lower() in DENIED_NAMES or path.suffix.lower() in {".pem", ".p12", ".key"}


def redact(text: str) -> str:
    for pattern in PATTERNS:
        text = pattern.sub(lambda match: (match.group(1) if match.lastindex else "") + "[REDACTED]", text)
    return text
