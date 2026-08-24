"""Read-only DeepSeek Web mode probing and deterministic mode selection.

This module never submits a chat, clicks Send, uploads an attachment, or reads
browser credentials.  It only consumes the loopback Browser Bridge's visible
control metadata and returns a requested profile for a later, user-approved
request.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEEPSEEK_HEALTH = "http://127.0.0.1:8791/health"
DEEPSEEK_MODE_PROBE = "http://127.0.0.1:8791/mode-probe"

MODE_ALIASES = {
    "quick_plain": "deepseek-web", "quick_search": "deepseek-web-search",
    "quick_thinking": "deepseek-web-quick-thinking",
    "expert_plain": "deepseek-web-expert", "expert_thinking": "deepseek-web-expert-thinking",
    "expert_thinking_search": "deepseek-web-expert-thinking-search",
    "vision_quick": "deepseek-web-vision-quick", "vision_expert": "deepseek-web-vision-expert",
    "vision_expert_thinking": "deepseek-web-vision-expert-thinking", "vision_search": "deepseek-web-vision-search",
    "file_extract": "deepseek-web-file-extract",
}
MODE_ALIASES["auto"] = MODE_ALIASES["quick_plain"]
MODE_ORDER = ("quick", "expert", "thinking", "search", "vision", "file")
PREFERENCE_ALIASES = {
    "fast": "quick_plain", "quick": "quick_plain", "instant": "quick_plain", "normal": "quick_plain",
    "search": "quick_search", "smart_search": "quick_search", "web_search": "quick_search", "internet_search": "quick_search",
    "thinking": "quick_thinking", "deep_thinking": "quick_thinking", "reasoner": "quick_thinking",
    "expert": "expert_plain", "pro": "expert_plain", "complex": "expert_plain",
    "vision": "vision_expert_thinking", "image": "vision_expert_thinking", "image_recognition": "vision_expert_thinking", "visual": "vision_expert_thinking",
    "file": "file_extract", "file_upload": "file_extract", "text_extraction": "file_extract",
    **{name: name for name in MODE_ALIASES},
}
ENGINEERING_TERMS = ("代码", "项目", "文件", "diff", "patch", "测试", "报错", "traceback", "powershell", "worker", "router", "agent", "api", "cli", "ui 布局", "本地", "commit", "git")
COMPLEX_TERMS = ("严谨分析", "架构", "多文件", "兼容", "安全", "协调器", "执行循环", "上下文采集", "patch draft")
SEARCH_TERMS = ("搜索", "最新", "当前", "官网", "文档最新版", "价格", "新闻", "发布", "今天")
IMAGE_TERMS = ("截图", "图片", "识图", "视觉", "看图", "按钮被截断", "界面", "ui截图", "图表", "表格截图")
FILE_TERMS = ("pdf", "docx", "文件上传", "提取文本", "附件")
CONTROL_NAMES = ("quick", "expert", "thinking", "search", "vision", "file")
_BLOCKED_ATTACHMENT_NAMES = {".env", "storage_state.json", "cookies.json", "token.json", "id_rsa"}
_BLOCKED_ATTACHMENT_SUFFIXES = {".key", ".pem", ".pfx", ".p12", ".env", ".sqlite", ".har"}
_ALLOWED_ATTACHMENT_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".pdf", ".txt", ".md", ".docx"}
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024


def mode_alias(mode: str) -> str:
    return MODE_ALIASES.get(mode, MODE_ALIASES["quick_plain"])


def _state_path() -> Path:
    return Path(os.getenv("XIAOYU_ROUTER_DEEPSEEK_MODE_STATE", str(Path.home() / ".codex-ai-router" / "deepseek-mode-probe.json")))


def _write_state(payload: dict[str, Any], path: Path | None = None) -> None:
    try:
        target = path or _state_path(); target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass


def load_probe_state(path: Path | None = None) -> dict[str, Any]:
    try:
        value = json.loads((path or _state_path()).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _probe_request(url: str, timeout: float = 3.0) -> tuple[int, dict[str, Any]]:
    if not url.startswith("http://127.0.0.1:"):
        raise ValueError("NON_LOOPBACK_PROBE_URL")
    with urlopen(Request(url, method="GET"), timeout=timeout) as response:
        value = json.loads(response.read().decode("utf-8"))
        return int(getattr(response, "status", 200)), value if isinstance(value, dict) else {}


def _unknown_controls(status: str = "UNKNOWN") -> dict[str, dict[str, Any]]:
    return {name: {"status": status, "controlDetected": False, "controllable": False} for name in CONTROL_NAMES}


def _classify_error(status: int, body: dict[str, Any]) -> str:
    code = str((body.get("error") or {}).get("code", "")) if isinstance(body.get("error"), dict) else ""
    if code == "LOGIN_REQUIRED": return "LOGIN_REQUIRED"
    if code == "CAPTCHA_PRESENT": return "CAPTCHA_REQUIRED"
    if code == "RISK_CONTROL_PRESENT": return "ACCOUNT_RISK"
    return "RATE_LIMITED" if status == 429 else "UNKNOWN"


def probe_deepseek_modes(*, health_url: str = DEEPSEEK_HEALTH, probe_url: str = DEEPSEEK_MODE_PROBE, timeout: float = 3.0, state_path: Path | None = None) -> dict[str, Any]:
    """Only GET loopback health/probe metadata; no page mutation or upload."""
    result: dict[str, Any] = {"probe": "READ_ONLY", "promptSent": False, "clickSend": False, "uploadAttempted": False, "modes": _unknown_controls()}
    try:
        status, health = _probe_request(health_url, timeout)
        if status != 200:
            code = _classify_error(status, health); result.update(status=code, error_code=code); _write_state(result, state_path); return result
        status, payload = _probe_request(probe_url, timeout)
    except HTTPError as exc:
        body: dict[str, Any] = {}
        try: body = json.loads(exc.read().decode("utf-8"))
        except (OSError, json.JSONDecodeError): pass
        code = _classify_error(exc.code, body); result.update(status=code, error_code=code); _write_state(result, state_path); return result
    except (URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        result.update(status="UNAVAILABLE", error_code="BRIDGE_OFFLINE"); _write_state(result, state_path); return result
    raw = payload.get("modes") if isinstance(payload.get("modes"), dict) else {}
    controls = _unknown_controls("UI_PROBE_FAILED")
    for name in CONTROL_NAMES:
        item = raw.get(name)
        if isinstance(item, dict): controls[name] = {"status": str(item.get("status", "UNKNOWN")), "controlDetected": bool(item.get("controlDetected")), "controllable": bool(item.get("controllable")), "selected": bool(item.get("selected"))}
    result.update({"status": "PASS" if status == 200 else "UI_PROBE_FAILED", "error_code": None if status == 200 else "UI_PROBE_FAILED", "modes": controls})
    for name, field in (("quick", "quick_available"), ("expert", "expert_available"), ("thinking", "thinking_available"), ("search", "search_available"), ("vision", "vision_available"), ("file", "file_upload_available")):
        result[field] = bool(payload.get(field, controls[name]["status"] == "AVAILABLE" and controls[name]["controllable"]))
    for name in ("current_base_mode", "current_thinking", "current_search", "current_modality", "ui_changed", "login_required", "captcha_required"):
        result[name] = payload.get(name)
    _write_state(result, state_path); return result


def _available(availability: dict[str, Any] | None, name: str) -> bool:
    if not isinstance(availability, dict): return False
    item = availability.get(name)
    if item is None and name == "quick": item = availability.get("normal")
    if isinstance(item, bool): return item
    if isinstance(item, str): return item == "AVAILABLE"
    return isinstance(item, dict) and item.get("status") == "AVAILABLE" and item.get("controllable", True) is not False


def _profile_available(profile: str, availability: dict[str, Any] | None, has_image: bool, has_file: bool) -> tuple[bool, str | None]:
    checks: list[tuple[str, str]] = [("quick", "UI_CHANGED")]
    if profile.startswith("expert") or profile in {"vision_expert", "vision_expert_thinking", "file_extract"}: checks = [("expert", "DEEPSEEK_EXPERT_MODE_UNAVAILABLE")]
    if "thinking" in profile: checks.append(("thinking", "DEEPSEEK_THINKING_UNAVAILABLE"))
    if "search" in profile: checks.append(("search", "DEEPSEEK_SEARCH_UNAVAILABLE"))
    if profile.startswith("vision"):
        checks.append(("vision", "DEEPSEEK_VISION_UNAVAILABLE"))
        if not has_image: return False, "DEEPSEEK_VISION_UNAVAILABLE"
    if profile == "file_extract":
        checks.append(("file", "DEEPSEEK_FILE_UPLOAD_UNAVAILABLE"))
        if not has_file: return False, "DEEPSEEK_FILE_UPLOAD_UNAVAILABLE"
    for control, code in checks:
        if not _available(availability, control): return False, code
    return True, None


def classify_task_difficulty(task_text: str, *, failure_reason: str | None = None) -> str:
    text = (task_text or "").lower()
    high_risk = ("删除文件", "密钥", "cookie", "token", "防火墙", "lan", "公网", "deploy", "git push", "私有 api replay", "绕过验证码", "绕过风控")
    complex_terms = ("多文件", "架构", "测试失败", "兼容", "agent", "router", "provider", "worker", "patch synthesizer", "context collector", "执行循环", "长日志", "test_v11", "流式 http 超时")
    medium_terms = ("单文件", "ui 布局", "布局", "小修", "简单 bug", "只读检查", "小 patch", "短日志", "报错")
    if any(term in text for term in high_risk): return "high_risk"
    if failure_reason or any(term in text for term in complex_terms) or ("测试" in text and ("超时" in text or "失败" in text)):
        return "complex"
    if any(term in text for term in medium_terms) or any(term in text for term in ENGINEERING_TERMS): return "medium"
    return "simple"


def escalation_target(selected_mode: str, *, reason: str | None = None, difficulty: str = "simple") -> dict[str, Any]:
    trigger_codes = {"PATCH_DRAFT_FORMAT_INVALID", "AGENT_PLAN_SCHEMA_INVALID", "DEEPSEEK_EMPTY_RESPONSE", "PATCH_DRAFT_UNAVAILABLE", "STRUCTURED_PATCH_NOT_GENERATED", "LOW_CONFIDENCE"}
    should_escalate = difficulty == "complex" or (reason or "") in trigger_codes
    upgrades = {"quick_plain": "quick_thinking", "quick_thinking": "expert_thinking", "expert_plain": "expert_thinking", "quick_search": "expert_thinking_search"}
    target = upgrades.get(selected_mode) if should_escalate else None
    return {"escalated": target is not None, "selected_mode": target or selected_mode, "escalation_reason": reason if target else None, "max_auto_escalation": 1}


def preflight_attachment(path_value: str | Path) -> dict[str, Any]:
    """Inspect only an explicitly supplied local path; never upload it.

    Metadata is deliberately minimal.  JPEG/WebP files require a later user
    confirmation after EXIF stripping; this preflight does not read image data.
    """
    target = Path(path_value)
    suffix = target.suffix.lower(); name = target.name.lower()
    result: dict[str, Any] = {"upload_allowed": False, "file_type": suffix.lstrip(".") or "unknown", "file_size": 0, "redaction_applied": suffix == ".png", "requires_user_confirm": True}
    if name in _BLOCKED_ATTACHMENT_NAMES or suffix in _BLOCKED_ATTACHMENT_SUFFIXES or suffix not in _ALLOWED_ATTACHMENT_SUFFIXES:
        result["reason"] = "SENSITIVE_FILE_UPLOAD_BLOCKED"; return result
    try:
        size = target.stat().st_size
    except OSError:
        result["reason"] = "ATTACHMENT_NOT_FOUND"; return result
    result["file_size"] = size
    if size > _MAX_ATTACHMENT_BYTES:
        result["reason"] = "ATTACHMENT_SIZE_LIMIT"; return result
    result["upload_allowed"] = True
    return result


def select_deepseek_mode(task_text: str, *, codex_mode: str = "CUSTOM_DEEPSEEK_TEXT_ONLY", tools_policy: str = "strict_reject", user_preference: str = "auto", explicit_model_alias: str | None = None, availability: dict[str, Any] | None = None, image_path: str | None = None, attachment_id: str | None = None, file_path: str | None = None, performance_mode: str = "balanced", failure_reason: str | None = None, search_allowed: bool = True, thinking_allowed: bool = True, expert_allowed: bool = True) -> dict[str, Any]:
    """Choose a profile without hiding unavailable controls or attachments."""
    text = (task_text or "").lower(); preference = (user_preference or "auto").lower().replace("固定", "").replace("模式", "").strip()
    # "auto" intentionally reuses the quick alias.  Do not let that
    # convenience entry replace the concrete quick_plain profile when a
    # caller explicitly supplies deepseek-web.
    aliases_by_model = {value: key for key, value in MODE_ALIASES.items() if key != "auto"}
    fixed = None if preference == "auto" else PREFERENCE_ALIASES.get(preference)
    explicit = aliases_by_model.get(explicit_model_alias or "") or {"deepseek-web-fast": "quick_plain", "deepseek-head": "expert_plain"}.get(explicit_model_alias or "")
    has_image, has_file = bool(image_path or attachment_id), bool(file_path or attachment_id)
    difficulty = classify_task_difficulty(task_text, failure_reason=failure_reason)
    performance = performance_mode if performance_mode in {"economy", "balanced", "accuracy"} else "balanced"
    if fixed: desired, why = fixed, "用户手动固定模式"
    elif explicit: desired, why = explicit, "显式模型别名"
    elif any(term in text for term in FILE_TERMS): desired, why = "file_extract", "包含文件或文档提取意图"
    elif has_image or any(term in text for term in IMAGE_TERMS): desired, why = "vision_expert_thinking", "包含截图/图片意图或显式图片附件"
    elif search_allowed and any(term in text for term in SEARCH_TERMS):
        desired, why = ("expert_thinking_search" if difficulty == "complex" else "quick_search"), "包含最新外部信息意图"
    elif difficulty == "high_risk": desired, why = "expert_thinking", "高风险任务只能生成审查建议"
    else:
        table = {
            "economy": {"simple": "quick_plain", "medium": "quick_thinking", "complex": "expert_plain"},
            "balanced": {"simple": "quick_plain", "medium": "quick_thinking", "complex": "expert_thinking"},
            "accuracy": {"simple": "quick_thinking", "medium": "expert_plain", "complex": "expert_thinking"},
        }
        desired, why = table[performance][difficulty], f"{performance} 性能策略的 {difficulty} 任务"
    ok, reason = _profile_available(desired, availability, has_image, has_file)
    return {"selected_mode": desired, "selected_model_alias": mode_alias(desired), "why_selected": why, "fallback_reason": reason, "mode_available": ok, "manual_override": bool(fixed), "codex_mode": codex_mode, "tools_policy": tools_policy, "performance_mode": performance, "task_difficulty": difficulty, "smart_search": "ON" if "search" in desired else "OFF", "thinking": "ON" if "thinking" in desired else "OFF", "input_modality": "image" if desired.startswith("vision") else "file" if desired == "file_extract" else "text", "max_auto_escalation": 1}
