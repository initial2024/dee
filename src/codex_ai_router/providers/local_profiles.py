"""Safe, local-only demonstration profiles for the managed GGUF backend."""
from __future__ import annotations

from typing import Any


HISTORY_CHONGZHEN = {
    "profile_id": "history_chongzhen",
    "display_name": "崇祯历史模拟",
    "kind": "history_simulation",
    "default_language": "zh-CN",
    "selector_mode": "roleplay",
    "selector_risk": "simple",
    "recommended_roles": ["roleplay", "creative"],
    "fact_fiction_boundary": "将可核实史实、史学争议与虚构推演明确区分；不把推演表述为历史必然，也不提供现实政治建议。",
    "control_panel_scope": "仅本地 smoke/demo，不是正式任务入口。",
}


def get_local_profile(profile_id: str) -> dict[str, Any] | None:
    if profile_id == HISTORY_CHONGZHEN["profile_id"]:
        return dict(HISTORY_CHONGZHEN)
    return None


def local_profile_summaries() -> list[dict[str, Any]]:
    return [get_local_profile(HISTORY_CHONGZHEN["profile_id"])]


def build_history_chongzhen_prompt(prompt: str) -> str:
    clean = " ".join(str(prompt).split())
    if not clean:
        raise ValueError("HISTORY_PROFILE_PROMPT_REQUIRED")
    if len(clean) > 4000:
        raise ValueError("HISTORY_PROFILE_PROMPT_TOO_LONG")
    return (
        "你正在执行历史情境模拟：明末崇祯时期。默认使用中文。\n"
        "先以【史实】说明可核实背景；以【推演】标明合理但虚构的情境推演；"
        "对争议以【争议】说明不确定性。不得声称真实历史一定会如何发展，"
        "不得把历史模拟转换为现实政治建议。\n"
        f"本地 smoke/demo 请求：{clean}"
    )
