"""Validation and safe persistence for review-only patch drafts."""
from __future__ import annotations

from datetime import datetime, timezone
import difflib
import json
from pathlib import Path, PurePosixPath
import re


_DIFF_BLOCK = re.compile(
    r"(?ms)^diff --git a/(?P<old>[^\r\n]+) b/(?P<new>[^\r\n]+)\r?\n"
    r"--- a/(?P=old)\r?\n\+\+\+ b/(?P=new)\r?\n@@",
)


class PatchDraftFormatError(ValueError):
    """A proposed patch is not a safe, reviewable project-relative diff."""


class PatchSynthesizerError(ValueError):
    """A structured patch cannot be converted safely into a review-only diff."""


_MAX_TARGET_BYTES = 512 * 1024


def _safe_relative_path(value: str) -> bool:
    path = PurePosixPath(value.replace("\\", "/"))
    return bool(value) and not path.is_absolute() and ".." not in path.parts and not re.match(r"^[A-Za-z]:", value)


def _checked_target(root: Path, raw_path: object) -> tuple[str, Path]:
    value = str(raw_path or "").replace("\\", "/")
    path = PurePosixPath(value)
    if path.is_absolute() or re.match(r"^[A-Za-z]:", value):
        raise PatchSynthesizerError("PATCH_PATH_INVALID")
    if ".." in path.parts:
        raise PatchSynthesizerError("PATCH_PATH_TRAVERSAL")
    if not _safe_relative_path(value):
        raise PatchSynthesizerError("PATCH_PATH_INVALID")
    target = (root / Path(*path.parts)).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:
        raise PatchSynthesizerError("PATCH_PATH_INVALID") from exc
    if not target.is_file():
        raise PatchSynthesizerError("PATCH_TARGET_FILE_NOT_FOUND")
    if target.stat().st_size > _MAX_TARGET_BYTES:
        raise PatchSynthesizerError("PATCH_TARGET_TOO_LARGE")
    return value, target


def extract_structured_patch(text: str) -> dict[str, object] | None:
    """Extract the first structured_patch object from plain text or a JSON fence."""
    source = str(text or "")
    candidates = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", source, flags=re.DOTALL | re.IGNORECASE)
    candidates.append(source)
    for candidate in candidates:
        decoder = json.JSONDecoder()
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("patch_type") == "structured_patch":
                return value
    return None


def synthesize_structured_patch(root: Path, payload: dict[str, object]) -> tuple[str, list[str], int]:
    """Generate a diff in memory from unique replace_block operations only."""
    if payload.get("patch_type") != "structured_patch" or not isinstance(payload.get("files"), list):
        raise PatchSynthesizerError("PATCH_DRAFT_FORMAT_INVALID")
    chunks: list[str] = []
    files_targeted: list[str] = []
    operations_count = 0
    for item in payload["files"]:
        if not isinstance(item, dict):
            raise PatchSynthesizerError("PATCH_DRAFT_FORMAT_INVALID")
        relative_path, target = _checked_target(root, item.get("path"))
        operations = item.get("operations")
        if not isinstance(operations, list) or not operations:
            raise PatchSynthesizerError("PATCH_DRAFT_EMPTY")
        original = target.read_text(encoding="utf-8")
        revised = original
        for operation in operations:
            if not isinstance(operation, dict) or operation.get("op") != "replace_block":
                raise PatchSynthesizerError("PATCH_DRAFT_FORMAT_INVALID")
            find, replace = operation.get("find"), operation.get("replace")
            if not isinstance(find, str) or not find:
                raise PatchSynthesizerError("PATCH_ANCHOR_NOT_FOUND")
            if not isinstance(replace, str):
                raise PatchSynthesizerError("PATCH_DRAFT_FORMAT_INVALID")
            if not replace.strip():
                raise PatchSynthesizerError("PATCH_DELETE_NOT_ALLOWED")
            matches = revised.count(find)
            if matches == 0:
                raise PatchSynthesizerError("PATCH_ANCHOR_NOT_FOUND")
            if matches != 1:
                raise PatchSynthesizerError("PATCH_ANCHOR_NOT_UNIQUE")
            revised = revised.replace(find, replace, 1)
            operations_count += 1
        if revised == original:
            raise PatchSynthesizerError("PATCH_DRAFT_EMPTY")
        body = "".join(difflib.unified_diff(
            original.splitlines(keepends=True), revised.splitlines(keepends=True),
            fromfile=f"a/{relative_path}", tofile=f"b/{relative_path}", lineterm="\n",
        ))
        if not body.strip():
            raise PatchSynthesizerError("PATCH_DRAFT_EMPTY")
        chunks.append(f"diff --git a/{relative_path} b/{relative_path}\n{body}\n")
        files_targeted.append(relative_path)
    diff_text = "".join(chunks)
    valid, _ = validate_unified_diff(diff_text)
    if not valid:
        raise PatchSynthesizerError("PATCH_DRAFT_EMPTY")
    return diff_text, files_targeted, operations_count


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


def persist_structured_patch_draft(
    root: Path,
    diff_text: str,
    *,
    task_name: str,
    files_targeted: list[str],
    operations_count: int,
    risk_level: str,
) -> tuple[Path, Path]:
    """Persist a synthesized diff and metadata only; never apply it."""
    valid, files = validate_unified_diff(diff_text)
    if not valid or files != files_targeted:
        raise PatchSynthesizerError("PATCH_DRAFT_EMPTY")
    now = datetime.now(timezone.utc)
    target_dir = root / "codex-handoff" / "patch-drafts"
    target_dir.mkdir(parents=True, exist_ok=True)
    base = now.strftime("%Y%m%d-%H%M%S") + "-a4f10-structured-patch"
    diff_path = target_dir / f"{base}.diff"
    metadata_path = target_dir / f"{base}.meta.json"
    diff_path.write_text(diff_text, encoding="utf-8")
    metadata_path.write_text(json.dumps({
        "created_at": now.isoformat(), "task_name": task_name, "source": "structured_patch",
        "files_targeted": files_targeted, "operations_count": operations_count,
        "unified_diff_detected": True, "patch_draft_applied": False,
        "tests_executed": False, "commit_created": False, "risk_level": risk_level,
        "redaction_applied": True,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return diff_path, metadata_path


__all__ = [
    "PatchDraftFormatError", "PatchSynthesizerError", "extract_structured_patch",
    "persist_patch_draft", "persist_structured_patch_draft", "synthesize_structured_patch",
    "validate_unified_diff",
]
