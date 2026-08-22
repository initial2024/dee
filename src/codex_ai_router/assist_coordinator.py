"""Deterministic coordinator for the OFFICIAL_ASSISTED_COORDINATOR mode.

The coordinator only classifies and partitions work.  It never invokes a
model, Codex, a shell command, or a remote provider.  A later caller may use
the returned handoff as input to an explicitly confirmed Local Agent/Codex
step, but this module itself is planning-only.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable, Iterable

from .local_agent import classify_risk
from .security.secrets import redact


ASSIST_COORDINATE_PATH = "/assist/coordinate"
OFFICIAL_ASSISTED_COORDINATOR = "OFFICIAL_ASSISTED_COORDINATOR"
ASSIST_STARTUP_INSTRUCTION = (
    "启动官方辅助模式。先调用小羽本地协调器：\n"
    "POST http://127.0.0.1:18789/assist/coordinate\n"
    "把当前任务发送给协调器。\n"
    "执行协调器返回的 codex_required_steps。\n"
    "不要执行被小羽标记为 local_agent_steps 的简单辅助任务。\n"
    "遇到高风险步骤先停止并询问用户。"
)

_WRITE_MARKERS = re.compile(
    r"修改|改动|改文件|修复|应用补丁|写入|保存|创建文件|删除|重命名|更新代码|格式化|\b(?:edit|modify|fix|apply|write|save|delete|rename|update|commit)\b",
    re.I,
)
_TEST_MARKERS = re.compile(r"测试|验证|回归|pytest|unittest|npm\s+(?:run\s+)?(?:test|lint|build)", re.I)
_COMMIT_MARKERS = re.compile(r"\bcommit\b|提交", re.I)
_MULTI_FILE_MARKERS = re.compile(r"多文件|多个文件|跨文件|整个项目|全仓库|重构|批量", re.I)
_READ_MARKERS = re.compile(r"只读|检查|查看|搜索|列出|读取|分析|审计|状态", re.I)
_HIGH_RISK_MARKERS = re.compile(
    r"删除\s*(?:所有|全部)|清空(?:仓库|目录|文件)|git\s+push|\bdeploy\b|部署|生产|防火墙|开放\s*(?:LAN|局域网|公网)|打印\s*(?:密钥|token|cookie)|绕过(?:验证码|风控)|自动登录",
    re.I,
)
_NEGATED_WRITE_MARKERS = re.compile(r"(?:不|未|无需|不要|禁止)\s*(?:修改|改动|改文件|修复|应用补丁|写入|保存|创建|删除|重命名|更新|格式化|提交|commit|edit|modify|fix|apply|write|save|delete|rename|update)", re.I)


def _actual_write_intent(task: str) -> bool:
    """Detect write intent after removing explicit non-write language."""
    cleaned = _NEGATED_WRITE_MARKERS.sub(" ", str(task or ""))
    # Patch drafts that are explicitly not applied are planning-only.
    cleaned = re.sub(r"(?:生成|创建)\s*(?:候选\s*)?(?:patch|diff|补丁)(?:\s*草案)?\s*(?:但\s*)?不\s*(?:应用|执行)", " ", cleaned, flags=re.I)
    return bool(_WRITE_MARKERS.search(cleaned))


class AssistCoordinatorError(ValueError):
    """Stable, UI-safe input error for the coordinator endpoint."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _safe_summary(task: str, limit: int = 500) -> str:
    value = redact(str(task or "")).strip()
    value = re.sub(r"(?i)(cookie|token|authorization|密码|凭据)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", value)
    return value[:limit]


def _healthy_provider_names(snapshot: Iterable[dict[str, Any]] | None) -> set[str]:
    names: set[str] = set()
    for item in snapshot or ():
        if not isinstance(item, dict) or item.get("enabled") is not True:
            continue
        status = str(item.get("status", "ENABLED")).upper()
        if status not in {"ENABLED", "READY", "PASS", "HEALTHY"}:
            continue
        provider_type = str(item.get("type", "")).upper()
        provider_id = str(item.get("id", item.get("provider_id", ""))).lower()
        if provider_type in {"LOCAL_MODEL", "LMSTUDIO", "LOCAL"} or provider_id in {"local-light", "local", "lmstudio"}:
            names.add("local-light")
        elif provider_type in {"DEEPSEEK_WEB_BRIDGE", "DEEPSEEK_BRIDGE"} or "deepseek" in provider_id:
            names.add("deepseek-head")
        elif provider_type in {"EXTERNAL_API_ALLOWED", "EXTERNAL"}:
            names.add("external-allowed")
    return names


def default_provider_snapshot() -> Iterable[dict[str, Any]]:
    """Read local provider health metadata only; never invoke a completion."""
    try:
        from .provider_allowlist import providers

        return providers()
    except Exception:
        return []


def _choose_brain(task: str, allow_brain: bool, snapshot: Iterable[dict[str, Any]] | None) -> tuple[str, str | None]:
    if not allow_brain:
        return "manual-plan", "BRAIN_DISABLED_BY_REQUEST"
    healthy = _healthy_provider_names(snapshot)
    if not healthy:
        return "manual-plan", "NO_HEALTHY_BRAIN_PROVIDER"
    if _MULTI_FILE_MARKERS.search(task) or _actual_write_intent(task):
        preferred = ("deepseek-head", "local-light", "external-allowed")
    else:
        preferred = ("local-light", "deepseek-head", "external-allowed")
    for name in preferred:
        if name in healthy:
            fallback = None if name == preferred[0] else f"{preferred[0].upper().replace('-', '_')}_UNAVAILABLE"
            return name, fallback
    return "manual-plan", "NO_HEALTHY_BRAIN_PROVIDER"


def _local_steps(task: str, limit: int, allow_readonly: bool, allow_patch_draft: bool) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if allow_readonly and _READ_MARKERS.search(task):
        steps.append({
            "type": "readonly",
            "description": "只读检查项目状态和变更文件名。",
            "requires_confirmation": False,
            "risk": "low",
            "command": "git status --short; git diff --name-only",
        })
    steps.append({
        "type": "plan",
        "description": "整理计划、风险和交接信息；不执行命令。",
        "requires_confirmation": False,
        "risk": "low",
        "command": "",
    })
    if allow_patch_draft and _actual_write_intent(task) and not _COMMIT_MARKERS.search(task):
        steps.append({
            "type": "patch_draft",
            "description": "生成候选补丁草案；不应用、不写入项目。",
            "requires_confirmation": False,
            "risk": "medium",
            "command": "",
        })
    return steps[: max(0, limit)]


def _codex_steps(task: str, risk: str) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    if _actual_write_intent(task):
        steps.append({"type": "apply_or_modify", "description": "审阅并应用多文件或写入变更。", "requires_confirmation": True, "risk": risk})
    if _TEST_MARKERS.search(task):
        steps.append({"type": "test", "description": "在官方 Codex 工具上下文中运行并修复测试。", "requires_confirmation": True, "risk": "medium"})
    if _COMMIT_MARKERS.search(task):
        steps.append({"type": "commit", "description": "审阅 diff 后执行本地 commit；禁止 push。", "requires_confirmation": True, "risk": risk})
    if _MULTI_FILE_MARKERS.search(task) and not steps:
        steps.append({"type": "codex_review", "description": "由 Codex 官方工具审阅跨文件任务。", "requires_confirmation": True, "risk": risk})
    return steps


@dataclass
class AssistCoordinator:
    """Plan-only coordinator with an injectable metadata snapshot for tests."""

    provider_snapshot: Callable[[], Iterable[dict[str, Any]]] | None = default_provider_snapshot

    def coordinate(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AssistCoordinatorError("INVALID_REQUEST")
        task = payload.get("task")
        if not isinstance(task, str) or not task.strip():
            raise AssistCoordinatorError("TASK_REQUIRED")
        mode = str(payload.get("mode", "official_assisted")).lower()
        if mode not in {"official_assisted", "official-assisted", OFFICIAL_ASSISTED_COORDINATOR.lower()}:
            raise AssistCoordinatorError("ASSIST_MODE_INVALID")
        try:
            max_steps = int(payload.get("max_local_steps", 5))
        except (TypeError, ValueError) as exc:
            raise AssistCoordinatorError("INVALID_MAX_LOCAL_STEPS") from exc
        if not 0 <= max_steps <= 5:
            raise AssistCoordinatorError("INVALID_MAX_LOCAL_STEPS")
        allow_brain = payload.get("allow_brain", True) is True
        allow_readonly = payload.get("allow_readonly", True) is True
        allow_patch_draft = payload.get("allow_patch_draft", True) is True
        snapshot = self.provider_snapshot() if self.provider_snapshot else None
        risk = classify_risk(task)
        high_risk = bool(_HIGH_RISK_MARKERS.search(task)) or risk == "high"
        recommended, fallback = _choose_brain(task, allow_brain, snapshot)
        if high_risk:
            return {
                "task_summary": _safe_summary(task),
                "recommended_brain": recommended,
                "local_agent_steps": [],
                "codex_required_steps": [],
                "codex_instruction": ASSIST_STARTUP_INSTRUCTION + "\n当前任务触发高风险停止，请先询问用户。",
                "risk_level": "high",
                "requires_official_codex": True,
                "stop_conditions": ["LOCAL_AGENT_HIGH_RISK_STOP"],
                "fallback_reason": "HIGH_RISK_STOP",
                "mode": OFFICIAL_ASSISTED_COORDINATOR,
                "codex_endpoint_touched": "NO",
                "codex_agent_auto_invoked": "NO",
                "brain_invoked": "NO",
                "workspace_write": "NO",
                "official_direct_unchanged": "YES",
                "codex_agentic_usage_bypass": "NO",
                "auto_file_modify": "NO",
                "auto_command_execute": "NO",
                "auto_commit": "NO",
                "auto_push": "NO",
                "auto_deploy": "NO",
                "external_api_called": "NO",
                "secrets_logged": "NO",
            }
        local = _local_steps(task, max_steps, allow_readonly, allow_patch_draft)
        codex = _codex_steps(task, risk)
        if not codex and not local:
            local = _local_steps(task, max_steps, allow_readonly, allow_patch_draft)
        requires_official = bool(codex)
        return {
            "task_summary": _safe_summary(task),
            "recommended_brain": recommended,
            "local_agent_steps": local,
            "codex_required_steps": codex,
            "codex_instruction": ASSIST_STARTUP_INSTRUCTION + ("\n请执行返回的 codex_required_steps。" if codex else "\n本任务可由小羽只读辅助完成。"),
            "risk_level": risk,
            "requires_official_codex": requires_official,
            "stop_conditions": ["CONFIRMATION_REQUIRED"] if codex else [],
            "fallback_reason": fallback,
            "mode": OFFICIAL_ASSISTED_COORDINATOR,
            "codex_endpoint_touched": "NO",
            "codex_agent_auto_invoked": "NO",
            "brain_invoked": "NO",
            "workspace_write": "NO",
            "official_direct_unchanged": "YES",
            "codex_agentic_usage_bypass": "NO",
            "auto_file_modify": "NO",
            "auto_command_execute": "NO",
            "auto_commit": "NO",
            "auto_push": "NO",
            "auto_deploy": "NO",
            "external_api_called": "NO",
            "secrets_logged": "NO",
        }


def startup_instruction() -> str:
    return ASSIST_STARTUP_INSTRUCTION


__all__ = [
    "ASSIST_COORDINATE_PATH",
    "ASSIST_STARTUP_INSTRUCTION",
    "AssistCoordinator",
    "AssistCoordinatorError",
    "default_provider_snapshot",
    "OFFICIAL_ASSISTED_COORDINATOR",
    "startup_instruction",
]
