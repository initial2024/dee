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

from .brain_providers import BrainProviderError, invoke_brain
from .context_collector import ContextCollector
from .local_agent import LocalAgent, classify_risk, make_plan
from .provider_allowlist import providers as allowlisted_providers


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
        name = {"DEEPSEEK_WEB_BRIDGE": "deepseek-bridge-direct", "LOCAL_MODEL": "local-light", "EXTERNAL_API_ALLOWED": "external-allowed"}.get(provider_type, str(item.get("id") or ""))
        if name:
            result[name] = {"status": item.get("status", "UNKNOWN"), "enabled": item.get("enabled") is True, "type": provider_type}
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


def _context_prompt(bundle: dict[str, Any], task: str) -> str:
    compact = {key: bundle.get(key) for key in ("project_root", "git_status", "changed_files", "relevant_files", "file_snippets", "diff_summary", "risk_flags", "loopback_ports")}
    return ("当前为 TEXT_ONLY 计划模式。不能调用工具，不能声称已经读取、修改、运行、提交或部署。"
            "请只输出 Agent Plan、补丁建议、Codex 指令、手动命令建议和风险检查。\n用户任务：" +
            str(task).strip() + "\n只读上下文（已脱敏）：\n" + json.dumps(compact, ensure_ascii=False))


def _plan_from_text(text: str, provider: str, task: str, difficulty: str) -> dict[str, Any]:
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
    if text.lstrip().startswith(("{", "[")):
        raise DeepSeekHeadCoordinatorError("AGENT_PLAN_SCHEMA_INVALID")
    risk = classify_risk(task)
    return {"task_summary": str(task).strip()[:1200], "brain_provider": provider, "risk_level": risk,
            "requires_files": difficulty != "simple", "requires_write": False, "requires_tests": False, "requires_commit": False,
            "steps": [{"type": "analysis", "description": text.strip()[:4000], "requires_confirmation": False, "risk": risk}],
            "patch_draft": None, "codex_instruction": "请在 Codex 中审查以下建议，任何写入、测试或提交均需人工确认。",
            "manual_commands": [], "stop_conditions": []}


@dataclass
class DeepSeekHeadCoordinator:
    root: Path
    agent: LocalAgent | None = None
    provider_snapshot: Callable[[], list[dict[str, Any]]] = allowlisted_providers
    collector: ContextCollector | None = None
    _bundles: dict[str, dict[str, Any]] = field(default_factory=dict)
    _plans: dict[str, dict[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.agent = self.agent or LocalAgent(self.root)
        self.collector = self.collector or ContextCollector(self.root, self.agent.ledger)

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
        bundle["task_difficulty"] = choice["task_difficulty"]
        bundle_id = "ctx-" + uuid.uuid4().hex[:12]
        self._bundles[bundle_id] = bundle
        selected = choice["selected_brain"]
        invoke = payload.get("invoke_brain") is True
        output_plan: dict[str, Any] | None = None
        error_code = None
        if invoke and classify_risk(task) == "high":
            error_code = "LOCAL_AGENT_HIGH_RISK_STOP"
        elif invoke and selected in {"deepseek-bridge-direct", "deepseek-head", "local-light"}:
            if selected == "deepseek-head":
                selected = "deepseek-bridge-direct"
            try:
                output_plan = _plan_from_text(invoke_brain(selected, _context_prompt(bundle, task)), selected, task, choice["task_difficulty"])
            except BrainProviderError as exc:
                error_code = exc.code
            except DeepSeekHeadCoordinatorError as exc:
                error_code = exc.code
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
        allow_patch_draft = payload.get("allow_patch_draft", True) is not False
        allow_apply = payload.get("allow_apply", False) is True
        allow_test = payload.get("allow_test", False) is True
        allow_commit = payload.get("allow_commit", False) is True
        actions = ["context_collect_read_only" if allow_readonly else "context_collection_disabled", "plan_only"]
        if allow_patch_draft and output_plan.get("requires_write"):
            actions.append("patch_draft_only")
        return {"status": "PASS" if not error_code else "ERROR", "context_bundle_id": bundle_id,
                "selected_brain": choice["selected_brain"], "task_difficulty": choice["task_difficulty"], "why_selected": choice["why_selected"],
                "provider_health": choice["provider_health"], "deepseek_plan_id": plan_id if selected.startswith("deepseek") else None,
                "agent_plan": output_plan, "local_agent_actions": actions,
                "requires_confirmation": bool(output_plan.get("requires_write") or output_plan.get("requires_tests") or output_plan.get("requires_commit")),
                "allow_apply": allow_apply, "allow_test": allow_test, "allow_commit": allow_commit,
                "risk_level": output_plan.get("risk_level", "low"), "fallback_reason": error_code or choice.get("fallback_reason"),
                "brain_invoked": "YES" if invoke and error_code is None else "NO", "error_code": error_code,
                "files_modified": "NO", "write_commands_executed": "NO", "tests_executed": "NO", "commit_created": "NO",
                "loopback_only": "YES", "worker_required": "NO", "wrangler_required": "NO", "tools_forwarded": "NO",
                "prompt_response_logged": "NO", "secrets_logged": "NO", "codex_agent_used": "NO", "codex_agentic_usage_required": "NO"}

    def context(self, context_id: str) -> dict[str, Any]:
        bundle = self._bundles.get(context_id)
        if bundle is None:
            raise DeepSeekHeadCoordinatorError("CONTEXT_BUNDLE_NOT_FOUND")
        return {"context_bundle_id": context_id, "context_bundle": bundle}

    def plan_from_context(self, context_id: str, *, invoke: bool = False) -> dict[str, Any]:
        bundle = self._bundles.get(context_id)
        if bundle is None:
            raise DeepSeekHeadCoordinatorError("CONTEXT_BUNDLE_NOT_FOUND")
        task = str(bundle.get("task") or "只读项目检查")
        selected = str(bundle.get("selected_brain") or "local-light")
        if selected == "local-agent-readonly":
            selected = "local-light"
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
        return {"status": "PASS", "context_bundle_id": context_id, "plan_id": plan_id, "agent_plan": plan,
                "brain_invoked": "YES" if invoke else "NO", "files_modified": "NO", "tests_executed": "NO", "commit_created": "NO"}

    def plan(self, plan_id: str) -> dict[str, Any]:
        value = self._plans.get(plan_id)
        if value is None:
            raise DeepSeekHeadCoordinatorError("DEEPSEEK_PLAN_NOT_FOUND")
        return value


__all__ = ["DEEPSEEK_HEAD_PATH", "DeepSeekHeadCoordinator", "DeepSeekHeadCoordinatorError", "choose_brain", "classify_task"]
