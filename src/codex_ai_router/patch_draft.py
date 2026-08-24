"""Validation and safe persistence for review-only patch drafts."""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path, PurePosixPath
import re


_DIFF_BLOCK = re.compile(
    r"(?ms)^diff --git a/(?P<old>[^\r\n]+) b/(?P<new>[^\r\n]+)\r?\n"
    r"--- a/(?P=old)\r?\n\+\+\+ b/(?P=new)\r?\n@@",
)


class PatchDraftFormatError(ValueError):
    """A proposed patch is not a safe, reviewable project-relative diff."""


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value.replace("\\", "/"))
    return bool(value) and not path.is_absolute() and ".." not in path.parts and not re.match(r"^[A-Za-z]:", value)


def validate_unified_diff(text: str) -> tuple[bool, list[str]]:
    """Return whether *text* contains a project-relative unified diff and its files."""
    matches = list(_DIFF_BLOCK.finditer(str(text or "")))
    if not matches:
        return False, []
    files: list[str] = []
    for match in matches:
        old_path, new_path = match.group("old"), match.group("new")
        if old_path != new_path or not _safe_relative_path(old_path):
            return False, []
        files.append(old_path)
    return True, files


def persist_patch_draft(
    root: Path,
    diff_text: str,
    *,
    task_name: str,
    brain_provider: str,
    context_bundle_id: str,
    risk_level: str,
) -> tuple[Path, Path, list[str]]:
    """Persist only a validated diff and redacted metadata; never apply it."""
    valid, files = validate_unified_diff(diff_text)
    if not valid:
        raise PatchDraftFormatError("PATCH_DRAFT_FORMAT_INVALID")
    now = datetime.now(timezone.utc)
    stamp = now.strftime("%Y%m%d-%H%M%S")
    target_dir = root / "codex-handoff" / "patch-drafts"
    target_dir.mkdir(parents=True, exist_ok=True)
    base = f"{stamp}-a4f10-patch-draft"
    diff_path = target_dir / f"{base}.diff"
    metadata_path = target_dir / f"{base}.meta.json"
    diff_path.write_text(diff_text.strip() + "\n", encoding="utf-8")
    metadata = {
        "created_at": now.isoformat(),
        "task_name": task_name,
        "brain_provider": brain_provider,
        "context_bundle_id": context_bundle_id,
        "files_targeted": files,
        "unified_diff_detected": True,
        "patch_draft_applied": False,
        "tests_executed": False,
        "commit_created": False,
        "risk_level": risk_level,
        "redaction_applied": True,
    }
    metadata_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return diff_path, metadata_path, files


__all__ = ["PatchDraftFormatError", "persist_patch_draft", "validate_unified_diff"]
