from __future__ import annotations

import re

from .classifier import classify
from .task import Risk


_UNSAFE = re.compile(
    r"\b(delete|remove|overwrite|drop\s+table|truncate|force\s+push|reset\s+--hard|credential|api[ _-]?key|secret|token|auth(?:entication|orization)?|security|payment|deploy(?:ment)?|production|migration|release\s+sign)\b|删除|覆盖|认证|鉴权|安全|支付|数据库迁移|生产部署|密钥|凭证|大(?:型)?重构",
    re.IGNORECASE,
)


def _bridge_risk(task: str) -> str:
    category, risk = classify(task)
    if _UNSAFE.search(task) or category == "HIGH_RISK" or risk in {Risk.HIGH, Risk.CRITICAL}:
        return "HIGH_RISK"
    if re.search(r"说明|总结|概述|解释|翻译|只读|不修改|日志", task, re.IGNORECASE):
        return "SIMPLE_READ"
    if category in {"LARGE_REFACTOR", "UNKNOWN"}:
        return "COMPLEX_CODE"
    if category in {"SMALL_CODE", "TEST_GENERATION"}:
        return "LOW_RISK_EDIT" if category == "SMALL_CODE" else "MEDIUM_CODE"
    if re.search(r"修复|代码|函数|测试|补丁", task, re.IGNORECASE):
        return "MEDIUM_CODE"
    if risk == Risk.MEDIUM:
        return "MEDIUM_CODE"
    return "SIMPLE_READ"


def explain_delegation(task: str, write_allowed: bool = False, risk_override: str = "auto") -> dict:
    bridge_risk = _bridge_risk(task) if risk_override == "auto" else {
        "simple": "SIMPLE_READ",
        "medium": "MEDIUM_CODE",
        "complex": "COMPLEX_CODE",
        "high": "HIGH_RISK",
    }.get(risk_override, "UNSAFE_OR_NEEDS_CONFIRMATION")
    high = bridge_risk in {"HIGH_RISK", "UNSAFE_OR_NEEDS_CONFIRMATION"}
    complex_task = bridge_risk == "COMPLEX_CODE"
    allowed = not high and not complex_task
    route = "ROUTER_READONLY" if bridge_risk == "SIMPLE_READ" else "ROUTER_ADVISORY"
    if not allowed:
        route = "OFFICIAL_CODEX"
    return {
        "risk": bridge_risk,
        "delegation_allowed": allowed,
        "recommended_route": route,
        "why": "high-risk changes remain under official Codex control" if high else ("complex work needs official Codex leadership" if complex_task else "Router may advise; official Codex reviews any file changes"),
        "skipped_providers": ["router_write"] if not write_allowed else [],
        "requires_official_codex": not allowed,
        "write_allowed_default": False,
        "OFFICIAL_STRONG_MODEL_RECOMMENDED": bool(high or complex_task),
    }
