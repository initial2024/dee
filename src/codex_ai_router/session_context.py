"""Local, metadata-only binding between Codex and DeepSeek helper work.

This module deliberately never reads browser state or records model prompts and
responses.  It stores short, sanitized status summaries so an operator can keep
one local task context across planning and execution turns.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
import uuid
from typing import Any

from .context_collector import sanitize_llm_or_response_text


_ID = re.compile(r"^[a-z0-9][a-z0-9-]{7,79}$")
_SENSITIVE_PATH = re.compile(r"(?i)(?:^|[\\/])(?:\.env|.*(?:token|cookie|secret|credential|password).*)(?:$|[\\/])")
_MAX_SUMMARY = 1200


class SessionContextError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_summary(value: object, *, limit: int = _MAX_SUMMARY) -> str:
    text = sanitize_llm_or_response_text(value).replace("\x00", " ").strip()
    return text[:limit]


def _safe_reference(value: object) -> str:
    path = str(value or "").strip().replace("\\", "/")
    if not path or Path(path).is_absolute() or ".." in Path(path).parts or _SENSITIVE_PATH.search(path):
        raise SessionContextError("SESSION_REFERENCE_NOT_ALLOWED")
    return path


class SessionContextHub:
    """Own the on-disk, local-only session artifact layout."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.sessions_root = self.root / "codex-handoff" / "sessions"

    def _id(self, session_id: str) -> str:
        if not _ID.fullmatch(str(session_id or "")):
            raise SessionContextError("TASK_SESSION_ID_INVALID")
        return str(session_id)

    def path(self, session_id: str) -> Path:
        return self.sessions_root / self._id(session_id)

    def _require(self, session_id: str) -> Path:
        directory = self.path(session_id)
        if not (directory / "session.json").is_file():
            raise SessionContextError("TASK_SESSION_NOT_FOUND")
        return directory

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _append_markdown(self, path: Path, heading: str, summary: str) -> None:
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"\n## {heading}\n\n{summary or '无可显示摘要。'}\n")

    def create(self, title: str) -> dict[str, Any]:
        safe_title = _safe_summary(title, limit=200)
        if not safe_title:
            raise SessionContextError("SESSION_TITLE_REQUIRED")
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        session_id = "task-" + uuid.uuid4().hex[:16]
        directory = self.path(session_id)
        directory.mkdir(parents=False, exist_ok=False)
        created = _now()
        self._write_json(directory / "session.json", {
            "task_session_id": session_id, "title": safe_title, "created_at": created,
            "updated_at": created, "local_only": True, "prompt_response_stored": False,
            "browser_session_data_read": False,
        })
        initial = {
            "rolling_summary.md": "# 滚动摘要\n\n仅保存脱敏状态摘要，不保存任务提示词或模型回复全文。\n",
            "codex_status.md": "# Codex 状态\n\n未同步。\n",
            "deepseek_plan.md": "# DeepSeek 计划\n\n未分析。\n",
            "decisions.md": "# 决策记录\n\n无待确认决策。\n",
        }
        for name, contents in initial.items():
            (directory / name).write_text(contents, encoding="utf-8", newline="\n")
        self._write_json(directory / "context_bundle.json", {"context_bundle": "NOT_COLLECTED", "redaction_applied": True})
        self._write_json(directory / "llm_context_bundle.json", {"llm_context_bundle": "NOT_GENERATED", "redaction_applied": True})
        self._write_json(directory / "patch_draft_status.json", {"status": "not_requested", "patch_draft_applied": False})
        self._write_json(directory / "safety_flags.json", self._safety_flags())
        return self.status(session_id)

    @staticmethod
    def _safety_flags() -> dict[str, Any]:
        return {
            "prompt_response_logged_by_default": "NO", "cookie_token_exported": "NO",
            "private_api_replay": "NO", "codex_window_scraping": "NO",
            "deepseek_cookie_read": "NO", "auto_apply": "NO", "auto_test": "NO",
            "auto_commit": "NO", "auto_push": "NO", "public_exposure": "NO", "lan_exposure": "NO",
        }

    def _session_json(self, directory: Path) -> dict[str, Any]:
        try:
            value = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionContextError("TASK_SESSION_CORRUPT") from exc
        if not isinstance(value, dict):
            raise SessionContextError("TASK_SESSION_CORRUPT")
        return value

    def _touch(self, directory: Path) -> None:
        data = self._session_json(directory)
        data["updated_at"] = _now()
        self._write_json(directory / "session.json", data)

    def status(self, session_id: str) -> dict[str, Any]:
        directory = self._require(session_id)
        session = self._session_json(directory)
        patch = self._read_json(directory / "patch_draft_status.json")
        return {
            "task_session_id": session_id, "title": _safe_summary(session.get("title"), limit=200),
            "created_at": session.get("created_at"), "updated_at": session.get("updated_at"),
            "session_directory": str(directory.relative_to(self.root)),
            "work_profile": self._safe_profile(session.get("work_profile")),
            "codex_synced": "YES" if (directory / "codex_status.md").read_text(encoding="utf-8").strip().endswith("未同步。") is False else "NO",
            "deepseek_analyzed": "YES" if (directory / "deepseek_plan.md").read_text(encoding="utf-8").strip().endswith("未分析。") is False else "NO",
            "patch_draft_status": patch.get("status", "not_requested"),
            "safety_flags": self._safety_flags(), "local_only": "YES",
        }

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _safe_profile(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        allowed = {"profile_id", "task_difficulty", "desired_reasoning", "codex_custom_mode", "codex_reasoning_strength", "deepseek_mode", "deepseek_ui_generation_preference", "deepseek_reasoning_strength", "deepseek_search_required", "deepseek_vision_required", "deepseek_file_required", "deepseek_combo_policy", "local_model_policy", "external_api_policy", "local_agent_policy", "search_policy", "vision_policy", "file_policy", "apply_policy", "test_policy", "commit_policy", "risk_level", "target_executor", "recommended_codex_mode", "recommended_codex_reasoning_strength", "user_confirmed_codex_mode", "handoff_created_at"}
        return {key: _safe_summary(value[key], limit=120) if isinstance(value[key], str) else value[key] for key in allowed if key in value}

    def append_codex_status_from_file(self, session_id: str, file_path: Path) -> dict[str, Any]:
        directory = self._require(session_id)
        source = file_path.expanduser().resolve()
        try:
            source.relative_to(self.root)
        except ValueError as exc:
            raise SessionContextError("SESSION_STATUS_FILE_OUTSIDE_PROJECT") from exc
        if _SENSITIVE_PATH.search(source.name) or source.suffix.lower() not in {".md", ".txt"}:
            raise SessionContextError("SESSION_STATUS_FILE_NOT_ALLOWED")
        try:
            contents = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SessionContextError("SESSION_STATUS_FILE_UNREADABLE") from exc
        return self.append_codex_status(session_id, _safe_summary(contents), reference=_safe_reference(source.relative_to(self.root).as_posix()))

    def append_codex_status(self, session_id: str, summary: str, *, reference: str | None = None) -> dict[str, Any]:
        directory = self._require(session_id)
        text = _safe_summary(summary)
        ref = _safe_reference(reference) if reference else None
        suffix = f"\n文件引用：`{ref}`" if ref else ""
        self._append_markdown(directory / "codex_status.md", "同步摘要", text + suffix)
        self._append_markdown(directory / "rolling_summary.md", "Codex 状态", text)
        self._touch(directory)
        return {"status": "APPENDED", "task_session_id": session_id, "summary_stored": "YES", "reference": ref, "prompt_response_logged": "NO"}

    def append_test_result(self, session_id: str, summary: str) -> dict[str, Any]:
        return self.append_codex_status(session_id, "测试结果：" + _safe_summary(summary))

    def append_commit(self, session_id: str, repo: str, commit: str) -> dict[str, Any]:
        if repo not in {"router", "bridge"} or not re.fullmatch(r"[0-9a-fA-F]{7,64}", str(commit or "")):
            raise SessionContextError("SESSION_COMMIT_INVALID")
        return self.append_codex_status(session_id, f"{repo} commit：{str(commit)[:64]}")

    def append_work_profile(self, session_id: str, profile: dict[str, Any], *, user_confirmed_codex_mode: str = "NO", handoff_created_at: str | None = None) -> dict[str, Any]:
        """Bind a sanitized Work Profile to a session without storing task text."""
        directory = self._require(session_id)
        if not isinstance(profile, dict) or not profile.get("profile_id"):
            raise SessionContextError("WORK_PROFILE_INVALID")
        allowed = {
            "profile_id", "task_difficulty", "desired_reasoning", "codex_custom_mode",
            "codex_reasoning_strength", "deepseek_mode", "local_model_policy",
            "deepseek_ui_generation_preference", "deepseek_reasoning_strength", "deepseek_search_required", "deepseek_vision_required", "deepseek_file_required", "deepseek_combo_policy",
            "external_api_policy", "local_agent_policy", "search_policy", "vision_policy",
            "file_policy", "apply_policy", "test_policy", "commit_policy", "risk_level",
            "target_executor",
        }
        safe_profile = {key: _safe_summary(profile[key], limit=120) for key in allowed if key in profile}
        safe_profile["recommended_codex_mode"] = safe_profile.get("codex_custom_mode", "UNKNOWN")
        safe_profile["recommended_codex_reasoning_strength"] = safe_profile.get("codex_reasoning_strength", "UNKNOWN")
        safe_profile["user_confirmed_codex_mode"] = "YES" if str(user_confirmed_codex_mode).upper() == "YES" else "NO"
        safe_profile["handoff_created_at"] = _safe_summary(handoff_created_at, limit=64) if handoff_created_at else None
        data = self._session_json(directory)
        data["work_profile"] = safe_profile
        self._write_json(directory / "session.json", data)
        self._append_markdown(directory / "rolling_summary.md", "Work Profile", f"已绑定 {safe_profile['profile_id']}；Codex 模式由用户确认：{safe_profile['user_confirmed_codex_mode']}。")
        self._touch(directory)
        return {"status": "WORK_PROFILE_BOUND", "task_session_id": session_id, **safe_profile, "prompt_response_logged": "NO"}

    # Explicit alias for integrations that use record terminology.
    record_work_profile = append_work_profile

    def store_context_bundles(self, session_id: str, local_bundle: dict[str, Any], llm_bundle: dict[str, Any]) -> None:
        directory = self._require(session_id)
        local = self._safe_bundle(local_bundle)
        llm = self._safe_bundle(llm_bundle)
        self._write_json(directory / "context_bundle.json", local)
        self._write_json(directory / "llm_context_bundle.json", llm)
        self._append_markdown(directory / "rolling_summary.md", "上下文更新", "已生成脱敏本地与 LLM 上下文包。")
        self._touch(directory)

    def _safe_bundle(self, bundle: dict[str, Any]) -> dict[str, Any]:
        allowed = ("project_root", "git_status", "changed_files", "relevant_files", "file_snippets", "diff_summary", "risk_flags", "loopback_ports", "collection_mode", "task_difficulty", "selected_brain", "session_context")
        def sanitize(value: Any) -> Any:
            if isinstance(value, str): return _safe_summary(value, limit=1800)
            if isinstance(value, list): return [sanitize(item) for item in value][:40]
            if isinstance(value, dict): return {sanitize(key): sanitize(item) for key, item in list(value.items())[:50]}
            return value
        result = {key: sanitize(bundle.get(key)) for key in allowed if key in bundle}
        result["redaction_applied"] = True
        result["prompt_response_stored"] = False
        return result

    def llm_context(self, session_id: str) -> dict[str, Any]:
        directory = self._require(session_id)
        session = self._session_json(directory)
        patch = self._read_json(directory / "patch_draft_status.json")
        return {
            "task_session_id": session_id, "title": _safe_summary(session.get("title"), limit=200),
            "rolling_summary": _safe_summary((directory / "rolling_summary.md").read_text(encoding="utf-8"), limit=1400),
            "decisions": _safe_summary((directory / "decisions.md").read_text(encoding="utf-8"), limit=1000),
            "codex_status": _safe_summary((directory / "codex_status.md").read_text(encoding="utf-8"), limit=1000),
            "patch_draft_status": {key: _safe_summary(value, limit=300) if isinstance(value, str) else value for key, value in patch.items() if key in {"status", "next_action_suggestion", "patch_draft_location", "unified_diff_detected"}},
            "work_profile": self._safe_profile(session.get("work_profile")),
            "session_context_sanitized": True,
        }

    def record_deepseek_outcome(self, session_id: str, result: dict[str, Any]) -> None:
        directory = self._require(session_id)
        error = str(result.get("error_code") or "")
        patch_status = {
            "status": "unavailable" if error == "PATCH_DRAFT_UNAVAILABLE" else ("created" if result.get("patch_draft_created") == "YES" else "not_requested"),
            "patch_draft_location": _safe_summary(result.get("patch_draft_location"), limit=300),
            "unified_diff_detected": result.get("unified_diff_detected", "NO"),
            "patch_draft_applied": False,
            "tests_executed": False,
            "commit_created": False,
            "redaction_applied": True,
        }
        if error == "PATCH_DRAFT_UNAVAILABLE":
            patch_status["missing_context_summary"] = "模型表示现有脱敏上下文不足。"
            patch_status["next_action_suggestion"] = "人工补充非敏感的必要文件片段后再审查。"
        self._write_json(directory / "patch_draft_status.json", patch_status)
        outcome = "已分析" if result.get("brain_invoked") == "YES" else "未调用模型；已生成本地计划状态。"
        self._append_markdown(directory / "deepseek_plan.md", "协调结果", _safe_summary(f"状态：{result.get('status')}；{outcome}；错误码：{error or 'NONE'}", limit=600))
        self._append_markdown(directory / "decisions.md", "待确认", "任何补丁应用、测试、提交均需人工确认。")
        self._append_markdown(directory / "rolling_summary.md", "DeepSeek 协作", _safe_summary(f"{outcome}。补丁草案状态：{patch_status['status']}。", limit=500))
        self._touch(directory)

    def clear_sensitive_cache(self, session_id: str) -> dict[str, Any]:
        directory = self._require(session_id)
        for name in ("context_bundle.json", "llm_context_bundle.json"):
            self._write_json(directory / name, {"cleared": True, "redaction_applied": True, "prompt_response_stored": False})
        self._append_markdown(directory / "rolling_summary.md", "敏感缓存清理", "已清理可再生成的上下文缓存；未删除状态和安全决策。")
        self._touch(directory)
        return {"status": "CLEARED", "task_session_id": session_id, "sensitive_cache_cleared": "YES", "prompt_response_logged": "NO"}


__all__ = ["SessionContextError", "SessionContextHub"]
