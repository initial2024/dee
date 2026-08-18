from __future__ import annotations

import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def isolated_worktree(repo: Path, label: str):
    """Create one writer-only worktree; callers must never share it."""
    repo = repo.resolve()
    base = repo / ".ai-worktrees"
    try:
        base.mkdir(exist_ok=True)
    except OSError:
        base = Path(tempfile.gettempdir()) / "codex-agent-worktrees"
        base.mkdir(exist_ok=True)
    path = base / f"{label}-{next(tempfile._get_candidate_names())}"
    subprocess.run(["git", "worktree", "add", "--detach", str(path), "HEAD"], cwd=repo, check=True, capture_output=True)
    try:
        yield path
    finally:
        subprocess.run(["git", "worktree", "remove", "--force", str(path)], cwd=repo, check=False, capture_output=True)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
