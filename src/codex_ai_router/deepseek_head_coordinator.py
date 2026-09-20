"""DeepSeek Head collaboration coordinator.

The default path is read-only and plan-only.  A model is invoked only when a
caller explicitly supplies ``invoke_brain=true``; DeepSeek then uses the
existing loopback Browser Bridge adapter, never Worker or Wrangler.  External
providers are selected for metadata only and are not sent project context by
this coordinator.
"""
from __future__ import annotations

import json
from pathlib import Path
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from .brain_providers import BrainProviderError, contains_high_risk_intent, contains_real_secret_value, invoke_brain
from .context_collector import ContextCollector, build_llm_context_bundle, sanitize_llm_or_response_text
from .local_agent import LocalAgent, classify_risk, make_plan
from .patch_draft import (
    PatchDraftFormatError,
    PatchSynthesizerError,
    extract_structured_patch,
    persist_patch_draft,
    persist_structured_patch_draft,
    synthesize_structured_patch,
    validate_unified_diff,
)
from .provider_allowlist import providers as allowlisted_providers
from .session_context import SessionContextError, SessionContextHub


DEEPSEEK_HEAD_PATH = "/deepseek-head/coordinate"
DEEPSEEK_HEAD_PROVIDER = "deepseek-bridge-direct"
_READ_ONLY = re.compile(r"(?i)只读|不修改|不改文件|不写入|查看|检查|搜索|列出|读取|审计|分析")
_COMPLEX = re.compile(r"(?i)架构|多文件|重构|worker|provider|agent|api|ui|日志|失败|复杂|实现|修复")


class DeepSeekHeadCoordinatorError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def classify_task(task: str) -> str:
    text = str(task or "")
    if _READ_ONLY.search(text) and not _COMPLEX.search(text):
        return "read_only"
    if _COMPLEX.search(text) or len(text) > 180:
        return "complex"
    if len(text) > 50:
        return "medium"
    return "simple"


def _provider_health(snapshot: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in snapshot:
        if not isinstance(item, dict):
            continue
        provider_type = str(item.get("type") or "")
        provider_id = str(item.get("id") or "")
        name = (
            "deepseek-bridge-direct" if provider_id == "deepseek-bridge-direct"
            else {"DEEPSEEK_WEB_BRIDGE": "deepseek-bridge-direct", "LOCAL_MODEL": "local-light", "EXTERNAL_API_ALLOWED": "external-allowed"}.get(provider_type, provider_id)
        )
        if name:
            result[name] = {
                "status": item.get("status", "UNKNOWN"),
                "enabled": item.get("enabled") is True,
                "type": provider_type,
                "scope": item.get("scope"),
                "fallback_eligible": item.get("fallback_eligible", True),
            }
    return result


def choose_brain(task: str, requested: str = "auto", snapshot: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    requested = requested or "auto"
    health = _provider_health(snapshot if snapshot is not None else allowlisted_providers())
    difficulty = classify_task(task)
    if requested != "auto":
        if requested == "external-allowed" and not (health.get(requested, {}).get("enabled") and health.get(requested, {}).get("status") == "ENABLED"):
            return {"selected_brain": "manual-plan", "task_difficulty": difficulty, "why_selected": "外部 API 未通过 allowlist/健康门控", "fallback_reason": "EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED", "provider_health": health}
        return {"selected_brain": requested, "task_difficulty": difficulty, "why_selected": "用户显式选择辅助脑", "fallback_reason": None, "provider_health": health}
    if difficulty == "read_only":
        return {"selected_brain": "local-agent-readonly", "task_difficulty": difficulty, "why_selected": "只读任务优先由 Local Agent 读取上下文", "fallback_reason": None, "provider_health": health}
    if difficulty == "complex":
        deep = health.get(DEEPSEEK_HEAD_PROVIDER, {})
        if deep.get("enabled") and deep.get("status") == "ENABLED":
            return {"selected_brain": DEEPSEEK_HEAD_PROVIDER, "task_difficulty": difficulty, "why_selected": "复杂工程任务优先 DeepSeek 首脑", "fallback_reason": None, "provider_health": health}
        for fallback in ("local-light", "external-allowed"):
            item = health.get(fallback, {})
            if item.get("enabled") and item.get("status") == "ENABLED":
                return {"selected_brain": fallback, "task_difficulty": difficulty, "why_selected": "DeepSeek 首脑不可用，选择健康辅助脑", "fallback_reason": "DEEPSEEK_BRIDGE_OFFLINE", "provider_health": health}
        return {"selected_brain": "manual-plan", "task_difficulty": difficulty, "why_selected": "没有健康且已启用的辅助脑", "fallback_reason": "NO_HEALTHY_BRAIN_PROVIDER", "provider_health": health}
    return {"selected_brain": "local-light", "task_difficulty": difficulty, "why_selected": "简单任务优先本地模型", "fallback_reason": None, "provider_health": health}


def _context_prompt(bundle: dict[str, Any], task: str, *, require_patch_draft: bool = False, format_retry: bool = False) -> str:
    compact = build_llm_context_bundle(bundle)
    safe_task = sanitize_llm_or_response_text(task).strip()
    patch_requirement = ""
    if require_patch_draft:
        patch_requirement = (
            "\n本次输出必须且只能是以下三者之一；不要输出分析散文、标题、复制按钮文字或解释："
            "(A) 一个 ```diff 代码块，包含 diff --git a/相对路径 b/相对路径、---、+++ 和 @@；"
            "(B) 一个 ```json 代码块，内容严格为 {\"patch_type\":\"structured_patch\",\"files\":[{\"path\":\"relative/path\",\"operations\":[{\"op\":\"replace_block\",\"find\":\"exact old block\",\"replace\":\"new block\",\"reason\":\"...\"}]}],\"suggested_tests\":[],\"risk_notes\":[]}；"
            "(C) 仅输出 PATCH_DRAFT_UNAVAILABLE，下一行使用 missing_context: 列出缺少的上下文。"
            "优先 A；无法生成 A 时使用 B。B 仅允许 replace_block，路径必须相对且位于项目内，find 必须唯一匹配，replace 不得为空。"
            "不得使用绝对路径、../、delete_file、rename_file、chmod 或 binary。"
        )
        if format_retry:
            patch_requirement += "这是一次格式修正请求：仅基于已有脱敏上下文重新输出 unified diff；不要重新收集上下文，不要调用工具。"
    return ("当前为 TEXT_ONLY 计划模式。不能调用工具，不能声称已经读取、修改、运行、提交或部署。"
            "请只输出 Agent Plan、补丁建议、Codex 指令、手动命令建议和风险检查。"
            "敏感配置字段和值已泛化，不要尝试恢复或索要它们。\n用户任务：" +
            safe_task + patch_requirement + "\n只读上下文（已泛化）：\n" + json.dumps(compact, ensure_ascii=False))


def _plan_from_text(text: str, provider: str, task: str, difficulty: str, *, suppress_model_text: bool = False) -> dict[str, Any]:
    if not text.strip():
        raise DeepSeekHeadCoordinatorError("DEEPSEEK_EMPTY_RESPONSE")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    required = {"task_summary", "brain_provider", "risk_level", "requires_files", "requires_write", "requires_tests", "requires_commit", "steps", "codex_instruction", "manual_commands", "stop_conditions"}
    if isinstance(parsed, dict) and required.issubset(parsed):
        parsed["brain_provider"] = provider
        parsed.setdefault("patch_draft", None)
        return parsed
    if isinstance(parsed, dict) and parsed.get("patch_type") == "structured_patch":
        text = "模型返回了结构化补丁草案；本地合成器会先验证路径和唯一锚点，且不会自动应用补丁。"
    elif text.lstrip().startswith(("{", "[")):
        raise DeepSeekHeadCoordinatorError("AGENT_PLAN_SCHEMA_INVALID")
    risk = classify_risk(task)
    description = "模型返回了补丁协议草案；本地验证器将决定是否保存 review-only unified diff，且不会自动应用。" if suppress_model_text else sanitize_llm_or_response_text(text)[:4000]
    return {"task_summary": str(task).strip()[:1200], "brain_provider": provider, "risk_level": risk,
            "requires_files": difficulty != "simple", "requires_write": False, "requires_tests": False, "requires_commit": False,
            "steps": [{"type": "analysis", "description": description, "requires_confirmation": False, "risk": risk}],
            "patch_draft": None, "codex_instruction": "请在 Codex 中审查以下建议，任何写入、测试或提交均需人工确认。",
            "manual_commands": [], "stop_conditions": []}


def _patch_draft_result(text: str, *, requested: bool) -> tuple[dict[str, Any], dict[str, object] | None]:
    """Classify a model reply without treating prose as a patch draft."""
    base = {
        "patch_draft_created": "NO", "unified_diff_detected": "NO",
        "patch_draft_location": None, "patch_draft_format_invalid": "NO",
        "patch_draft_unavailable": "NO", "files_targeted": [],
        "patch_draft_source": "not_requested" if not requested else "not_attempted", "structured_patch_detected": "NO",
        "structured_patch_synthesized": "NO", "patch_synthesizer_error_code": None,
        "patch_draft_metadata": None,
    }
    if not requested:
        return base, None
    if not text.strip():
        base.update({"patch_draft_location": "NONE", "patch_draft_metadata": "NONE"})
        return base, None
    if "PATCH_DRAFT_UNAVAILABLE" in text.upper():
        base.update({"patch_draft_unavailable": "YES", "patch_draft_source": "unavailable"})
        return base, None
    valid, files = validate_unified_diff(text)
    if valid:
        base.update({"patch_draft_created": "YES", "unified_diff_detected": "YES", "files_targeted": files, "patch_draft_source": "unified_diff"})
        return base, None
    structured = extract_structured_patch(text)
    if structured is not None:
        base.update({"patch_draft_source": "structured_patch", "structured_patch_detected": "YES"})
        return base, structured
    base["patch_draft_format_invalid"] = "YES"
    return base, None


def _sanitize_api_response(value: Any) -> Any:
    """Keep API/CLI/UI diagnostic output free of model text and credential labels."""
    if isinstance(value, str):
        return sanitize_llm_or_response_text(value)
    if isinstance(value, list):
        return [_sanitize_api_response(item) for item in value]
    if isinstance(value, dict):
        return {key: _sanitize_api_response(item) for key, item in value.items()}
    return value


@dataclass
class DeepSeekHeadCoordinator:
    root: Path
    agent: LocalAgent | None = None
    provider_snapshot: Callable[[], list[dict[str, Any]]] = allowlisted_providers
    collector: ContextCollector | None = None
    session_hub: SessionContextHub | None = None
    _bundles: dict[str, dict[str, Any]] = field(default_factory=dict)
    _plans: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.agent = self.agent or LocalAgent(self.root)
        self.collector = self.collector or ContextCollector(self.root, self.agent.ledger)
        self.session_hub = self.session_hub or SessionContextHub(self.root)

    def coordinate(self, payload: dict[str, Any]) -> dict[str, Any]:
        task = payload.get("task")
        if not isinstance(task, str) or not task.strip():
            raise DeepSeekHeadCoordinatorError("TASK_REQUIRED")
        requested_root = payload.get("project_root")
        if requested_root is not None:
            try:
                if Path(str(requested_root)).expanduser().resolve() != self.root:
                    raise DeepSeekHeadCoordinatorError("PROJECT_ROOT_NOT_ALLOWED")
            except (OSError, RuntimeError):
                raise DeepSeekHeadCoordinatorError("PROJECT_ROOT_NOT_ALLOWED")
        requested = str(payload.get("brain_provider", payload.get("brain", "auto")) or "auto")
        task_session_id = payload.get("task_session_id")
        use_session_context = payload.get("use_session_context") is True
        if task_session_id is not None and not isinstance(task_session_id, str):
            raise DeepSeekHeadCoordinatorError("TASK_SESSION_ID_INVALID")
        if use_session_context and not task_session_id:
            raise DeepSeekHeadCoordinatorError("TASK_SESSION_ID_REQUIRED")
        if task_session_id:
            try:
                self.session_hub.status(task_session_id)
            except SessionContextError as exc:
                raise DeepSeekHeadCoordinatorError(exc.code) from exc
        choice = choose_brain(task, requested, self.provider_snapshot())
        if payload.get("collect_context", True) is False:
            bundle = {
                "task": task[:1200], "project_root": str(self.root), "git_status": "NOT_COLLECTED", "changed_files": [],
                "relevant_files": [], "file_snippets": [], "diff_summary": "NOT_COLLECTED", "risk_flags": [],
                "redaction_applied": True, "collection_mode": "read_only", "selected_brain": choice["selected_brain"],
                "task_difficulty": choice["task_difficulty"], "recent_agent_metadata": [], "loopback_ports": {}, "commands_executed": [],
                "files_modified": "NO", "write_commands_executed": "NO", "tests_executed": "NO", "secrets_logged": "NO", "prompt_response_logged": "NO",
            }
        else:
            bundle = self.collector.collect(task, selected_brain=choice["selected_brain"])
        if task_session_id and use_session_context:
            try:
                bundle["session_context"] = self.session_hub.llm_context(task_session_id)
            except SessionContextError as exc:
                raise DeepSeekHeadCoordinatorError(exc.code) from exc
        bundle["task_difficulty"] = choice["task_difficulty"]
        bundle_id = "ctx-" + uuid.uuid4().hex[:12]
        self._bundles[bundle_id] = bundle
        if task_session_id:
            try:
                self.session_hub.store_context_bundles(task_session_id, bundle, build_llm_context_bundle(bundle))
            except SessionContextError as exc:
                raise DeepSeekHeadCoordinatorError(exc.code) from exc
        selected = choice["selected_brain"]
        invoke = payload.get("invoke_brain") is True
        patch_draft_requested = payload.get("allow_patch_draft") is True
        selected_mode = payload.get("selected_mode")
        search = payload.get("search")
        output_plan: dict[str, Any] | None = None
        raw_brain_text = ""
        error_code = None
        provider_error_stage: str | None = None
        provider_error_code: str | None = None
        bridge_send_attempted = "NO"
        bridge_ui_send_attempt_count = 0
        model_output_available = "NO"
        adapter_diagnostics: dict[str, Any] = {}
        if invoke and (classify_risk(task) == "high" or contains_high_risk_intent(task) or contains_real_secret_value(task) or bool(bundle.get("secret_value_detected"))):
            error_code = "LOCAL_AGENT_HIGH_RISK_STOP"
            provider_error_stage = "before_bridge_send"
            provider_error_code = "SANITIZER_BLOCKED_BEFORE_SEND"
        elif invoke and selected in {"deepseek-bridge-direct", "deepseek-head"} and not choice["provider_health"].get("deepseek-bridge-direct", {}).get("enabled"):
            error_code = "DEEPSEEK_BRIDGE_DIRECT_ALLOWLIST_BLOCKED"
            provider_error_stage = "before_bridge_send"
            provider_error_code = error_code
        elif invoke and selected in {"deepseek-bridge-direct", "deepseek-head", "local-light"}:
            if selected == "deepseek-head":
                selected = "deepseek-bridge-direct"
            try:
                try:
                    prompt = _context_prompt(bundle, task, require_patch_draft=patch_draft_requested)
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    raise DeepSeekHeadCoordinatorError("LLM_CONTEXT_BUNDLE_BUILD_FAILED")
                raw_brain_text = invoke_brain(selected, prompt, selected_mode=str(selected_mode) if selected_mode is not None else None, search=search if isinstance(search, bool) else None)
                model_output_available = "YES" if raw_brain_text else "NO"
                bridge_send_attempted = "YES" if selected == "deepseek-bridge-direct" and raw_brain_text else "NO"
                bridge_ui_send_attempt_count = 1 if bridge_send_attempted == "YES" else 0
                output_plan = _plan_from_text(raw_brain_text, selected, task, choice["task_difficulty"], suppress_model_text=patch_draft_requested)
            except BrainProviderError as exc:
                error_code = exc.code
                provider_error_stage = str(exc.metadata.get("provider_error_stage") or "before_bridge_send")
                provider_error_code = str(exc.code)
                bridge_send_attempted = str(exc.metadata.get("bridge_send_attempted") or "NO")
                bridge_ui_send_attempt_count = int(exc.metadata.get("bridge_ui_send_attempt_count") or 0)
                model_output_available = str(exc.metadata.get("model_output_available") or "NO")
                adapter_diagnostics = {key: value for key, value in exc.metadata.items() if key in {"provider_error_code", "bridge_error_code", "bridge_stage", "bridge_reason", "requested_profile", "resolved_profile", "reasoning_axis_type", "available_reasoning_strengths", "max_available_reasoning_strength", "high_reasoning_supported", "max_reasoning_supported", "actual_reasoning", "actual_search", "http_post_to_bridge_attempted"}}
            except DeepSeekHeadCoordinatorError as exc:
                error_code = exc.code
                provider_error_stage = "before_bridge_send"
                provider_error_code = exc.code
        elif invoke and selected == "external-allowed":
            error_code = "EXTERNAL_CONTEXT_FORWARDING_NOT_AUTHORIZED"
        if output_plan is None:
            level = classify_risk(task)
            fallback_provider = selected if selected in {"local-light", "deepseek-head", "deepseek-bridge-direct", "hybrid-agent"} else "local-light"
            output_plan = make_plan(task, brain_provider=fallback_provider).to_dict()
            output_plan.update({"patch_draft": None, "stop_conditions": ["LOCAL_AGENT_HIGH_RISK_STOP"] if level == "high" else [], "context_bundle_id": bundle_id})
        plan_id = str(output_plan.get("plan_id") or "plan-" + uuid.uuid4().hex[:12])
        output_plan["context_bundle_id"] = bundle_id
        output_plan["plan_id"] = plan_id
        self._plans[plan_id] = output_plan
        allow_readonly = payload.get("allow_readonly", True) is not False
        allow_patch_draft = patch_draft_requested
        allow_apply = payload.get("allow_apply", False) is True
        allow_test = payload.get("allow_test", False) is True
        allow_commit = payload.get("allow_commit", False) is True
        actions = ["context_collect_read_only" if allow_readonly else "context_collection_disabled", "plan_only"]
        if allow_patch_draft and output_plan.get("requires_write"):
            actions.append("patch_draft_only")
        draft, structured_patch = _patch_draft_result(raw_brain_text, requested=patch_draft_requested)
        if not error_code and draft["patch_draft_source"] == "unified_diff" and draft["patch_draft_created"] == "YES":
            try:
                path, metadata_path, files = persist_patch_draft(
                    self.root, raw_brain_text, task_name="a4f10-patch-draft", brain_provider=selected,
                    context_bundle_id=bundle_id, risk_level=str(output_plan.get("risk_level", "low")),
                )
                location = str(path.relative_to(self.root))
                draft.update({"patch_draft_location": location, "patch_draft_metadata": str(metadata_path.relative_to(self.root)), "files_targeted": files})
                output_plan["patch_draft"] = location
            except PatchDraftFormatError:
                draft.update({"patch_draft_created": "NO", "unified_diff_detected": "NO", "patch_draft_format_invalid": "YES"})
        elif not error_code and structured_patch is not None:
            try:
                diff_text, files, operations_count = synthesize_structured_patch(self.root, structured_patch)
                path, metadata_path = persist_structured_patch_draft(
                    self.root, diff_text, task_name="a4f10-structured-patch", files_targeted=files,
                    operations_count=operations_count, risk_level=str(output_plan.get("risk_level", "low")),
                )
                location = str(path.relative_to(self.root))
                draft.update({
                    "patch_draft_created": "YES", "unified_diff_detected": "YES",
                    "structured_patch_synthesized": "YES", "patch_draft_location": location,
                    "patch_draft_metadata": str(metadata_path.relative_to(self.root)), "files_targeted": files,
                })
                output_plan["patch_draft"] = location
            except PatchSynthesizerError as exc:
                draft.update({
                    "patch_draft_source": "invalid", "patch_draft_format_invalid": "YES",
                    "patch_synthesizer_error_code": str(exc),
                })
        if not error_code and patch_draft_requested and draft["patch_draft_created"] == "NO":
            error_code = "PATCH_DRAFT_UNAVAILABLE" if draft["patch_draft_unavailable"] == "YES" else (draft["patch_synthesizer_error_code"] or "PATCH_DRAFT_FORMAT_INVALID")
        retry_available = bool(model_output_available == "YES" and error_code and error_code.startswith("PATCH_") and error_code != "PATCH_DRAFT_UNAVAILABLE")
        result = {"status": "PASS" if not error_code else "ERROR", "context_bundle_id": bundle_id,
                "task_session_id": task_session_id,
                "selected_brain": choice["selected_brain"], "task_difficulty": choice["task_difficulty"], "why_selected": choice["why_selected"],
                "provider_health": choice["provider_health"], "deepseek_plan_id": plan_id if selected.startswith("deepseek") else None,
                "agent_plan": output_plan, "local_agent_actions": actions,
                "requires_confirmation": bool(output_plan.get("requires_write") or output_plan.get("requires_tests") or output_plan.get("requires_commit")),
                "allow_apply": allow_apply, "allow_test": allow_test, "allow_commit": allow_commit,
                "risk_level": output_plan.get("risk_level", "low"), "fallback_reason": error_code or choice.get("fallback_reason"),
                "brain_invoked": "YES" if invoke and raw_brain_text else "NO", "error_code": error_code,
                "provider_error_stage": provider_error_stage, "provider_error_code": provider_error_code,
                "provider_error_sanitized": "YES", "bridge_send_attempted": bridge_send_attempted,
                "bridge_ui_send_attempt_count": bridge_ui_send_attempt_count,
                "model_output_available": model_output_available,
                "deepseek_request_sent": bridge_send_attempted if selected == "deepseek-bridge-direct" else "NO",
                "patch_draft_retry_sent": "NO",
                **draft, "retry_available": retry_available, "retry_reason": "NO_UNIFIED_DIFF" if retry_available else None,
                "retry_policy": "FORMAT_ONLY_RETRY" if retry_available else None, "retry_prompt_exposed": "NO", "patch_draft_applied": "NO",
                "files_modified": "NO", "write_commands_executed": "NO", "tests_executed": "NO", "commit_created": "NO",
                "loopback_only": "YES", "worker_required": "NO", "wrangler_required": "NO", "tools_forwarded": "NO",
                "prompt_response_logged": "NO", "sensitive_data_logged": "NO", "codex_agent_used": "NO", "codex_agentic_usage_required": "NO"}
        result.update(adapter_diagnostics)
        if task_session_id:
            try:
                self.session_hub.record_deepseek_outcome(task_session_id, result)
            except SessionContextError as exc:
                raise DeepSeekHeadCoordinatorError(exc.code) from exc
        return _sanitize_api_response(result)

    def context(self, context_id: str) -> dict[str, Any]:
        bundle = self._bundles.get(context_id)
        if bundle is None:
            raise DeepSeekHeadCoordinatorError("CONTEXT_BUNDLE_NOT_FOUND")
        return _sanitize_api_response({"context_bundle_id": context_id, "context_bundle": build_llm_context_bundle(bundle)})

    def plan_from_context(self, context_id: str, *, invoke: bool = False) -> dict[str, Any]:
        bundle = self._bundles.get(context_id)
        if bundle is None:
            raise DeepSeekHeadCoordinatorError("CONTEXT_BUNDLE_NOT_FOUND")
        task = str(bundle.get("task") or "只读项目检查")
        selected = str(bundle.get("selected_brain") or "local-light")
        if selected == "local-agent-readonly":
            selected = "local-light"
        if invoke and (contains_high_risk_intent(task) or contains_real_secret_value(task) or bool(bundle.get("secret_value_detected"))):
            raise DeepSeekHeadCoordinatorError("LOCAL_AGENT_HIGH_RISK_STOP")
        if invoke and selected in {"deepseek-bridge-direct", "local-light"}:
            raw = invoke_brain(selected, _context_prompt(bundle, task))
            plan = _plan_from_text(raw, selected, task, str(bundle.get("task_difficulty") or "unknown"))
        else:
            plan = make_plan(task, brain_provider=selected if selected in {"local-light", "deepseek-head", "deepseek-bridge-direct", "hybrid-agent"} else "local-light").to_dict()
            plan["patch_draft"] = None
        plan_id = str(plan.get("plan_id") or "plan-" + uuid.uuid4().hex[:12])
        plan["plan_id"] = plan_id
        plan["context_bundle_id"] = context_id
        self._plans[plan_id] = plan
        return _sanitize_api_response({"status": "PASS", "context_bundle_id": context_id, "plan_id": plan_id, "agent_plan": plan,
                "brain_invoked": "YES" if invoke else "NO", "files_modified": "NO", "tests_executed": "NO", "commit_created": "NO"}
        )

    def plan(self, plan_id: str) -> dict[str, Any]:
        value = self._plans.get(plan_id)
        if value is None:
            raise DeepSeekHeadCoordinatorError("DEEPSEEK_PLAN_NOT_FOUND")
        return value


__all__ = ["DEEPSEEK_HEAD_PATH", "DeepSeekHeadCoordinator", "DeepSeekHeadCoordinatorError", "choose_brain", "classify_task"]
