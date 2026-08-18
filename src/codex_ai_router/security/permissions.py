from __future__ import annotations

from pathlib import Path


def canonical_inside(root: Path, candidate: Path) -> Path:
    base = root.resolve(strict=False)
    target = candidate.resolve(strict=False)
    try:
        target.relative_to(base)
    except ValueError as exc:
        raise PermissionError(f"path outside workspace: {candidate}") from exc
    return target


def can_mutate(risk: str, provider: str) -> bool:
    return risk not in {"HIGH", "CRITICAL"} and provider in {"api", "local"}
