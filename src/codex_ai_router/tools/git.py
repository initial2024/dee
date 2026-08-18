from pathlib import Path
from .subprocess_runner import run_command


def git_status(root: Path) -> str:
    return run_command(["git", "status", "--short"], root)[1]


def git_diff(root: Path) -> str:
    return run_command(["git", "diff", "--no-ext-diff"], root)[1]
