from __future__ import annotations

import subprocess
from pathlib import Path

ALLOW = {"pytest", "python", "npm", "pnpm", "yarn", "gradle", "gradlew", "cargo", "go", "git"}


def run_command(args: list[str], cwd: Path, timeout: int = 60, output_limit: int = 65536) -> tuple[int, str]:
    if not args or Path(args[0]).name.lower() not in ALLOW:
        raise PermissionError("command is not allowlisted")
    try:
        run = subprocess.run(args, cwd=cwd, capture_output=True, text=True, timeout=timeout, shell=False)
        output = (run.stdout + run.stderr)[:output_limit]
        return run.returncode, output
    except subprocess.TimeoutExpired as exc:
        return 124, (exc.stdout or "")[:output_limit] + "\nCOMMAND_TIMEOUT"
