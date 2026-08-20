from __future__ import annotations

from pathlib import Path
import subprocess


def compact_handoff(root: Path, task: str, tests: str = "NOT_RUN", blockers: str = "NONE", constraints: str = "") -> dict:
    """Deterministic, secret-free new-thread handoff when provider switching is unsupported."""
    def git(*args: str) -> str:
        completed = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace")
        return completed.stdout.strip() if completed.returncode == 0 else "UNKNOWN"
    return {
        "task": task,
        "repo": str(root.resolve()),
        "head": git("rev-parse", "HEAD"),
        "modified_files": [line[3:] for line in git("status", "--porcelain").splitlines() if len(line) > 3],
        "tests": tests,
        "blockers": blockers,
        "constraints": constraints,
    }
