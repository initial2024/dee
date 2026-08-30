"""Local-only Work Profile bindings for Codex and Xiaoyu providers.

The profile is a policy description, not a runner.  It never contacts a
provider, reads Codex UI state, or stores prompts, responses, or credentials.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any


REASONING_STRENGTHS = ("very_low", "light", "medium", "high", "very_high", "max")
REASONING_STRENGTH_ZH = {
    "very_low": "极低",
    "light": "轻度",
    "medium": "中",
    "high": "高",
    "very_high": "极高",
    "max": "最高",
}
REASONING_STRENGTH_ALIASES = {
    **{key: key for key in REASONING_STRENGTHS},
    **{value: key for key, value in REASONING_STRENGTH_ZH.items()},
}


@dataclass(frozen=True)
class WorkProfile:
    profile_id: str
    task_difficulty: str
    desired_reasoning: str
    codex_custom_mode: str
    codex_reasoning_strength: str
    deepseek_mode: str
    local_model_policy: str
    external_api_policy: str
    local_agent_policy: str
    search_policy: str
    vision_policy: str
    file_policy: str
    apply_policy: str
    test_policy: str
    commit_policy: str
    risk_level: str
    target_executor: str = "xiaoyu_local"

    def __post_init__(self) -> None:
        if self.codex_reasoning_strength not in REASONING_STRENGTHS:
            raise ValueError("INVALID_REASONING_STRENGTH")
        if self.desired_reasoning not in REASONING_STRENGTHS:
            raise ValueError("INVALID_DESIRED_REASONING")

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    to_dict = as_dict


def _profile(**values: Any) -> WorkProfile:
    return WorkProfile(**values)


DEFAULT_WORK_PROFILES: dict[str, WorkProfile] = {
    "simple_readonly": _profile(
        profile_id="simple_readonly", task_difficulty="simple", desired_reasoning="light",
        codex_custom_mode="CUSTOM", codex_reasoning_strength="light", deepseek_mode="quick_plain",
        local_model_policy="prefer_local_light", external_api_policy="disabled",
        local_agent_policy="readonly", search_policy="disabled_by_default", vision_policy="disabled",
        file_policy="disabled", apply_policy="disabled", test_policy="disabled", commit_policy="disabled",
        risk_level="low",
    ),
    "medium_analysis": _profile(
        profile_id="medium_analysis", task_difficulty="medium", desired_reasoning="medium",
        codex_custom_mode="CUSTOM", codex_reasoning_strength="medium", deepseek_mode="quick_thinking",
        local_model_policy="context_summary", external_api_policy="disabled",
        local_agent_policy="readonly_plus_plan", search_policy="on_demand", vision_policy="explicit_only",
        file_policy="explicit_only", apply_policy="disabled", test_policy="disabled", commit_policy="disabled",
        risk_level="medium",
    ),
    "medium_patch_draft": _profile(
        profile_id="medium_patch_draft", task_difficulty="medium", desired_reasoning="high",
        codex_custom_mode="CUSTOM", codex_reasoning_strength="high", deepseek_mode="expert_thinking",
        local_model_policy="context_compression", external_api_policy="disabled",
        local_agent_policy="review_only_patch_draft", search_policy="disabled_by_default", vision_policy="explicit_only",
        file_policy="explicit_only", apply_policy="disabled", test_policy="disabled", commit_policy="disabled",
        risk_level="medium",
    ),
    "complex_debug": _profile(
        profile_id="complex_debug", task_difficulty="complex", desired_reasoning="high",
        codex_custom_mode="CUSTOM", codex_reasoning_strength="high", deepseek_mode="expert_thinking",
        local_model_policy="context_summary_only", external_api_policy="disabled",
        local_agent_policy="readonly_plus_patch_draft", search_policy="on_demand", vision_policy="explicit_only",
        file_policy="explicit_only", apply_policy="user_confirm_required", test_policy="user_confirm_required",
        commit_policy="disabled", risk_level="complex",
    ),
    "patch_review_high_risk": _profile(
        profile_id="patch_review_high_risk", task_difficulty="high_risk", desired_reasoning="very_high",
        codex_custom_mode="CUSTOM", codex_reasoning_strength="very_high", deepseek_mode="expert_thinking",
        local_model_policy="not_final_reviewer", external_api_policy="disabled",
        local_agent_policy="static_review_only", search_policy="disabled_by_default", vision_policy="explicit_only",
        file_policy="explicit_only", apply_policy="disabled", test_policy="disabled", commit_policy="disabled",
        risk_level="high_risk",
    ),
    "official_codex_handoff": _profile(
        profile_id="official_codex_handoff", task_difficulty="complex", desired_reasoning="max",
        codex_custom_mode="OFFICIAL_DIRECT", codex_reasoning_strength="max", deepseek_mode="review_or_plan_only",
        local_model_policy="context_pack_only", external_api_policy="disabled",
        local_agent_policy="context_pack_only", search_policy="on_demand", vision_policy="explicit_only",
        file_policy="explicit_only", apply_policy="codex_official_only", test_policy="codex_official_only",
        commit_policy="codex_official_only", risk_level="complex", target_executor="codex_official",
    ),
}

# Friendly aliases used by callers and tests.
WORK_PROFILES = DEFAULT_WORK_PROFILES
PROFILES = DEFAULT_WORK_PROFILES


_HIGH_RISK_RE = re.compile(
    r"(?i)(?:打印|导出|泄露).*(?:密钥|key|token|cookie|authorization)|"
    r"(?:git\s+push|deploy|部署生产|开放(?:lan|公网)|防火墙|绕过(?:验证码|风控)|"
    r"replay[^\n]*(?:私有|private)\s*api|删除所有文件|清空仓库|storage[_ ]?state)"
)
_COMPLEX_RE = re.compile(r"(?i)(?:多文件|test[_ -]?v11|测试失败|超时|connection\s*abort|router|agent|provider|bridge|上下文|失败循环|复杂)")
_PATCH_RE = re.compile(r"(?i)(?:patch|补丁|修复|bug|修改|应用|草案|diff)")
_SIMPLE_RE = re.compile(r"(?i)(?:只读|状态|查看|搜索|列出|读取|说明|总结|分析但不修改|不修改文件|不改文件)")


def normalize_reasoning_strength(value: str | None, default: str = "medium") -> str:
    return REASONING_STRENGTH_ALIASES.get(str(value or "").strip(), default)


def classify_work_profile(task: str, *, production_impact: bool = False, needs_official: bool = False) -> str:
    """Classify task text without executing or contacting any provider."""
    text = str(task or "").strip()
    if production_impact or _HIGH_RISK_RE.search(text):
        return "patch_review_high_risk"
    if needs_official:
        return "official_codex_handoff"
    if _COMPLEX_RE.search(text):
        return "complex_debug"
    if _PATCH_RE.search(text):
        return "medium_patch_draft"
    if _SIMPLE_RE.search(text) or not text:
        return "simple_readonly"
    return "medium_analysis"


def select_work_profile(task: str, *, profile_id: str | None = None, production_impact: bool = False, needs_official: bool = False) -> WorkProfile:
    selected = profile_id or classify_work_profile(task, production_impact=production_impact, needs_official=needs_official)
    if selected not in DEFAULT_WORK_PROFILES:
        raise ValueError("WORK_PROFILE_NOT_FOUND")
    return DEFAULT_WORK_PROFILES[selected]


def get_work_profile(profile_id: str) -> WorkProfile:
    try:
        return DEFAULT_WORK_PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError("WORK_PROFILE_NOT_FOUND") from exc


def map_profile(profile: WorkProfile | str) -> dict[str, Any]:
    value = get_work_profile(profile) if isinstance(profile, str) else profile
    data = value.as_dict()
    data.update({
        "deepseek": {"mode": value.deepseek_mode, "search": value.search_policy == "always", "search_policy": value.search_policy},
        "local_model": {"policy": value.local_model_policy},
        "external_api": {"policy": value.external_api_policy, "enabled": False},
        "local_agent": {"policy": value.local_agent_policy},
    })
    return data


def generate_codex_handoff(profile: WorkProfile | str, *, task: str | None = None) -> str:
    value = get_work_profile(profile) if isinstance(profile, str) else profile
    strength = REASONING_STRENGTH_ZH[value.codex_reasoning_strength]
    if value.codex_custom_mode == "OFFICIAL_DIRECT":
        opening = "请在 Codex 官方直连模式中继续执行。"
    else:
        opening = f"请在 Codex 自定义模式中选择：推理强度={strength}。"
    return (
        f"{opening}\n"
        f"推荐 Work Profile：{value.profile_id}；任务难度：{value.task_difficulty}。\n"
        f"辅助 DeepSeek 模式：{value.deepseek_mode}；本地 Agent 策略：{value.local_agent_policy}。\n"
        "小羽只提供脱敏计划、只读结果或 review-only 草案；不会读取或操控 Codex 官方 UI。\n"
        "需要真实写入、测试或提交时，由 Codex 官方工具在人工确认后执行。"
    )


def build_handoff(profile: WorkProfile | str, *, task: str | None = None) -> str:
    return generate_codex_handoff(profile, task=task)


def confirm_codex_mode(profile: WorkProfile | str, *, confirmed_mode: bool = False, confirmed_strength: str | None = None) -> dict[str, Any]:
    value = get_work_profile(profile) if isinstance(profile, str) else profile
    strength = normalize_reasoning_strength(confirmed_strength, value.codex_reasoning_strength) if confirmed_strength else None
    return {
        "profile_id": value.profile_id,
        "recommended_codex_mode": value.codex_custom_mode,
        "recommended_codex_reasoning_strength": value.codex_reasoning_strength,
        "user_confirmed_codex_mode": "YES" if confirmed_mode else "NO",
        "user_confirmed_codex_reasoning_strength": strength,
        "codex_ui_scraping": "NO",
        "codex_ui_automation": "NO",
    }


def profile_summary(profile: WorkProfile | str) -> dict[str, Any]:
    value = get_work_profile(profile) if isinstance(profile, str) else profile
    result = map_profile(value)
    result["reasoning_strength_zh"] = REASONING_STRENGTH_ZH[value.codex_reasoning_strength]
    result["codex_handoff_instruction"] = generate_codex_handoff(value)
    result["codex_ui_scraping"] = "NO"
    result["codex_ui_automation"] = "NO"
    return result


__all__ = [
    "WorkProfile", "REASONING_STRENGTHS", "REASONING_STRENGTH_ZH", "DEFAULT_WORK_PROFILES",
    "WORK_PROFILES", "PROFILES", "normalize_reasoning_strength", "classify_work_profile",
    "select_work_profile", "get_work_profile", "map_profile", "generate_codex_handoff",
    "build_handoff", "confirm_codex_mode", "profile_summary",
]
