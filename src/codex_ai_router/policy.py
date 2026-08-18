from __future__ import annotations

from .task import Mode, Risk


def choose_mode(category: str, risk: Risk, requested: Mode | None = None, local_available: bool = True, api_available: bool = True) -> Mode:
    if requested:
        return requested
    if risk in (Risk.HIGH, Risk.CRITICAL):
        return Mode.CODEX_ONLY if risk is Risk.CRITICAL else Mode.CODEX_API
    if category in {"SIMPLE_READ", "LOG_ANALYSIS", "DOCS", "TRANSLATION"}:
        return Mode.LOCAL_ONLY if local_available else (Mode.API_ONLY if api_available else Mode.CODEX_ONLY)
    if category == "TEST_GENERATION":
        return Mode.API_ONLY if api_available else (Mode.LOCAL_ONLY if local_available else Mode.CODEX_ONLY)
    if category == "LARGE_REFACTOR":
        return Mode.CODEX_API
    return Mode.AUTO_TRIAD if (local_available or api_available) else Mode.CODEX_ONLY


def requires_codex_gate(risk: Risk) -> bool:
    return risk in (Risk.HIGH, Risk.CRITICAL)
