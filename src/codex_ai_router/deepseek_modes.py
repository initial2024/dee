"""Read-only DeepSeek Web mode probing and deterministic mode selection.

This module never submits a chat, clicks Send, uploads an attachment, or reads
browser credentials.  It only consumes the loopback Browser Bridge's visible
control metadata and returns a requested profile for a later, user-approved
request.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEEPSEEK_HEALTH = "http://127.0.0.1:8791/health"
DEEPSEEK_MODE_PROBE = "http://127.0.0.1:8791/mode-probe"

MODE_ALIASES = {
    "quick_plain": "deepseek-web", "quick_search": "deepseek-web-search",
    "quick_thinking": "deepseek-web-quick-thinking",
    "expert_plain": "deepseek-web-expert", "expert_thinking": "deepseek-web-expert-thinking",
    "expert_thinking_search": "deepseek-web-expert-thinking-search",
    "expert_max_review": "deepseek-web-expert-max-review",
    "vision_quick": "deepseek-web-vision-quick", "vision_expert": "deepseek-web-vision-expert",
    "vision_expert_thinking": "deepseek-web-vision-expert-thinking", "vision_search": "deepseek-web-vision-search",
    "file_extract": "deepseek-web-file-extract",
    "best_available_reasoning": "deepseek-web-quick-thinking",
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

UiGeneration = Literal["legacy", "three_in_one", "unknown"]
ReasoningStrength = Literal["off", "low", "medium", "high", "max", "unsupported", "unknown"]
SearchMode = Literal["off", "on", "tool_required", "unavailable", "unknown"]
AttachmentMode = Literal["off", "available", "requires_attachment", "unavailable", "unknown"]
ResponseProtocol = Literal["browser_dom", "responses_api_compatible", "chat_completions_compatible", "unknown"]
ModeSwitchStrategy = Literal["legacy_buttons", "unified_menu", "profile_menu", "unsupported"]
ReasoningAxisType = Literal["none", "binary_toggle", "strength_levels", "unknown"]
ReasoningOptionsStatus = Literal["full", "partial", "binary", "unavailable", "unknown"]


@dataclass(frozen=True)
class DeepSeekCapabilityProfile:
    """A capability description, never a credential or request description.

    The values deliberately describe what a visible Browser Bridge reports.  A
    profile can therefore be used by selectors and tests without implying that
    a DeepSeek request was sent.
    """

    profile_id: str
    ui_generation: UiGeneration = "unknown"
    base_model_family: Literal["flash", "pro", "web_default", "unknown"] = "unknown"
    reasoning_strength: ReasoningStrength = "unknown"
    search_mode: SearchMode = "unknown"
    vision_mode: AttachmentMode = "unknown"
    file_mode: AttachmentMode = "unknown"
    response_protocol: ResponseProtocol = "browser_dom"
    supports_codex: bool | Literal["unknown"] = "unknown"
    supports_tools: bool | Literal["unknown"] = "unknown"
    supports_reasoning_items: bool | Literal["unknown"] = "unknown"
    mode_switch_strategy: ModeSwitchStrategy = "unsupported"
    reasoning_axis_type: ReasoningAxisType = "unknown"
    available_reasoning_strengths: tuple[ReasoningStrength, ...] = ()
    max_available_reasoning_strength: ReasoningStrength = "unknown"
    high_reasoning_supported: bool = False
    max_reasoning_supported: bool = False
    reasoning_strength_options_status: ReasoningOptionsStatus = "unknown"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


LEGACY_PROFILE_MAP: dict[str, DeepSeekCapabilityProfile] = {
    "quick_plain": DeepSeekCapabilityProfile("quick_plain", base_model_family="flash", reasoning_strength="off", search_mode="off", vision_mode="off", file_mode="off", mode_switch_strategy="legacy_buttons"),
    "quick_thinking": DeepSeekCapabilityProfile("quick_thinking", base_model_family="flash", reasoning_strength="medium", search_mode="off", vision_mode="off", file_mode="off", mode_switch_strategy="legacy_buttons"),
    "quick_search": DeepSeekCapabilityProfile("quick_search", base_model_family="flash", reasoning_strength="low", search_mode="on", vision_mode="off", file_mode="off", mode_switch_strategy="legacy_buttons"),
    "expert_plain": DeepSeekCapabilityProfile("expert_plain", base_model_family="pro", reasoning_strength="off", search_mode="off", vision_mode="off", file_mode="off", mode_switch_strategy="legacy_buttons"),
    "expert_thinking": DeepSeekCapabilityProfile("expert_thinking", base_model_family="pro", reasoning_strength="high", search_mode="off", vision_mode="off", file_mode="off", mode_switch_strategy="legacy_buttons"),
    "expert_max_review": DeepSeekCapabilityProfile("expert_max_review", base_model_family="pro", reasoning_strength="max", search_mode="off", vision_mode="off", file_mode="off", mode_switch_strategy="legacy_buttons"),
    "expert_thinking_search": DeepSeekCapabilityProfile("expert_thinking_search", base_model_family="pro", reasoning_strength="high", search_mode="on", vision_mode="off", file_mode="off", mode_switch_strategy="legacy_buttons"),
    "vision_expert_thinking": DeepSeekCapabilityProfile("vision_expert_thinking", base_model_family="pro", reasoning_strength="high", search_mode="off", vision_mode="requires_attachment", file_mode="off", mode_switch_strategy="legacy_buttons"),
    "file_extract": DeepSeekCapabilityProfile("file_extract", base_model_family="pro", reasoning_strength="high", search_mode="off", vision_mode="off", file_mode="requires_attachment", mode_switch_strategy="legacy_buttons"),
}

# The new UI has no quick/expert base-mode axis.  Keep the old public aliases,
# but normalize them to capability-first profiles whenever the probe reports
# the three-in-one generation.
THREE_IN_ONE_PROFILE_MAP: dict[str, tuple[str, ReasoningStrength, SearchMode]] = {
    "quick_plain": ("low_reasoning", "low", "off"),
    "quick_thinking": ("medium_reasoning", "medium", "off"),
    "expert_thinking": ("high_reasoning", "high", "off"),
    "expert_max_review": ("max_reasoning_review", "max", "off"),
    "quick_search": ("medium_search", "medium", "on"),
    "expert_thinking_search": ("high_search", "high", "on"),
}


def best_available_reasoning_profile(probe: dict[str, Any] | None = None) -> DeepSeekCapabilityProfile:
    """Return the strongest verified no-search profile without overstating it."""
    data = probe if isinstance(probe, dict) else {}
    axis = str(data.get("reasoning_axis_type", "unknown"))
    maximum = str(data.get("max_available_reasoning_strength", "unknown"))
    if maximum not in {"off", "low", "medium", "high", "max"}:
        strengths = data.get("available_reasoning_strengths")
        maximum = "max" if isinstance(strengths, (list, tuple)) and "max" in strengths else "high" if isinstance(strengths, (list, tuple)) and "high" in strengths else "medium" if isinstance(strengths, (list, tuple)) and "medium" in strengths else "low" if isinstance(strengths, (list, tuple)) and "low" in strengths else "off"
    actual: ReasoningStrength = maximum if maximum in {"low", "medium", "high", "max"} else "off"
    default_strengths = ("off", "medium") if axis == "binary_toggle" else ("off", actual)
    raw_strengths = data.get("available_reasoning_strengths")
    strengths: tuple[ReasoningStrength, ...] = tuple(raw_strengths) if isinstance(raw_strengths, (list, tuple)) and raw_strengths else default_strengths
    return DeepSeekCapabilityProfile(
        "best_available_reasoning", ui_generation="three_in_one", base_model_family="web_default",
        reasoning_strength=actual, search_mode="off", vision_mode="off", file_mode="off",
        mode_switch_strategy="unified_menu", reasoning_axis_type=axis if axis in {"none", "binary_toggle", "strength_levels", "unknown"} else "unknown",
        available_reasoning_strengths=strengths, max_available_reasoning_strength=actual,
        high_reasoning_supported=actual in {"high", "max"}, max_reasoning_supported=actual == "max",
        reasoning_strength_options_status="binary" if axis == "binary_toggle" else "full" if actual != "off" else "unavailable",
    )


def reasoning_capability_text(probe: dict[str, Any] | None = None) -> str:
    """Return UI-safe wording that reflects verified visible reasoning controls."""
    data = probe if isinstance(probe, dict) else {}
    axis = str(data.get("reasoning_axis_type", "unknown"))
    maximum = str(data.get("max_available_reasoning_strength", "unknown"))
    if axis == "binary_toggle":
        return "当前 DeepSeek 网页仅检测到深度思考开关；本地项目可使用已开启思考并关闭搜索，但不等同于 high/max（实际强度=medium）。"
    if maximum == "max":
        return "当前网页已检测到 max 思考；高风险审查可请求 max，并在验证后关闭联网搜索。"
    if maximum == "high":
        return "本地项目默认高思考并关闭搜索；当前网页最高可用思考强度为 high。"
    if maximum in {"low", "medium"}:
        return f"当前网页最高可用思考强度为 {maximum}；仅使用已验证强度并在验证后关闭联网搜索。"
    return "高风险审查所需的 max 思考未在当前网页 UI 中检测到；请使用 Codex official 或其他支持 max 的后端。"


def capability_profile(profile_id: str, *, ui_generation: UiGeneration = "unknown", probe: dict[str, Any] | None = None) -> DeepSeekCapabilityProfile:
    """Return a stable capability profile for a legacy alias or new profile id."""
    if profile_id == "best_available_reasoning":
        base = best_available_reasoning_profile(probe)
    else:
        base = LEGACY_PROFILE_MAP.get(profile_id, DeepSeekCapabilityProfile(profile_id))
    if ui_generation == "three_in_one" and profile_id in THREE_IN_ONE_PROFILE_MAP:
        canonical, strength, search = THREE_IN_ONE_PROFILE_MAP[profile_id]
        base = DeepSeekCapabilityProfile(
            canonical,
            ui_generation="three_in_one",
            base_model_family="web_default",
            reasoning_strength=strength,
            search_mode=search,
            vision_mode="off",
            file_mode="off",
            mode_switch_strategy="unified_menu",
        )
    if not isinstance(probe, dict):
        return DeepSeekCapabilityProfile(**{**base.as_dict(), "ui_generation": ui_generation if ui_generation != "unknown" else base.ui_generation})
    values = base.as_dict()
    values["ui_generation"] = probe.get("ui_generation", ui_generation) or ui_generation
    for field in ("base_model_family", "reasoning_strength", "search_mode", "vision_mode", "file_mode", "response_protocol", "mode_switch_strategy", "reasoning_axis_type", "available_reasoning_strengths", "max_available_reasoning_strength", "reasoning_strength_options_status"):
        if probe.get(field) is not None:
            values[field] = probe[field]
    for field in ("supports_codex", "supports_tools", "supports_reasoning_items", "high_reasoning_supported", "max_reasoning_supported"):
        if field in probe:
            values[field] = probe[field]
    return DeepSeekCapabilityProfile(**values)


def legacy_profile_for_mode(mode: str) -> DeepSeekCapabilityProfile:
    return capability_profile(mode if mode in LEGACY_PROFILE_MAP else "quick_plain")
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
    # New three-in-one metadata is additive.  Old bridges remain readable; when
    # thinking and search are both visible without quick/expert controls, the
    # shape itself is sufficient evidence for the new generation.
    for name, default in (
        ("ui_generation", "unknown"), ("three_in_one_available", False),
        ("legacy_buttons_available", bool(result.get("quick_available") or result.get("expert_available"))),
        ("profile_menu_available", False), ("reasoning_strength_available", bool(result.get("thinking_available"))),
        ("reasoning_strength_options", []), ("current_reasoning_strength", "unknown"),
        ("reasoning_strength_options_status", "unknown"), ("reasoning_strength_warning", None),
        ("search_combo_supported", "unknown"), ("vision_available", False),
        ("file_upload_available", False), ("current_profile_label", "unknown"),
        ("base_mode_available", bool(result.get("quick_available") or result.get("expert_available"))),
        ("base_mode_required", True), ("base_mode_axis", "available"),
        ("base_mode_status", "available"),
        ("reasoning_axis_type", "unknown"), ("available_reasoning_strengths", []),
        ("max_available_reasoning_strength", "unknown"), ("high_reasoning_supported", False),
        ("max_reasoning_supported", False),
    ):
        result[name] = payload.get(name, default)
    inferred_three_in_one = bool(
        (result.get("thinking_available") and result.get("search_available") and not result.get("legacy_buttons_available"))
        or result.get("three_in_one_available")
        or result.get("profile_menu_available")
    )
    if inferred_three_in_one:
        result["ui_generation"] = "three_in_one"
        result["three_in_one_available"] = True
        result["legacy_buttons_available"] = False
        result["base_mode_available"] = False
        result["base_mode_required"] = False
        result["base_mode_axis"] = "absent"
        result["base_mode_status"] = "not_applicable_for_three_in_one"
    if result.get("ui_generation") not in {"legacy", "three_in_one", "unknown"}:
        result["ui_generation"] = "unknown"
    current_strength = str(result.get("current_reasoning_strength") or "unknown")
    if current_strength == "unknown" and result.get("current_thinking") is True:
        current_strength = "medium"
        result["current_reasoning_strength"] = current_strength
    options = result.get("reasoning_strength_options")
    if not isinstance(options, list):
        options = []
        result["reasoning_strength_options"] = options
    if options:
        result["reasoning_strength_options_status"] = "full"
        result["reasoning_strength_warning"] = None
    elif current_strength != "unknown" and result.get("reasoning_strength_available"):
        result["reasoning_strength_options_status"] = "partial"
        result["reasoning_strength_warning"] = "REASONING_OPTIONS_PARTIAL"
    if result.get("ui_generation") == "three_in_one":
        result["reasoning_strength_available"] = bool(result.get("thinking_available") or result.get("reasoning_strength_available"))
        if result.get("reasoning_axis_type") == "unknown" and result.get("thinking_available"):
            result["reasoning_axis_type"] = "binary_toggle" if not options else "strength_levels"
        if result.get("reasoning_axis_type") == "binary_toggle":
            result["available_reasoning_strengths"] = ["off", "medium"]
            result["max_available_reasoning_strength"] = "medium"
            result["high_reasoning_supported"] = False
            result["max_reasoning_supported"] = False
            result["reasoning_strength_options_status"] = "binary"
        if result.get("current_profile_label") in {None, "", "unknown"}:
            result["current_profile_label"] = f"{current_strength}/{('search' if result.get('current_search') else 'no-search')}"
    if result.get("ui_generation") == "legacy":
        result["base_mode_axis"] = "available"
        result["base_mode_required"] = True
    result["capability_profile"] = capability_profile(
        str(result.get("current_profile_label") or "web_default"),
        ui_generation=result["ui_generation"],
        probe={
            "reasoning_strength": result.get("current_reasoning_strength"), "search_mode": "on" if result.get("current_search") else "off",
            "reasoning_axis_type": result.get("reasoning_axis_type"), "available_reasoning_strengths": result.get("available_reasoning_strengths"),
            "max_available_reasoning_strength": result.get("max_available_reasoning_strength"), "high_reasoning_supported": result.get("high_reasoning_supported"),
            "max_reasoning_supported": result.get("max_reasoning_supported"), "reasoning_strength_options_status": result.get("reasoning_strength_options_status"),
        },
    ).as_dict()
    result["best_available_reasoning_profile"] = best_available_reasoning_profile(result).as_dict()
    _write_state(result, state_path); return result


def _available(availability: dict[str, Any] | None, name: str) -> bool:
    if not isinstance(availability, dict): return False
    item = availability.get(name)
    if item is None and name == "quick": item = availability.get("normal")
    if isinstance(item, bool): return item
    if isinstance(item, str): return item == "AVAILABLE"
    return isinstance(item, dict) and item.get("status") == "AVAILABLE" and item.get("controllable", True) is not False


def _combo_supported(availability: dict[str, Any] | None) -> bool | None:
    if not isinstance(availability, dict) or "search_combo_supported" not in availability:
        return None
    value = availability.get("search_combo_supported")
    return value if isinstance(value, bool) else None


def _profile_available(profile: str, availability: dict[str, Any] | None, has_image: bool, has_file: bool) -> tuple[bool, str | None]:
    three_in_one = isinstance(availability, dict) and availability.get("ui_generation") == "three_in_one"
    if profile == "best_available_reasoning" and not _available(availability, "thinking"):
        return False, "DEEPSEEK_THINKING_UNAVAILABLE"
    if three_in_one and profile in {"expert_thinking", "expert_thinking_search", "expert_max_review"}:
        requested = "max" if profile == "expert_max_review" else "high"
        supported_key = "max_reasoning_supported" if requested == "max" else "high_reasoning_supported"
        if availability.get(supported_key) is False or availability.get("reasoning_axis_type") == "binary_toggle":
            return False, "PATCH_REVIEW_MAX_REASONING_UNAVAILABLE" if requested == "max" else "DEEPSEEK_REASONING_STRENGTH_UNAVAILABLE"
    checks: list[tuple[str, str]] = [] if three_in_one else [("quick", "UI_CHANGED")]
    if not three_in_one and (profile.startswith("expert") or profile in {"vision_expert", "vision_expert_thinking", "file_extract"}): checks = [("expert", "DEEPSEEK_EXPERT_MODE_UNAVAILABLE")]
    if "thinking" in profile or profile == "expert_max_review": checks.append(("thinking", "DEEPSEEK_THINKING_UNAVAILABLE"))
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
    elif difficulty == "high_risk": desired, why = "expert_max_review", "高风险任务只能生成最高强度审查建议"
    else:
        table = {
            "economy": {"simple": "quick_plain", "medium": "quick_thinking", "complex": "expert_plain"},
            "balanced": {"simple": "quick_plain", "medium": "quick_thinking", "complex": "expert_thinking"},
            "accuracy": {"simple": "quick_thinking", "medium": "expert_plain", "complex": "expert_thinking"},
        }
        desired, why = table[performance][difficulty], f"{performance} 性能策略的 {difficulty} 任务"
    if isinstance(availability, dict) and availability.get("ui_generation") == "three_in_one" and availability.get("reasoning_axis_type") == "binary_toggle":
        if not fixed and desired in {"expert_thinking", "expert_thinking_search"} and difficulty != "high_risk":
            desired, why = "best_available_reasoning", "当前网页仅提供二值深度思考；使用已验证的 medium/no-search 能力"
    split_strategy: dict[str, str] | None = None
    combo = _combo_supported(availability)
    if desired == "expert_thinking_search" and combo is False:
        # A split is an explicit policy decision, not a successful expert+
        # search combination.  Callers can reject it by checking the code.
        split_strategy = {"first": "quick_search", "then": "expert_thinking"}
        fallback_reason = "DEEPSEEK_THREE_IN_ONE_COMBO_UNAVAILABLE"
        ok = _available(availability, "search") and _available(availability, "expert") and _available(availability, "thinking")
        reason = fallback_reason
    else:
        ok, reason = _profile_available(desired, availability, has_image, has_file)
    generation = str((availability or {}).get("ui_generation", "unknown"))
    selected_profile = capability_profile(desired, ui_generation=generation).as_dict()
    if desired == "best_available_reasoning":
        selected_profile = best_available_reasoning_profile(availability).as_dict()
    return {"selected_mode": desired, "selected_model_alias": mode_alias(desired), "selected_profile": selected_profile, "reasoning_strength": selected_profile["reasoning_strength"], "search_required": "search" in desired, "vision_required": desired.startswith("vision"), "file_required": desired == "file_extract", "combo_policy": "allow_split_search_then_reason" if split_strategy else "require_exact", "split_strategy": split_strategy, "fallback_reason": reason, "error_code": "DEEPSEEK_SEARCH_COMBO_UNAVAILABLE" if split_strategy else reason, "mode_available": ok, "manual_override": bool(fixed), "codex_mode": codex_mode, "tools_policy": tools_policy, "performance_mode": performance, "task_difficulty": difficulty, "smart_search": "ON" if "search" in desired else "OFF", "thinking": "ON" if selected_profile["reasoning_strength"] != "off" else "OFF", "input_modality": "image" if desired.startswith("vision") else "file" if desired == "file_extract" else "text", "max_auto_escalation": 1}


__all__ = [
    "DeepSeekCapabilityProfile", "LEGACY_PROFILE_MAP", "THREE_IN_ONE_PROFILE_MAP", "best_available_reasoning_profile", "reasoning_capability_text", "capability_profile",
    "legacy_profile_for_mode", "MODE_ALIASES", "mode_alias", "probe_deepseek_modes",
    "select_deepseek_mode", "classify_task_difficulty", "escalation_target",
    "preflight_attachment", "load_probe_state",
]
