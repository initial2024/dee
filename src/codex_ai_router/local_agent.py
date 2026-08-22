"""Local, confirmation-gated execution layer for Xiaoyu Router.

This module deliberately keeps the model (the ``brain``) separate from the
execution layer.  A brain may propose a plan, but only this module can turn a
plan into an action, and every workspace mutation, test run, or commit is
explicitly gated by ``confirm=True``.  The default planner is deterministic so
CLI/UI discovery never needs to call a provider or DeepSeek.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from typing import Any, Callable, Iterable

from .provider_allowlist import providers as allowlisted_providers
from .security.secrets import is_sensitive_path, redact
from .brain_providers import BrainProviderError, invoke_brain


BRAIN_PROVIDERS = ("local-light", "deepseek-head", "external-allowed", "hybrid-agent")
PLAN_MODES = ("READ_ONLY", "PLAN_ONLY", "PATCH_DRAFT", "APPLY_WITH_CONFIRM", "SAFE_TEST", "COMMIT_WITH_CONFIRM")

_HIGH_RISK_PATTERNS = (
    r"\bgit\s+push\b", r"\bpush\b", r"deploy", r"production", r"生产", r"部署",
    r"firewall", r"防火墙", r"公网", r"开放\s*(?:lan|局域网)", r"\bLAN\b",
    r"\.ssh", r"ssh\s*key", r"密钥", r"private\s+key", r"私钥",
    r"password", r"密码", r"cookie", r"token", r"authorization", r"api[_ -]?key", r"secret", r"凭据",
    r"captcha", r"验证码", r"风控", r"绕过", r"bypass", r"pow\s*solver",
    r"curl[_-]?cffi", r"remote\s+script", r"远程脚本", r"rm\s+-rf", r"del\s+/s",
    r"删除\s*(?:所有|全部)", r"清空\s*(?:目录|文件)", r"format\s+disk", r"修改系统",
)
_WRITE_PATTERNS = (
    r"\b(write|edit|modify|change|create|implement|fix|apply|save|delete|rename|format|update)\b",
    r"修改", r"改(?:动|文件|代码|配置)", r"编辑", r"写入", r"保存", r"创建", r"实现", r"修复",
    r"删除", r"重命名", r"格式化", r"更新(?:代码|文件|配置)?", r"应用(?:补丁|改动)?",
)
_WRITE_NEGATION_PATTERNS = (
    # Remove explicit non-write phrases before checking positive write verbs.
    # This keeps "检查并修复" writable while classifying "不修改文件" as read-only.
    r"(?:不|未|无需|不要|禁止)\s*(?:修改|改(?:动)?|写入|保存|创建|删除|重命名|应用(?:补丁|改动)?|修复|更新|格式化|提交|commit|write|edit|modify|change|create|implement|fix|apply|save|delete|rename|format|update)",
    r"(?:只读|检查(?:当前)?状态|查看|搜索|列出|读取)",
    r"分析\s*(?:但|不过|，|,)?\s*不\s*(?:修改|改(?:动)?|写入|保存)",
    r"(?:生成|创建)\s*(?:候选\s*)?(?:unified\s+)?(?:diff|patch|补丁)(?:\s*(?:草案|draft))?\s*(?:但\s*)?不\s*(?:应用|执行)",
)
_TEST_PATTERNS = (r"\btest(?:s|ing)?\b", r"pytest", r"unittest", r"npm\s+(?:run\s+)?(?:test|lint|build)", r"测试", r"验证", r"回归")
_COMMIT_PATTERNS = (r"\bcommit\b", r"提交", r"提交\s*commit")
_READ_ONLY_PATTERNS = (r"\bread[- ]?only\b", r"只读", r"审计", r"查看", r"检查", r"分析", r"列出", r"读取")


class LocalAgentError(RuntimeError):
    """Stable, UI-safe error code for the local execution layer."""

    def __init__(self, code: str, message: str | None = None):
        super().__init__(message or code)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_text(value: object, limit: int = 500) -> str:
    text = redact(str(value or "")).strip()
    text = re.sub(r"(?i)(cookie|token|authorization|密码|凭据)\s*[:=]\s*[^\s,;]+", r"\1=[REDACTED]", text)
    return text[:limit]


def _fingerprint(task: str) -> str:
    return hashlib.sha256(task.encode("utf-8", "replace")).hexdigest()[:16]


def _matches(task: str, patterns: Iterable[str]) -> bool:
    value = task.lower()
    return any(re.search(pattern, value, flags=re.I) for pattern in patterns)


def _write_intent_text(task: str) -> str:
    """Return task text with explicit read-only/negated write phrases removed."""
    value = str(task or "").lower()
    for pattern in _WRITE_NEGATION_PATTERNS:
        value = re.sub(pattern, " ", value, flags=re.I)
    return value


def _requires_write(task: str) -> bool:
    """Classify actual write intent without treating negated verbs as writes."""
    value = _write_intent_text(task)
    return any(re.search(pattern, value, flags=re.I) for pattern in (*_WRITE_PATTERNS, *_COMMIT_PATTERNS))


def classify_risk(task: str, explicit: str = "auto") -> str:
    """Return the user-facing ``low|medium|high`` risk bucket."""
    chosen = str(explicit or "auto").lower()
    if chosen in {"low", "medium", "high"}:
        return chosen
    if _matches(task, _HIGH_RISK_PATTERNS):
        return "high"
    if _requires_write(task):
        return "medium"
    return "low"


def _permission(risk: str) -> str:
    return str(risk or "medium").upper()


def _brain_gate(provider: str) -> dict[str, Any]:
    if provider not in BRAIN_PROVIDERS:
        raise LocalAgentError("BRAIN_PROVIDER_INVALID")
    if provider != "external-allowed":
        return {"provider": provider, "status": "CONFIGURED_NOT_CALLED", "allowlisted": "YES", "key_visible": "NO"}
    # The allowlist snapshot exposes presence/status metadata only.  It never
    # returns an API key, Authorization value, or provider response body.
    try:
        records = allowlisted_providers()
    except Exception:
        records = []
    enabled = [item for item in records if item.get("type") == "EXTERNAL_API_ALLOWED" and item.get("enabled") is True and item.get("status") == "ENABLED"]
    if not enabled:
        raise LocalAgentError("EXTERNAL_PROVIDER_NOT_ALLOWLIST_ENABLED")
    return {"provider": provider, "status": "ALLOWLIST_ENABLED_NOT_CALLED", "allowlisted": "YES", "key_visible": "NO"}


def _high_risk_reason(task: str) -> str | None:
    if _matches(task, _HIGH_RISK_PATTERNS):
        return "LOCAL_AGENT_HIGH_RISK_STOP"
    return None


def _manual_commands(task: str, requires_tests: bool) -> list[str]:
    commands = ["git status --short", "git diff --name-only"]
    if requires_tests:
        commands.append("python -m unittest -q")
    if _requires_write(task):
        commands.append("git diff --check")
    return commands


def _codex_instruction(risk: str, requires_write: bool, requires_tests: bool, requires_commit: bool) -> str:
    lines = [
        "请由小羽 Local Agent 按计划执行；模型只提供建议，不代表已执行任何工具。",
        "先显示文件列表和 diff 摘要，再逐步请求人工确认。",
    ]
    if requires_write:
        lines.append("写入前必须使用 APPLY_WITH_CONFIRM，并由用户确认具体文件和补丁。")
    if requires_tests:
        lines.append("测试前必须使用 SAFE_TEST，并确认白名单命令。")
    if requires_commit:
        lines.append("提交前必须使用 COMMIT_WITH_CONFIRM；只允许本地 commit，禁止 push。")
    if risk == "high":
        lines.append("该任务触发高风险停止，禁止自动执行；需要用户改写为低风险、可审计步骤。")
    return " ".join(lines)


def _build_steps(risk: str, requires_write: bool, requires_tests: bool, requires_commit: bool, denied: str | None) -> list[dict[str, Any]]:
    if denied:
        return [{"type": "read", "description": "高风险任务已停止，仅返回安全边界说明。", "command": "", "files": [], "requires_confirmation": False, "risk": "high"}]
    steps: list[dict[str, Any]] = [{"type": "read", "description": "读取 git 状态和变更文件名。", "command": "git status --short; git diff --name-only", "files": [], "requires_confirmation": False, "risk": "low"}]
    if requires_write:
        steps.append({"type": "patch", "description": "生成候选 unified diff；不自动应用。", "command": "", "files": [], "requires_confirmation": True, "risk": risk})
        steps.append({"type": "apply", "description": "显示 diff 摘要后人工确认并应用补丁。", "command": "git apply --check <patch-file>", "files": [], "requires_confirmation": True, "risk": risk})
    if requires_tests:
        steps.append({"type": "test", "description": "运行预先声明的白名单测试。", "command": "python -m unittest -q", "files": [], "requires_confirmation": True, "risk": "low"})
    if requires_commit:
        steps.append({"type": "commit", "description": "仅在人工确认后提交本地 git commit。", "command": "git commit -m <message>", "files": [], "requires_confirmation": True, "risk": risk})
    return steps


@dataclass
class AgentPlan:
    plan_id: str
    created_at: str
    task_summary: str
    task_fingerprint: str
    brain_provider: str
    brain_status: str
    risk_level: str
    requires_files: bool
    requires_write: bool
    requires_tests: bool
    requires_commit: bool
    steps: list[dict[str, Any]] = field(default_factory=list)
    codex_instruction: str = ""
    manual_commands: list[str] = field(default_factory=list)
    deny_reason: str | None = None
    status: str = "PLANNED"
    files_touched: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def make_plan(task: str, brain_provider: str = "local-light", risk: str = "auto") -> AgentPlan:
    if not isinstance(task, str) or not task.strip():
        raise LocalAgentError("TASK_REQUIRED")
    gate = _brain_gate(brain_provider)
    level = classify_risk(task, risk)
    denied = "LOCAL_AGENT_HIGH_RISK_STOP" if level == "high" else _high_risk_reason(task)
    requires_write = _requires_write(task)
    requires_tests = _matches(task, _TEST_PATTERNS) or requires_write
    requires_commit = _matches(task, _COMMIT_PATTERNS)
    # A high-risk plan never escalates itself into a write/test/commit action.
    if denied:
        requires_write = requires_tests = requires_commit = False
    plan_id = "agent-" + uuid.uuid4().hex[:12]
    return AgentPlan(
        plan_id=plan_id,
        created_at=_now(),
        task_summary=_safe_text(task),
        task_fingerprint=_fingerprint(task),
        brain_provider=brain_provider,
        brain_status=gate["status"],
        risk_level=level,
        requires_files=_matches(task, _READ_ONLY_PATTERNS) or requires_write,
        requires_write=requires_write,
        requires_tests=requires_tests,
        requires_commit=requires_commit,
        steps=_build_steps(level, requires_write, requires_tests, requires_commit, denied),
        codex_instruction=_codex_instruction(level, requires_write, requires_tests, requires_commit),
        manual_commands=_manual_commands(task, requires_tests),
        deny_reason=denied,
    )


def _storage_root() -> Path:
    configured = os.getenv("XIAOYU_ROUTER_AGENT_DIR")
    return Path(configured).expanduser() if configured else Path.home() / ".codex-ai-router" / "local-agent"


class LocalAgent:
    """Confirmation-gated local executor.

    ``root`` is the only workspace allowed for file and git operations.  The
    plan/ledger directory is outside the workspace and stores metadata only;
    no prompt, response, key, cookie, token, or Authorization value is logged.
    """

    SAFE_TESTS: dict[str, tuple[str, ...]] = {
        "python-unittest": ("python", "-m", "unittest", "-q"),
        "pytest": ("python", "-m", "pytest", "-q"),
        "npm-test": ("npm", "test"),
        "npm-lint": ("npm", "run", "lint"),
        "npm-build": ("npm", "run", "build"),
        "git-diff-check": ("git", "diff", "--check"),
    }
    _SAFE_READ_COMMANDS = {
        "git-status": ("git", "status", "--short"),
        "git-diff-names": ("git", "diff", "--name-only"),
    }

    def __init__(self, root: Path | None = None, storage: Path | None = None, runner: Callable[..., tuple[int, str]] | None = None):
        self.root = (root or Path.cwd()).resolve()
        self.storage = (storage or _storage_root()).expanduser().resolve()
        self.plans_dir = self.storage / "plans"
        self.drafts_dir = self.storage / "drafts"
        self.ledger = self.storage / "ledger.jsonl"
        self._runner = runner

    def _record(self, plan: AgentPlan | None, action: str, status: str, error_code: str | None = None, duration_ms: int = 0, files: list[str] | None = None, confirmed: bool = False) -> None:
        payload = {
            "id": plan.plan_id if plan else None,
            "created_at": _now(),
            "brain_provider": plan.brain_provider if plan else None,
            "mode": action,
            "risk_level": plan.risk_level if plan else None,
            "action": action,
            "status": status,
            "duration_ms": duration_ms,
            "error_code": error_code,
            "files_touched": list(files or []),
            "user_confirmed": bool(confirmed),
            "saved_body": False,
        }
        try:
            self.storage.mkdir(parents=True, exist_ok=True)
            with self.ledger.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            # Diagnostics must never make a safe action fail.
            return

    def _save_plan(self, plan: AgentPlan) -> Path:
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        target = self.plans_dir / (plan.plan_id + ".json")
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)
        return target

    def _load_plan(self, plan_id: str) -> AgentPlan:
        if not re.fullmatch(r"agent-[a-f0-9]{12}", str(plan_id or "")):
            raise LocalAgentError("PLAN_ID_INVALID")
        try:
            data = json.loads((self.plans_dir / (plan_id + ".json")).read_text(encoding="utf-8"))
            return AgentPlan(**{key: data[key] for key in AgentPlan.__dataclass_fields__ if key in data})
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise LocalAgentError("PLAN_NOT_FOUND") from exc

    def plan(self, task: str, brain_provider: str = "local-light", risk: str = "auto", invoke_brain_now: bool = False) -> dict[str, Any]:
        started = time.monotonic()
        plan = make_plan(task, brain_provider, risk)
        brain_output: str | None = None
        brain_error: str | None = None
        if invoke_brain_now and not plan.deny_reason:
            try:
                brain_output = invoke_brain(brain_provider, task)
                plan.brain_status = "CALLED_NOT_LOGGED"
            except BrainProviderError as exc:
                brain_error = exc.code
                plan.brain_status = "BRAIN_ERROR"
        path = self._save_plan(plan)
        self._record(plan, "plan", "DENIED" if plan.deny_reason else "PASS", plan.deny_reason, int((time.monotonic() - started) * 1000), confirmed=False)
        result = plan.to_dict()
        result.update({"mode": "PLAN_ONLY", "workspace_write": "NO", "plan_path": str(path), "brain_invoked": "YES" if invoke_brain_now and not plan.deny_reason else "NO", "brain_output": _safe_text(brain_output, 12000) if brain_output else None, "brain_error_code": brain_error, "prompt_response_logged": "NO", "codex_agent_used": "NO", "codex_agentic_usage_required": "NO", "auto_file_modify": "NO", "auto_command_execute": "NO", "auto_commit": "NO", "auto_push": "NO", "auto_deploy": "NO", "secrets_logged": "NO"})
        return result

    @staticmethod
    def _run(args: tuple[str, ...], cwd: Path, timeout: int = 60) -> tuple[int, str]:
        if not args:
            raise LocalAgentError("COMMAND_REQUIRED")
        try:
            completed = subprocess.run(list(args), cwd=cwd, capture_output=True, text=True, timeout=timeout, shell=False, check=False)
        except subprocess.TimeoutExpired:
            return 124, "COMMAND_TIMEOUT"
        return completed.returncode, (completed.stdout + completed.stderr)[:65536]

    def _execute(self, args: tuple[str, ...], timeout: int = 60) -> tuple[int, str]:
        if self._runner is not None:
            return self._runner(args, self.root, timeout)
        return self._run(args, self.root, timeout)

    def readonly(self, task: str = "", plan_id: str | None = None) -> dict[str, Any]:
        plan = self._load_plan(plan_id) if plan_id else None
        started = time.monotonic(); results: list[dict[str, Any]] = []
        for name, command in self._SAFE_READ_COMMANDS.items():
            code, output = self._execute(command, 30)
            results.append({"name": name, "exit_code": code, "output": _safe_text(output, 4000)})
        status = "PASS" if all(item["exit_code"] == 0 for item in results) else "READONLY_CHECK_FAILED"
        self._record(plan, "readonly", status, None if status == "PASS" else status, int((time.monotonic() - started) * 1000), confirmed=False)
        return {"status": status, "mode": "READ_ONLY", "plan_id": plan.plan_id if plan else None, "workspace_write": "NO", "results": results, "files_touched": [], "prompt_response_logged": "NO", "secrets_logged": "NO"}

    def _require_confirm(self, plan: AgentPlan, action: str, confirm: bool) -> None:
        if plan.deny_reason:
            raise LocalAgentError(plan.deny_reason)
        if not confirm:
            self._record(plan, action, "CONFIRMATION_REQUIRED", "CONFIRMATION_REQUIRED", confirmed=False)
            raise LocalAgentError("CONFIRMATION_REQUIRED")

    def draft_patch(self, plan_id: str, confirm: bool = False, patch_text: str = "") -> dict[str, Any]:
        plan = self._load_plan(plan_id); self._require_confirm(plan, "draft-patch", confirm)
        if not plan.requires_write:
            raise LocalAgentError("PATCH_NOT_REQUIRED")
        if patch_text and any(re.search(pattern, patch_text, flags=re.I) for pattern in _HIGH_RISK_PATTERNS):
            raise LocalAgentError("LOCAL_AGENT_HIGH_RISK_STOP")
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        target = self.drafts_dir / (plan.plan_id + ".patch")
        content = patch_text or ("# Candidate patch only; review and replace before apply.\n" "# No files were modified by Xiaoyu Local Agent.\n")
        target.write_text(content, encoding="utf-8")
        plan.status = "PATCH_DRAFTED"; self._save_plan(plan)
        self._record(plan, "draft-patch", "PASS", files=[], confirmed=True)
        return {"status": "PATCH_DRAFTED", "mode": "PATCH_DRAFT", "plan_id": plan.plan_id, "patch_path": str(target), "applied": "NO", "files_touched": [], "prompt_response_logged": "NO", "secrets_logged": "NO"}

    def apply(self, plan_id: str, patch_file: str, confirm: bool = False) -> dict[str, Any]:
        plan = self._load_plan(plan_id); self._require_confirm(plan, "apply", confirm)
        patch = Path(patch_file).expanduser().resolve()
        try:
            patch.relative_to(self.root)
        except ValueError:
            try:
                patch.relative_to(self.drafts_dir.resolve())
            except ValueError as exc:
                raise LocalAgentError("PATCH_OUTSIDE_WORKSPACE") from exc
        if is_sensitive_path(patch) or patch.suffix.lower() != ".patch":
            raise LocalAgentError("PATCH_FILE_DENIED")
        try:
            patch_text = patch.read_text(encoding="utf-8")
        except OSError as exc:
            raise LocalAgentError("PATCH_FILE_UNREADABLE") from exc
        if any(re.search(pattern, patch_text, flags=re.I) for pattern in _HIGH_RISK_PATTERNS):
            raise LocalAgentError("LOCAL_AGENT_HIGH_RISK_STOP")
        check_code, check_output = self._execute(("git", "apply", "--check", str(patch)), 60)
        if check_code != 0:
            self._record(plan, "apply", "PATCH_CHECK_FAILED", "PATCH_CHECK_FAILED", confirmed=True)
            return {"status": "PATCH_CHECK_FAILED", "mode": "APPLY_WITH_CONFIRM", "plan_id": plan.plan_id, "applied": "NO", "detail": _safe_text(check_output), "files_touched": []}
        apply_code, apply_output = self._execute(("git", "apply", str(patch)), 60)
        status = "APPLIED" if apply_code == 0 else "PATCH_APPLY_FAILED"
        plan.status = status; self._save_plan(plan)
        self._record(plan, "apply", status, None if apply_code == 0 else status, confirmed=True)
        return {"status": status, "mode": "APPLY_WITH_CONFIRM", "plan_id": plan.plan_id, "applied": "YES" if apply_code == 0 else "NO", "detail": _safe_text(apply_output), "files_touched": []}

    def test(self, plan_id: str, test_name: str = "python-unittest", confirm: bool = False) -> dict[str, Any]:
        plan = self._load_plan(plan_id); self._require_confirm(plan, "test", confirm)
        if test_name not in self.SAFE_TESTS:
            raise LocalAgentError("TEST_NOT_ALLOWLISTED")
        started = time.monotonic(); code, output = self._execute(self.SAFE_TESTS[test_name], 300)
        status = "PASS" if code == 0 else "TEST_FAILED"
        self._record(plan, "test", status, None if code == 0 else status, int((time.monotonic() - started) * 1000), confirmed=True)
        return {"status": status, "mode": "SAFE_TEST", "plan_id": plan.plan_id, "test": test_name, "exit_code": code, "output": _safe_text(output, 8000), "prompt_response_logged": "NO", "secrets_logged": "NO"}

    def commit(self, plan_id: str, files: list[str], message: str, confirm: bool = False) -> dict[str, Any]:
        plan = self._load_plan(plan_id); self._require_confirm(plan, "commit", confirm)
        if not files:
            raise LocalAgentError("COMMIT_FILES_REQUIRED")
        if not message or _matches(message, _HIGH_RISK_PATTERNS):
            raise LocalAgentError("COMMIT_MESSAGE_DENIED")
        safe_files: list[str] = []
        for value in files:
            target = (self.root / value).resolve()
            try:
                target.relative_to(self.root)
            except ValueError as exc:
                raise LocalAgentError("COMMIT_FILE_OUTSIDE_WORKSPACE") from exc
            if ".git" in target.parts or is_sensitive_path(target):
                raise LocalAgentError("COMMIT_FILE_DENIED")
            safe_files.append(str(target.relative_to(self.root)))
        started = time.monotonic()
        add_code, add_output = self._execute(tuple(["git", "add", "--", *safe_files]), 60)
        if add_code != 0:
            return {"status": "COMMIT_STAGE_FAILED", "mode": "COMMIT_WITH_CONFIRM", "plan_id": plan.plan_id, "pushed": "NO", "detail": _safe_text(add_output)}
        commit_code, commit_output = self._execute(("git", "commit", "-m", _safe_text(message, 120)), 120)
        status = "COMMITTED" if commit_code == 0 else "COMMIT_FAILED"
        self._record(plan, "commit", status, None if commit_code == 0 else status, int((time.monotonic() - started) * 1000), safe_files, confirmed=True)
        return {"status": status, "mode": "COMMIT_WITH_CONFIRM", "plan_id": plan.plan_id, "exit_code": commit_code, "pushed": "NO", "files_touched": safe_files, "detail": _safe_text(commit_output), "prompt_response_logged": "NO", "secrets_logged": "NO"}

    def stop(self, plan_id: str | None = None) -> dict[str, Any]:
        plan = self._load_plan(plan_id) if plan_id else None
        if plan:
            plan.status = "STOPPED"; self._save_plan(plan)
        self._record(plan, "stop", "STOPPED", confirmed=False)
        return {"status": "STOPPED", "mode": "PLAN_ONLY", "plan_id": plan.plan_id if plan else None, "auto_file_modify": "NO", "auto_command_execute": "NO"}


__all__ = ["AgentPlan", "BRAIN_PROVIDERS", "LocalAgent", "LocalAgentError", "PLAN_MODES", "classify_risk", "make_plan"]
